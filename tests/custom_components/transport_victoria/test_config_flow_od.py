"""T18.4 TripView-style origin→destination creation-flow tests (light env).

Covers the single origin → destination → line → confirm creation flow for both
watch and board entry types, plus the derived-mode helper and the watch
data-schema backward-compat guarantee.

Real-runtime equivalents live in ``test_config_flow_od_ha.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from custom_components.transport_victoria import config_flow as cf
from custom_components.transport_victoria.board import line_value
from custom_components.transport_victoria.const import (
    CONF_ALL_DESTINATIONS,
    CONF_API_KEY,
    CONF_BOARD_NAME,
    CONF_DESTINATION_STOP_ID,
    CONF_DIRECTION,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_ROUTE_ID,
    CONF_ROUTE_ID_FILTER,
    CONF_SOURCE_ENTRY_ID,
    CONF_STOP_ID,
    CONF_STOP_NAME,
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
    # Ownership contract: the hub owns the credential; the feature carries none.
    assert CONF_API_KEY not in data
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
    # Ownership contract: the board parent carries the association, no credential.
    assert CONF_API_KEY not in created["data"]
    assert created["data"][CONF_SOURCE_ENTRY_ID] == "src-1"
    # Exactly one pin subentry attached in the SAME create call.
    subentries = created["subentries"]
    assert len(subentries) == 1
    pin = subentries[0]
    assert pin["subentry_type"] == SUBENTRY_TYPE_PIN
    assert pin["data"][CONF_ROUTE_ID] == "R_BUS_220"
    assert pin["unique_id"] == pin["data"]["pin_key"]
    # Pin subentry data never carries a credential.
    assert CONF_API_KEY not in pin["data"]


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
# Destination-step options (the Journey "change at stop" choice was removed)
# ---------------------------------------------------------------------------


async def test_dest_options_watch_has_all_and_specific_no_journey(
    bus_db: Path, monkeypatch
):
    """A watch's destination step offers All destinations + Specific, never Journey."""
    flow = _new_flow(bus_db, ENTRY_TYPE_WATCH, monkeypatch)
    values: list = []

    def _record_option(**kwargs):
        values.append(kwargs.get("value"))
        return object()

    monkeypatch.setattr(cf.selector, "SelectOptionDict", _record_option)
    monkeypatch.setattr(
        flow, "async_show_form", lambda **kw: {"type": "form", "step_id": "dest"}
    )

    await flow.async_step_dest(None)

    assert "__all__" in values
    assert "specific" in values
    assert "journey" not in values


async def test_dest_options_board_has_specific_only(bus_db: Path, monkeypatch):
    """A board's destination step offers only Specific (no All, no Journey)."""
    flow = _new_flow(bus_db, ENTRY_TYPE_BOARD, monkeypatch)
    values: list = []

    def _record_option(**kwargs):
        values.append(kwargs.get("value"))
        return object()

    monkeypatch.setattr(cf.selector, "SelectOptionDict", _record_option)
    monkeypatch.setattr(
        flow, "async_show_form", lambda **kw: {"type": "form", "step_id": "dest"}
    )

    await flow.async_step_dest(None)

    assert values == ["specific"]
