"""T10 board entity unit tests (light env, stub style).

Covers the entity layer that exposes the T9 merged board as real Home Assistant
entities — without touching aggregation semantics:

* ranked departure sensors (``departure_1..N``): timestamps, compact attributes,
  sync_index on slot 1 only, unknown when fewer than N rows;
* the board disruption binary sensor (trustworthy OFF via feed-health);
* the direction select (opposite-direction mapping + runtime toggle);
* the coordinator adapter (executor offload, realtime flag, generation-swap
  surfacing, unique-id contract).

Entities are instantiated against a fake coordinator/data; no real HA runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.transport_victoria import board_coordinator as bc
from custom_components.transport_victoria.board import (
    PinData,
    opposite_direction_pin,
    opposite_direction_pins,
    pin_key,
)
from custom_components.transport_victoria.board_aggregation import (
    FEED_FAILED,
    FEED_OK,
    _GenerationSwapped,
    AlertMatch,
    BoardAlert,
    BoardDeparture,
    BoardResult,
    FeedHealth,
    RealtimeBoardData,
)
from custom_components.transport_victoria.binary_sensor import (
    BoardDisruptionBinarySensor,
)
from custom_components.transport_victoria.board_coordinator import BoardCoordinator
from custom_components.transport_victoria.const import (
    BOARD_DIRECTION_CONFIGURED,
    BOARD_DIRECTION_OPPOSITE,
    BOARD_ROLE_DEPARTURE,
    BOARD_ROLE_DIRECTION,
    BOARD_ROLE_DISRUPTION,
    CONF_BOARD_NAME,
    CONF_DEPARTURE_COUNT,
    CONF_ENTRY_TYPE,
    CONF_REALTIME_ENABLED,
    CONF_SUBENTRY_ORDER,
    ENTRY_TYPE_BOARD,
    board_entity_unique_id,
)
from custom_components.transport_victoria.select import BoardDirectionSelect
from custom_components.transport_victoria.sensor import BoardDepartureSensor

MEL = ZoneInfo("Australia/Melbourne")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dep(
    *,
    trip_id: str = "T1",
    scheduled: datetime | None = None,
    expected: datetime | None = None,
    delay: int = 0,
    realtime_state: str = "live",
    cancelled: bool = False,
    skipped: bool = False,
    **kw,
) -> BoardDeparture:
    scheduled = scheduled or datetime(2026, 6, 1, 8, 0, 0)
    fields = dict(
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
        scheduled_departure=scheduled,
        scheduled_seconds=0,
        expected_departure=expected,
        delay_seconds=delay,
        is_cancelled=cancelled,
        is_skipped=skipped,
        skipped_stops_count=1 if skipped else 0,
        realtime_state=realtime_state,
        trip_id=trip_id,
        stop_sequence=1,
        route_color=None,
        direction_id=0,
    )
    fields.update(kw)
    return BoardDeparture(**fields)


def _result(
    rows: list[BoardDeparture],
    *,
    alerts: list[BoardAlert] | None = None,
    feed_health: dict | None = None,
    sync_index: str = "abc123",
) -> BoardResult:
    return BoardResult(
        rows=rows,
        alerts=alerts or [],
        alerts_by_pin={},
        feed_health=feed_health or {},
        sync_index=sync_index,
        generated_at=datetime(2026, 6, 1, 8, 0, 0),
    )


class FakeCoordinator:
    """Minimal coordinator double for entity instantiation tests."""

    def __init__(self, data: BoardResult | None = None) -> None:
        self.data = data
        self.last_update_success = True
        self.local_tz = MEL
        self.pin_by_key: dict[str, PinData] = {}
        self.direction_mode = BOARD_DIRECTION_CONFIGURED

    def set_direction_mode(self, mode: str) -> None:
        self.direction_mode = mode

    async def async_request_refresh(self) -> None:
        self._refreshed = True


def _entry(entry_id: str = "board1", **options) -> SimpleNamespace:
    opts = {
        CONF_DEPARTURE_COUNT: 5,
        CONF_REALTIME_ENABLED: True,
        CONF_SUBENTRY_ORDER: [],
    }
    opts.update(options)
    return SimpleNamespace(
        entry_id=entry_id,
        data={CONF_BOARD_NAME: "My Board", CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD},
        options=opts,
        subentries={},
        title="My Board",
    )


class FakeHass:
    """Minimal hass double — runs executor jobs synchronously and records them."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(time_zone="Australia/Melbourne")
        self.executor_calls: list[tuple] = []

    async def async_add_executor_job(self, fn, *args, **kwargs):
        self.executor_calls.append((fn, args, kwargs))
        return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# Unique-id + naming contract
