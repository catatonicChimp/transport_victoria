"""T18.1 F4/F5 — realtime option toggles + source-disable gating (light env).

F4: a ``realtime_enabled`` option toggle must take effect post-setup for watch
and board (rebuild/dispose the realtime client) without a full reload.
F5: a disabled source hub must stop running dependents (fleet + route tracker
raise UpdateFailed so entities go unavailable), not just watches/boards.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.transport_victoria.board_coordinator import BoardCoordinator
from custom_components.transport_victoria.const import (
    CONF_API_KEY,
    CONF_BOARD_NAME,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_REALTIME_ENABLED,
    CONF_ROUTE_ID,
    CONF_STOP_ID,
    CONF_TRACK_VEHICLES,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_SOURCE,
    ENTRY_TYPE_WATCH,
)
from custom_components.transport_victoria.coordinator import (
    PTVDataUpdateCoordinator,
)
from custom_components.transport_victoria.device_tracker import (
    FleetTrackerCoordinator,
    RouteVehicleCoordinator,
)
from custom_components.transport_victoria.coordinator import UpdateFailed


def _entry(entry_id: str, data: dict, disabled_by=None) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=entry_id, data=data, options={}, disabled_by=disabled_by
    )


def _source_data(key: str | None) -> dict:
    data = {CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE}
    if key is not None:
        data[CONF_API_KEY] = key
    return data


def _hass(source_key: str | None, *, disabled: bool = False):
    hub = _entry("hub1", _source_data(source_key), disabled_by="user" if disabled else None)

    def async_entries(domain):
        return [hub]

    def async_get_entry(sid):
        return hub if sid == "hub1" else None

    class _H:
        data = {DOMAIN: {"hub1": {"readiness": "ready"}}}

        @property
        def config_entries(self):
            return SimpleNamespace(async_entries=async_entries, async_get_entry=async_get_entry)

        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

        def async_create_task(self, coro):
            return asyncio.create_task(coro)

    return _H()


def _watch_entry(realtime: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="w1",
        title="w1",
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_MODE: "metro", CONF_STOP_ID: "S1"},
        options={CONF_REALTIME_ENABLED: realtime},
    )


def _board_entry(realtime: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="b1",
        title="b1",
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD, CONF_BOARD_NAME: "B"},
        options={CONF_REALTIME_ENABLED: realtime, CONF_TRACK_VEHICLES: False},
        subentries={},
    )


# ---------------------------------------------------------------------------
# F4 — realtime option toggle takes effect post-setup
# ---------------------------------------------------------------------------


class TestRealtimeToggle:
    def test_watch_toggle_off_disposes_client(self):
        hass = _hass("k")
        entry = _watch_entry(realtime=True)
        c = PTVDataUpdateCoordinator(hass, entry, 0, Path("db"))
        c._sync_realtime_client()
        assert c._client is not None

        entry.options = {CONF_REALTIME_ENABLED: False}
        c._sync_realtime_client()
        assert c._client is None
        assert c._realtime_enabled is False

    def test_watch_toggle_on_rebuilds_client(self):
        hass = _hass("k")
        entry = _watch_entry(realtime=False)
        c = PTVDataUpdateCoordinator(hass, entry, 0, Path("db"))
        c._sync_realtime_client()
        assert c._client is None

        entry.options = {CONF_REALTIME_ENABLED: True}
        c._sync_realtime_client()
        assert c._client is not None
        assert c._realtime_enabled is True

    def test_watch_toggle_noop_when_unchanged(self):
        hass = _hass("k")
        entry = _watch_entry(realtime=True)
        c = PTVDataUpdateCoordinator(hass, entry, 0, Path("db"))
        c._sync_realtime_client()
        client = c._client
        c._sync_realtime_client()  # unchanged → same client object
        assert c._client is client

    def test_board_toggle_off_disposes_client(self):
        hass = _hass("k")
        entry = _board_entry(realtime=True)
        c = BoardCoordinator(hass, entry, Path("db"))
        c.hass = hass
        c._sync_realtime_client()
        assert c._client is not None

        entry.options = {CONF_REALTIME_ENABLED: False}
        c._sync_realtime_client()
        assert c._client is None

    def test_board_toggle_on_rebuilds_client(self):
        hass = _hass("k")
        entry = _board_entry(realtime=False)
        c = BoardCoordinator(hass, entry, Path("db"))
        c.hass = hass
        c._sync_realtime_client()
        assert c._client is None

        entry.options = {CONF_REALTIME_ENABLED: True}
        c._sync_realtime_client()
        assert c._client is not None


# ---------------------------------------------------------------------------
# F5 — source disable stops running dependents
# ---------------------------------------------------------------------------


class TestSourceDisableGating:
    def test_fleet_raises_when_source_disabled(self):
        hass = _hass("k", disabled=True)
        entry = SimpleNamespace(
            entry_id="w1",
            title="w1",
            data={CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_MODE: "metro", CONF_STOP_ID: "S1"},
            options={CONF_TRACK_VEHICLES: True},
        )
        c = FleetTrackerCoordinator(hass, entry, Path("db"))
        c.hass = hass
        with pytest.raises(UpdateFailed):
            asyncio.run(c._async_update_data())

    def test_route_tracker_raises_when_source_disabled(self):
        hass = _hass("k", disabled=True)
        entry = SimpleNamespace(
            entry_id="t1",
            title="t1",
            data={CONF_ENTRY_TYPE: "route_tracker", CONF_MODE: "metro", CONF_ROUTE_ID: "R1"},
            options={},
        )
        c = RouteVehicleCoordinator(hass, entry, 0)
        c.hass = hass
        with pytest.raises(UpdateFailed):
            asyncio.run(c._async_update_data())

    def test_fleet_enabled_when_source_healthy(self):
        # A healthy source must NOT raise the disabled guard at the top.
        hass = _hass("k", disabled=False)
        entry = SimpleNamespace(
            entry_id="w1",
            title="w1",
            data={CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_MODE: "tram", CONF_STOP_ID: "S1"},
            options={CONF_TRACK_VEHICLES: True},
        )
        c = FleetTrackerCoordinator(hass, entry, Path("db"))
        c.hass = hass
        # Tram → no modes → returns the (empty) tracked set rather than raising.
        result = asyncio.run(c._async_update_data())
        assert result == {}
