"""T9 board aggregation unit tests (light env, stub style).

Covers ``custom_components.transport_victoria.board_aggregation``: cross-pin
merge ordering, deterministic tie-breaks, via-hint dedupe, pin filtering,
realtime join (delay/cancel/skip), static fallback (disabled/failed/stale),
shared feed caching, alert matching, and the pin-order-independent sync index.

Pure functions are exercised directly against the T6 schedule + realtime
fixtures with injected ports — no event loop, no homeassistant objects.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.transport_victoria import board_aggregation as ba
from custom_components.transport_victoria.board import PinData, line_pins, pin_key
from custom_components.transport_victoria.board_aggregation import (
    DELAY_UNKNOWN,
    FEED_FAILED,
    FEED_OK,
    FEED_STALE,
    REALTIME_LIVE,
    REALTIME_SCHEDULED,
    REALTIME_STALE,
    AlertMatch,
    BoardAlert,
    BoardDeparture,
    FeedHealth,
    _departure_sort_key,
    aggregate_board,
    compute_sync_index,
    fetch_board_realtime,
    match_board_alerts,
    route_type_to_mode,
)
from custom_components.transport_victoria.realtime.client import _decode_feed
from custom_components.transport_victoria.realtime.service_alerts import (
    ActivePeriod,
    ServiceAlert,
    is_alert_active,
    parse_service_alerts,
)
from custom_components.transport_victoria.realtime.trip_updates import (
    TripUpdateIndex,
    parse_trip_updates_index,
)

from tests.gtfs_builder import REFERENCE_NOW
from tests.realtime_fixtures import load_bytes

BASE = REFERENCE_NOW  # 2026-06-01 08:00 local
NOW_UTC = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)

R_FRANKSTON = "R_METRO_FRANKSTON"
R_WERRIBEE = "R_METRO_WERRIBEE"
RICHMOND = "RICHMOND"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pin(
    route_id: str,
    boarding: str,
    boarding_name: str = "",
    *,
    all_dest: bool = True,
    dest: str | None = None,
    direction: int | None = None,
    via: str | None = None,
    label_to: str = "",
    platform: str | None = None,
    include: bool = True,
    route_type: int = 1,
) -> PinData:
    return PinData(
        pin_key=pin_key(route_id, boarding, dest, direction, platform, via),
        route_type=route_type,
        route_id=route_id,
        boarding_stop_id=boarding,
        boarding_stop_name=boarding_name,
        all_destinations=all_dest,
        destination_stop_id=dest,
        direction_id=direction,
        via_hint=via,
        label_to=label_to,
        platform_filter=platform,
        include=include,
    )


def _dep(
    *,
    trip_id: str = "T",
    seq: int = 1,
    eff: datetime | None = None,
    route: str = "220",
    headsign: str = "City",
    boarding: str = "S1",
    pin_index: int = 0,
    pin_key_: str = "p",
    **kw,
) -> BoardDeparture:
    eff = eff or BASE
    fields = dict(
        pin_key=pin_key_,
        pin_order_index=pin_index,
        route_id="R",
        route_short_name=route,
        route_long_name="",
        headsign=headsign,
        destination_label=headsign,
        boarding_stop_id=boarding,
        boarding_stop_name="",
        platform=None,
        scheduled_departure=eff,
        scheduled_seconds=0,
        expected_departure=eff,
        delay_seconds=0,
        is_cancelled=False,
        is_skipped=False,
        skipped_stops_count=0,
        realtime_state=REALTIME_LIVE,
        trip_id=trip_id,
        stop_sequence=seq,
        route_color=None,
        direction_id=None,
    )
    fields.update(kw)
    return BoardDeparture(**fields)


def _alert(
    alert_id: str = "a",
    header: str = "h",
    *,
    route_ids=(),
    stop_ids=(),
    trip_ids=(),
    periods=None,
) -> ServiceAlert:
    return ServiceAlert(
        alert_id=alert_id,
        header=header,
        description="",
        cause="UNKNOWN_CAUSE",
        effect="SIGNIFICANT_DELAYS",
        active_periods=tuple(periods or ()),
        route_ids=tuple(route_ids),
        stop_ids=tuple(stop_ids),
        trip_ids=tuple(trip_ids),
        direction_ids=(),
    )


def _merge_indexes(*indexes: TripUpdateIndex) -> TripUpdateIndex:
    stop_delays: dict = {}
    trip_cancelled: dict[str, bool] = {}
    trip_skipped_count: dict[str, int] = {}
    for idx in indexes:
        stop_delays.update(idx.stop_delays)
        trip_cancelled.update(idx.trip_cancelled)
        trip_skipped_count.update(idx.trip_skipped_count)
    return TripUpdateIndex(stop_delays, trip_cancelled, trip_skipped_count)


class CountingClient:
    """Async realtime client stub that records fetch_feed calls."""

    def __init__(self, feeds: dict[tuple[str, str], object]) -> None:
        self.feeds = feeds
        self.calls: list[tuple[str, str]] = []

    async def fetch_feed(self, mode: str, feed_type: str) -> object:
        self.calls.append((mode, feed_type))
        result = self.feeds[(mode, feed_type)]
        if isinstance(result, Exception):
            raise result
        return result


# ---------------------------------------------------------------------------
# route_type → mode
# ---------------------------------------------------------------------------


class TestRouteTypeToMode:
    def test_standard_and_hvt_mappings(self):
        assert route_type_to_mode(0) == "tram"
        assert route_type_to_mode(1) == "metro"
        assert route_type_to_mode(2) == "vline"
        assert route_type_to_mode(3) == "bus"
        assert route_type_to_mode(400) == "metro"
        assert route_type_to_mode(701) == "regional-bus"
        assert route_type_to_mode(204) == "vline"

    def test_unmapped_returns_none(self):
        assert route_type_to_mode(999) is None


# ---------------------------------------------------------------------------
# Sort + dedupe (pure)
# ---------------------------------------------------------------------------


class TestSortAndDedupe:
    def test_route_sort_value_numeric_first(self):
        assert ba._route_sort_value("2") < ba._route_sort_value("10")
        assert ba._route_sort_value("10") < ba._route_sort_value("City")
        assert ba._route_sort_value("96") < ba._route_sort_value("Frankston")

    def test_tie_breaks_are_deterministic(self):
        # Same effective time → boarding stop id breaks the tie.
        a = _dep(trip_id="A", boarding="S1", pin_index=1)
        b = _dep(trip_id="B", boarding="S2", pin_index=0)
        assert _departure_sort_key(a) < _departure_sort_key(b)

        # Same stop → route number breaks the tie.
        c = _dep(trip_id="C", boarding="S1", route="2")
        d = _dep(trip_id="D", boarding="S1", route="10")
        assert _departure_sort_key(c) < _departure_sort_key(d)

        # Same stop + route → headsign, then pin index, then trip id.
        e = _dep(trip_id="E", boarding="S1", route="2", headsign="City", pin_index=0)
        f = _dep(trip_id="F", boarding="S1", route="2", headsign="City", pin_index=1)
        assert _departure_sort_key(e) < _departure_sort_key(f)

        g = _dep(trip_id="G", boarding="S1", route="2", headsign="City", pin_index=0)
        h = _dep(trip_id="H", boarding="S1", route="2", headsign="City", pin_index=0)
        assert _departure_sort_key(g) < _departure_sort_key(h)  # trip id last

    def test_sort_is_stable_across_identical_lists(self):
        rows = [
            _dep(trip_id="X", boarding="S1", pin_index=1),
            _dep(trip_id="Y", boarding="S1", pin_index=0),
        ]
        first = sorted(rows, key=_departure_sort_key)
        second = sorted(rows, key=_departure_sort_key)
        assert [r.trip_id for r in first] == [r.trip_id for r in second]

    def test_dedupe_keeps_first_pin_and_preserves_loop_occurrences(self):
        # Two occurrences of the same trip at different sequences are KEPT.
        loop_a = _dep(trip_id="LOOP", seq=1, pin_key_="p1")
        loop_b = _dep(trip_id="LOOP", seq=4, pin_key_="p1")
        # A duplicate of seq=1 from another pin is DROPPED (first pin wins).
        dup = _dep(trip_id="LOOP", seq=1, pin_key_="p2")
        result = ba._dedupe([loop_a, dup, loop_b])
        assert [r.stop_sequence for r in result] == [1, 4]
        assert all(r.pin_key == "p1" for r in result)


# ---------------------------------------------------------------------------
# Sync index (pure)
# ---------------------------------------------------------------------------


class TestSyncIndex:
    def test_empty_board_has_stable_index(self):
        assert compute_sync_index([]) == compute_sync_index([])

    def test_stable_for_identical_rows(self):
        rows = [_dep(trip_id="A", eff=BASE), _dep(trip_id="B", eff=BASE)]
        assert compute_sync_index(rows) == compute_sync_index(rows)

    def test_changes_when_row_time_changes(self):
        before = [_dep(trip_id="A", eff=BASE)]
        after = [_dep(trip_id="A", eff=BASE + timedelta(minutes=5))]
        assert compute_sync_index(before) != compute_sync_index(after)

    def test_changes_when_row_set_changes(self):
        assert compute_sync_index([_dep(trip_id="A")]) != compute_sync_index(
            [_dep(trip_id="A"), _dep(trip_id="B")]
        )

    def test_changes_on_cancel_or_skip_status(self):
        base = _dep(trip_id="A", eff=BASE)
        cancelled = _dep(trip_id="A", eff=BASE, is_cancelled=True)
        skipped = _dep(trip_id="A", eff=BASE, is_skipped=True, skipped_stops_count=1)
        assert compute_sync_index([base]) != compute_sync_index([cancelled])
        assert compute_sync_index([base]) != compute_sync_index([skipped])

    def test_independent_of_pin_order(self):
        a = _dep(trip_id="A", eff=BASE, pin_index=0, pin_key_="p1")
        b = _dep(trip_id="B", eff=BASE, pin_index=1, pin_key_="p2")
        # Same two rows, swapped pin_order_index (order-independent).
        a2 = _dep(trip_id="A", eff=BASE, pin_index=1, pin_key_="p1")
        b2 = _dep(trip_id="B", eff=BASE, pin_index=0, pin_key_="p2")
        assert compute_sync_index([a, b]) == compute_sync_index([a2, b2])


# ---------------------------------------------------------------------------
# Merge ordering across pins (metro_db)
# ---------------------------------------------------------------------------


class TestMergeOrdering:
    def test_interleaves_two_routes_at_a_shared_stop(self, metro_db: Path):
        pins = [
            _pin(R_FRANKSTON, RICHMOND, "Richmond"),
            _pin(R_WERRIBEE, RICHMOND, "Richmond"),
        ]
        result = aggregate_board(metro_db, pins, BASE, realtime_enabled=False)

        # Frankston + Werribee both serve Richmond; merged ascending by time.
        trip_ids = [r.trip_id for r in result.rows]
        assert trip_ids == [
            "T_FRK_OUT_2",  # 08:07
            "T_WRB_OUT_2",  # 08:22
            "T_FRK_IN_1",   # 08:28
            "T_FRK_OUT_3",  # 09:07
            "T_WRB_OUT_3",  # 09:22
            "T_FRK_LATE",   # 01:10 next day (post-midnight)
        ]
        times = [r.effective_departure for r in result.rows]
        assert times == sorted(times)

    def test_rows_are_time_sorted_across_pins(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond"),
                _pin(R_WERRIBEE, RICHMOND, "Richmond")]
        result = aggregate_board(metro_db, pins, BASE, realtime_enabled=False)
        effective = [r.effective_departure for r in result.rows]
        assert effective == sorted(effective)


# ---------------------------------------------------------------------------
# via-hint dedupe (bus_db)
# ---------------------------------------------------------------------------


class TestViaHintDedupe:
    def test_two_via_pins_collapse_to_one_row_set(self, bus_db: Path):
        pins = line_pins(bus_db, "B_SUNSHINE", "Sunshine", "B_CITY")
        assert len(pins) == 2  # City via Footscray + City via Kensington

        result = aggregate_board(bus_db, pins, BASE, realtime_enabled=False)

        # Both pins surface the same City-bound trips; deduped to one set.
        trip_ids = [r.trip_id for r in result.rows]
        assert len(trip_ids) == len(set(trip_ids))  # no duplicate trips
        # Every surviving row carries the first-in-order pin's identity.
        assert all(r.pin_key == pins[0].pin_key for r in result.rows)

    def test_via_dedupe_surface_count(self, bus_db: Path):
        pins = line_pins(bus_db, "B_SUNSHINE", "Sunshine", "B_CITY")
        result = aggregate_board(bus_db, pins, BASE, realtime_enabled=False)
        # City-bound trips at Sunshine: T_BUS_LONG, T_BUS_VIA_A, T_BUS_VIA_B.
        assert sorted(r.trip_id for r in result.rows) == [
            "T_BUS_LONG", "T_BUS_VIA_A", "T_BUS_VIA_B",
        ]


# ---------------------------------------------------------------------------
# Pin filtering (include + invalid)
# ---------------------------------------------------------------------------


class TestPinFiltering:
    def test_include_false_pin_is_skipped(self, metro_db: Path):
        pins = [
            _pin(R_FRANKSTON, RICHMOND, "Richmond"),
            _pin(R_WERRIBEE, RICHMOND, "Richmond", include=False),
        ]
        result = aggregate_board(metro_db, pins, BASE, realtime_enabled=False)
        assert all(r.route_id == R_FRANKSTON for r in result.rows)
        assert result.rows  # non-empty (Frankston still contributes)

    def test_invalid_pin_is_skipped(self, metro_db: Path):
        pins = [
            _pin(R_FRANKSTON, RICHMOND, "Richmond"),
            _pin(R_WERRIBEE, RICHMOND, "Richmond"),
        ]
        invalid = {pins[1].pin_key}
        result = aggregate_board(
            metro_db, pins, BASE, realtime_enabled=False, invalid_pins=invalid
        )
        assert all(r.route_id == R_FRANKSTON for r in result.rows)


# ---------------------------------------------------------------------------
# Realtime join (delay / cancel / skip)
# ---------------------------------------------------------------------------


def _realtime_index() -> TripUpdateIndex:
    return _merge_indexes(
        parse_trip_updates_index(_decode_feed(load_bytes("trip_update_delay"))),
        parse_trip_updates_index(_decode_feed(load_bytes("trip_update_canceled"))),
        parse_trip_updates_index(_decode_feed(load_bytes("trip_update_skipped"))),
    )


class TestRealtimeJoin:
    def test_delay_join(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = aggregate_board(
            metro_db, pins, BASE,
            trip_updates=_realtime_index(),
        )
        row = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_2")
        assert row.delay_seconds == 90
        assert row.expected_departure == row.scheduled_departure + timedelta(seconds=90)
        assert row.realtime_state == REALTIME_LIVE

    def test_cancel_join(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = aggregate_board(
            metro_db, pins, BASE, trip_updates=_realtime_index(),
        )
        row = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_3")
        assert row.is_cancelled is True

    def test_skip_join(self, metro_db: Path):
        pins = [_pin(R_WERRIBEE, RICHMOND, "Richmond")]
        result = aggregate_board(
            metro_db, pins, BASE, trip_updates=_realtime_index(),
        )
        row = next(r for r in result.rows if r.trip_id == "T_WRB_OUT_2")
        assert row.is_skipped is True
        assert row.skipped_stops_count == 1

    def test_unmatched_trip_stays_scheduled(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = aggregate_board(
            metro_db, pins, BASE, trip_updates=_realtime_index(),
        )
        # T_FRK_IN_1 has no trip update in the fixtures → scheduled, delay -1.
        row = next(r for r in result.rows if r.trip_id == "T_FRK_IN_1")
        assert row.realtime_state == REALTIME_SCHEDULED
        assert row.delay_seconds == DELAY_UNKNOWN
        assert row.expected_departure is None


# ---------------------------------------------------------------------------
# Static fallback (disabled / failed / stale)
# ---------------------------------------------------------------------------


class TestStaticFallback:
    def test_realtime_disabled_all_scheduled(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = aggregate_board(metro_db, pins, BASE, realtime_enabled=False)
        assert result.rows
        assert all(r.realtime_state == REALTIME_SCHEDULED for r in result.rows)
        assert all(r.delay_seconds == DELAY_UNKNOWN for r in result.rows)

    def test_feed_failed_rows_scheduled_and_health_failed(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        health = {("metro", "trip-updates"): FeedHealth(FEED_FAILED)}
        result = aggregate_board(
            metro_db, pins, BASE,
            trip_updates=None,  # fetch failed → no realtime index
            feed_health=health,
        )
        assert result.rows
        assert all(r.realtime_state == REALTIME_SCHEDULED for r in result.rows)
        assert result.feed_health[("metro", "trip-updates")].state == FEED_FAILED

    def test_stale_ttl_marks_rows_stale(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = aggregate_board(
            metro_db, pins, BASE,
            trip_updates=_realtime_index(),
            trip_updates_stale=True,
        )
        matched = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_2")
        assert matched.realtime_state == REALTIME_STALE
        assert matched.delay_seconds == 90  # still applies the stale delay


# ---------------------------------------------------------------------------
# Shared feed cache (async fetch helper)
# ---------------------------------------------------------------------------


class TestSharedCache:
    async def test_two_pins_same_mode_one_fetch(self):
        client = CountingClient({
            ("metro", "trip-updates"): _decode_feed(load_bytes("header_tick_0")),
            ("metro", "service-alerts"): _decode_feed(load_bytes("header_tick_0")),
        })
        pins = [_pin(R_FRANKSTON, RICHMOND, route_type=1),
                _pin(R_WERRIBEE, RICHMOND, route_type=1)]
        data = await fetch_board_realtime(client, pins, True, now=NOW_UTC)

        trip_calls = [c for c in client.calls if c == ("metro", "trip-updates")]
        alert_calls = [c for c in client.calls if c == ("metro", "service-alerts")]
        assert len(trip_calls) == 1
        assert len(alert_calls) == 1
        assert len(client.calls) == 2
        assert data.trip_updates is not None

    async def test_realtime_disabled_makes_no_requests(self):
        client = CountingClient({})
        pins = [_pin(R_FRANKSTON, RICHMOND, route_type=1)]
        data = await fetch_board_realtime(client, pins, False)
        assert client.calls == []
        assert data.trip_updates is None
        assert data.alerts == []
        assert data.feed_health == {}

    async def test_feed_failure_records_health(self):
        client = CountingClient({
            ("metro", "trip-updates"): RuntimeError("boom"),
            ("metro", "service-alerts"): _decode_feed(load_bytes("header_tick_0")),
        })
        pins = [_pin(R_FRANKSTON, RICHMOND, route_type=1)]
        data = await fetch_board_realtime(client, pins, True, now=NOW_UTC)
        assert data.feed_health[("metro", "trip-updates")].state == FEED_FAILED
        assert data.feed_health[("metro", "service-alerts")].state == FEED_OK
        assert data.trip_updates is None  # all trip feeds failed

    async def test_stale_feed_detected(self, monkeypatch):
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        monkeypatch.setattr(ba, "feed_fetched_at", lambda m, f: old)
        client = CountingClient({
            ("metro", "trip-updates"): _decode_feed(load_bytes("trip_update_delay")),
            ("metro", "service-alerts"): _decode_feed(load_bytes("header_tick_0")),
        })
        pins = [_pin(R_FRANKSTON, RICHMOND, route_type=1)]
        data = await fetch_board_realtime(client, pins, True, now=NOW_UTC)
        assert data.trip_updates_stale is True
        assert data.feed_health[("metro", "trip-updates")].state == FEED_STALE


# ---------------------------------------------------------------------------
# Alert matching
# ---------------------------------------------------------------------------


class TestAlertMatching:
    def _pins(self):
        return {
            _pin(R_FRANKSTON, RICHMOND, "Richmond").pin_key: _pin(R_FRANKSTON, RICHMOND, "Richmond"),
            _pin(R_WERRIBEE, RICHMOND, "Richmond").pin_key: _pin(R_WERRIBEE, RICHMOND, "Richmond"),
        }

    def test_route_level_match(self):
        pins = self._pins()
        trips = {k: {"T_FRK_OUT_2"} if "FRANKSTON" in v.route_id else {"T_WRB_OUT_2"}
                 for k, v in pins.items()}
        alerts = [_alert(route_ids=(R_FRANKSTON,))]
        matched = match_board_alerts(alerts, pins, trips, NOW_UTC)
        assert len(matched) == 1
        assert matched[0].matches[0].pin_key == next(
            k for k, v in pins.items() if v.route_id == R_FRANKSTON
        )
        assert matched[0].matches[0].reasons == ("route",)

    def test_stop_level_match(self):
        pins = self._pins()
        trips = {k: set() for k in pins}
        alerts = [_alert(stop_ids=(RICHMOND,))]
        matched = match_board_alerts(alerts, pins, trips, NOW_UTC)
        assert len(matched) == 1
        assert {m.pin_key for m in matched[0].matches} == set(pins)
        assert all(m.reasons == ("stop",) for m in matched[0].matches)

    def test_trip_level_match(self):
        pins = self._pins()
        trips = {k: {"T_FRK_OUT_2"} for k in pins}
        alerts = [_alert(trip_ids=("T_FRK_OUT_2",))]
        matched = match_board_alerts(alerts, pins, trips, NOW_UTC)
        # Every pin has the trip, so all match (trip is pin-agnostic here).
        assert {m.pin_key for m in matched[0].matches} == set(pins)

    def test_inactive_alert_excluded(self):
        pins = self._pins()
        trips = {k: set() for k in pins}
        past = ActivePeriod(
            start=datetime(2020, 1, 1, tzinfo=timezone.utc),
            end=datetime(2021, 1, 1, tzinfo=timezone.utc),
        )
        alerts = [_alert(route_ids=(R_FRANKSTON,), periods=(past,))]
        assert match_board_alerts(alerts, pins, trips, NOW_UTC) == []

    def test_unscoped_alert_matches_all_pins(self):
        pins = self._pins()
        trips = {k: set() for k in pins}
        alerts = [_alert()]  # no selectors → agency-wide
        matched = match_board_alerts(alerts, pins, trips, NOW_UTC)
        assert {m.pin_key for m in matched[0].matches} == set(pins)
        assert all(m.reasons == ("agency",) for m in matched[0].matches)

    def test_fixture_alert_matches_route_stop_and_trip(self):
        pins = self._pins()
        trips = {k: {"T_FRK_OUT_2"} for k in pins}
        feed = _decode_feed(load_bytes("alert_service"))
        alerts = parse_service_alerts(feed)
        assert len(alerts) == 1
        matched = match_board_alerts(alerts, pins, trips, NOW_UTC)
        assert len(matched) == 1
        assert matched[0].alert_id == "alert-1"
        # The alert's route/stop/trip selectors all hit the Frankston pin.
        frankston_key = next(k for k, v in pins.items() if v.route_id == R_FRANKSTON)
        match = next(m for m in matched[0].matches if m.pin_key == frankston_key)
        assert set(match.reasons) == {"route", "stop", "trip"}

    def test_aggregate_board_attaches_alerts_per_pin(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond"),
                _pin(R_WERRIBEE, RICHMOND, "Richmond")]
        feed = _decode_feed(load_bytes("alert_service"))
        alerts = parse_service_alerts(feed)
        result = aggregate_board(
            metro_db, pins, BASE,
            realtime_enabled=False,
            alerts=alerts,
            now_utc=NOW_UTC,
        )
        # alert_service scopes route R_METRO_FRANKSTON + stop RICHMOND + trip.
        # The Frankston pin matches via route/stop/trip; the Werribee pin only
        # via the shared stop.
        assert result.alerts
        frankston_key = pins[0].pin_key
        werribee_key = pins[1].pin_key
        assert frankston_key in result.alerts_by_pin
        assert werribee_key in result.alerts_by_pin


# ---------------------------------------------------------------------------
# Sync index through the full aggregation
# ---------------------------------------------------------------------------


class TestAggregatedSyncIndex:
    def test_identical_aggregations_share_index(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond"),
                _pin(R_WERRIBEE, RICHMOND, "Richmond")]
        r1 = aggregate_board(metro_db, pins, BASE, realtime_enabled=False)
        r2 = aggregate_board(metro_db, pins, BASE, realtime_enabled=False)
        assert r1.sync_index == r2.sync_index

    def test_index_ignores_pin_order(self, metro_db: Path):
        a = [_pin(R_FRANKSTON, RICHMOND, "Richmond"),
             _pin(R_WERRIBEE, RICHMOND, "Richmond")]
        b = [_pin(R_WERRIBEE, RICHMOND, "Richmond"),
             _pin(R_FRANKSTON, RICHMOND, "Richmond")]
        r_a = aggregate_board(metro_db, a, BASE, realtime_enabled=False)
        r_b = aggregate_board(metro_db, b, BASE, realtime_enabled=False)
        assert r_a.sync_index == r_b.sync_index

    def test_index_changes_when_rows_change(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        r1 = aggregate_board(metro_db, pins, BASE, realtime_enabled=False)
        r2 = aggregate_board(
            metro_db, pins, BASE, trip_updates=_realtime_index(),
        )
        # Realtime changes T_FRK_OUT_2's effective time → index differs.
        assert r1.sync_index != r2.sync_index


# ---------------------------------------------------------------------------
# Purity (no event loop / hass)
# ---------------------------------------------------------------------------


class TestPurity:
    def test_aggregate_is_sync_and_returns_pure_data(self, metro_db: Path):
        # Runs synchronously with injected ports; no hass/event loop involved.
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = aggregate_board(metro_db, pins, BASE, realtime_enabled=False)
        assert isinstance(result.rows, list)
        assert isinstance(result.rows[0], BoardDeparture)
        assert result.generated_at == BASE

    def test_result_model_has_no_ha_objects(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = aggregate_board(metro_db, pins, BASE, realtime_enabled=False)
        for row in result.rows:
            assert not hasattr(row, "hass")
            assert not hasattr(row, "coordinator")
        assert isinstance(result.feed_health, dict)
