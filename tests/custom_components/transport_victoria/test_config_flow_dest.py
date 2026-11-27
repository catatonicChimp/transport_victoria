"""Config-flow destination step tests (T7), built on the T6 bus fixture.

Verifies the #2 scenario: a bus watch can select a specific destination (a
short-working terminus) and have its direction derived and stored, while the
"All destinations" default is preserved.
"""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from custom_components.transport_victoria import config_flow as cf
from custom_components.transport_victoria.const import (
    CONF_ALL_DESTINATIONS,
    CONF_DESTINATION_STOP_ID,
    CONF_DESTINATION_STOP_NAME,
    CONF_DIRECTION,
    CONF_MODE,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    MODE_BUS,
)
from custom_components.transport_victoria.routing.destination import destination_value


class FakeHass:
    """Minimal hass double — runs executor jobs synchronously (they are DB calls)."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(config_dir="/unused")

    async def async_add_executor_job(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


@pytest.fixture
def make_flow(bus_db: Path, monkeypatch):
    def _make():
        flow = cf.PTVConfigFlow()
        flow.hass = FakeHass()
        flow._config = {
            CONF_MODE: MODE_BUS,
            CONF_STOP_ID: "B_SUNSHINE",
            CONF_STOP_NAME: "Sunshine",
        }
        captured: dict = {}

        async def fake_confirm(user_input=None):
            captured["confirmed"] = True
            return {"step_id": "confirm"}

        def fake_show_form(*args, **kwargs):
            captured["form"] = kwargs
            return {"type": "form", "step_id": "dest"}

        monkeypatch.setattr(flow, "async_step_confirm", fake_confirm)
        monkeypatch.setattr(flow, "async_show_form", fake_show_form)
        # Point the flow's DB anchor at the fixture generation (skip download).
        monkeypatch.setattr(cf, "_db_path", lambda cfg: bus_db)
        return flow, captured

    return _make


async def test_bus_destination_step_lists_terminals(make_flow):
    flow, captured = make_flow()
    await flow.async_step_dest(None)

    assert captured["form"] is not None
    assert flow._dest_route_id == "R_BUS_220"
    names = {c.terminal_stop_name for c in flow._dest_candidates}
    assert {"Footscray", "Braybrook", "City", "Sunshine"} <= names


async def test_bus_specific_destination_closes_issue(make_flow):
    flow, captured = make_flow()
    await flow.async_step_dest(None)

    footscray = next(
        c for c in flow._dest_candidates
        if c.terminal_stop_name == "Footscray" and c.via_hint is None
    )
    await flow.async_step_dest({"destination_choice": destination_value(footscray)})

    assert captured["confirmed"] is True
    assert flow._config[CONF_DESTINATION_STOP_ID] == "B_FOOTSCRAY"
    assert flow._config[CONF_DESTINATION_STOP_NAME] == "Footscray"
    assert flow._config[CONF_DIRECTION] == 0
    assert flow._config[CONF_ALL_DESTINATIONS] is False


async def test_all_destinations_preserved(make_flow):
    flow, captured = make_flow()
    await flow.async_step_dest(None)
    await flow.async_step_dest({"destination_choice": "__all__"})

    assert captured["confirmed"] is True
    assert flow._config[CONF_ALL_DESTINATIONS] is True
    assert flow._config[CONF_DESTINATION_STOP_ID] is None
    assert flow._config[CONF_DIRECTION] is None