# ---------------------------------------------------------------------------


class TestEntityContract:
    def test_departure_unique_id_uses_contract(self):
        entry = _entry("parent1")
        sensor = BoardDepartureSensor(FakeCoordinator(), entry, 2)
        assert sensor._attr_unique_id == board_entity_unique_id("parent1", "departure", 2)
        assert sensor._attr_unique_id == "parent1:departure:2"

    def test_departure_unique_ids_differ_by_slot(self):
        entry = _entry()
        a = BoardDepartureSensor(FakeCoordinator(), entry, 1)
        b = BoardDepartureSensor(FakeCoordinator(), entry, 2)
        assert a._attr_unique_id != b._attr_unique_id

    def test_singleton_roles_and_has_entity_name(self):
        entry = _entry("parent1")
        disruption = BoardDisruptionBinarySensor(FakeCoordinator(), entry)
        direction = BoardDirectionSelect(FakeCoordinator(), entry)
        assert disruption._attr_unique_id == "parent1:disruption:0"
        assert direction._attr_unique_id == "parent1:direction:0"

        dep = BoardDepartureSensor(FakeCoordinator(), entry, 1)
        assert dep._attr_has_entity_name is True
        assert dep._attr_name == "Departure 1"
        assert disruption._attr_has_entity_name is True
        assert direction._attr_has_entity_name is True

    def test_device_info_uses_board_entry(self):
        entry = _entry("parent1")
        sensor = BoardDepartureSensor(FakeCoordinator(), entry, 1)
        info = sensor.device_info
        assert info is not None


# ---------------------------------------------------------------------------
# Ranked departure sensors
# ---------------------------------------------------------------------------


class TestDepartureSensors:
    def test_native_value_is_expected_when_realtime(self):
        scheduled = datetime(2026, 6, 1, 8, 0, 0)
        expected = datetime(2026, 6, 1, 8, 2, 0)
        data = _result([_dep(trip_id="T1", scheduled=scheduled, expected=expected)])
        sensor = BoardDepartureSensor(FakeCoordinator(data), _entry(), 1)
        assert sensor.native_value == expected.replace(tzinfo=MEL)

    def test_native_value_falls_back_to_scheduled(self):
        scheduled = datetime(2026, 6, 1, 8, 0, 0)
        data = _result(
            [_dep(trip_id="T1", scheduled=scheduled, expected=None,
                  realtime_state="scheduled", delay=-1)]
        )
        sensor = BoardDepartureSensor(FakeCoordinator(data), _entry(), 1)
        assert sensor.native_value == scheduled.replace(tzinfo=MEL)

    def test_fewer_rows_than_slots_are_unknown(self):
        data = _result([_dep(trip_id="T1"), _dep(trip_id="T2"), _dep(trip_id="T3")])
        coordinator = FakeCoordinator(data)
        assert BoardDepartureSensor(coordinator, _entry(), 1).native_value is not None
        assert BoardDepartureSensor(coordinator, _entry(), 3).native_value is not None
        assert BoardDepartureSensor(coordinator, _entry(), 4).native_value is None
        assert BoardDepartureSensor(coordinator, _entry(), 5).native_value is None

    def test_attributes_are_compact_iso(self):
        scheduled = datetime(2026, 6, 1, 8, 0, 0)
        expected = datetime(2026, 6, 1, 8, 2, 30)
        data = _result(
            [_dep(trip_id="T1", scheduled=scheduled, expected=expected, delay=150)]
        )
        sensor = BoardDepartureSensor(FakeCoordinator(data), _entry(), 1)
        attrs = sensor.extra_state_attributes
        assert attrs["scheduled"] == scheduled.isoformat()
        assert attrs["expected"] == expected.isoformat()
        assert attrs["delay_seconds"] == 150
        assert attrs["trip_id"] == "T1"
        assert attrs["route"] == "220"
        assert attrs["destination"] == "City"
        # No whole-board structure leaks into the per-row attributes.
        for key in ("rows", "alerts", "alerts_by_pin", "feed_health"):
            assert key not in attrs

    def test_sync_index_only_on_departure_1(self):
        data = _result([_dep(trip_id="T1"), _dep(trip_id="T2")], sync_index="sha1abc")
        coordinator = FakeCoordinator(data)
        assert BoardDepartureSensor(coordinator, _entry(), 1).extra_state_attributes["sync_index"] == "sha1abc"
        assert "sync_index" not in BoardDepartureSensor(coordinator, _entry(), 2).extra_state_attributes

    def test_realtime_disabled_no_rt_attrs(self):
        scheduled = datetime(2026, 6, 1, 8, 0, 0)
        data = _result(
            [_dep(trip_id="T1", scheduled=scheduled, expected=None,
                  realtime_state="scheduled", delay=-1)]
        )
        sensor = BoardDepartureSensor(FakeCoordinator(data), _entry(), 1)
        attrs = sensor.extra_state_attributes
        assert attrs["expected"] is None
        assert attrs["delay_seconds"] == -1
        assert attrs["realtime_state"] == "scheduled"

    def test_available_false_when_coordinator_failed(self):
        coordinator = FakeCoordinator(_result([_dep()]))
        coordinator.last_update_success = False
        sensor = BoardDepartureSensor(coordinator, _entry(), 1)
        assert sensor.available is False


