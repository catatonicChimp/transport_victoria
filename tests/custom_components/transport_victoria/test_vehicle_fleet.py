"""T16 dynamic vehicle-fleet tracking unit tests (light env).

Covers the pure ``vehicle_fleet`` reconcile (matching, appear/remove lifecycle,
cap), the ``_fleet_scope`` derivation (watch/board/tram), the coordinator's
stale-feed "keep last state" behaviour, the entity naming/unique-id contract,
the opt-in gate, and the removal of the legacy double-throttle limiter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.transport_victoria import device_tracker as dt_module
from custom_components.transport_victoria.device_tracker import (
    FleetTrackerCoordinator,
    PTVFleetVehicleTracker,
    _fleet_scope,
)
from custom_components.transport_victoria.const import (
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_ROUTE_ID_FILTER,
    CONF_STOP_ID,
    CONF_TRACK_VEHICLES,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_WATCH,
    MODE_METRO,
    MODE_TRAM,
)
from custom_components.transport_victoria.gtfs.repository import (
    route_ids_for_trips,
    routes_for_stop,
)
from custom_components.transport_victoria.realtime.client import FeedMeta
from custom_components.transport_victoria.realtime.vehicle_positions import (
    VehiclePosition,
    parse_vehicle_positions_by_vehicle,
)
from custom_components.transport_victoria.vehicle_fleet import (
    GRACE_ABSENT_SECONDS,
    MAX_VEHICLES_PER_ENTRY,
    feed_is_stale,
    update_fleet,
    vehicle_state,
)

from tests.realtime_fixtures import ROUTE_FRANKSTON, vehicle_positions_fleet

NOW = datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
WATCHED = frozenset({ROUTE_FRANKSTON})  # R_METRO_FRANKSTON


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fleet_positions() -> dict[str, VehiclePosition]:
    return parse_vehicle_positions_by_vehicle(vehicle_positions_fleet())


def _route_lookup(db_path: Path, positions: dict[str, VehiclePosition]) -> dict[str, str]:
    trips = [p.trip_id for p in positions.values() if p.trip_id and not p.route_id]
    return route_ids_for_trips(db_path, trips)


def _vp(vehicle_id: str, route_id: str = "R") -> VehiclePosition:
    return VehiclePosition(
        trip_id=f"T_{vehicle_id}",
        latitude=-37.8,
        longitude=145.0,
        bearing=None,
        speed=None,
        speed_kmh=None,
        timestamp=None,
        vehicle_id=vehicle_id,
        vehicle_label=None,
        current_stop_id=None,
        current_status="IN_TRANSIT_TO",
        route_id=route_id,
    )


class FakeClient:
    """Records fetch calls and returns a fixed (feed, meta) pair."""

    def __init__(self, feed: object, meta: FeedMeta) -> None:
        self._feed = feed
        self._meta = meta
        self.calls: list[tuple[str, str]] = []

    async def fetch_feed_with_meta(self, mode: str, feed_type: str):
        self.calls.append((mode, feed_type))
        return self._feed, self._meta


class _HTTPError(Exception):
    """Stands in for ``aiohttp.ClientResponseError`` (carries ``.status``)."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"HTTP {status}")


class RaisingClient:
    async def fetch_feed_with_meta(self, mode: str, feed_type: str):
        raise _HTTPError(500)


class FakeHass:
    def __init__(self, db_path: str | Path) -> None:
        self.config = SimpleNamespace(time_zone="Australia/Melbourne")
        self.data = {DOMAIN: {"db_path": str(db_path)}}

    async def async_add_executor_job(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def _watch_entry(**kwargs) -> SimpleNamespace:
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
        CONF_MODE: MODE_METRO,
        CONF_ROUTE_ID_FILTER: ROUTE_FRANKSTON,
        CONF_STOP_ID: "RICHMOND",
    }
    options = {CONF_TRACK_VEHICLES: True}
    data.update(kwargs.pop("data", {}))
    options.update(kwargs.pop("options", {}))
    return SimpleNamespace(
        entry_id="watch1",
        title="Coburg",
        data=data,
        options=options,
        subentries={},
        runtime_data=None,
        async_on_unload=lambda *a, **k: None,
    )


# ---------------------------------------------------------------------------
# Fleet matching (route filter incl. trip_id → route_id lookup)
# ---------------------------------------------------------------------------


