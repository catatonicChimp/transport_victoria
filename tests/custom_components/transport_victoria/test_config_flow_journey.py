"""T18.4 TripView-style stop-pair journey tests (light env, stub style).

Covers the single four-step creation flow (origin → destination → line →
confirm) for both watch and board entry types, plus the derived-mode helper and
the watch data-schema backward-compat guarantee.

Real-runtime equivalents live in ``test_config_flow_journey_ha.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from custom_components.transport_victoria import config_flow as cf
from custom_components.transport_victoria.board import line_value
from custom_components.transport_victoria.const import (
    CONF_ALL_DESTINATIONS,
    CONF_BOARD_NAME,
    CONF_DESTINATION_STOP_ID,
    CONF_DIRECTION,
    CONF_ENTRY_TYPE,
    CONF_JOURNEY_DESTINATION_STOP_ID,
    CONF_JOURNEY_MODE,
    CONF_MIN_TRANSFER_MINUTES,
    CONF_MODE,
    CONF_ROUTE_ID,
    CONF_ROUTE_ID_FILTER,
    CONF_SOURCE_ENTRY_ID,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_TRANSFER_STOP_ID,
    CONF_TRANSFER_STOP_NAME,
    CONF_WATCH_NAME,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_WATCH,
    MODE_BUS,
    SUBENTRY_TYPE_PIN,
)


class FakeHass:
    """Minimal hass double — runs executor jobs synchronously."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(config_dir="/unused")

    async def async_add_executor_job(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def _new_flow(db_path: Path, entry_type: str, monkeypatch) -> cf.PTVConfigFlow:
    flow = cf.PTVConfigFlow()
    flow.hass = FakeHass()
    flow._config = {CONF_ENTRY_TYPE: entry_type}
    monkeypatch.setattr(cf, "_db_path", lambda cfg: db_path)
    monkeypatch.setattr(flow, "_get_source_entry_id", lambda: "src-1")
    return flow


async def _walk_to_confirm(flow: cf.PTVConfigFlow) -> None:
    """Drive origin → destination → line selection on the bus fixture."""
    await flow.async_step_origin({"query": "Sun"})
    await flow.async_step_origin({CONF_STOP_ID: "B_SUNSHINE"})
    await flow.async_step_dest({"destination_choice": "specific", "dest_query": "Cit"})
    await flow.async_step_dest(
        {"destination_choice": "specific", CONF_DESTINATION_STOP_ID: "B_CITY"}
    )
    # Two "City" routings → the lines step renders a dropdown (no auto-advance).
    assert flow._line_choices
    await flow.async_step_lines({"line_select": line_value(flow._line_choices[0])})


async def test_full_walk_creates_watch_entry(bus_db: Path, monkeypatch):
    flow = _new_flow(bus_db, ENTRY_TYPE_WATCH, monkeypatch)
    created: dict = {}

    def fake_create_entry(**kwargs):
        created.update(kwargs)
        return {"type": "create_entry"}

    monkeypatch.setattr(flow, "async_create_entry", fake_create_entry)

    await _walk_to_confirm(flow)
    await flow.async_step_confirm({CONF_WATCH_NAME: "My Watch"})

    assert created["title"] == "My Watch"
    data = created["data"]
    assert data[CONF_ENTRY_TYPE] == ENTRY_TYPE_WATCH
    assert data[CONF_STOP_ID] == "B_SUNSHINE"
    assert data[CONF_STOP_NAME] == "Sunshine"
    assert data[CONF_DESTINATION_STOP_ID] == "B_CITY"
    assert data[CONF_MODE] == MODE_BUS
    assert data[CONF_DIRECTION] == 0
    assert data[CONF_ROUTE_ID_FILTER] == "R_BUS_220"
    assert data[CONF_SOURCE_ENTRY_ID] == "src-1"
    # No board subentries for a watch.
    assert "subentries" not in created or not created.get("subentries")


async def test_full_walk_creates_board_and_pin(bus_db: Path, monkeypatch):
    flow = _new_flow(bus_db, ENTRY_TYPE_BOARD, monkeypatch)
    created: dict = {}

    def fake_create_entry(**kwargs):
        created.update(kwargs)
        return {"type": "create_entry"}

    monkeypatch.setattr(flow, "async_create_entry", fake_create_entry)

    await _walk_to_confirm(flow)
    await flow.async_step_confirm({CONF_BOARD_NAME: "My Board"})

    assert created["title"] == "My Board"
    assert created["data"][CONF_ENTRY_TYPE] == ENTRY_TYPE_BOARD
    assert created["data"][CONF_BOARD_NAME] == "My Board"
    # Exactly one pin subentry attached in the SAME create call.
    subentries = created["subentries"]
    assert len(subentries) == 1
    pin = subentries[0]
    assert pin["subentry_type"] == SUBENTRY_TYPE_PIN
    assert pin["data"][CONF_ROUTE_ID] == "R_BUS_220"
    assert pin["unique_id"] == pin["data"]["pin_key"]


async def test_board_confirm_screen_shows_board_name_field(bus_db: Path, monkeypatch):
    flow = _new_flow(bus_db, ENTRY_TYPE_BOARD, monkeypatch)
    required: list[str] = []

    def _record_required(name, *args, **kwargs):
        required.append(name)
        return object()

    monkeypatch.setattr(cf.vol, "Required", _record_required)

    def fake_show_form(**kwargs):
        return {"type": "form", "step_id": "confirm"}

    monkeypatch.setattr(flow, "async_show_form", fake_show_form)

    await _walk_to_confirm(flow)
    await flow.async_step_confirm(None)

    assert CONF_BOARD_NAME in required
    assert CONF_WATCH_NAME not in required


async def test_watch_data_schema_backward_compatible(bus_db: Path, monkeypatch):
    """The stored watch data keeps every key the old flow produced (no renames)."""
    flow = _new_flow(bus_db, ENTRY_TYPE_WATCH, monkeypatch)
    created: dict = {}

    def fake_create_entry(**kwargs):
        created.update(kwargs)
        return {"type": "create_entry"}

    monkeypatch.setattr(flow, "async_create_entry", fake_create_entry)

    await _walk_to_confirm(flow)
    await flow.async_step_confirm({CONF_WATCH_NAME: "W"})

    data = created["data"]
    # The coordinator requires these keys verbatim.
    assert CONF_MODE in data
    assert CONF_STOP_ID in data
    assert CONF_STOP_NAME in data
    assert CONF_DESTINATION_STOP_ID in data
    assert CONF_ALL_DESTINATIONS in data
    assert data[CONF_ALL_DESTINATIONS] is False


def test_mode_for_route_type_helper():
    assert cf._mode_for_route_type(3) == MODE_BUS
    assert cf._mode_for_route_type(400) == "metro"
    assert cf._mode_for_route_type(2) == "vline"
    assert cf._mode_for_route_type(None) == "metro"
    assert cf._mode_for_route_type(999) == "metro"  # unknown → fallback


# ---------------------------------------------------------------------------
# Journey ("change at stop") sub-flow — no direct-service validation
# ---------------------------------------------------------------------------


async def test_journey_flow_walk_has_no_direct_service_error(
    bus_db: Path, monkeypatch
):
    """origin → journey → transfer → final destination → confirm, with no
    "No direct service found" error anywhere (a journey's legs need not be
    direct)."""
    flow = _new_flow(bus_db, ENTRY_TYPE_WATCH, monkeypatch)

    captured: dict = {}

    async def fake_confirm(user_input=None):
        captured["confirm"] = True
        return {"step_id": "confirm"}

    monkeypatch.setattr(flow, "async_step_confirm", fake_confirm)

    # origin
    await flow.async_step_origin({"query": "Sun"})
    await flow.async_step_origin({CONF_STOP_ID: "B_SUNSHINE"})

    # destination → journey (explicit choice)
    await flow.async_step_dest({"destination_choice": "journey"})

    # transfer stop search + select → advances straight to journey_dest
    await flow.async_step_journey({"transfer_query": "Foo"})
    await flow.async_step_journey({CONF_TRANSFER_STOP_ID: "B_FOOTSCRAY"})
    assert flow._config[CONF_TRANSFER_STOP_ID] == "B_FOOTSCRAY"
    assert flow._config[CONF_TRANSFER_STOP_NAME] == "Footscray"

    # final destination search + select → confirm
    await flow.async_step_journey_dest({"journey_dest_query": "Cit"})
    await flow.async_step_journey_dest({
        CONF_JOURNEY_DESTINATION_STOP_ID: "B_CITY",
        CONF_MIN_TRANSFER_MINUTES: "5",
    })

    assert captured["confirm"] is True
    assert flow._config[CONF_JOURNEY_MODE] is True
    assert flow._config[CONF_JOURNEY_DESTINATION_STOP_ID] == "B_CITY"


async def test_journey_transfer_not_directly_reachable_still_advances(
    bus_db: Path, monkeypatch
):
    """Even a transfer with no direct origin→transfer service advances (the
    whole point of a journey is to change services) — no "no_direct_service"."""
    flow = _new_flow(bus_db, ENTRY_TYPE_WATCH, monkeypatch)

    advanced: dict = {}

    async def fake_journey_dest(user_input=None):
        advanced["reached"] = True
        return {"step_id": "journey_dest"}

    monkeypatch.setattr(flow, "async_step_journey_dest", fake_journey_dest)

    await flow.async_step_origin({"query": "Sun"})
    await flow.async_step_origin({CONF_STOP_ID: "B_SUNSHINE"})
    await flow.async_step_dest({"destination_choice": "journey"})

    # Select a transfer WITHOUT going through the search (so the stop id need
    # not be directly reachable): the flow must not validate it.
    flow._transfer_search_results = [
        cf.StopResult(
            stop_id="B_KENSINGTON", stop_name="Kensington", stop_lat=None,
            stop_lon=None, location_type=0, parent_station=None,
        )
    ]
    await flow.async_step_journey({CONF_TRANSFER_STOP_ID: "B_KENSINGTON"})

    assert advanced["reached"] is True
    assert flow._config[CONF_TRANSFER_STOP_ID] == "B_KENSINGTON"


async def test_board_dest_has_no_journey_option(bus_db: Path, monkeypatch):
    """Boards (and their pins) are route-scoped, so a board's destination step
    neither offers nor routes to the journey choice — only a watch gets it."""
    flow = _new_flow(bus_db, ENTRY_TYPE_BOARD, monkeypatch)

    journey_called: dict = {}

    async def fake_journey(user_input=None):
        journey_called["called"] = True
        return {"step_id": "journey"}

    monkeypatch.setattr(flow, "async_step_journey", fake_journey)
    monkeypatch.setattr(
        flow, "async_show_form", lambda **kw: {"type": "form", "step_id": "dest"}
    )

    # Submitting "journey" on a board does NOT branch to the journey step.
    await flow.async_step_dest({"destination_choice": "journey"})
    assert journey_called.get("called") is not True


async def test_watch_dest_offers_journey(bus_db: Path, monkeypatch):
    """A departure watch's destination step DOES offer the journey choice."""
    flow = _new_flow(bus_db, ENTRY_TYPE_WATCH, monkeypatch)

    branched: dict = {}

    async def fake_journey(user_input=None):
        branched["journey"] = True
        return {"step_id": "journey"}

    monkeypatch.setattr(flow, "async_step_journey", fake_journey)

    await flow.async_step_dest({"destination_choice": "journey"})

    assert branched["journey"] is True
