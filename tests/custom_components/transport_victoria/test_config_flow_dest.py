"""T18.4 config-flow destination-search + line-selection tests (light env).

Covers the TripView-style origin→destination flow's second and third steps — the
destination search (``async_step_dest``) and the "lines found" dropdown
(``async_step_lines``) — built on the T6 bus / V/Line fixtures.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from custom_components.transport_victoria import config_flow as cf
from custom_components.transport_victoria.const import (
    CONF_ALL_DESTINATIONS,
    CONF_DESTINATION_STOP_ID,
    CONF_DESTINATION_STOP_NAME,
    CONF_ENTRY_TYPE,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_WATCH,
)
from custom_components.transport_victoria.routing.destination import line_label


class FakeHass:
    """Minimal hass double — runs executor jobs synchronously (they are DB calls)."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(config_dir="/unused")

    async def async_add_executor_job(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


@pytest.fixture
def make_bus_flow(bus_db: Path, monkeypatch):
    def _make():
        flow = cf.PTVConfigFlow()
        flow.hass = FakeHass()
        flow._config = {
            CONF_STOP_ID: "B_SUNSHINE",
            CONF_STOP_NAME: "Sunshine",
        }
        captured: dict = {}

        async def fake_lines(user_input=None):
            captured["advanced_to_lines"] = True
            return {"step_id": "lines"}

        def fake_show_form(*args, **kwargs):
            captured["form"] = kwargs
            return {"type": "form", "step_id": "dest"}

        monkeypatch.setattr(flow, "async_step_lines", fake_lines)
        monkeypatch.setattr(flow, "async_show_form", fake_show_form)
        monkeypatch.setattr(cf, "_db_path", lambda cfg: bus_db)
        return flow, captured

    return _make


async def test_dest_search_selects_destination(make_bus_flow):
    flow, captured = make_bus_flow()
    await flow.async_step_dest(
        {"destination_choice": "specific", "dest_query": "Cit"}
    )
    assert flow._dest_search_results

    await flow.async_step_dest(
        {"destination_choice": "specific", CONF_DESTINATION_STOP_ID: "B_CITY"}
    )
    assert captured["advanced_to_lines"] is True
    assert flow._config[CONF_DESTINATION_STOP_ID] == "B_CITY"
    assert flow._config[CONF_DESTINATION_STOP_NAME] == "City"


async def test_lines_lists_all_candidates_no_duplicates(bus_db: Path, monkeypatch):
    """The "lines found" dropdown lists every O/D candidate with unique labels."""
    flow = cf.PTVConfigFlow()
    flow.hass = FakeHass()
    flow._config = {
        CONF_STOP_ID: "B_SUNSHINE",
        CONF_STOP_NAME: "Sunshine",
        CONF_DESTINATION_STOP_ID: "B_CITY",
        CONF_DESTINATION_STOP_NAME: "City",
    }
    captured: dict = {}

    def fake_show_form(*args, **kwargs):
        captured["form"] = kwargs
        return {"type": "form", "step_id": "lines"}

    monkeypatch.setattr(flow, "async_show_form", fake_show_form)
    monkeypatch.setattr(cf, "_db_path", lambda cfg: bus_db)

    await flow.async_step_lines(None)

    # Two "City" routings (via Footscray / via Kensington) → two candidates.
    assert len(flow._line_choices) == 2
    labels = [line_label(l) for l in flow._line_choices]
    assert len(labels) == len(set(labels))  # no duplicate option labels
    assert any("via" in lbl for lbl in labels)


async def test_lines_single_candidate_auto_advances(vline_db: Path, monkeypatch):
    """A single route+direction candidate skips the dropdown entirely."""
    flow = cf.PTVConfigFlow()
    flow.hass = FakeHass()
    flow._config = {
        CONF_STOP_ID: "V_SC",
        CONF_STOP_NAME: "Southern Cross",
        CONF_DESTINATION_STOP_ID: "V_GEELONG",
        CONF_DESTINATION_STOP_NAME: "Geelong",
    }
    captured: dict = {}

    async def fake_confirm(user_input=None):
        captured["confirm"] = True
        return {"step_id": "confirm"}

    monkeypatch.setattr(flow, "async_step_confirm", fake_confirm)
    monkeypatch.setattr(cf, "_db_path", lambda cfg: vline_db)

    await flow.async_step_lines(None)

    assert captured["confirm"] is True
    assert flow._selected_line is not None
    assert flow._selected_line.route_id == "R_VLINE_GEELONG"


# ---------------------------------------------------------------------------
# T18.4 fixes: stop labels, All-destinations default + fallback, translations
# ---------------------------------------------------------------------------


def test_stop_search_label_includes_type_and_id():
    from custom_components.transport_victoria.gtfs.repository import StopResult

    def _stop(stop_id, name, route_type):
        return StopResult(
            stop_id=stop_id, stop_name=name, stop_lat=None, stop_lon=None,
            location_type=0, parent_station=None, route_type=route_type,
        )

    assert cf._stop_search_label(_stop("14308", "Coburg Station", 400)) == \
        "Coburg Station (Metro Train, 14308)"
    assert cf._stop_search_label(_stop("14317", "Coburg Cemetery/Bell St", 0)) == \
        "Coburg Cemetery/Bell St (Tram, 14317)"
    # Unknown route type → just name + id.
    assert cf._stop_search_label(_stop("9", "X", 999)) == "X (9)"


def _new_watch_flow(db_path: Path, monkeypatch, config: dict) -> cf.PTVConfigFlow:
    flow = cf.PTVConfigFlow()
    flow.hass = FakeHass()
    flow._config = {CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, **config}
    monkeypatch.setattr(cf, "_db_path", lambda cfg: db_path)
    monkeypatch.setattr(flow, "_get_source_entry_id", lambda: "src-1")
    return flow


async def test_all_destinations_choice_advances_to_confirm(metro_db: Path, monkeypatch):
    flow = _new_watch_flow(
        metro_db, monkeypatch,
        {CONF_STOP_ID: "CAULFIELD", CONF_STOP_NAME: "Caulfield"},
    )
    captured: dict = {}

    async def fake_confirm(user_input=None):
        captured["confirm"] = True
        return {"step_id": "confirm"}

    monkeypatch.setattr(flow, "async_step_confirm", fake_confirm)

    await flow.async_step_dest({"destination_choice": "__all__"})

    assert captured["confirm"] is True
    assert flow._config[CONF_ALL_DESTINATIONS] is True


async def test_specific_destination_without_direct_service_falls_back(
    metro_db: Path, monkeypatch
):
    """CAULFIELD→WERRIBEE share no line → fall back to All destinations (watch)."""
    flow = _new_watch_flow(
        metro_db, monkeypatch,
        {
            CONF_STOP_ID: "CAULFIELD", CONF_STOP_NAME: "Caulfield",
            CONF_DESTINATION_STOP_ID: "WERRIBEE",
            CONF_DESTINATION_STOP_NAME: "Werribee",
        },
    )
    captured: dict = {}

    async def fake_confirm(user_input=None):
        captured["confirm"] = True
        return {"step_id": "confirm"}

    monkeypatch.setattr(flow, "async_step_confirm", fake_confirm)

    await flow.async_step_lines(None)

    assert captured["confirm"] is True
    assert flow._config[CONF_ALL_DESTINATIONS] is True
    assert flow._config[CONF_DESTINATION_STOP_ID] is None


async def test_board_default_is_not_all_destinations(metro_db: Path, monkeypatch):
    flow = cf.PTVConfigFlow()
    flow.hass = FakeHass()
    flow._config = {CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD, CONF_STOP_ID: "CAULFIELD"}
    monkeypatch.setattr(cf, "_db_path", lambda cfg: metro_db)
    captured: dict = {}

    async def fake_confirm(user_input=None):
        captured["confirm"] = True
        return {"step_id": "confirm"}

    monkeypatch.setattr(flow, "async_step_confirm", fake_confirm)
    monkeypatch.setattr(
        flow, "async_show_form", lambda **kw: {"type": "form", "step_id": "dest"}
    )

    # Empty submit on a board → default "specific" → re-shows the dest form
    # (does NOT jump to confirm as All destinations would).
    await flow.async_step_dest({})
    assert captured.get("confirm") is not True


# ---------------------------------------------------------------------------
# Direction-choice visibility (DEFECT 2) + query persistence (DEFECT 3)
# ---------------------------------------------------------------------------


def _record_schema(monkeypatch) -> dict:
    """Record field names + defaults as ``vol.Required``/``vol.Optional`` are
    called (the light env stubs voluptuous, so we can't introspect the built
    schema object)."""
    recorded: dict = {"fields": [], "defaults": {}}

    def _marker(name, *args, **kwargs):
        recorded["fields"].append(name)
        if "default" in kwargs:
            recorded["defaults"][name] = kwargs["default"]
        return object()

    monkeypatch.setattr(cf.vol, "Required", _marker)
    monkeypatch.setattr(cf.vol, "Optional", _marker)
    return recorded


async def test_dest_initial_render_has_no_direction_field(make_bus_flow, monkeypatch):
    flow, _ = make_bus_flow()
    recorded = _record_schema(monkeypatch)
    await flow.async_step_dest(None)
    assert "direction_select" not in recorded["fields"]


async def test_dest_direction_choice_renders_direction_field(
    make_bus_flow, monkeypatch
):
    flow, _ = make_bus_flow()
    recorded = _record_schema(monkeypatch)
    await flow.async_step_dest({"destination_choice": "direction"})
    # The origin stop's direction candidates were loaded…
    assert flow._direction_choices
    # …and the direction_select field is rendered (its options come from
    # _direction_choices).
    assert "direction_select" in recorded["fields"]


async def test_dest_direction_rerender_keeps_radio_on_direction(
    make_bus_flow, monkeypatch
):
    """The radio stays on "direction" so a direction submit isn't swallowed as
    "All destinations"."""
    flow, _ = make_bus_flow()
    recorded = _record_schema(monkeypatch)
    await flow.async_step_dest({"destination_choice": "direction"})
    assert recorded["defaults"].get("destination_choice") == "direction"


async def test_origin_query_default_persists_on_rerender(make_bus_flow, monkeypatch):
    flow, _ = make_bus_flow()
    recorded = _record_schema(monkeypatch)
    await flow.async_step_origin({"query": "Sun"})
    assert flow._stop_search_results
    assert recorded["defaults"].get("query") == "Sun"


async def test_dest_query_default_persists_on_rerender(make_bus_flow, monkeypatch):
    flow, _ = make_bus_flow()
    recorded = _record_schema(monkeypatch)
    await flow.async_step_dest(
        {"destination_choice": "specific", "dest_query": "Cit"}
    )
    assert flow._dest_search_results
    assert recorded["defaults"].get("dest_query") == "Cit"


def test_translations_have_destination_abort_and_steps():
    from pathlib import Path as _P
    root = _P(__file__).parents[3] / "custom_components" / "transport_victoria"
    strings = json.loads((root / "strings.json").read_text())
    en = json.loads((root / "translations" / "en.json").read_text())

    # The TripView origin→destination steps each have a title + description.
    for step in ("origin", "lines"):
        assert step in strings["config"]["step"], step
        assert "title" in strings["config"]["step"][step], step
    # The journey (change-at-stop) steps are gone.
    assert "journey" not in strings["config"]["step"]
    assert "journey_dest" not in strings["config"]["step"]

    # The abort reason resolves on both the config and subentry surfaces.
    assert "destination_unreachable" in strings["config"]["abort"]
    assert "destination_unreachable" in strings["config_subentries"]["pin"]["abort"]

    # The direction dropdown has a label + description (no raw "direction_select").
    dest_data = strings["config"]["step"]["dest"]["data"]
    assert dest_data["direction_select"] == "Direction"
    assert (
        strings["config"]["step"]["dest"]["data_description"]["direction_select"]
        == "Pick the line direction you want to track."
    )

    # en.json mirrors strings.json byte-for-byte.
    assert strings == en