class TestFleetMatching:
    def test_matches_feed_route_and_trip_id_lookup(self, metro_db: Path):
        positions = _fleet_positions()
        update = update_fleet(
            {}, list(positions.values()), WATCHED, _route_lookup(metro_db, positions), {}, NOW
        )
        # V_A / V_B match via feed route_id; V_D matches via trips-table lookup;
        # V_C (Werribee) is dropped.
        assert set(update.vehicles) == {"V_A", "V_B", "V_D"}
        assert set(update.added) == {"V_A", "V_B", "V_D"}
        assert update.removed == ()

    def test_route_ids_for_trips_returns_schedule_mapping(self, metro_db: Path):
        mapping = route_ids_for_trips(metro_db, ["T_FRK_OUT_3", "T_WRB_OUT_1"])
        assert mapping["T_FRK_OUT_3"] == "R_METRO_FRANKSTON"
        assert mapping["T_WRB_OUT_1"] == "R_METRO_WERRIBEE"

    def test_vehicle_without_vehicle_id_is_dropped(self, metro_db: Path):
        pos = _vp("", route_id=ROUTE_FRANKSTON)
        update = update_fleet({}, [pos], WATCHED, {}, {}, NOW)
        assert update.vehicles == {}
        assert update.added == ()

    def test_vehicle_state_mapping(self):
        assert vehicle_state("IN_TRANSIT_TO") == "moving"
        assert vehicle_state("STOPPED_AT") == "stationary"
        assert vehicle_state("INCOMING_AT") == "stationary"
        assert vehicle_state(None) is None

    def test_route_resolution_precedence(self):
        from custom_components.transport_victoria.vehicle_fleet import resolve_vehicle_route

        # 1. feed route_id wins over both lookups.
        assert resolve_vehicle_route(
            _vp("V1", route_id="R_FEED"), {"T_V1": "R_TABLE"}, {"T_V1": "R_UPDATES"}
        ) == "R_FEED"
        # 2. trips table fallback.
        assert resolve_vehicle_route(
            _vp("V1", route_id=""), {"T_V1": "R_TABLE"}, {}
        ) == "R_TABLE"
        # 3. trip-updates descriptor fallback.
        assert resolve_vehicle_route(
            _vp("V1", route_id=""), {}, {"T_V1": "R_UPDATES"}
        ) == "R_UPDATES"
        # Unresolvable → None.
        assert resolve_vehicle_route(_vp("V1", route_id=""), {}, {}) is None

    def test_trip_updates_index_exposes_trip_routes(self):
        from custom_components.transport_victoria.realtime.trip_updates import (
            parse_trip_updates_index,
        )
        from tests.realtime_fixtures import trip_update_delay

        index = parse_trip_updates_index(trip_update_delay())
        assert index.trip_routes == {"T_FRK_OUT_2": "R_METRO_FRANKSTON"}


# ---------------------------------------------------------------------------
# Appear / remove lifecycle (grace)
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_appear_then_removed_after_grace(self, metro_db: Path):
        positions = _fleet_positions()
        lookup = _route_lookup(metro_db, positions)

        # Poll 1: appear.
        u1 = update_fleet({}, list(positions.values()), WATCHED, lookup, {}, NOW)
        assert set(u1.added) == {"V_A", "V_B", "V_D"}
        tracked = u1.vehicles

        # Poll 2 (+30s): still present — no transitions.
        t2 = NOW + timedelta(seconds=30)
        u2 = update_fleet(tracked, list(positions.values()), WATCHED, lookup, {}, t2)
        assert u2.added == () and u2.removed == ()
        tracked = u2.vehicles

        # Three consecutive misses (empty feed) at +60s/+90s/+120s.
        for step in (60, 90):
            t = NOW + timedelta(seconds=step)
            u = update_fleet(tracked, [], WATCHED, lookup, {}, t)
            assert u.removed == ()  # within grace — retained with last state
            tracked = u.vehicles

        u_final = update_fleet(tracked, [], WATCHED, lookup, {}, NOW + timedelta(seconds=120))
        assert set(u_final.removed) == {"V_A", "V_B", "V_D"}
        assert u_final.vehicles == {}

    def test_grace_boundary(self, metro_db: Path):
        pos = _vp("V_X", route_id=ROUTE_FRANKSTON)
        tracked = update_fleet({}, [pos], WATCHED, {}, {}, NOW).vehicles
        # Absent just under the grace → retained.
        before = update_fleet(
            tracked, [], WATCHED, {}, {}, NOW + timedelta(seconds=GRACE_ABSENT_SECONDS - 1)
        )
        assert before.removed == ()
        # Absent exactly at the grace → removed.
        at = update_fleet(
            tracked, [], WATCHED, {}, {}, NOW + timedelta(seconds=GRACE_ABSENT_SECONDS)
        )
        assert at.removed == ("V_X",)


# ---------------------------------------------------------------------------
# Cap
# ---------------------------------------------------------------------------


class TestCap:
    def test_cap_drops_overflow_by_vehicle_id(self):
        positions = [_vp(f"V_{i}") for i in range(5)]
        update = update_fleet({}, positions, frozenset({"R"}), {}, {}, NOW, max_vehicles=3)
        assert set(update.vehicles) == {"V_0", "V_1", "V_2"}
        assert set(update.added) == {"V_0", "V_1", "V_2"}  # dropped never reported added
        assert update.removed == ()

    def test_default_cap_is_30(self):
        assert MAX_VEHICLES_PER_ENTRY == 30