# ---------------------------------------------------------------------------
# Disruption binary sensor
# ---------------------------------------------------------------------------


def _board_alert(alert_id: str = "a1", header: str = "Frankston line disruption") -> BoardAlert:
    return BoardAlert(
        alert_id=alert_id,
        header=header,
        description="",
        cause="UNKNOWN_CAUSE",
        effect="SIGNIFICANT_DELAYS",
        active_from=None,
        active_until=None,
        matches=(AlertMatch(pin_key="p1", reasons=("route",)),),
    )


class TestDisruptionSensor:
    def test_on_when_alert_matches(self):
        data = _result([_dep()], alerts=[_board_alert()])
        sensor = BoardDisruptionBinarySensor(FakeCoordinator(data), _entry())
        assert sensor.is_on is True

    def test_off_clean(self):
        data = _result(
            [_dep()],
            feed_health={("metro", "service-alerts"): FeedHealth(FEED_OK)},
        )
        sensor = BoardDisruptionBinarySensor(FakeCoordinator(data), _entry())
        assert sensor.is_on is False
        assert sensor.available is True

    def test_unavailable_when_alert_feed_failed(self):
        data = _result(
            [_dep()],
            feed_health={("metro", "service-alerts"): FeedHealth(FEED_FAILED)},
        )
        sensor = BoardDisruptionBinarySensor(FakeCoordinator(data), _entry())
        assert sensor.available is False

    def test_alert_feed_failure_does_not_block_trip_feed(self):
        # A failed trip-updates feed is NOT an alert feed → OFF stays confident.
        data = _result(
            [_dep()],
            feed_health={("metro", "trip-updates"): FeedHealth(FEED_FAILED)},
        )
        sensor = BoardDisruptionBinarySensor(FakeCoordinator(data), _entry())
        assert sensor.available is True
        assert sensor.is_on is False

    def test_attributes_compact_header_truncated(self):
        header = "x" * 200
        data = _result([_dep()], alerts=[_board_alert(header=header)])
        sensor = BoardDisruptionBinarySensor(FakeCoordinator(data), _entry())
        attrs = sensor.extra_state_attributes
        assert attrs["alert_count"] == 1
        assert len(attrs["alerts"][0]["header"]) == 120
        assert attrs["alerts"][0]["id"] == "a1"
        assert attrs["alerts"][0]["pins"] == ["p1"]


