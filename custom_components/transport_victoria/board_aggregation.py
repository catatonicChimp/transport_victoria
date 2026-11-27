"""Trip-group board aggregation (T9).

Pure, testable merge of several departure *pins* into one TripView-style
departure board, plus a thin async realtime-fetch helper.  Nothing here imports
``homeassistant`` and nothing here creates entities (that is T10).

The core :func:`aggregate_board` is a plain synchronous function whose ports are
all injected — the schedule DB path, the parsed realtime index/alerts, the feed
health map, and the clock — so the light-env unit tests exercise every merge,
join, sort, dedupe, alert and sync-index rule against the T6 fixtures with no
event loop.

Realtime is joined by *trip id only* (see :ref:`join rule <join-rule>`); a row
without a match falls back to its scheduled time.  A failed feed never sinks the
board: the row stays scheduled, the failure is recorded in ``feed_health``.

.. _join-rule:

**Join rule.**  A GTFS-RT ``TripUpdate`` is matched to a scheduled departure by
the GTFS ``trip_id`` string only (keyed ``"<trip_id>:<stop_id>"`` against the
row's ``actual_stop_id``).  The trip descriptor's ``start_date`` / ``start_time``
are ignored: PTV trip ids are unique per run in the static schedule, so no date
disambiguation is required, and the existing coordinator parser already ignores
them (this package preserves that behaviour).  Failure mode: a scheduled trip
with no matching ``TripUpdate`` (or with realtime disabled / the feed failed) is
emitted with ``realtime_state="scheduled"`` and ``delay_seconds=-1``.

**Dedupe.**  Identical departures surfaced by more than one pin (the
via-hint-duplicate case: two pins on the same route/direction/terminal) collapse
to one row keyed by ``(trip_id, stop_sequence)`` — not trip id alone, so a loop
trip that boards the same stop at two different sequences keeps both
occurrences.  The first pin in board order owns the surviving row's identity.

**Sort.**  Ascending by effective time (realtime expected else scheduled),
tie-broken by boarding stop id → route number → headsign → pin order index →
trip id.  The pin order index participates near the end, so TripView's
"order by time across all pins" holds and pin order only decides exact ties.

**Sync index.**  :func:`compute_sync_index` fingerprints the rows' identity +
effective time + cancel/skip status (but not pin order or ``realtime_state``),
so dashboard scroll stays stable across polls and pin reordering.

Result model (all pure data):

* :class:`BoardDeparture` — one departure row (per pin, per trip occurrence).
* :class:`BoardAlert` / :class:`AlertMatch` — matched service alerts.
* :class:`FeedHealth` — per-``(mode, feed_type)`` health.
* :class:`BoardResult` — rows (time-sorted), alerts, feed health, sync index.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .board import PinData
from .const import (
    FEED_SERVICE_ALERTS,
    FEED_TRIP_UPDATES,
    MODE_BUS,
    MODE_METRO,
    MODE_REGIONAL_BUS,
    MODE_TRAM,
    MODE_VLINE,
    REALTIME_CACHE_TTL_SECONDS,
)
from .gtfs.repository import DepartureRow, next_departures
from .realtime.client import feed_fetched_at
from .realtime.service_alerts import ServiceAlert, is_alert_active, parse_service_alerts
from .realtime.trip_updates import TripUpdateIndex, parse_trip_updates_index

_LOGGER = logging.getLogger(__name__)

# Realtime states for a departure row (``BoardDeparture.realtime_state``).
REALTIME_SCHEDULED = "scheduled"   # no realtime data (disabled / no match / failed)
REALTIME_LIVE = "live"             # a fresh TripUpdate was applied
REALTIME_STALE = "stale"           # a TripUpdate matched but the feed is old
# Reserved (not emitted by T9): a trip is known to realtime but no stop-level
# prediction is published yet.  T10/T13 finalise its exact trigger once the
# trip-update parser distinguishes "delay 0" from "no delay field present".
REALTIME_PENDING = "pending"

# Feed health states (``FeedHealth.state``).
FEED_OK = "ok"
FEED_STALE = "stale"
FEED_FAILED = "failed"

# ``delay_seconds`` sentinel for a row with no realtime data at all.
DELAY_UNKNOWN = -1

# How many scheduled departures to fetch per pin.  Over-fetched on purpose so
# cross-pin dedupe (and a board-wide ``limit``) never under-fill the merged
# result when several pins share trips.
_PER_PIN_LIMIT = 20


# ---------------------------------------------------------------------------
# route_type → realtime mode
# ---------------------------------------------------------------------------

# GTFS route_type → the PTV realtime mode whose feeds cover it.  Standard GTFS
# values (used by the T6 fixtures) and PTV's extended HVT values are both mapped.
# 204 (V/Line coach) and 701 (rural bus) are each *listed* by two modes in
# ``ROUTE_TYPE_MAP``; the primary here matches PTV's actual bundle layout
# (coaches under V/Line, rural buses under regional-bus).
_ROUTE_TYPE_TO_MODE: dict[int, str] = {
    0: MODE_TRAM,           # tram
    1: MODE_METRO,          # standard GTFS train → metro (fixtures)
    2: MODE_VLINE,          # V/Line rail
    3: MODE_BUS,            # metro bus
    204: MODE_VLINE,         # V/Line coach (HVT)
    400: MODE_METRO,         # PTV HVT metro train
    701: MODE_REGIONAL_BUS,   # rural bus (HVT)
}


def route_type_to_mode(route_type: int) -> str | None:
    """Return the realtime mode for a GTFS route_type, or None if unmapped."""
    return _ROUTE_TYPE_TO_MODE.get(route_type)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class FeedHealth:
    """Health of one realtime feed ``(mode, feed_type)``."""

    state: str                       # "ok" | "stale" | "failed"
    age: timedelta | None = None     # for failed: time since last success


@dataclass(slots=True)
class BoardDeparture:
    """One departure row in the merged board (pure data, no HA objects)."""

    # Provenance: which pin produced this row, and that pin's 0-based position
    # in the board's canonical order (participates LAST in the time sort).
    pin_key: str
    pin_order_index: int

    route_id: str
    route_short_name: str
    route_long_name: str
    headsign: str
    destination_label: str           # pin label_to, else trip headsign

    boarding_stop_id: str
    boarding_stop_name: str
    platform: str | None

    scheduled_departure: datetime    # absolute local time
    scheduled_seconds: int           # departure_seconds (service-day offset)
    expected_departure: datetime | None   # scheduled + delay (None when no RT)
    delay_seconds: int               # realtime delay, or DELAY_UNKNOWN (-1)
    is_cancelled: bool
    is_skipped: bool                 # this stop is SKIPPED for the trip
    skipped_stops_count: int         # trip-level SKIPPED stop count
    realtime_state: str              # scheduled | live | stale (pending reserved)

    trip_id: str
    stop_sequence: int               # preserves loop-trip occurrences (dedupe)
    route_color: str | None
    direction_id: int | None

    @property
    def effective_departure(self) -> datetime:
        """The time the board orders by: realtime expected else scheduled."""
        return self.expected_departure or self.scheduled_departure


@dataclass(slots=True, frozen=True)
class AlertMatch:
    """Why one alert matches one pin (identity + selector reason)."""

    pin_key: str
    reasons: tuple[str, ...]   # e.g. ("route", "stop"), ("trip",), ("agency",)


@dataclass(slots=True, frozen=True)
class BoardAlert:
    """A service alert matched to one or more board pins."""

    alert_id: str
    header: str
    description: str
    cause: str                     # informational; never used for matching
    effect: str
    active_from: datetime | None   # first active period (display)
    active_until: datetime | None
    matches: tuple[AlertMatch, ...]


@dataclass(slots=True)
class BoardResult:
    """The merged board: time-sorted rows, alerts, feed health, sync index."""

    rows: list[BoardDeparture]
    alerts: list[BoardAlert]                        # deduped, board-wide
    alerts_by_pin: dict[str, list[BoardAlert]]      # pin_key → matched alerts
    feed_health: dict[tuple[str, str], FeedHealth]
    sync_index: str
    generated_at: datetime


@dataclass(slots=True)
class RealtimeBoardData:
    """Parsed realtime inputs for one board (returned by the fetch helper)."""

    trip_updates: TripUpdateIndex | None
    alerts: list[ServiceAlert]
    feed_health: dict[tuple[str, str], FeedHealth]
    trip_updates_stale: bool = False


# ---------------------------------------------------------------------------
# Realtime fetch (async, thin; T10 wires this into the coordinator)
# ---------------------------------------------------------------------------


async def fetch_board_realtime(
    client,
    pins: list[PinData],
    realtime_enabled: bool,
    *,
    now: datetime | None = None,
) -> RealtimeBoardData:
    """Fetch + parse realtime feeds for a board, one request per (mode, feed).

    ``client`` is any object exposing ``async fetch_feed(mode, feed_type)``
    (the production ``PTVRealtimeClient``, or a stub in tests).  Pins sharing a
    mode are deduplicated, so N pins on one mode make exactly one request per
    feed type — no request multiplication.  A per-feed failure is recorded in
    ``feed_health`` and never raised; the board stays healthy and falls back
    to scheduled data.

    Args:
        client: Realtime client (``fetch_feed`` port).
        pins: The board's pins (order-independent here).
        realtime_enabled: False short-circuits all fetches (empty result).
        now: Clock for staleness classification (defaults to UTC now).

    Returns:
        :class:`RealtimeBoardData` (all-empty when realtime is disabled).
    """
    if not realtime_enabled:
        return RealtimeBoardData(trip_updates=None, alerts=[], feed_health={})

    now = now or datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        # Normalise naive (UTC) clocks for age arithmetic against cache times.
        now = now.replace(tzinfo=timezone.utc)

    modes = _distinct_modes(pins)

    feed_health: dict[tuple[str, str], FeedHealth] = {}

    stop_delays: dict = {}
    trip_cancelled: dict[str, bool] = {}
    trip_skipped_count: dict[str, int] = {}
    trip_updates_stale = False
    any_trip_feed = False

    for mode in modes:
        key = (mode, FEED_TRIP_UPDATES)
        try:
            feed = await client.fetch_feed(mode, FEED_TRIP_UPDATES)
            index = parse_trip_updates_index(feed)
            stop_delays.update(index.stop_delays)
            trip_cancelled.update(index.trip_cancelled)
            trip_skipped_count.update(index.trip_skipped_count)
            any_trip_feed = True
            fetched_at = feed_fetched_at(mode, FEED_TRIP_UPDATES)
            if _is_stale(fetched_at, now):
                feed_health[key] = FeedHealth(FEED_STALE)
                trip_updates_stale = True
            else:
                feed_health[key] = FeedHealth(FEED_OK)
        except Exception as exc:  # noqa: BLE001 - per-feed isolation
            _LOGGER.warning("trip-updates fetch failed for %s: %s", mode, exc)
            feed_health[key] = FeedHealth(FEED_FAILED, age=_feed_age(mode, FEED_TRIP_UPDATES, now))

    trip_updates: TripUpdateIndex | None = None
    if any_trip_feed:
        trip_updates = TripUpdateIndex(
            stop_delays=stop_delays,
            trip_cancelled=trip_cancelled,
            trip_skipped_count=trip_skipped_count,
        )

    alerts: list[ServiceAlert] = []
    for mode in modes:
        key = (mode, FEED_SERVICE_ALERTS)
        try:
            feed = await client.fetch_feed(mode, FEED_SERVICE_ALERTS)
            alerts.extend(parse_service_alerts(feed))
            feed_health[key] = FeedHealth(FEED_OK)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("service-alerts fetch failed for %s: %s", mode, exc)
            feed_health[key] = FeedHealth(FEED_FAILED, age=_feed_age(mode, FEED_SERVICE_ALERTS, now))

    return RealtimeBoardData(
        trip_updates=trip_updates,
        alerts=alerts,
        feed_health=feed_health,
        trip_updates_stale=trip_updates_stale,
    )


def _distinct_modes(pins: list[PinData]) -> list[str]:
    """Return the deduplicated, order-stable list of realtime modes for pins."""
    modes: list[str] = []
    seen: set[str] = set()
    for pin in pins:
        if not pin.include:
            continue
        mode = route_type_to_mode(pin.route_type)
        if mode and mode not in seen:
            seen.add(mode)
            modes.append(mode)
    return modes


def _is_stale(fetched_at: datetime | None, now: datetime) -> bool:
    """True if a cached feed is older than the realtime TTL."""
    if fetched_at is None:
        return False
    return (now - fetched_at) > timedelta(seconds=REALTIME_CACHE_TTL_SECONDS)


def _feed_age(mode: str, feed_type: str, now: datetime) -> timedelta | None:
    fetched_at = feed_fetched_at(mode, feed_type)
    if fetched_at is None:
        return None
    return now - fetched_at


# ---------------------------------------------------------------------------
# Core aggregation (pure, synchronous)
# ---------------------------------------------------------------------------


def aggregate_board(
    db_path: Path,
    pins: list[PinData],
    now: datetime,
    *,
    realtime_enabled: bool = True,
    trip_updates: TripUpdateIndex | None = None,
    trip_updates_stale: bool = False,
    alerts: list[ServiceAlert] | None = None,
    feed_health: dict[tuple[str, str], FeedHealth] | None = None,
    now_utc: datetime | None = None,
    invalid_pins: Iterable[str] = (),
    limit: int | None = None,
) -> BoardResult:
    """Merge ``pins`` into one time-sorted departure board.

    Steps (all pure):

    1. Skip disabled pins (``include`` False) and pins in ``invalid_pins``.
    2. Query each remaining pin's scheduled departures via
       :func:`~.gtfs.repository.next_departures` (``direction_stop_id`` is the
       pin's destination unless ``all_destinations``; route/platform filters
       honoured).
    3. Join realtime (delay/cancel/skip) onto each scheduled departure by trip
       id (see :ref:`join rule <join-rule>`); rows without a match (or with
       realtime disabled / failed) stay ``realtime_state="scheduled"``.
    4. Dedupe identical departures across pins — keyed by
       ``(trip_id, stop_sequence)`` so a loop trip's distinct occurrences are
       kept while via-hint-duplicate pins collapse to one row (first pin in
       order wins).
    5. Sort ascending by effective time, tie-broken by
       boarding stop id → route number → headsign → pin order index → trip id.
    6. Match active service alerts to pins by route/stop/trip selector.
    7. Compute the :func:`sync_index` fingerprint (pin-order independent).

    Args:
        db_path: GTFS generation anchor (resolved via ``store.resolve``).
        pins: Pins in canonical display order (order index = list position).
        now: Local naive datetime (drives the schedule query + sort).
        realtime_enabled: False → all rows scheduled, no realtime join.
        trip_updates: Parsed realtime index (from ``fetch_board_realtime``).
        trip_updates_stale: True marks matched rows ``realtime_state="stale"``.
        alerts: Parsed service alerts (unfiltered; active-window checked here).
        feed_health: Per-feed health (passed through to the result).
        now_utc: Aware UTC clock for alert active-window checks.
        invalid_pins: pin_keys considered unresolvable (skipped).
        limit: Cap the returned rows (None → uncapped).

    Returns:
        :class:`BoardResult`.
    """
    invalid = set(invalid_pins)
    now_utc = now_utc or _to_utc(now)
    feed_health = dict(feed_health) if feed_health else {}

    # Kept pins in canonical order (order index = position in this list).
    kept: list[tuple[int, PinData]] = []
    for idx, pin in enumerate(pins):
        if not pin.include or pin.pin_key in invalid:
            continue
        kept.append((idx, pin))

    # --- Build raw rows (pin order), then dedupe + sort --------------------
    trips_by_pin: dict[str, set[str]] = {}
    raw_rows: list[BoardDeparture] = []

    for pin_index, pin in kept:
        destination = None if pin.all_destinations else pin.destination_stop_id
        departure_rows = next_departures(
            db_path,
            pin.boarding_stop_id,
            now,
            limit=_PER_PIN_LIMIT,
            direction_stop_id=destination,
            route_id_filter=pin.route_id,
            platform_filter=pin.platform_filter,
        )

        pin_trips: set[str] = set()
        for row in departure_rows:
            pin_trips.add(row.trip_id)
            raw_rows.append(
                _build_departure(pin, pin_index, row, trip_updates, realtime_enabled,
                                 trip_updates_stale)
            )
        trips_by_pin[pin.pin_key] = pin_trips

    rows = _dedupe(raw_rows)
    rows.sort(key=_departure_sort_key)
    if limit is not None:
        rows = rows[:limit]

    # --- Alerts ------------------------------------------------------------
    pins_by_key = {pin.pin_key: pin for _idx, pin in kept}
    board_alerts = match_board_alerts(alerts or [], pins_by_key, trips_by_pin, now_utc)

    alerts_by_pin: dict[str, list[BoardAlert]] = {}
    for alert in board_alerts:
        for match in alert.matches:
            alerts_by_pin.setdefault(match.pin_key, []).append(alert)

    return BoardResult(
        rows=rows,
        alerts=board_alerts,
        alerts_by_pin=alerts_by_pin,
        feed_health=feed_health,
        sync_index=compute_sync_index(rows),
        generated_at=now,
    )


def _build_departure(
    pin: PinData,
    pin_index: int,
    row: DepartureRow,
    trip_updates: TripUpdateIndex | None,
    realtime_enabled: bool,
    stale_flag: bool,
) -> BoardDeparture:
    """Join realtime onto one scheduled departure row."""
    key = f"{row.trip_id}:{row.actual_stop_id}"
    stop_delay = trip_updates.stop_delays.get(key) if trip_updates else None

    is_cancelled = False
    skipped_count = 0
    is_skipped = False
    if trip_updates is not None:
        is_cancelled = trip_updates.trip_cancelled.get(row.trip_id, False)
        skipped_count = trip_updates.trip_skipped_count.get(row.trip_id, 0)
        if stop_delay is not None:
            is_cancelled = is_cancelled or stop_delay.is_cancelled
            is_skipped = stop_delay.is_skipped

    if realtime_enabled and stop_delay is not None:
        delay = stop_delay.delay_seconds
        expected = row.scheduled_departure + timedelta(seconds=delay)
        state = REALTIME_STALE if stale_flag else REALTIME_LIVE
    else:
        delay = DELAY_UNKNOWN
        expected = None
        state = REALTIME_SCHEDULED

    if pin.all_destinations:
        destination_label = row.trip_headsign
    else:
        destination_label = pin.label_to or row.trip_headsign

    return BoardDeparture(
        pin_key=pin.pin_key,
        pin_order_index=pin_index,
        route_id=row.route_id,
        route_short_name=row.route_short_name,
        route_long_name=row.route_long_name,
        headsign=row.trip_headsign,
        destination_label=destination_label,
        boarding_stop_id=pin.boarding_stop_id,
        boarding_stop_name=pin.boarding_stop_name,
        platform=row.platform_code,
        scheduled_departure=row.scheduled_departure,
        scheduled_seconds=row.departure_seconds,
        expected_departure=expected,
        delay_seconds=delay,
        is_cancelled=is_cancelled,
        is_skipped=is_skipped,
        skipped_stops_count=skipped_count,
        realtime_state=state,
        trip_id=row.trip_id,
        stop_sequence=row.stop_sequence,
        route_color=row.route_color,
        direction_id=row.direction_id,
    )


def _dedupe(rows: list[BoardDeparture]) -> list[BoardDeparture]:
    """Collapse identical departures across pins (first-in-order pin wins).

    Keyed by ``(trip_id, stop_sequence)`` — not trip id alone — so a loop trip
    that boards the same stop at two different sequences keeps BOTH occurrences
    while two via-hint pins surfacing the same trip occurrence collapse to one.
    """
    seen: set[tuple[str, int]] = set()
    out: list[BoardDeparture] = []
    for row in rows:
        key = (row.trip_id, row.stop_sequence)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _route_sort_value(route_short_name: str) -> tuple:
    """Numeric routes first (ordered numerically), then non-numeric lexically."""
    s = (route_short_name or "").strip()
    try:
        return (0, int(s), s)
    except ValueError:
        return (1, 0, s.lower())


def _departure_sort_key(d: BoardDeparture) -> tuple:
    """Time-ascending sort with deterministic tie-breaks (pin order near last)."""
    return (
        d.effective_departure,
        d.boarding_stop_id,
        _route_sort_value(d.route_short_name),
        d.headsign,
        d.pin_order_index,
        d.trip_id,
    )


def _to_utc(now: datetime) -> datetime:
    """Normalise a naive datetime to aware UTC (assumes it is already UTC)."""
    if now.tzinfo is not None:
        return now
    return now.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Alert matching
# ---------------------------------------------------------------------------


def match_board_alerts(
    alerts: list[ServiceAlert],
    pins_by_key: dict[str, PinData],
    trips_by_pin: dict[str, set[str]],
    now_utc: datetime,
) -> list[BoardAlert]:
    """Match active alerts to pins by informed_entity selectors.

    An alert matches a pin when it is active at ``now_utc`` AND at least one of
    its informed_entity selectors references the pin:

    * ``route_id`` == the pin's route;
    * ``stop_id`` == the pin's boarding (or destination) stop;
    * ``trip.trip_id`` == one of the pin's departure trips.

    An alert with *no* route/stop/trip selector (agency-wide) matches every pin.
    Matching is by selector identity only — ``cause`` is never consulted.
    """
    board_alerts: list[BoardAlert] = []
    for alert in alerts:
        if not is_alert_active(alert, now_utc):
            continue

        matches: list[AlertMatch] = []
        has_scope = bool(alert.route_ids or alert.stop_ids or alert.trip_ids)

        for pin_key, pin in pins_by_key.items():
            reasons: list[str] = []
            if pin.route_id in alert.route_ids:
                reasons.append("route")
            if pin.boarding_stop_id in alert.stop_ids or (
                pin.destination_stop_id is not None
                and pin.destination_stop_id in alert.stop_ids
            ):
                reasons.append("stop")
            if trips_by_pin.get(pin_key, set()) & set(alert.trip_ids):
                reasons.append("trip")
            if not has_scope:
                reasons.append("agency")

            if reasons:
                matches.append(AlertMatch(pin_key=pin_key, reasons=tuple(reasons)))

        if matches:
            board_alerts.append(
                BoardAlert(
                    alert_id=alert.alert_id,
                    header=alert.header,
                    description=alert.description,
                    cause=alert.cause,
                    effect=alert.effect,
                    active_from=alert.active_periods[0].start if alert.active_periods else None,
                    active_until=alert.active_periods[0].end if alert.active_periods else None,
                    matches=tuple(matches),
                )
            )

    return board_alerts


# ---------------------------------------------------------------------------
# Sync index
# ---------------------------------------------------------------------------


def compute_sync_index(rows: list[BoardDeparture]) -> str:
    """Return a stable fingerprint over the board's rows (pin-order independent).

    TripView ``Trip_GetSyncIndex`` semantics: identical when the board's rows
    are unchanged, different only when actual rows change.  The fingerprint
    covers each row's identity (``trip_id`` + ``stop_sequence``), its effective
    departure time, and its cancel/skip status — folded in so a status-only
    change still bumps the index — but NOT the pin order or ``realtime_state``,
    so reordering pins (or a staleness transition) keeps the dashboard's scroll
    position stable.
    """
    canonical = sorted(
        (
            r.trip_id,
            r.stop_sequence,
            int(r.effective_departure.timestamp()),
            int(r.is_cancelled),
            int(r.is_skipped),
            r.skipped_stops_count,
        )
        for r in rows
    )
    raw = "|".join(
        f"{tid}:{seq}:{ts}:{cancelled}:{skipped}:{skipped_count}"
        for tid, seq, ts, cancelled, skipped, skipped_count in canonical
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