# ---------------------------------------------------------------------------
# Feed staleness classification
# ---------------------------------------------------------------------------


class TestStaleness:
    def test_stale_header_classified(self):
        assert feed_is_stale(NOW - timedelta(seconds=240), None, NOW) is True
        assert feed_is_stale(NOW - timedelta(seconds=60), None, NOW) is False
        # Future header (clock skew) is fresh.
        assert feed_is_stale(NOW + timedelta(minutes=10), None, NOW) is False

    def test_fetched_at_fallback(self):
        assert feed_is_stale(None, NOW - timedelta(seconds=240), NOW) is True
        assert feed_is_stale(None, NOW, NOW) is False

    def test_no_signal_is_fresh(self):
        assert feed_is_stale(None, None, NOW) is False


# ---------------------------------------------------------------------------
# Scope derivation (watch / board / tram)
# ---------------------------------------------------------------------------


class TestFleetScope:
    def test_watch_with_route_filter(self):
        entry = _watch_entry()
        assert _fleet_scope(entry) == (
            frozenset({ROUTE_FRANKSTON}), frozenset({MODE_METRO}), frozenset(),
        )

    def test_watch_without_route_filter_derives_from_stop(self):
        entry = _watch_entry(data={CONF_ROUTE_ID_FILTER: None})
        routes, modes, stops = _fleet_scope(entry)
        assert routes == frozenset()  # no explicit route: resolved at reconcile
        assert modes == frozenset({MODE_METRO})
        assert stops == frozenset({"RICHMOND"})

    def test_tram_watch_has_no_modes(self):
        entry = _watch_entry(
            data={CONF_MODE: MODE_TRAM, CONF_ROUTE_ID_FILTER: "R_TRAM_96"}
        )
        routes, modes, stops = _fleet_scope(entry)
        assert routes == frozenset({"R_TRAM_96"})
        assert modes == frozenset()  # tram has no vehicle-positions feed
        assert stops == frozenset()

    def test_board_union_skips_tram(self):
        from custom_components.transport_victoria.board import PinData

        def pin(route_id, route_type):
            return PinData(
                pin_key=route_id, route_type=route_type, route_id=route_id,
                boarding_stop_id="S", boarding_stop_name="S",
            )

        subentries = {
            "s1": SimpleNamespace(data=pin("R_METRO_FRANKSTON", 400).to_dict()),
            "s2": SimpleNamespace(data=pin("R_TRAM_96", 0).to_dict()),
        }
        entry = SimpleNamespace(
            entry_id="board1", title="Board",
            data={CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD},
            options={}, subentries=subentries,
        )
        routes, modes, stops = _fleet_scope(entry)
        assert routes == frozenset({"R_METRO_FRANKSTON", "R_TRAM_96"})
        assert modes == frozenset({MODE_METRO})  # tram mode skipped
        assert stops == frozenset({"S"})


# ---------------------------------------------------------------------------
# Stop-derived fleet scope (unfiltered watch)
# ---------------------------------------------------------------------------


class TestFleetStopFallback:
    def test_routes_for_stop_returns_serving_routes(self, metro_db: Path):
        assert routes_for_stop(metro_db, "RICHMOND") == {
            "R_METRO_FRANKSTON", "R_METRO_WERRIBEE",
        }
        assert routes_for_stop(metro_db, "CAULFIELD") == {"R_METRO_FRANKSTON"}
        assert routes_for_stop(metro_db, "NOPE") == set()

    async def test_unfiltered_watch_resolves_routes_from_stop(
        self, metro_db: Path, monkeypatch
    ):
        monkeypatch.setattr(dt_module.dt_util, "utcnow", lambda: NOW)

        entry = _watch_entry(
            data={CONF_ROUTE_ID_FILTER: None, CONF_STOP_ID: "RICHMOND"}
        )
        coordinator = FleetTrackerCoordinator(FakeHass(metro_db), entry, metro_db)
        coordinator._client = FakeClient(
            vehicle_positions_fleet(), FeedMeta(header_timestamp=NOW, fetched_at=NOW)
        )
        await coordinator._async_update_data()
        # RICHMOND serves both Frankston and Werribee, so every fixture vehicle
        # (V_C on Werribee included) now matches the stop-derived scope.
        assert set(coordinator._tracked) == {"V_A", "V_B", "V_C", "V_D"}

    async def test_unfiltered_watch_stop_without_routes_has_no_fleet(
        self, metro_db: Path, monkeypatch
    ):
        monkeypatch.setattr(dt_module.dt_util, "utcnow", lambda: NOW)

        entry = _watch_entry(
            data={CONF_ROUTE_ID_FILTER: None, CONF_STOP_ID: "NOPE"}
        )
        coordinator = FleetTrackerCoordinator(FakeHass(metro_db), entry, metro_db)
        coordinator._client = FakeClient(
            vehicle_positions_fleet(), FeedMeta(header_timestamp=NOW, fetched_at=NOW)
        )
        await coordinator._async_update_data()
        assert coordinator._tracked == {}
        # No reconcile happened, so no fleet update (and thus no entities).
        assert coordinator.last_fleet_update is None