# ---------------------------------------------------------------------------
# Direction select
# ---------------------------------------------------------------------------


class TestDirectionSelect:
    def test_options_and_default(self):
        select = BoardDirectionSelect(FakeCoordinator(), _entry())
        assert select.options == [BOARD_DIRECTION_CONFIGURED, BOARD_DIRECTION_OPPOSITE]
        assert select.current_option == BOARD_DIRECTION_CONFIGURED

    async def test_select_option_flips_and_refreshes(self):
        coordinator = FakeCoordinator()
        select = BoardDirectionSelect(coordinator, _entry())
        await select.async_select_option(BOARD_DIRECTION_OPPOSITE)
        assert coordinator.direction_mode == BOARD_DIRECTION_OPPOSITE
        assert getattr(coordinator, "_refreshed", False) is True


# ---------------------------------------------------------------------------
# Opposite-direction mapping (pure)
# ---------------------------------------------------------------------------


def _vline_pin(direction: int = 0) -> PinData:
    return PinData(
        pin_key=pin_key("R_VLINE_GEELONG", "V_SC", "V_GEELONG", direction),
        route_type=2,
        route_id="R_VLINE_GEELONG",
        boarding_stop_id="V_SC",
        boarding_stop_name="Southern Cross",
        all_destinations=False,
        destination_stop_id="V_GEELONG",
        destination_stop_name="Geelong",
        direction_id=direction,
        label_from="Geelong from Southern Cross",
        label_to="Geelong",
    )


class TestOppositeDirection:
    def test_flips_vline_direction(self, vline_db: Path):
        flipped = opposite_direction_pin(vline_db, _vline_pin(0))
        assert flipped.direction_id == 1
        assert flipped.destination_stop_id is None  # destination filter off
        assert flipped.label_to == "Southern Cross"
        # Original pin is unchanged (transform returns a copy).
        assert _vline_pin(0).direction_id == 0

    def test_flip_back_is_symmetric(self, vline_db: Path):
        flipped = opposite_direction_pin(vline_db, _vline_pin(0))
        back = opposite_direction_pin(vline_db, flipped)
        assert back.direction_id == 0
        assert back.label_to == "Geelong"

    def test_all_destinations_pin_unchanged(self, vline_db: Path):
        pin = PinData(
            pin_key=pin_key("R_VLINE_GEELONG", "V_SC", None, None),
            route_type=2,
            route_id="R_VLINE_GEELONG",
            boarding_stop_id="V_SC",
            boarding_stop_name="Southern Cross",
            all_destinations=True,
        )
        assert opposite_direction_pin(vline_db, pin) is pin

    def test_single_direction_route_unchanged(self, tram_db: Path):
        pin = PinData(
            pin_key=pin_key("R_TRAM_96", "T96_TERM", "T96_EB", 0),
            route_type=0,
            route_id="R_TRAM_96",
            boarding_stop_id="T96_TERM",
            boarding_stop_name="St Kilda Beach",
            all_destinations=False,
            destination_stop_id="T96_EB",
            destination_stop_name="East Brunswick",
            direction_id=0,
        )
        result = opposite_direction_pin(tram_db, pin)
        assert result.direction_id == 0
        assert result.destination_stop_id == "T96_EB"  # unchanged

    def test_batch_transform(self, vline_db: Path):
        pins = [_vline_pin(0), _vline_pin(1)]
        flipped = opposite_direction_pins(vline_db, pins)
        assert flipped[0].direction_id == 1
        assert flipped[1].direction_id == 0
        assert [p.pin_key for p in flipped] == [p.pin_key for p in pins]


# ---------------------------------------------------------------------------
# Coordinator adapter
# ---------------------------------------------------------------------------


