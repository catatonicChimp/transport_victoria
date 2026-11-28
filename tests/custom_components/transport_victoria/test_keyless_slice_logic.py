"""T18.2 keyless vertical slice — light-env logic tests.

Pure entity-logic counterparts for the real-HA vertical slice
(``test_keyless_slice.py``) that the light env can reach without the HA runtime:

* the ``scheduled_only`` entity attribute (watch + board departure sensors) —
  present (True) only when the source hub is credential-less, even with no rows;
* the disruption binary-sensor platform gate (NOT created when scheduled-only,
  created when a credential exists) — the fresh-setup semantic;
* the coordinator never builds a realtime client when scheduled-only (the
  empty-KeyID guard's call-site half; the client preflight half is already
  covered by ``test_realtime_client.py::test_empty_api_key_raises_without_http_call``).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from custom_components.transport_victoria import binary_sensor as bsen
from custom_components.transport_victoria.binary_sensor import (
    PTVDisruptionBinarySensor,
)
from custom_components.transport_victoria.coordinator import PTVDataUpdateCoordinator
from custom_components.transport_victoria.const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_STOP_ID,
    ENTRY_TYPE_SOURCE,
    ENTRY_TYPE_WATCH,
)
from custom_components.transport_victoria.models import CoordinatorData, Departure
from custom_components.transport_victoria.sensor import (
    BoardDepartureSensor,
    PTVDepartureSensor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_entry(entry_id: str = "hub1", key: str | None = None) -> SimpleNamespace:
    data = {CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE}
    if key is not None:
        data[CONF_API_KEY] = key
    return SimpleNamespace(entry_id=entry_id, data=data, options={}, disabled_by=None)


def _hass(key: str | None) -> SimpleNamespace:
    """A hass double with a single source entry (credential-less when key None)."""
    hub = _source_entry(key=key)

    def async_entries(domain):
        return [hub]

    def async_get_entry(sid):
        return hub if sid == "hub1" else None

    return SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=async_entries, async_get_entry=async_get_entry
        ),
    )


def _departure(**kw) -> Departure:
    fields = dict(
        trip_id="T1",
        route_id="R1",
        route_short_name="220",
        route_long_name="",
        headsign="City",
        direction_id=0,
        scheduled_departure=datetime(2026, 6, 1, 8, 0, 0),
    )
    fields.update(kw)
    return Departure(**fields)


class _Coord:
    """Minimal coordinator double for watch departure sensors."""

    def __init__(self, departures: list[Departure]) -> None:
        self.data = CoordinatorData(departures=departures, alerts=[])
        self.last_update_success = True


class _BoardCoord:
    """Minimal board-coordinator double (a ``BoardResult`` with one row)."""

    def __init__(self, row) -> None:
        self.data = row
        self.last_update_success = True
        self.pin_by_key = {}
        self.local_tz = None


# ---------------------------------------------------------------------------
# ``scheduled_only`` attribute (watch + board)
# ---------------------------------------------------------------------------


class TestScheduledOnlyAttribute:
    def test_watch_departure_flagged_when_keyless(self):
        sensor = PTVDepartureSensor(_Coord([_departure()]), _watch_entry(), 1)
        sensor.hass = _hass(key=None)
        attrs = sensor.extra_state_attributes
        assert attrs["scheduled_only"] is True
        assert attrs["realtime_state"] == "scheduled"

    def test_watch_departure_not_flagged_when_keyed(self):
        sensor = PTVDepartureSensor(_Coord([_departure()]), _watch_entry(), 1)
        sensor.hass = _hass(key="k")
        attrs = sensor.extra_state_attributes
        assert "scheduled_only" not in attrs

    def test_watch_departure_flagged_even_with_no_rows(self):
        # Persistent explanation: the banner survives an empty departure list.
        sensor = PTVDepartureSensor(_Coord([]), _watch_entry(), 1)
        sensor.hass = _hass(key=None)
        assert sensor.extra_state_attributes == {"scheduled_only": True}

    def test_board_departure_flagged_when_keyless(self):
        from custom_components.transport_victoria.board_aggregation import (
            BoardDeparture,
            BoardResult,
        )

        row = BoardDeparture(
            pin_key="p1",
            pin_order_index=0,
            route_id="R1",
            route_short_name="220",
            route_long_name="",
            headsign="City",
            destination_label="City",
            boarding_stop_id="S1",
            boarding_stop_name="Sunshine",
            platform=None,
            scheduled_departure=datetime(2026, 6, 1, 8, 0, 0),
            scheduled_seconds=0,
            expected_departure=None,
            delay_seconds=-1,
            is_cancelled=False,
            is_skipped=False,
            skipped_stops_count=0,
            realtime_state="scheduled",
            trip_id="T1",
            stop_sequence=1,
            route_color=None,
            direction_id=0,
        )
        result = BoardResult(
            rows=[row], alerts=[], alerts_by_pin={}, feed_health={},
            sync_index="s", generated_at=datetime(2026, 6, 1, 8, 0, 0),
        )
        sensor = BoardDepartureSensor(_BoardCoord(result), _board_entry(), 1)
        sensor.hass = _hass(key=None)
        attrs = sensor.extra_state_attributes
        assert attrs["scheduled_only"] is True
        assert attrs["realtime_state"] == "scheduled"
        assert attrs["delay_seconds"] == -1  # board sentinel: no prediction
        assert attrs["expected"] is None

    def test_board_departure_not_flagged_when_keyed(self):
        from custom_components.transport_victoria.board_aggregation import (
            BoardDeparture,
            BoardResult,
        )

        row = BoardDeparture(
            pin_key="p1", pin_order_index=0, route_id="R1", route_short_name="220",
            route_long_name="", headsign="City", destination_label="City",
            boarding_stop_id="S1", boarding_stop_name="Sunshine", platform=None,
            scheduled_departure=datetime(2026, 6, 1, 8, 0, 0), scheduled_seconds=0,
            expected_departure=datetime(2026, 6, 1, 8, 0, 30), delay_seconds=30,
            is_cancelled=False, is_skipped=False, skipped_stops_count=0,
            realtime_state="live", trip_id="T1", stop_sequence=1,
            route_color=None, direction_id=0,
        )
        result = BoardResult(
            rows=[row], alerts=[], alerts_by_pin={}, feed_health={},
            sync_index="s", generated_at=datetime(2026, 6, 1, 8, 0, 0),
        )
        sensor = BoardDepartureSensor(_BoardCoord(result), _board_entry(), 1)
        sensor.hass = _hass(key="k")
        assert "scheduled_only" not in sensor.extra_state_attributes


# ---------------------------------------------------------------------------
# Disruption binary-sensor platform gate (NOT created when scheduled-only)
# ---------------------------------------------------------------------------


def _watch_entry(entry_id: str = "w1") -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=entry_id,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH},
        options={},
        title="w1",
        runtime_data=object(),
    )


def _board_entry(entry_id: str = "b1") -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=entry_id,
        data={CONF_ENTRY_TYPE: "board"},
        options={},
        title="b1",
        runtime_data=object(),
    )


class TestDisruptionPlatformGate:
    async def test_not_created_when_scheduled_only(self):
        added: list = []

        def _add(entities):
            added.extend(entities)

        await bsen.async_setup_entry(_hass(key=None), _watch_entry(), _add)
        assert added == []

    async def test_created_when_key_present(self):
        added: list = []

        def _add(entities):
            added.extend(entities)

        await bsen.async_setup_entry(_hass(key="k"), _watch_entry(), _add)
        assert len(added) == 1
        assert isinstance(added[0], PTVDisruptionBinarySensor)


# ---------------------------------------------------------------------------
# Empty-key guard (coordinator call-site half)
# ---------------------------------------------------------------------------


class TestCoordinatorKeylessClient:
    def test_no_client_built_when_keyless(self):
        from pathlib import Path

        hass = _hass(key=None)
        entry = SimpleNamespace(
            entry_id="w1",
            title="w1",
            data={CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_MODE: "metro", CONF_STOP_ID: "S1"},
            options={},
        )
        c = PTVDataUpdateCoordinator(hass, entry, 0, Path("db"))
        c.hass = hass
        c._sync_realtime_client()
        assert c._client is None
        assert c._realtime_enabled is False
