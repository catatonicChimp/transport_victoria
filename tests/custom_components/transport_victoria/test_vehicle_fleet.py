"""T16 dynamic vehicle-fleet tracking unit tests (light env).

Covers the pure ``vehicle_fleet`` reconcile (matching, appear/remove lifecycle,
cap), the ``_fleet_scope`` derivation (watch/board/tram), the coordinator's
stale-feed "keep last state" behaviour, the entity naming/unique-id contract,
the opt-in gate, and the removal of the legacy double-throttle limiter.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.transport_victoria import device_tracker as dt_module
from custom_components.transport_victoria.device_tracker import (
    FleetTrackerCoordinator,
    PTVFleetVehicleTracker,
    RouteVehicleCoordinator,
    _fleet_scope,
)
from custom_components.transport_victoria.const import (
    CONF_ENTRY_TYPE,
    CONF_MAX_VEHICLES,
    CONF_MODE,
    CONF_ROUTE_ID,
    CONF_ROUTE_ID_FILTER,
    CONF_ROUTE_SHORT_NAME,
    CONF_STOP_ID,
    CONF_TRACK_VEHICLES,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_WATCH,
    MODE_METRO,
    MODE_TRAM,
)
from custom_components.transport_victoria.gtfs.repository import (
    route_ids_for_trips,
    routes_for_stop,
    trip_terminals,
)
from custom_components.transport_victoria.realtime.client import FeedError, FeedMeta
from custom_components.transport_victoria.realtime.vehicle_positions import (
    VehiclePosition,
    parse_vehicle_positions_by_vehicle,
)
from custom_components.transport_victoria.vehicle_fleet import (
    GRACE_ABSENT_SECONDS,
    MAX_VEHICLES_PER_ENTRY,
    FleetVehicle,
    feed_is_stale,
    matched_trip_ids,
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
# Terminal / destination enrichment
# ---------------------------------------------------------------------------


class TestTripTerminals:
    def test_returns_terminal_info(self, metro_db: Path):
        result = trip_terminals(metro_db, ["T_FRK_OUT_1", "T_WRB_OUT_1", "T_UNKNOWN"])
        assert result["T_FRK_OUT_1"].terminal_stop_id == "FRANKSTON"
        assert result["T_FRK_OUT_1"].terminal_stop_name == "Frankston"
        assert result["T_FRK_OUT_1"].direction_id == 0
        assert result["T_WRB_OUT_1"].terminal_stop_name == "Werribee"
        assert "T_UNKNOWN" not in result

    def test_empty_input_returns_empty(self, metro_db: Path):
        assert trip_terminals(metro_db, []) == {}


class TestFleetEnrichment:
    def test_reconcile_enriches_terminal(self, metro_db: Path):
        positions = _fleet_positions()
        lookup = _route_lookup(metro_db, positions)
        matched = matched_trip_ids(list(positions.values()), WATCHED, lookup, {})
        terminals = trip_terminals(metro_db, sorted(matched))
        update = update_fleet(
            {}, list(positions.values()), WATCHED, lookup, {}, NOW, terminals=terminals
        )
        assert update.vehicles["V_A"].terminal_stop_name == "Frankston"
        assert update.vehicles["V_A"].terminal_stop_id == "FRANKSTON"
        assert update.vehicles["V_A"].trip_terminal_direction_id == 0
        # V_D (no feed route) resolves via the trips table and also gets a terminal.
        assert update.vehicles["V_D"].terminal_stop_name == "Frankston"
        # V_C is on the unwatched Werribee route and is dropped entirely.
        assert "V_C" not in update.vehicles

    def test_unknown_trip_has_none_terminal(self, metro_db: Path):
        pos = _vp("V_X", route_id=ROUTE_FRANKSTON)
        update = update_fleet({}, [pos], WATCHED, {}, {}, NOW, terminals={})
        vehicle = update.vehicles["V_X"]
        assert vehicle.terminal_stop_name is None
        assert vehicle.terminal_stop_id is None
        assert vehicle.trip_terminal_direction_id is None


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
# Destination / direction on the tracker name + attributes
# ---------------------------------------------------------------------------


class TestEntityDestination:
    def _entity(self, terminal_name: str | None = None) -> PTVFleetVehicleTracker:
        entry = _watch_entry()
        vehicle = FleetVehicle(
            vehicle_id="V123456",
            route_id=ROUTE_FRANKSTON,
            trip_id="T_FRK_OUT_1",
            latitude=-37.8,
            longitude=145.0,
            bearing=None,
            speed_kmh=None,
            vehicle_label=None,
            current_status="IN_TRANSIT_TO",
            occupancy_status=None,
            direction_id=0,
            last_seen=NOW,
            terminal_stop_id="FRANKSTON" if terminal_name else None,
            terminal_stop_name=terminal_name,
            trip_terminal_direction_id=0 if terminal_name else None,
        )
        coordinator = SimpleNamespace(
            data={"V123456": vehicle}, last_update_success=True
        )
        return PTVFleetVehicleTracker(coordinator, entry, "V123456")

    def test_name_includes_terminal(self):
        assert self._entity("Flinders Street").name == "Vehicle 3456 \u2192 Flinders Street"

    def test_name_falls_back_without_terminal(self):
        assert self._entity(None).name == "Vehicle 3456"

    def test_attributes_include_destination(self):
        attrs = self._entity("Flinders Street").extra_state_attributes
        assert attrs["destination"] == "Flinders Street"
        assert attrs["terminal_stop_id"] == "FRANKSTON"
        assert attrs["trip_terminal_direction_id"] == 0
        assert attrs["trip_id"] == "T_FRK_OUT_1"
        assert attrs["direction_id"] == 0

    def test_name_updates_live_when_trip_changes(self):
        from dataclasses import replace

        entity = self._entity("Flinders Street")
        assert entity.name == "Vehicle 3456 \u2192 Flinders Street"
        entity.coordinator.data["V123456"] = replace(
            entity.coordinator.data["V123456"],
            terminal_stop_name="Werribee",
            terminal_stop_id="WERRIBEE",
        )
        assert entity.name == "Vehicle 3456 \u2192 Werribee"


# ---------------------------------------------------------------------------
# Legacy limiter removal
# ---------------------------------------------------------------------------


class TestLegacyLimiterRemoval:
    def test_device_tracker_has_no_async_rate_limiter(self):
        # The old AsyncRateLimiter(24/60s) double-throttle is gone — the shared
        # client's PerFeedRateLimiter is the single enforcement point.
        assert not hasattr(dt_module, "AsyncRateLimiter")


# ---------------------------------------------------------------------------
# Route tracker — missing feed degrades gracefully (tram has no vehicle-positions)
# ---------------------------------------------------------------------------


class _RouteFeedClient:
    """Stub client for the route tracker's ``fetch_feed`` port."""

    def __init__(self, feed=None, error=None) -> None:
        self._feed = feed
        self._error = error
        self.calls: list[tuple[str, str]] = []

    async def fetch_feed(self, mode: str, feed_type: str):
        self.calls.append((mode, feed_type))
        if self._error is not None:
            raise self._error
        return self._feed