class TestCoordinatorAdapter:
    def _coordinator(self, hass: FakeHass, entry) -> BoardCoordinator:
        coordinator = BoardCoordinator(hass, entry, Path("gtfs.db"))
        coordinator._client = SimpleNamespace()  # fake realtime client
        coordinator._direction_mode = BOARD_DIRECTION_CONFIGURED
        return coordinator

    async def test_update_offloads_to_executor(self, monkeypatch):
        hass = FakeHass()
        entry = _entry()
        coordinator = self._coordinator(hass, entry)

        captured: dict = {}

        async def fake_fetch(client, pins, realtime_enabled, *, now=None, run_sync=None):
            captured["fetch_run_sync"] = run_sync
            return RealtimeBoardData(
                trip_updates=None, alerts=[], feed_health={}, stale_modes=frozenset()
            )

        def fake_aggregate(*args, **kwargs):
            captured["aggregate"] = True
            return _result([_dep()])

        monkeypatch.setattr(bc, "fetch_board_realtime", fake_fetch)
        monkeypatch.setattr(bc, "aggregate_board", fake_aggregate)

        result = await coordinator._async_update_data()

        # fetch offloads its parse via the coordinator's executor run_sync.
        assert captured["fetch_run_sync"] is coordinator._run_sync
        assert captured["fetch_run_sync"] is not None
        # aggregate_board ran (through the executor, the only _run_sync call).
        assert captured["aggregate"] is True
        assert len(hass.executor_calls) == 1
        assert isinstance(result, BoardResult)

    async def test_opposite_direction_transforms_pins(self, monkeypatch):
        hass = FakeHass()
        entry = _entry()
        coordinator = self._coordinator(hass, entry)
        coordinator._direction_mode = BOARD_DIRECTION_OPPOSITE

        captured: dict = {}

        async def fake_fetch(client, pins, realtime_enabled, *, now=None, run_sync=None):
            captured["pins"] = pins
            return RealtimeBoardData(
                trip_updates=None, alerts=[], feed_health={}, stale_modes=frozenset()
            )

        def fake_aggregate(*args, **kwargs):
            captured["agg_pins"] = args[1]
            return _result([_dep()])

        monkeypatch.setattr(bc, "fetch_board_realtime", fake_fetch)
        monkeypatch.setattr(bc, "aggregate_board", fake_aggregate)

        await coordinator._async_update_data()
        # The transform ran inside the executor (two executor calls: transform
        # + aggregate), and the same transformed pins reached both ports.
        assert len(hass.executor_calls) == 2

    async def test_realtime_disabled_passes_false(self, monkeypatch):
        hass = FakeHass()
        entry = _entry(realtime_enabled=False)
        coordinator = self._coordinator(hass, entry)

        captured: dict = {}

        async def fake_fetch(client, pins, realtime_enabled, *, now=None, run_sync=None):
            captured["fetch_re"] = realtime_enabled
            return RealtimeBoardData(
                trip_updates=None, alerts=[], feed_health={}, stale_modes=frozenset()
            )

        def fake_aggregate(*args, **kwargs):
            captured["agg_re"] = kwargs.get("realtime_enabled")
            return _result([_dep()])

        monkeypatch.setattr(bc, "fetch_board_realtime", fake_fetch)
        monkeypatch.setattr(bc, "aggregate_board", fake_aggregate)

        await coordinator._async_update_data()
        assert captured["fetch_re"] is False
        assert captured["agg_re"] is False

    async def test_generation_swap_surfaces(self, monkeypatch):
        hass = FakeHass()
        coordinator = self._coordinator(hass, _entry())

        async def fake_fetch(client, pins, realtime_enabled, *, now=None, run_sync=None):
            return RealtimeBoardData(
                trip_updates=None, alerts=[], feed_health={}, stale_modes=frozenset()
            )

        def fake_aggregate(*args, **kwargs):
            raise _GenerationSwapped(None)

        monkeypatch.setattr(bc, "fetch_board_realtime", fake_fetch)
        monkeypatch.setattr(bc, "aggregate_board", fake_aggregate)

        # The adapter does NOT swallow the (already-once-retried) swap — it
        # surfaces it so the coordinator marks entities unavailable.
        with pytest.raises(_GenerationSwapped):
            await coordinator._async_update_data()

    def test_departure_count_reads_options(self):
        hass = FakeHass()
        coordinator = self._coordinator(hass, _entry(departure_count=7))
        assert coordinator.departure_count == 7
