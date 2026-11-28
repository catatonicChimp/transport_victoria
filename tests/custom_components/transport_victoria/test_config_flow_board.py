"""T8 board config-flow tests (light env, stub style — mirrors T7's dest tests).

Covers the parent board entry creation step and the supported-subentry-types
declaration without needing the real HA runtime.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.transport_victoria import config_flow as cf
from custom_components.transport_victoria.const import (
    CONF_BOARD_NAME,
    CONF_DEPARTURE_COUNT,
    CONF_ENTRY_TYPE,
    CONF_SUBENTRY_ORDER,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_WATCH,
    SUBENTRY_TYPE_PIN,
)


class FakeHass:
    """Minimal hass double — runs executor jobs synchronously."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(config_dir="/unused")
        self._tasks = []

    def async_create_task(self, coro):
        self._tasks.append(coro)

    async def async_add_executor_job(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def _noop_refresh(*args, **kwargs):
    return True


async def test_board_name_creates_entry(monkeypatch):
    """Board name step: a valid name creates the board (two-step UX)."""
    flow = cf.PTVConfigFlow()
    flow.hass = FakeHass()
    captured: dict = {}

    async def fake_create(board_name):
        captured["board_name"] = board_name
        return {"type": "create_entry", "title": board_name}

    monkeypatch.setattr(flow, "_create_board_entry", fake_create)

    result = await flow.async_step_monitor_name(
        {CONF_BOARD_NAME: "  My Board  "}
    )
    assert captured["board_name"] == "My Board"
    assert result["type"] == "create_entry"


async def test_board_name_requires_name(monkeypatch):
    """Board name step: an empty name re-renders with a required error."""
    flow = cf.PTVConfigFlow()
    flow.hass = FakeHass()
    captured: dict = {}

    def fake_show_form(**kwargs):
        captured.update(kwargs)
        return {"type": "form", "step_id": "monitor_name"}

    monkeypatch.setattr(flow, "async_show_form", fake_show_form)

    await flow.async_step_monitor_name(
        {CONF_BOARD_NAME: "   "}
    )
    assert captured["errors"] == {CONF_BOARD_NAME: "required"}


def test_supported_subentry_types_board_only():
    board_entry = SimpleNamespace(data={CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD})
    watch_entry = SimpleNamespace(data={CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH})

    types = cf.PTVConfigFlow.async_get_supported_subentry_types(board_entry)
    assert SUBENTRY_TYPE_PIN in types
    assert cf.PTVConfigFlow.async_get_supported_subentry_types(watch_entry) == {}