def _route_entry(mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="route1",
        title="Route Tracker",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_ROUTE_TRACKER,
            CONF_MODE: mode,
            CONF_ROUTE_ID: "R_METRO_FRANKSTON" if mode == MODE_METRO else "R_TRAM_6",
            CONF_ROUTE_SHORT_NAME: "Frankston" if mode == MODE_METRO else "6",
        },
        options={CONF_MAX_VEHICLES: 5},
    )


class TestRouteTrackerMissingFeed:
    async def test_tram_missing_feed_degrades_and_logs_once(self, caplog):
        coordinator = RouteVehicleCoordinator(
            FakeHass(Path("x.db")), _route_entry(MODE_TRAM), 0
        )
        coordinator._client = _RouteFeedClient(
            error=FeedError(404, "feed not published for this mode", unavailable=True)
        )
        with caplog.at_level(logging.WARNING):
            result1 = await coordinator._async_update_data()
            result2 = await coordinator._async_update_data()

        # Setup succeeds (no exception); the tracker degrades to zero vehicles.
        assert result1 == []
        assert result2 == []
        warnings = [r for r in caplog.records if "not published" in r.message]
        assert len(warnings) == 1

    async def test_metro_route_tracker_unchanged(self):
        coordinator = RouteVehicleCoordinator(
            FakeHass(Path("x.db")), _route_entry(MODE_METRO), 0
        )
        coordinator._client = _RouteFeedClient(feed=vehicle_positions_fleet())
        result = await coordinator._async_update_data()
        # Frankston route: V_A / V_B match; V_C (Werribee) and V_D (no route) drop.
        assert {pos.vehicle_id for pos in result} == {"V_A", "V_B"}

    async def test_route_tracker_auth_error_still_raises(self):
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coordinator = RouteVehicleCoordinator(
            FakeHass(Path("x.db")), _route_entry(MODE_METRO), 0
        )
        coordinator._client = _RouteFeedClient(error=FeedError(401, "auth rejected"))
        with pytest.raises(Exception) as excinfo:
            await coordinator._async_update_data()
        # Auth failure surfaces (ConfigEntryAuthFailed == Exception) — not degraded
        # to empty, and not the retryable UpdateFailed path.
        assert not isinstance(excinfo.value, UpdateFailed)