# ---------------------------------------------------------------------------
# Coordinator — stale feed keeps last state (does not raise / remove)
# ---------------------------------------------------------------------------


class TestCoordinatorStale:
    async def test_stale_feed_keeps_tracked_vehicles(self, metro_db: Path, monkeypatch):
        monkeypatch.setattr(dt_module.dt_util, "utcnow", lambda: NOW)

        entry = _watch_entry()
        coordinator = FleetTrackerCoordinator(FakeHass(metro_db), entry, metro_db)
        feed = vehicle_positions_fleet()

        # Fresh feed → vehicles appear.
        coordinator._client = FakeClient(
            feed, FeedMeta(header_timestamp=NOW, fetched_at=NOW)
        )
        await coordinator._async_update_data()
        assert set(coordinator._tracked) == {"V_A", "V_B", "V_D"}

        # Stale feed (old header) with the SAME vehicles still present → keeps
        # state (no removal, no exception).
        stale_meta = FeedMeta(header_timestamp=NOW - timedelta(minutes=10), fetched_at=None)
        coordinator._client = FakeClient(feed, stale_meta)
        await coordinator._async_update_data()
        assert set(coordinator._tracked) == {"V_A", "V_B", "V_D"}
        assert coordinator.last_fleet_update.removed == ()

    async def test_fetch_failure_keeps_last_state(self, metro_db: Path, monkeypatch):
        monkeypatch.setattr(dt_module.dt_util, "utcnow", lambda: NOW)

        entry = _watch_entry()
        coordinator = FleetTrackerCoordinator(FakeHass(metro_db), entry, metro_db)
        coordinator._client = FakeClient(
            vehicle_positions_fleet(), FeedMeta(header_timestamp=NOW, fetched_at=NOW)
        )
        await coordinator._async_update_data()
        assert set(coordinator._tracked) == {"V_A", "V_B", "V_D"}

        # A fetch failure must NOT start the removal grace — keep last state.
        coordinator._client = RaisingClient()
        await coordinator._async_update_data()
        assert set(coordinator._tracked) == {"V_A", "V_B", "V_D"}


# ---------------------------------------------------------------------------
# Opt-in gate
# ---------------------------------------------------------------------------


class TestOptIn:
    async def test_off_creates_no_entities(self, metro_db: Path):
        entry = _watch_entry(options={CONF_TRACK_VEHICLES: False})
        added: list = []
        await dt_module.async_setup_entry(
            FakeHass(metro_db), entry, lambda ents: added.extend(ents)
        )
        assert added == []
        # No fleet coordinator stashed either.
        assert "fleet_coordinator" not in (entry.runtime_data or {})


# ---------------------------------------------------------------------------
# Entity naming / unique-id contract
# ---------------------------------------------------------------------------


class TestEntityContract:
    def _entity(self, vehicle_id: str = "V123456") -> PTVFleetVehicleTracker:
        entry = _watch_entry()
        coordinator = SimpleNamespace(data={}, last_update_success=True)
        return PTVFleetVehicleTracker(coordinator, entry, vehicle_id)

    def test_unique_id_format(self):
        assert self._entity()._attr_unique_id == "watch1:vehiclefleet:V123456"

    def test_name_uses_id_suffix_and_has_entity_name_false(self):
        entity = self._entity("V123456")
        assert entity._attr_has_entity_name is False
        assert entity.name == "Vehicle 3456"

    def test_short_id_uses_whole_id(self):
        assert self._entity("V12").name == "Vehicle V12"

    def test_state_motion_and_fallback(self):
        entity = self._entity()
        # Empty data → no vehicle → "not_home".
        assert entity.state == "not_home"
        assert entity.latitude is None

    def test_available_follows_coordinator(self):
        entity = self._entity()
        assert entity.available is True
        entity.coordinator.last_update_success = False
        assert entity.available is False


# ---------------------------------------------------------------------------
# Legacy limiter removal
# ---------------------------------------------------------------------------


class TestLegacyLimiterRemoval:
    def test_device_tracker_has_no_async_rate_limiter(self):
        # The old AsyncRateLimiter(24/60s) double-throttle is gone — the shared
        # client's PerFeedRateLimiter is the single enforcement point.
        assert not hasattr(dt_module, "AsyncRateLimiter")
