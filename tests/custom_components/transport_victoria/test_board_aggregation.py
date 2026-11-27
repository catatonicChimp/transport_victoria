"""T9 board aggregation unit tests (light env, stub style).

Covers ``custom_components.transport_victoria.board_aggregation``: cross-pin
merge ordering, deterministic tie-breaks, via-hint dedupe, pin filtering,
sequence-aware realtime join (delay/cancel/skip/absolute), static fallback
(disabled/failed/stale), lookback+horizon windowing, per-mode freshness, shared
feed caching (fetch/parse split), selector-based alert matching, clock
discipline, and the pin-order-independent sync index.

Pure functions are exercised directly against the T6 schedule + realtime
fixtures with injected ports — no event loop, no homeassistant objects (except
the async fetch helper, run under pytest-asyncio).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from google.transit import gtfs_realtime_pb2 as rt

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
    _classify_freshness,
    _departure_sort_key,
    aggregate_board,
    compute_sync_index,
    fetch_board_realtime,
    match_board_alerts,
    route_type_to_mode,
)
from custom_components.transport_victoria.realtime.client import FeedMeta, _decode_feed
from custom_components.transport_victoria.gtfs.repository import next_departures
from custom_components.transport_victoria.realtime.service_alerts import (
    ActivePeriod,
    SelectorRecord,
    ServiceAlert,
    is_alert_active,
    parse_service_alerts,
)
from custom_components.transport_victoria.realtime.trip_updates import (
    TripStopUpdate,
    TripUpdateIndex,
    parse_trip_updates,
    parse_trip_updates_index,
)

from tests.gtfs_builder import REFERENCE_NOW, import_bundle
from tests.realtime_fixtures import load_bytes

MEL = ZoneInfo("Australia/Melbourne")
BASE = REFERENCE_NOW  # 2026-06-01 08:00 local naive
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


def _agg(db: Path, pins: list[PinData], **kw):
    defaults = dict(now=BASE, local_tz=MEL, now_utc=NOW_UTC, realtime_enabled=False)
    defaults.update(kw)
    return aggregate_board(db, pins, **defaults)


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


def _sel(**kw) -> SelectorRecord:
    return SelectorRecord(**kw)


def _alert(
    alert_id: str = "a",
    header: str = "h",
    *,
    selectors=(),
    periods=None,
    feed_mode: str | None = None,
) -> ServiceAlert:
    return ServiceAlert(
        alert_id=alert_id,
        header=header,
        description="",
        cause="UNKNOWN_CAUSE",
        effect="SIGNIFICANT_DELAYS",
        active_periods=tuple(periods or ()),
        selectors=tuple(selectors),
        feed_mode=feed_mode,
    )


def _merge_indexes(*indexes: TripUpdateIndex) -> TripUpdateIndex:
    by_sequence: dict = {}
    by_stop_id: dict = {}
    trip_cancelled: dict[str, bool] = {}
    trip_skipped_count: dict[str, int] = {}
    for idx in indexes:
        by_sequence.update(idx.by_sequence)
        by_stop_id.update(idx.by_stop_id)
        trip_cancelled.update(idx.trip_cancelled)
        trip_skipped_count.update(idx.trip_skipped_count)
    return TripUpdateIndex(by_sequence, by_stop_id, trip_cancelled, trip_skipped_count)


def _feed(trip_id, stops):
    """Build a trip-update FeedMessage from ``stops`` specs.

    Each stop spec: dict with optional ``stop_id``, ``seq``, ``delay``,
    ``abs_time`` (epoch secs), ``skipped``.
    """
    feed = rt.FeedMessage()
    feed.header.timestamp = 1_750_000_000
    entity = feed.entity.add()
    entity.id = f"tu-{trip_id}"
    tu = entity.trip_update
    tu.trip.trip_id = trip_id
    for spec in stops:
        s = tu.stop_time_update.add()
        if spec.get("stop_id"):
            s.stop_id = spec["stop_id"]
        if spec.get("seq") is not None:
            s.stop_sequence = spec["seq"]
        if spec.get("delay") is not None:
            s.departure.delay = spec["delay"]
        if spec.get("abs_time") is not None:
            s.departure.time = spec["abs_time"]
        if spec.get("skipped"):
            s.schedule_relationship = rt.TripUpdate.StopTimeUpdate.SKIPPED
    return feed


class CountingClient:
    """Async realtime client stub returning ``(feed, FeedMeta)`` per call."""

    def __init__(self, feeds: dict[tuple[str, str], object]) -> None:
        self.feeds = feeds
        self.calls: list[tuple[str, str]] = []

    async def fetch_feed(self, mode: str, feed_type: str):
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

    def test_tie_breaks_are_deterministic(self):
        a = _dep(trip_id="A", boarding="S1", pin_index=1)
        b = _dep(trip_id="B", boarding="S2", pin_index=0)
        assert _departure_sort_key(a) < _departure_sort_key(b)

        c = _dep(trip_id="C", boarding="S1", route="2")
        d = _dep(trip_id="D", boarding="S1", route="10")
        assert _departure_sort_key(c) < _departure_sort_key(d)

        e = _dep(trip_id="E", boarding="S1", route="2", headsign="City", pin_index=0)
        f = _dep(trip_id="F", boarding="S1", route="2", headsign="City", pin_index=1)
        assert _departure_sort_key(e) < _departure_sort_key(f)

        g = _dep(trip_id="G", boarding="S1", route="2", headsign="City", pin_index=0)
        h = _dep(trip_id="H", boarding="S1", route="2", headsign="City", pin_index=0)
        assert _departure_sort_key(g) < _departure_sort_key(h)

    def test_dedupe_keeps_first_pin_and_preserves_loop_occurrences(self):
        loop_a = _dep(trip_id="LOOP", seq=1, pin_key_="p1")
        loop_b = _dep(trip_id="LOOP", seq=4, pin_key_="p1")
        dup = _dep(trip_id="LOOP", seq=1, pin_key_="p2")
        result = ba._dedupe([loop_a, dup, loop_b])
        assert [r.stop_sequence for r in result] == [1, 4]
        assert all(r.pin_key == "p1" for r in result)


# ---------------------------------------------------------------------------
# Sync index (pure)
# ---------------------------------------------------------------------------


class TestSyncIndex:
    def test_stable_for_identical_rows(self):
        rows = [_dep(trip_id="A", eff=BASE), _dep(trip_id="B", eff=BASE)]
        assert compute_sync_index(rows) == compute_sync_index(rows)

    def test_changes_when_row_time_changes(self):
        before = [_dep(trip_id="A", eff=BASE)]
        after = [_dep(trip_id="A", eff=BASE + timedelta(minutes=5))]
        assert compute_sync_index(before) != compute_sync_index(after)

    def test_changes_on_cancel_or_skip_status(self):
        base = _dep(trip_id="A", eff=BASE)
        cancelled = _dep(trip_id="A", eff=BASE, is_cancelled=True)
        skipped = _dep(trip_id="A", eff=BASE, is_skipped=True, skipped_stops_count=1)
        assert compute_sync_index([base]) != compute_sync_index([cancelled])
        assert compute_sync_index([base]) != compute_sync_index([skipped])

    def test_independent_of_pin_order(self):
        a = _dep(trip_id="A", eff=BASE, pin_index=0, pin_key_="p1")
        b = _dep(trip_id="B", eff=BASE, pin_index=1, pin_key_="p2")
        a2 = _dep(trip_id="A", eff=BASE, pin_index=1, pin_key_="p1")
        b2 = _dep(trip_id="B", eff=BASE, pin_index=0, pin_key_="p2")
        assert compute_sync_index([a, b]) == compute_sync_index([a2, b2])

    def test_ignores_host_timezone(self):
        # The index hashes the ISO wall-clock string, never an epoch, so the
        # same naive local times hash identically regardless of host TZ.
        wall = datetime(2026, 6, 1, 8, 7, 0)
        idx1 = compute_sync_index([_dep(trip_id="A", eff=wall)])
        idx2 = compute_sync_index([_dep(trip_id="A", eff=datetime(2026, 6, 1, 8, 7, 0))])
        assert idx1 == idx2
        assert idx1 != compute_sync_index(
            [_dep(trip_id="A", eff=datetime(2026, 6, 1, 8, 8, 0))]
        )


# ---------------------------------------------------------------------------
# Merge ordering across pins (metro_db)
# ---------------------------------------------------------------------------


class TestMergeOrdering:
    def test_interleaves_two_routes_at_a_shared_stop(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond"),
                _pin(R_WERRIBEE, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins)

        trip_ids = [r.trip_id for r in result.rows]
        # T_FRK_LATE (01:10 next day) is beyond the 3h horizon → dropped.
        assert trip_ids == [
            "T_FRK_OUT_2",  # 08:07
            "T_WRB_OUT_2",  # 08:22
            "T_FRK_IN_1",   # 08:28
            "T_FRK_OUT_3",  # 09:07
            "T_WRB_OUT_3",  # 09:22
        ]
        times = [r.effective_departure for r in result.rows]
        assert times == sorted(times)

    def test_horizon_drops_far_future(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        # With a short horizon the post-midnight T_FRK_LATE is excluded.
        result = _agg(metro_db, pins, horizon=timedelta(hours=1))
        assert "T_FRK_LATE" not in {r.trip_id for r in result.rows}


# ---------------------------------------------------------------------------
# via-hint dedupe (bus_db)
# ---------------------------------------------------------------------------


class TestViaHintDedupe:
    def test_two_via_pins_collapse_to_one_row_set(self, bus_db: Path):
        pins = line_pins(bus_db, "B_SUNSHINE", "Sunshine", "B_CITY")
        assert len(pins) == 2
        result = _agg(bus_db, pins)
        trip_ids = [r.trip_id for r in result.rows]
        assert len(trip_ids) == len(set(trip_ids))
        assert all(r.pin_key == pins[0].pin_key for r in result.rows)

    def test_via_dedupe_surface_count(self, bus_db: Path):
        pins = line_pins(bus_db, "B_SUNSHINE", "Sunshine", "B_CITY")
        result = _agg(bus_db, pins)
        assert sorted(r.trip_id for r in result.rows) == [
            "T_BUS_LONG", "T_BUS_VIA_A", "T_BUS_VIA_B",
        ]


# ---------------------------------------------------------------------------
# Pin filtering (include + invalid)
# ---------------------------------------------------------------------------


class TestPinFiltering:
    def test_include_false_pin_is_skipped(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond"),
                _pin(R_WERRIBEE, RICHMOND, "Richmond", include=False)]
        result = _agg(metro_db, pins)
        assert all(r.route_id == R_FRANKSTON for r in result.rows)
        assert result.rows

    def test_invalid_pin_is_skipped(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond"),
                _pin(R_WERRIBEE, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, invalid_pins={pins[1].pin_key})
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
        result = _agg(metro_db, pins, realtime_enabled=True,
                      trip_updates=_realtime_index())
        row = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_2")
        assert row.delay_seconds == 90
        assert row.expected_departure == row.scheduled_departure + timedelta(seconds=90)
        assert row.realtime_state == REALTIME_LIVE

    def test_cancel_join(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, realtime_enabled=True,
                      trip_updates=_realtime_index())
        row = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_3")
        assert row.is_cancelled is True
        # A cancelled trip keeps its scheduled time (no delay applied).
        assert row.effective_departure == row.scheduled_departure
        assert row.delay_seconds == DELAY_UNKNOWN

    def test_skip_join(self, metro_db: Path):
        pins = [_pin(R_WERRIBEE, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, realtime_enabled=True,
                      trip_updates=_realtime_index())
        row = next(r for r in result.rows if r.trip_id == "T_WRB_OUT_2")
        assert row.is_skipped is True
        assert row.skipped_stops_count == 1

    def test_unmatched_trip_stays_scheduled(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, realtime_enabled=True,
                      trip_updates=_realtime_index())
        row = next(r for r in result.rows if r.trip_id == "T_FRK_IN_1")
        assert row.realtime_state == REALTIME_SCHEDULED
        assert row.delay_seconds == DELAY_UNKNOWN
        assert row.expected_departure is None


# ---------------------------------------------------------------------------
# Sequence-aware join (F1): loop trips with two occurrences
# ---------------------------------------------------------------------------


class TestSequenceAwareJoin:
    def test_loop_two_occurrences_get_own_delay_and_skip(self, tram_db: Path):
        # T_TRAM_LOOP visits T96_TERM at seq 1 (10:00) and seq 4 (10:40).
        feed = _feed("T_TRAM_LOOP", [
            {"stop_id": "T96_TERM", "seq": 1, "delay": 90},
            {"stop_id": "T96_TERM", "seq": 4, "delay": 240, "skipped": True},
        ])
        index = parse_trip_updates_index(feed)

        pins = [_pin("R_TRAM_96", "T96_TERM", "St Kilda", route_type=0)]
        result = _agg(tram_db, pins, realtime_enabled=True, trip_updates=index)

        rows = [r for r in result.rows if r.trip_id == "T_TRAM_LOOP"]
        assert len(rows) == 2
        by_seq = {r.stop_sequence: r for r in rows}
        assert by_seq[1].delay_seconds == 90
        assert by_seq[1].is_skipped is False
        assert by_seq[4].delay_seconds == 240
        assert by_seq[4].is_skipped is True

    def test_sequence_only_update_is_not_discarded(self, tram_db: Path):
        # A stop update carrying only stop_sequence (no stop_id) still joins.
        feed = _feed("T_TRAM_LOOP", [{"seq": 1, "delay": 60}])
        index = parse_trip_updates_index(feed)
        assert ("T_TRAM_LOOP", 1) in index.by_sequence
        pins = [_pin("R_TRAM_96", "T96_TERM", "St Kilda", route_type=0)]
        result = _agg(tram_db, pins, realtime_enabled=True, trip_updates=index)
        row = next(r for r in result.rows
                   if r.trip_id == "T_TRAM_LOOP" and r.stop_sequence == 1)
        assert row.delay_seconds == 60


# ---------------------------------------------------------------------------
# Absolute-time prediction (additional)
# ---------------------------------------------------------------------------


class TestAbsoluteTimePrediction:
    def test_absolute_time_becomes_expected(self, metro_db: Path):
        # T_FRK_OUT_2 departs Richmond 08:07 local = 22:07 UTC (2026-06-01).
        epoch = int(datetime(2026, 5, 31, 22, 7, 0, tzinfo=timezone.utc).timestamp())
        feed = _feed("T_FRK_OUT_2", [
            {"stop_id": "RICHMOND", "seq": 2, "abs_time": epoch},
        ])
        index = parse_trip_updates_index(feed)
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, realtime_enabled=True, trip_updates=index)
        row = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_2")
        assert row.expected_departure == datetime(2026, 6, 1, 8, 7, 0)
        assert row.delay_seconds == 0
        assert row.realtime_state == REALTIME_LIVE

    def test_absolute_time_early_gives_negative_delay(self, metro_db: Path):
        # 08:07 scheduled, but predicted 08:05 → delay -120s.
        epoch = int(datetime(2026, 5, 31, 22, 5, 0, tzinfo=timezone.utc).timestamp())
        feed = _feed("T_FRK_OUT_2", [
            {"stop_id": "RICHMOND", "seq": 2, "abs_time": epoch},
        ])
        index = parse_trip_updates_index(feed)
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, realtime_enabled=True, trip_updates=index)
        row = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_2")
        assert row.delay_seconds == -120
        assert row.expected_departure == datetime(2026, 6, 1, 8, 5, 0)

    def test_no_prediction_stays_scheduled_not_live(self, metro_db: Path):
        # A stop update with no delay and no time → no prediction → scheduled.
        feed = _feed("T_FRK_OUT_2", [{"stop_id": "RICHMOND", "seq": 2}])
        index = parse_trip_updates_index(feed)
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, realtime_enabled=True, trip_updates=index)
        row = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_2")
        assert row.realtime_state == REALTIME_SCHEDULED
        assert row.delay_seconds == DELAY_UNKNOWN
        assert row.expected_departure is None


# ---------------------------------------------------------------------------
# Static fallback (disabled / failed / stale) + gating (F6)
# ---------------------------------------------------------------------------


class TestStaticFallback:
    def test_realtime_disabled_all_scheduled(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, realtime_enabled=False)
        assert result.rows
        assert all(r.realtime_state == REALTIME_SCHEDULED for r in result.rows)
        assert all(r.delay_seconds == DELAY_UNKNOWN for r in result.rows)

    def test_disabled_ignores_cancel_skip_index(self, metro_db: Path):
        # F6: with realtime disabled, the index is not consumed at all.
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, realtime_enabled=False,
                      trip_updates=_realtime_index())
        cancelled = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_3")
        assert cancelled.is_cancelled is False
        assert all(r.is_skipped is False for r in result.rows)
        assert all(r.skipped_stops_count == 0 for r in result.rows)

    def test_feed_failed_rows_scheduled_and_health_failed(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        health = {("metro", "trip-updates"): FeedHealth(FEED_FAILED)}
        result = _agg(metro_db, pins, trip_updates=None, feed_health=health)
        assert all(r.realtime_state == REALTIME_SCHEDULED for r in result.rows)
        assert result.feed_health[("metro", "trip-updates")].state == FEED_FAILED

    def test_stale_mode_marks_that_modes_rows_stale(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, realtime_enabled=True,
                      trip_updates=_realtime_index(), stale_modes={"metro"})
        matched = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_2")
        assert matched.realtime_state == REALTIME_STALE
        assert matched.delay_seconds == 90


# ---------------------------------------------------------------------------
# Lookback + horizon window (F5)
# ---------------------------------------------------------------------------


class TestLookbackHorizon:
    def test_delayed_past_service_appears(self, metro_db: Path):
        # now = 08:30: a 08:20 service delayed +15min → 08:35 (future) surfaces.
        now = datetime(2026, 6, 1, 8, 30, 0)
        feed = _feed("T_WRB_OUT_2", [{"stop_id": "RICHMOND", "seq": 2, "delay": 900}])
        index = parse_trip_updates_index(feed)
        pins = [_pin(R_WERRIBEE, RICHMOND, "Richmond")]
        result = aggregate_board(
            metro_db, pins, now, local_tz=MEL, now_utc=NOW_UTC,
            realtime_enabled=True, trip_updates=index,
        )
        row = next(r for r in result.rows if r.trip_id == "T_WRB_OUT_2")
        # 08:22 scheduled + 15min = 08:37 (ranks by delayed time).
        assert row.expected_departure == datetime(2026, 6, 1, 8, 37, 0)
        assert row in result.rows

    def test_delayed_row_ranks_by_delayed_time(self, metro_db: Path):
        # A delayed 08:22 service (→08:37) ranks BEFORE an on-time 09:07 service.
        now = datetime(2026, 6, 1, 8, 30, 0)
        feed = _feed("T_WRB_OUT_2", [{"stop_id": "RICHMOND", "seq": 2, "delay": 900}])
        index = parse_trip_updates_index(feed)
        pins = [_pin(R_WERRIBEE, RICHMOND, "Richmond"),
                _pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = aggregate_board(
            metro_db, pins, now, local_tz=MEL, now_utc=NOW_UTC,
            realtime_enabled=True, trip_updates=index,
        )
        order = [r.trip_id for r in result.rows]
        # T_WRB_OUT_2 (delayed 08:37) ranks before on-time T_FRK_OUT_3 (09:07).
        assert order.index("T_WRB_OUT_2") < order.index("T_FRK_OUT_3")

    def test_negative_delay_early(self, metro_db: Path):
        feed = _feed("T_FRK_OUT_2", [{"stop_id": "RICHMOND", "seq": 2, "delay": -120}])
        index = parse_trip_updates_index(feed)
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins, realtime_enabled=True, trip_updates=index)
        row = next(r for r in result.rows if r.trip_id == "T_FRK_OUT_2")
        assert row.delay_seconds == -120
        assert row.expected_departure == datetime(2026, 6, 1, 8, 5, 0)

    def test_cancelled_row_retained_in_window(self, metro_db: Path):
        # A cancelled 08:07 service (scheduled within lookback, effective=scheduled)
        # still surfaces and ranks at its scheduled time.
        now = datetime(2026, 6, 1, 8, 30, 0)
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = aggregate_board(
            metro_db, pins, now, local_tz=MEL, now_utc=NOW_UTC,
            realtime_enabled=True, trip_updates=_realtime_index(),
        )
        cancelled = [r for r in result.rows if r.trip_id == "T_FRK_OUT_3"]
        assert cancelled and cancelled[0].is_cancelled is True


class TestPerPinCap:
    def test_dense_pin_a_does_not_starve_future_rows(self, tmp_path):
        db = _build_dense_db(tmp_path)
        pins = [_pin("R_A", "S", "Shared", route_type=1),
                _pin("R_B", "S", "Shared", route_type=0)]
        now = datetime(2026, 6, 1, 8, 0, 0)
        result = aggregate_board(
            db, pins, now, local_tz=MEL, now_utc=NOW_UTC, realtime_enabled=False,
        )
        trip_ids = [r.trip_id for r in result.rows]
        # Route A has 45 departures; ~22 are in the past (pulled in by the
        # lookback) and would have consumed the old 20-row per-pin cap.  Its
        # future trips (08:00+) must still surface, and B's earliest too.
        assert "T_A_22" in trip_ids   # A's 08:00 departure
        assert "T_B_0" in trip_ids    # B's earliest (08:03)


def _build_dense_db(tmp_path: Path) -> Path:
    """Build a two-route generation: 45 A trips + 5 B trips at a shared stop."""
    stops = [{"stop_id": "S", "stop_name": "Shared", "stop_lat": -37.8,
              "stop_lon": 144.9, "stop_code": "S", "location_type": None,
              "parent_station": "", "platform_code": "", "wheelchair_boarding": 0}]
    routes = [
        {"route_id": "R_A", "route_short_name": "A", "route_long_name": "A Line",
         "route_type": 1, "route_color": "000000"},
        {"route_id": "R_B", "route_short_name": "B", "route_long_name": "B Line",
         "route_type": 0, "route_color": "111111"},
    ]
    trips = []
    stop_times = []
    # A: 45 trips every 2 min starting 07:16 (07:16..08:44).
    for i in range(45):
        tid = f"T_A_{i:02d}"
        trips.append({"trip_id": tid, "route_id": "R_A", "service_id": "SVC",
                      "shape_id": "", "trip_headsign": "A Dest", "direction_id": 0,
                      "block_id": ""})
        t = 7 * 3600 + 16 * 60 + i * 120
        stop_times.append({"trip_id": tid, "stop_id": "S", "stop_sequence": 1,
                           "arrival_time": _hms(t), "departure_time": _hms(t)})
    # B: 5 trips at 08:03..08:11.
    for i in range(5):
        tid = f"T_B_{i}"
        trips.append({"trip_id": tid, "route_id": "R_B", "service_id": "SVC",
                      "shape_id": "", "trip_headsign": "B Dest", "direction_id": 0,
                      "block_id": ""})
        t = 8 * 3600 + 3 * 60 + i * 120
        stop_times.append({"trip_id": tid, "stop_id": "S", "stop_sequence": 1,
                           "arrival_time": _hms(t), "departure_time": _hms(t)})
    calendar = [{"service_id": "SVC", "start_date": "20260520", "end_date": "20260701",
                 "monday": 1, "tuesday": 1, "wednesday": 1, "thursday": 1,
                 "friday": 1, "saturday": 1, "sunday": 1}]
    return import_bundle(tmp_path, routes=routes, trips=trips, stops=stops,
                         stop_times=stop_times, calendar=calendar)


def _hms(total_seconds: int) -> str:
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Fetch + parse split (F7) and shared cache + freshness (F4)
# ---------------------------------------------------------------------------


class TestSharedCache:
    async def test_two_pins_same_mode_one_fetch(self):
        empty = _decode_feed(load_bytes("header_tick_0"))
        meta = FeedMeta(header_timestamp=NOW_UTC, fetched_at=NOW_UTC)
        client = CountingClient({
            ("metro", "trip-updates"): (empty, meta),
            ("metro", "service-alerts"): (empty, meta),
        })
        pins = [_pin(R_FRANKSTON, RICHMOND, route_type=1),
                _pin(R_WERRIBEE, RICHMOND, route_type=1)]
        data = await fetch_board_realtime(client, pins, True, now=NOW_UTC)

        trip_calls = [c for c in client.calls if c == ("metro", "trip-updates")]
        alert_calls = [c for c in client.calls if c == ("metro", "service-alerts")]
        assert len(trip_calls) == 1
        assert len(alert_calls) == 1
        assert len(client.calls) == 2

    async def test_realtime_disabled_makes_no_requests(self):
        client = CountingClient({})
        data = await fetch_board_realtime(client, [_pin(R_FRANKSTON, RICHMOND, route_type=1)], False)
        assert client.calls == []
        assert data.trip_updates is None
        assert data.alerts == []

    async def test_feed_failure_records_health(self):
        client = CountingClient({
            ("metro", "trip-updates"): RuntimeError("boom"),
            ("metro", "service-alerts"): (
                _decode_feed(load_bytes("header_tick_0")),
                FeedMeta(header_timestamp=NOW_UTC, fetched_at=NOW_UTC),
            ),
        })
        data = await fetch_board_realtime(
            client, [_pin(R_FRANKSTON, RICHMOND, route_type=1)], True, now=NOW_UTC,
        )
        assert data.feed_health[("metro", "trip-updates")].state == FEED_FAILED
        assert data.trip_updates is None

    async def test_parse_once_per_board(self, monkeypatch):
        empty = _decode_feed(load_bytes("header_tick_0"))
        meta = FeedMeta(header_timestamp=NOW_UTC, fetched_at=NOW_UTC)
        client = CountingClient({
            ("metro", "trip-updates"): (empty, meta),
            ("metro", "service-alerts"): (empty, meta),
        })
        calls = {"trips": 0, "alerts": 0}
        real_trips = ba.parse_trip_updates_index
        real_alerts = ba.parse_service_alerts

        def fake_trips(feed, **kw):
            calls["trips"] += 1
            return real_trips(feed)
        def fake_alerts(feed, **kw):
            calls["alerts"] += 1
            return real_alerts(feed, **kw)

        monkeypatch.setattr(ba, "parse_trip_updates_index", fake_trips)
        monkeypatch.setattr(ba, "parse_service_alerts", fake_alerts)

        pins = [_pin(R_FRANKSTON, RICHMOND, route_type=1),
                _pin(R_WERRIBEE, RICHMOND, route_type=1)]
        await fetch_board_realtime(client, pins, True, now=NOW_UTC)
        assert calls["trips"] == 1
        assert calls["alerts"] == 1


class TestFreshness:
    def test_frozen_header_marks_stale(self):
        # Same header_timestamp twice; fetched_at fresh → header drives staleness.
        old = NOW_UTC - timedelta(minutes=10)
        meta = FeedMeta(header_timestamp=old, fetched_at=NOW_UTC)
        state, _ = _classify_freshness(meta, NOW_UTC)
        assert state == FEED_STALE

    def test_healthy_header_live(self):
        meta = FeedMeta(header_timestamp=NOW_UTC - timedelta(seconds=10),
                        fetched_at=NOW_UTC)
        state, _ = _classify_freshness(meta, NOW_UTC)
        assert state == FEED_OK

    def test_future_header_clamped_to_live(self):
        future = NOW_UTC + timedelta(minutes=30)
        meta = FeedMeta(header_timestamp=future, fetched_at=NOW_UTC)
        state, _ = _classify_freshness(meta, NOW_UTC)
        assert state == FEED_OK

    def test_missing_header_falls_back_to_fetched_at(self):
        meta = FeedMeta(header_timestamp=None,
                        fetched_at=NOW_UTC - timedelta(minutes=10))
        state, _ = _classify_freshness(meta, NOW_UTC)
        assert state == FEED_STALE

    async def test_per_mode_staleness_isolation(self):
        # Train feed stale, tram feed fresh → only train rows marked stale.
        stale_meta = FeedMeta(header_timestamp=NOW_UTC - timedelta(minutes=10),
                              fetched_at=NOW_UTC)
        fresh_meta = FeedMeta(header_timestamp=NOW_UTC, fetched_at=NOW_UTC)
        client = CountingClient({
            ("metro", "trip-updates"): (_decode_feed(load_bytes("trip_update_delay")), stale_meta),
            ("metro", "service-alerts"): (_decode_feed(load_bytes("header_tick_0")), fresh_meta),
            ("tram", "trip-updates"): (_decode_feed(load_bytes("header_tick_0")), fresh_meta),
            ("tram", "service-alerts"): (_decode_feed(load_bytes("header_tick_0")), fresh_meta),
        })
        pins = [_pin(R_FRANKSTON, RICHMOND, route_type=1),
                _pin("R_TRAM_96", "T96_TERM", route_type=0)]
        data = await fetch_board_realtime(client, pins, True, now=NOW_UTC)
        assert data.stale_modes == frozenset({"metro"})


# ---------------------------------------------------------------------------
# Alert matching (F2): selector AND/OR, direction 0, agency, dedupe
# ---------------------------------------------------------------------------


class TestAlertMatching:
    def _pins(self):
        return {
            _pin(R_FRANKSTON, RICHMOND, "Richmond").pin_key:
                _pin(R_FRANKSTON, RICHMOND, "Richmond"),
            _pin(R_WERRIBEE, RICHMOND, "Richmond").pin_key:
                _pin(R_WERRIBEE, RICHMOND, "Richmond"),
        }

    def test_route_plus_stop_requires_both(self):
        pins = self._pins()
        trips = {k: set() for k in pins}
        # route Frankston AND stop RICHMOND: only the Frankston pin (which also
        # boards RICHMOND) matches.
        alert = _alert(selectors=(_sel(route_id=R_FRANKSTON, stop_id=RICHMOND),))
        matched = match_board_alerts([alert], pins, trips, NOW_UTC)
        assert len(matched) == 1
        frankston = next(k for k, v in pins.items() if v.route_id == R_FRANKSTON)
        assert [m.pin_key for m in matched[0].matches] == [frankston]
        assert set(matched[0].matches[0].reasons) == {"route", "stop"}

    def test_route_plus_stop_wrong_stop_does_not_match(self):
        pins = self._pins()
        trips = {k: set() for k in pins}
        # route Frankston AND stop "OTHER" (not a pin stop) → matches nothing.
        alert = _alert(selectors=(_sel(route_id=R_FRANKSTON, stop_id="OTHER"),))
        assert match_board_alerts([alert], pins, trips, NOW_UTC) == []

    def test_direction_zero_selector_works(self):
        pins = {p.pin_key: p for p in
                [_pin(R_FRANKSTON, RICHMOND, "Richmond", all_dest=False,
                      dest="FRANKSTON", direction=0)]}
        trips = {k: set() for k in pins}
        alert = _alert(selectors=(_sel(direction_id=0),))
        matched = match_board_alerts([alert], pins, trips, NOW_UTC)
        assert len(matched) == 1
        assert matched[0].matches[0].reasons == ("direction",)

    def test_agency_scoped_multimode(self):
        # Agency-scoped alert from the metro feed matches only the metro pin.
        pins = {
            _pin(R_FRANKSTON, RICHMOND, route_type=1).pin_key:
                _pin(R_FRANKSTON, RICHMOND, route_type=1),
            _pin("R_TRAM_96", "T96_TERM", route_type=0).pin_key:
                _pin("R_TRAM_96", "T96_TERM", route_type=0),
        }
        trips = {k: set() for k in pins}
        alert = _alert(selectors=(_sel(agency_id="PTV"),), feed_mode="metro")
        matched = match_board_alerts([alert], pins, trips, NOW_UTC)
        metro_key = next(k for k, v in pins.items() if v.route_type == 1)
        assert [m.pin_key for m in matched[0].matches] == [metro_key]
        assert matched[0].matches[0].reasons == ("agency",)

    def test_truly_unscoped_matches_all(self):
        pins = self._pins()
        trips = {k: set() for k in pins}
        alert = _alert(selectors=(_sel(),))
        matched = match_board_alerts([alert], pins, trips, NOW_UTC)
        assert {m.pin_key for m in matched[0].matches} == set(pins)

    def test_dedupe_by_alert_id_merges_reasons(self):
        pins = self._pins()
        trips = {k: {"T_FRK_OUT_2"} for k in pins}
        # Two ServiceAlert objects sharing alert_id, each matching via a
        # different selector — deduped into one BoardAlert with merged matches.
        alerts = [
            _alert(alert_id="a1", selectors=(_sel(route_id=R_FRANKSTON),)),
            _alert(alert_id="a1", selectors=(_sel(stop_id=RICHMOND),)),
        ]
        matched = match_board_alerts(alerts, pins, trips, NOW_UTC)
        assert len(matched) == 1
        assert matched[0].alert_id == "a1"

    def test_inactive_alert_excluded(self):
        pins = self._pins()
        trips = {k: set() for k in pins}
        past = ActivePeriod(
            start=datetime(2020, 1, 1, tzinfo=timezone.utc),
            end=datetime(2021, 1, 1, tzinfo=timezone.utc),
        )
        alert = _alert(selectors=(_sel(route_id=R_FRANKSTON),), periods=(past,))
        assert match_board_alerts([alert], pins, trips, NOW_UTC) == []

    def test_fixture_alert_matches_route_stop_trip(self):
        pins = self._pins()
        frankston = next(k for k, v in pins.items() if v.route_id == R_FRANKSTON)
        werribee = next(k for k, v in pins.items() if v.route_id == R_WERRIBEE)
        trips = {frankston: {"T_FRK_OUT_2"}, werribee: {"T_WRB_OUT_2"}}
        feed = _decode_feed(load_bytes("alert_service"))
        alerts = parse_service_alerts(feed)
        matched = match_board_alerts(alerts, pins, trips, NOW_UTC)
        assert len(matched) == 1
        assert matched[0].alert_id == "alert-1"
        m = next(x for x in matched[0].matches if x.pin_key == frankston)
        assert set(m.reasons) == {"route", "stop", "trip"}

    def test_parser_preserves_direction_zero_and_agency(self):
        feed = rt.FeedMessage()
        entity = feed.entity.add()
        entity.id = "a"
        alert = entity.alert
        ie = alert.informed_entity.add()
        ie.agency_id = "PTV"
        ie.route_type = 3
        ie.trip.trip_id = "T1"
        ie.trip.direction_id = 0
        alert.header_text.translation.add().text = "H"

        alerts = parse_service_alerts(feed)
        assert len(alerts) == 1
        sel = alerts[0].selectors[0]
        assert sel.agency_id == "PTV"
        assert sel.route_type_id == 3
        assert sel.trip_id == "T1"
        assert sel.direction_id == 0  # key presence, not truthiness


# ---------------------------------------------------------------------------
# Clock discipline (F3)
# ---------------------------------------------------------------------------


class TestClockDiscipline:
    def test_naive_now_utc_raises_typeerror(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        with pytest.raises(TypeError):
            aggregate_board(metro_db, pins, BASE, local_tz=MEL,
                            now_utc=datetime(2026, 6, 1, 8, 0, 0))  # naive

    def test_aware_local_now_is_normalized(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        aware_now = BASE.replace(tzinfo=MEL)
        result = aggregate_board(
            metro_db, pins, aware_now, local_tz=MEL, now_utc=NOW_UTC,
            realtime_enabled=False,
        )
        assert result.rows  # no crash; aware local now accepted


# ---------------------------------------------------------------------------
# Sync index through the full aggregation
# ---------------------------------------------------------------------------


class TestAggregatedSyncIndex:
    def test_identical_aggregations_share_index(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond"),
                _pin(R_WERRIBEE, RICHMOND, "Richmond")]
        r1 = _agg(metro_db, pins)
        r2 = _agg(metro_db, pins)
        assert r1.sync_index == r2.sync_index

    def test_index_ignores_pin_order(self, metro_db: Path):
        a = [_pin(R_FRANKSTON, RICHMOND, "Richmond"),
             _pin(R_WERRIBEE, RICHMOND, "Richmond")]
        b = [_pin(R_WERRIBEE, RICHMOND, "Richmond"),
             _pin(R_FRANKSTON, RICHMOND, "Richmond")]
        assert _agg(metro_db, a).sync_index == _agg(metro_db, b).sync_index

    def test_index_changes_when_rows_change(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        r1 = _agg(metro_db, pins)
        r2 = _agg(metro_db, pins, realtime_enabled=True, trip_updates=_realtime_index())
        assert r1.sync_index != r2.sync_index


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


class TestPurity:
    def test_aggregate_is_sync_and_returns_pure_data(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins)
        assert isinstance(result.rows, list)
        assert isinstance(result.rows[0], BoardDeparture)
        assert result.generated_at == BASE

    def test_result_model_has_no_ha_objects(self, metro_db: Path):
        pins = [_pin(R_FRANKSTON, RICHMOND, "Richmond")]
        result = _agg(metro_db, pins)
        for row in result.rows:
            assert not hasattr(row, "hass")
            assert not hasattr(row, "coordinator")
        assert isinstance(result.feed_health, dict)


# ---------------------------------------------------------------------------
# F1a: pagination (many past candidates must not hide upcoming rows)
# ---------------------------------------------------------------------------


def _build_many_db(tmp_path: Path, n: int, prefix: str = "T_A_") -> Path:
    """Build a single-route generation with ``n`` departures 1 min apart from 07:00."""
    stops = [{"stop_id": "S", "stop_name": "Shared", "stop_lat": -37.8,
              "stop_lon": 144.9, "stop_code": "S", "location_type": None,
              "parent_station": "", "platform_code": "", "wheelchair_boarding": 0}]
    routes = [{"route_id": "R_A", "route_short_name": "A", "route_long_name": "A Line",
               "route_type": 1, "route_color": "000000"}]
    trips = []
    stop_times = []
    for i in range(n):
        tid = f"{prefix}{i:03d}"
        trips.append({"trip_id": tid, "route_id": "R_A", "service_id": "SVC",
                      "shape_id": "", "trip_headsign": "A", "direction_id": 0,
                      "block_id": ""})
        t = 7 * 3600 + i * 60
        stop_times.append({"trip_id": tid, "stop_id": "S", "stop_sequence": 1,
                           "arrival_time": _hms(t), "departure_time": _hms(t)})
    calendar = [{"service_id": "SVC", "start_date": "20260520", "end_date": "20260701",
                 "monday": 1, "tuesday": 1, "wednesday": 1, "thursday": 1,
                 "friday": 1, "saturday": 1, "sunday": 1}]
    return import_bundle(tmp_path, routes=routes, trips=trips, stops=stops,
                         stop_times=stop_times, calendar=calendar)


class TestPagination:
    def test_many_past_candidates_do_not_hide_upcoming(self, tmp_path):
        # 130 departures 07:00..09:09; now=09:00 with a 2h lookback pulls in all
        # 130, the first ~120 of which are past.  Pagination must still surface
        # the upcoming (09:00+) rows.
        db = _build_many_db(tmp_path, 130)
        pins = [_pin("R_A", "S", "Shared", route_type=1)]
        now = datetime(2026, 6, 1, 9, 0, 0)
        result = aggregate_board(
            db, pins, now, local_tz=MEL, now_utc=NOW_UTC, realtime_enabled=False,
            lookback=timedelta(hours=2),
        )
        trip_ids = [r.trip_id for r in result.rows]
        assert "T_A_120" in trip_ids  # 07:00 + 120min = 09:00 (upcoming)
        # All surfaced rows are at/after now.
        assert all(r.effective_departure >= now for r in result.rows)


# ---------------------------------------------------------------------------
# F1b: cross-service-date ordering (repository)
# ---------------------------------------------------------------------------


def _build_pm_db(tmp_path: Path) -> Path:
    """Daily service with a post-midnight (31:30) trip and a same-day (08:00) trip."""
    stops = [{"stop_id": "S", "stop_name": "S", "stop_lat": -37.8, "stop_lon": 144.9,
              "stop_code": "S", "location_type": None, "parent_station": "",
              "platform_code": "", "wheelchair_boarding": 0}]
    routes = [{"route_id": "R", "route_short_name": "1", "route_long_name": "Line",
               "route_type": 1, "route_color": "000000"}]
    trips = [
        {"trip_id": "T_PM", "route_id": "R", "service_id": "SVC", "shape_id": "",
         "trip_headsign": "D", "direction_id": 0, "block_id": ""},
        {"trip_id": "T_AM", "route_id": "R", "service_id": "SVC", "shape_id": "",
         "trip_headsign": "D", "direction_id": 0, "block_id": ""},
    ]
    stop_times = [
        {"trip_id": "T_PM", "stop_id": "S", "stop_sequence": 1,
         "arrival_time": "31:30:00", "departure_time": "31:30:00"},
        {"trip_id": "T_AM", "stop_id": "S", "stop_sequence": 1,
         "arrival_time": "08:00:00", "departure_time": "08:00:00"},
    ]
    calendar = [{"service_id": "SVC", "start_date": "20260520", "end_date": "20260701",
                 "monday": 1, "tuesday": 1, "wednesday": 1, "thursday": 1,
                 "friday": 1, "saturday": 1, "sunday": 1}]
    return import_bundle(tmp_path, routes=routes, trips=trips, stops=stops,
                         stop_times=stop_times, calendar=calendar)


class TestCrossDateOrdering:
    def test_post_midnight_orders_by_real_time(self, tmp_path):
        db = _build_pm_db(tmp_path)
        now = datetime(2026, 6, 1, 8, 0, 0)  # Monday
        rows = next_departures(db, "S", now, limit=10, lookback_seconds=2700)
        # The post-midnight T_PM (Monday 07:30, from Sunday's service date) must
        # sort BEFORE T_AM (Monday 08:00) by real wall-clock time.
        times = [r.scheduled_departure for r in rows]
        assert times == sorted(times)
        assert rows[0].trip_id == "T_PM"
        assert rows[0].scheduled_departure == datetime(2026, 6, 1, 7, 30, 0)


# ---------------------------------------------------------------------------
# F2: sequence-zero presence
# ---------------------------------------------------------------------------


class TestSequenceZero:
    def test_sequence_zero_lands_in_by_sequence(self):
        feed = _feed("T", [{"stop_id": "S", "seq": 0, "delay": 30}])
        index = parse_trip_updates_index(feed)
        assert ("T", 0) in index.by_sequence
        assert index.by_sequence[("T", 0)].delay_seconds == 30

    def test_sequence_zero_joins(self):
        update = TripStopUpdate(delay_seconds=30, absolute_time=None, is_skipped=False)
        index = TripUpdateIndex(
            by_sequence={("T", 0): update},
            by_stop_id={},
            trip_cancelled={},
            trip_skipped_count={},
        )
        assert ba._lookup_update(index, "T", 0, "S") is update


# ---------------------------------------------------------------------------
# F3: selector conjunction + zero-selector
# ---------------------------------------------------------------------------


class TestSelectorConjunction:
    def test_agency_conjunction_with_route_cross_mode(self):
        # Selector names route R_A AND agency on the metro feed; a bus-mode pin
        # on R_A must NOT match (agency is conjuncted, not bypassed).
        pins = {p.pin_key: p for p in [_pin("R_A", "S", "S", route_type=3)]}
        trips = {k: set() for k in pins}
        alert = _alert(selectors=(_sel(agency_id="X", route_id="R_A"),),
                       feed_mode="metro")
        assert match_board_alerts([alert], pins, trips, NOW_UTC) == []

    def test_agency_route_type_conjunction_matches_same_feed(self):
        pins = {p.pin_key: p for p in [_pin("R_A", "S", "S", route_type=1)]}
        trips = {k: set() for k in pins}
        alert = _alert(selectors=(_sel(agency_id="X", route_type_id=1),),
                       feed_mode="metro")
        matched = match_board_alerts([alert], pins, trips, NOW_UTC)
        assert len(matched) == 1
        assert set(matched[0].matches[0].reasons) == {"agency", "route_type"}

    def test_zero_selector_matches_all_pins(self):
        pins = {
            _pin(R_FRANKSTON, RICHMOND, "Richmond").pin_key:
                _pin(R_FRANKSTON, RICHMOND, "Richmond"),
            _pin(R_WERRIBEE, RICHMOND, "Richmond").pin_key:
                _pin(R_WERRIBEE, RICHMOND, "Richmond"),
        }
        trips = {k: set() for k in pins}
        alert = _alert(selectors=())  # no informed_entity selectors
        matched = match_board_alerts([alert], pins, trips, NOW_UTC)
        assert {m.pin_key for m in matched[0].matches} == set(pins)


# ---------------------------------------------------------------------------
# F4: legacy parser precedence
# ---------------------------------------------------------------------------


class TestLegacyPrecedence:
    def test_arrival_delay_when_departure_has_absolute_time(self):
        # departure carries only an absolute time; arrival carries a delay.  The
        # legacy flat map must report the arrival delay (historical precedence),
        # while the board index prefers the absolute time (deliberate divergence).
        feed = rt.FeedMessage()
        entity = feed.entity.add()
        entity.id = "t"
        tu = entity.trip_update
        tu.trip.trip_id = "T"
        stu = tu.stop_time_update.add()
        stu.stop_id = "S"
        stu.departure.time = 1_750_000_000
        stu.arrival.delay = 120

        legacy = parse_trip_updates(feed)
        assert legacy["T:S"].delay_seconds == 120  # arrival delay, not 0

        index = parse_trip_updates_index(feed)
        # No stop_sequence → lands in by_stop_id; the board index carries the
        # absolute time (delay None), deliberately divergent from the legacy map.
        updates = index.by_stop_id[("T", "S")]
        assert len(updates) == 1
        assert updates[0].delay_seconds is None
        assert updates[0].absolute_time is not None


# ---------------------------------------------------------------------------
# F5: fetch-port shape + per-feed isolation
# ---------------------------------------------------------------------------


class ProductionLikeClient:
    """Mimics PTVRealtimeClient: bare ``fetch_feed`` + ``fetch_feed_with_meta``."""

    def __init__(self, feeds):
        self.feeds = feeds
        self.meta_calls: list[tuple[str, str]] = []

    async def fetch_feed(self, mode, feed_type):
        r = self.feeds[(mode, feed_type)]
        if isinstance(r, Exception):
            raise r
        feed, _meta = r
        return feed  # bare feed (like the production plain fetch_feed)

    async def fetch_feed_with_meta(self, mode, feed_type):
        self.meta_calls.append((mode, feed_type))
        return self.feeds[(mode, feed_type)]


class TestFetchPort:
    async def test_production_client_meta_port_used(self):
        delay_feed = _decode_feed(load_bytes("trip_update_delay"))
        meta = FeedMeta(header_timestamp=NOW_UTC, fetched_at=NOW_UTC)
        client = ProductionLikeClient({
            ("metro", "trip-updates"): (delay_feed, meta),
            ("metro", "service-alerts"): (_decode_feed(load_bytes("header_tick_0")), meta),
        })
        data = await fetch_board_realtime(
            client, [_pin(R_FRANKSTON, RICHMOND, route_type=1)], True, now=NOW_UTC,
        )
        assert client.meta_calls  # fetch_feed_with_meta was preferred
        assert data.trip_updates is not None
        assert data.feed_health[("metro", "trip-updates")].state == FEED_OK

    async def test_bare_feed_client_is_wrapped(self):
        # A client exposing only bare fetch_feed (no meta) is wrapped with an
        # empty FeedMeta → classified fresh, still parsed.
        feed = _decode_feed(load_bytes("trip_update_delay"))

        class BareClient:
            async def fetch_feed(self, mode, feed_type):
                return feed

        data = await fetch_board_realtime(
            BareClient(), [_pin(R_FRANKSTON, RICHMOND, route_type=1)], True, now=NOW_UTC,
        )
        assert data.trip_updates is not None
        assert data.feed_health[("metro", "trip-updates")].state == FEED_OK

    async def test_parser_exception_isolated(self, monkeypatch):
        good = _decode_feed(load_bytes("trip_update_delay"))
        empty = _decode_feed(load_bytes("header_tick_0"))
        client = CountingClient({
            ("metro", "trip-updates"): (good, FeedMeta(NOW_UTC, NOW_UTC)),
            ("metro", "service-alerts"): (empty, FeedMeta(NOW_UTC, NOW_UTC)),
        })

        def boom(feed, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(ba, "parse_service_alerts", boom)
        data = await fetch_board_realtime(
            client, [_pin(R_FRANKSTON, RICHMOND, route_type=1)], True, now=NOW_UTC,
        )
        # Trip-updates feed healthy; the failing service-alert feed isolated.
        assert data.feed_health[("metro", "trip-updates")].state == FEED_OK
        assert data.feed_health[("metro", "service-alerts")].state == FEED_FAILED
        assert data.trip_updates is not None


# ---------------------------------------------------------------------------
# F1 (round 5): destination eligibility pushed into SQL → pagination correct
# ---------------------------------------------------------------------------


def _build_dest_db(tmp_path: Path, n_other: int, n_dest: int) -> Path:
    """A route where the first ``n_other`` trips go O→B (not the destination) and
    the last ``n_dest`` trips go O→D (the destination), all 1 min apart."""
    stops = [
        {"stop_id": "O", "stop_name": "Origin", "stop_lat": -37.8, "stop_lon": 144.9,
         "stop_code": "O", "location_type": None, "parent_station": "",
         "platform_code": "", "wheelchair_boarding": 0},
        {"stop_id": "B", "stop_name": "Other", "stop_lat": -37.9, "stop_lon": 144.9,
         "stop_code": "B", "location_type": None, "parent_station": "",
         "platform_code": "", "wheelchair_boarding": 0},
        {"stop_id": "D", "stop_name": "Dest", "stop_lat": -38.0, "stop_lon": 144.9,
         "stop_code": "D", "location_type": None, "parent_station": "",
         "platform_code": "", "wheelchair_boarding": 0},
    ]
    routes = [{"route_id": "R_D", "route_short_name": "1", "route_long_name": "Line",
               "route_type": 1, "route_color": "000000"}]
    trips = []
    stop_times = []
    for i in range(n_other):
        tid = f"T_B_{i:03d}"
        trips.append({"trip_id": tid, "route_id": "R_D", "service_id": "SVC",
                      "shape_id": "", "trip_headsign": "B", "direction_id": 0,
                      "block_id": ""})
        t = 7 * 3600 + i * 60
        stop_times.append({"trip_id": tid, "stop_id": "O", "stop_sequence": 1,
                           "arrival_time": _hms(t), "departure_time": _hms(t)})
        stop_times.append({"trip_id": tid, "stop_id": "B", "stop_sequence": 2,
                           "arrival_time": _hms(t + 600), "departure_time": _hms(t + 600)})
    for i in range(n_dest):
        tid = f"T_D_{i:03d}"
        trips.append({"trip_id": tid, "route_id": "R_D", "service_id": "SVC",
                      "shape_id": "", "trip_headsign": "D", "direction_id": 0,
                      "block_id": ""})
        t = 9 * 3600 + i * 60
        stop_times.append({"trip_id": tid, "stop_id": "O", "stop_sequence": 1,
                           "arrival_time": _hms(t), "departure_time": _hms(t)})
        stop_times.append({"trip_id": tid, "stop_id": "D", "stop_sequence": 2,
                           "arrival_time": _hms(t + 600), "departure_time": _hms(t + 600)})
    calendar = [{"service_id": "SVC", "start_date": "20260520", "end_date": "20260701",
                 "monday": 1, "tuesday": 1, "wednesday": 1, "thursday": 1,
                 "friday": 1, "saturday": 1, "sunday": 1}]
    return import_bundle(tmp_path, routes=routes, trips=trips, stops=stops,
                         stop_times=stop_times, calendar=calendar)


class TestDestinationFilterPagination:
    def test_zero_match_first_page_surfaces_destination_rows(self, tmp_path):
        # 120 O→B trips (07:00..08:59) precede 10 O→D trips (09:00..09:09).  A
        # destination=D pin must surface the D trips even though the first page
        # of raw route rows would be entirely ineligible.
        db = _build_dest_db(tmp_path, 120, 10)
        pins = [_pin("R_D", "O", "Origin", all_dest=False, dest="D", route_type=1)]
        result = aggregate_board(db, pins, BASE, local_tz=MEL, now_utc=NOW_UTC,
                                 realtime_enabled=False)
        trip_ids = [r.trip_id for r in result.rows]
        assert trip_ids and all(t.startswith("T_D_") for t in trip_ids)
        assert sorted(trip_ids) == [f"T_D_{i:03d}" for i in range(10)]

    def test_partial_first_page_surfaces_rows_beyond(self, tmp_path):
        # 60 O→B + 130 O→D: the D trips span two pages; all 130 must surface.
        db = _build_dest_db(tmp_path, 60, 130)
        pins = [_pin("R_D", "O", "Origin", all_dest=False, dest="D", route_type=1)]
        result = aggregate_board(
            db, pins, datetime(2026, 6, 1, 9, 0, 0), local_tz=MEL, now_utc=NOW_UTC,
            realtime_enabled=False, horizon=timedelta(hours=3),
        )
        trip_ids = sorted(r.trip_id for r in result.rows)
        assert all(t.startswith("T_D_") for t in trip_ids)
        assert len(trip_ids) > 100  # paginated past the first page


# ---------------------------------------------------------------------------
# F2 (round 5): generation pinning + restart-on-swap
# ---------------------------------------------------------------------------


class TestGenerationPinning:
    def test_pages_pin_one_resolved_snapshot(self, tmp_path, monkeypatch):
        db = _build_many_db(tmp_path, 130)  # 2 pages
        captured: list = []

        real_next = ba.next_departures

        def fake_next(db_path, *args, **kwargs):
            captured.append(kwargs.get("resolved_db"))
            return real_next(db_path, *args, **kwargs)

        monkeypatch.setattr(ba, "next_departures", fake_next)
        pins = [_pin("R_A", "S", "Shared", route_type=1)]
        aggregate_board(db, pins, datetime(2026, 6, 1, 9, 0, 0), local_tz=MEL,
                        now_utc=NOW_UTC, realtime_enabled=False,
                        lookback=timedelta(hours=2))
        # Every page used the SAME resolved generation path.
        assert len(captured) > 1
        assert len(set(captured)) == 1

    def test_generation_swap_restarts_once(self, tmp_path, monkeypatch):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        gen_a_anchor = _build_many_db(tmp_path / "a", 130, "A_")
        gen_b_anchor = _build_many_db(tmp_path / "b", 150, "B_")
        # Resolve the REAL generation files before monkeypatching.
        gen_a = ba.store.resolve_db(gen_a_anchor)
        gen_b = ba.store.resolve_db(gen_b_anchor)

        calls = {"n": 0}

        def fake_resolve(anchor):
            calls["n"] += 1
            return gen_b if calls["n"] >= 3 else gen_a

        monkeypatch.setattr(ba.store, "resolve_db", fake_resolve)

        pins = [_pin("R_A", "S", "Shared", route_type=1)]
        result = aggregate_board(
            tmp_path / "gtfs.db", pins, datetime(2026, 6, 1, 9, 0, 0),
            local_tz=MEL, now_utc=NOW_UTC, realtime_enabled=False,
            lookback=timedelta(hours=2),
        )
        trip_ids = [r.trip_id for r in result.rows]
        # Coherent: all rows from the fresh generation (B_), none from the old (A_).
        assert trip_ids and all(t.startswith("B_") for t in trip_ids)
        assert not any(t.startswith("A_") for t in trip_ids)
        assert len(trip_ids) == len(set(trip_ids))  # no duplicates
        # A restart was detected (more than a single pin+page resolution).
        assert calls["n"] >= 3
