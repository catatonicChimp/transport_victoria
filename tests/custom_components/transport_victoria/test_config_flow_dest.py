"""T18.4 config-flow destination-search + line-selection tests (light env).

Covers the TripView-style O/D journey's second and third steps — the
destination search (``async_step_dest``) and the "lines found" dropdown
(``async_step_lines``) — built on the T6 bus / V/Line fixtures.
"""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from custom_components.transport_victoria import config_flow as cf
from custom_components.transport_victoria.const import (
    CONF_DESTINATION_STOP_ID,
    CONF_DESTINATION_STOP_NAME,
    CONF_STOP_ID,
    CONF_STOP_NAME,
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


async def test_dest_journey_choice_branches(make_bus_flow):
    flow, captured = make_bus_flow()

    async def fake_journey(user_input=None):
        captured["journey"] = True
        return {"step_id": "journey"}

    # Patch on the instance (async_step_dest calls ``self.async_step_journey``).
    flow.async_step_journey = fake_journey

    await flow.async_step_dest({"destination_choice": "journey"})
    assert captured["journey"] is True


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
