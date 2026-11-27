"""Trip-group board aggregation (T9).

Pure, testable merge of several departure *pins* into one TripView-style
departure board, plus a thin async realtime-fetch helper.  Nothing here imports
``homeassistant`` and nothing here creates entities (that is T10).

The core :func:`aggregate_board` is a plain synchronous function whose ports are
all injected — the schedule DB path, the parsed realtime index/alerts, the feed
health map, and BOTH clocks (local ``now`` + aware UTC ``now_utc``) — so the
light-env unit tests exercise every merge, join, sort, dedupe, alert and
sync-index rule against the T6 fixtures with no event loop.

.. _join-rule:

**Join rule.**  A GTFS-RT ``TripUpdate`` is matched to a scheduled departure by
the GTFS ``trip_id`` string + ``stop_sequence`` (keyed ``(trip_id, stop_sequence)``
in the sequence-aware index).  When an update carries no ``stop_sequence`` the
join falls back to ``(trip_id, stop_id)`` — but only when exactly ONE such update
matches that stop (a loop trip visiting one stop twice without sequence numbers
is therefore treated as unmatched, never guessed).  The trip descriptor's
``start_date`` / ``start_time`` are captured on each update for provenance but are
**not** part of the join key: the scheduled rows carry no ``start_date``, and PTV
trip ids are unique per run within a feed's service window, so the trip-id +
sequence key is unambiguous in practice.  Failure mode: a scheduled trip with no
matching update (or realtime disabled / the feed failed) is emitted with
``realtime_state="scheduled"`` and ``delay_seconds=-1``.

**Prediction.**  A stop update with a ``delay`` yields ``expected = scheduled +
delay`` (delay may be negative).  One with an absolute ``time`` (and no delay)
yields ``expected = time`` converted from UTC to the board's ``local_tz`` and
``delay = expected - scheduled``.  One with neither carries **no** prediction:
the row stays ``realtime_state="scheduled"`` (never ``live``), even though its
cancellation/skip flags may still be set from trip/stop level.

**Dedupe.**  Identical departures surfaced by more than one pin (the
via-hint-duplicate case: two pins on the same route/direction/terminal) collapse
to one row keyed by ``(trip_id, stop_sequence)`` — not trip id alone, so a loop
trip that boards the same stop at two different sequences keeps both
occurrences.  The first pin in board order owns the surviving row's identity.

**Sort.**  Ascending by effective time (realtime expected else scheduled),
tie-broken by boarding stop id → route number → headsign → pin order index →
trip id.  The pin order index participates near the end, so TripView's
"order by time across all pins" holds and pin order only decides exact ties.

**Clock discipline.**  ``now`` is the local clock (naive; drives the schedule
query and the effective-time window).  ``now_utc`` is a REQUIRED aware-UTC
datetime (alerts + freshness); passing a naive one raises ``TypeError``.
``local_tz`` (a ``zoneinfo``) is REQUIRED and is how absolute RT times are
converted to local and how a naive ``now`` is interpreted.

**Freshness.**  A feed is classified fresh/stale from its GTFS-RT
``header.timestamp`` (primary) or, when that is absent, its ``fetched_at``
(fallback) — both carried on the ``FeedMeta`` returned by the fetch port, never
read from the production module cache.  Staleness is applied PER MODE: a stale
trip-updates feed marks only that mode's pins' rows stale, and service-alert
staleness never affects a departure's ``realtime_state``.

**Sync index.**  :func:`compute_sync_index` fingerprints the rows' identity +
effective wall-clock time (ISO, never an epoch) + cancel/skip status — but not
pin order or ``realtime_state`` — so dashboard scroll stays stable across polls
and pin reordering, and is independent of the host timezone.

Result model (all pure data):

* :class:`BoardDeparture` — one departure row (per pin, per trip occurrence).
* :class:`BoardAlert` / :class:`AlertMatch` — matched service alerts.
* :class:`FeedHealth` — per-``(mode, feed_type)`` health.
* :class:`BoardResult` — rows (time-sorted), alerts, feed health, sync index.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
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
)
from .gtfs import store
from .gtfs.repository import DepartureRow, next_departures
from .realtime.client import FeedMeta
from .realtime.service_alerts import (
    SelectorRecord,
    ServiceAlert,
    is_alert_active,
    parse_service_alerts,
)
from .realtime.trip_updates import TripStopUpdate, TripUpdateIndex, parse_trip_updates_index

_LOGGER = logging.getLogger(__name__)

# Realtime states for a departure row (``BoardDeparture.realtime_state``).
REALTIME_SCHEDULED = "scheduled"   # no realtime data (disabled / no match / failed)
REALTIME_LIVE = "live"             # a fresh TripUpdate was applied
REALTIME_STALE = "stale"           # a TripUpdate matched but the feed is old
# Reserved (not emitted by T9): a trip is known to realtime but no stop-level
# prediction is published yet.  T10/T13 finalise its exact trigger.
REALTIME_PENDING = "pending"

# Feed health states (``FeedHealth.state``).
FEED_OK = "ok"
FEED_STALE = "stale"
FEED_FAILED = "failed"

# ``delay_seconds`` sentinel for a row with no realtime prediction at all.
DELAY_UNKNOWN = -1

# --- Scheduling window ----------------------------------------------------
# A delayed-in-past departure (scheduled slightly before ``now`` but pushed into
# the future by realtime) must still surface, so the schedule query runs a
# bounded lookback behind ``now``.
SCHEDULE_LOOKBACK = timedelta(minutes=45)
# Upper bound on how far ahead the board looks (replaces an unbounded LIMIT-only
# query so a sparse stop cannot spill arbitrarily far into the future).
SCHEDULE_HORIZON = timedelta(hours=3)
# Page size for walking a pin's full bounded window (lookback..horizon).  The
# aggregation paginates rather than truncating, so a pin with many past
# departures in the lookback cannot starve its upcoming rows; the display limit
# is applied only after the effective-time filter.
_PER_PIN_PAGE = 100


class _GenerationSwapped(Exception):
    """Raised internally when the GTFS generation pointer changes mid-aggregation.

    Carries the freshly-resolved generation (or None if resolution itself
    failed) so the caller can restart once with a single coherent snapshot.
    """

    def __init__(self, new_generation) -> None:
        self.new_generation = new_generation
        super().__init__("GTFS generation swapped during board aggregation")

# --- Freshness thresholds -------------------------------------------------
# A feed whose header/fetch timestamp is older than this is "stale".
_FEED_STALE_AFTER = timedelta(seconds=180)
# A header timestamp more than this far in the FUTURE is clock skew → fresh.
_FEED_FUTURE_SKEW = timedelta(minutes=5)

# Cap + deterministic ordering for board alerts (drop order = alert_id).
MAX_BOARD_ALERTS = 50


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
    age: timedelta | None = None     # data age when classifiable; None for failed


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

    scheduled_departure: datetime    # absolute local time (naive)
    scheduled_seconds: int           # departure_seconds (service-day offset)
    expected_departure: datetime | None   # scheduled + delay (None when no RT)
    delay_seconds: int               # realtime delay (may be negative), or -1
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
    alerts: list[BoardAlert]                        # deduped, capped, board-wide
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
    stale_modes: frozenset[str] = frozenset()   # modes w/ stale trip-updates


# ---------------------------------------------------------------------------
# Realtime fetch (async I/O) + parse (sync, offloaded)
# ---------------------------------------------------------------------------


async def fetch_board_realtime(
    client,
    pins: list[PinData],
    realtime_enabled: bool,
    *,
    now: datetime | None = None,
    run_sync=None,
) -> RealtimeBoardData:
    """Fetch + parse realtime feeds for a board, one request per (mode, feed).

    Runs in two phases:

    * **I/O (on the event loop):** the client's feed port is awaited once per
      distinct ``(mode, feed_type)`` — N pins on one mode make exactly one
      request per feed type, no multiplication.  The port returns
      ``(feed, FeedMeta)`` so freshness is classified from the fetched data
      itself.  Two port shapes are accepted: a client exposing
      ``fetch_feed_with_meta`` (the production ``PTVRealtimeClient``) is used
      directly; otherwise ``fetch_feed`` is called and its return value detected
      (a 2-tuple ``(feed, FeedMeta)`` is used as-is, a bare feed is wrapped with
      ``FeedMeta(header_timestamp=None)``).
    * **Parse (offloaded):** all protobuf parsing runs inside
      :func:`_parse_fetched` via ``run_sync`` — by default ``asyncio.to_thread``
      so the blocking C-extension decode never touches the event loop (matching
      the realtime client's decode-in-executor pattern and T2's sqlite pattern).
      The T10 coordinator adapter must keep this offload in place.

    A per-feed failure — the fetch itself, a malformed return shape, freshness
    classification, or a parser exception — is isolated and recorded in
    ``feed_health`` and never raised; one bad feed leaves the others and the
    board healthy (falling back to scheduled data).

    Args:
        client: Realtime client.  Either exposes ``async fetch_feed_with_meta(
            mode, feed_type) -> (feed, FeedMeta)`` or ``async fetch_feed(mode,
            feed_type) -> (feed, FeedMeta) | feed``.
        pins: The board's pins (order-independent here).
        realtime_enabled: False short-circuits all fetches (empty result).
        now: Aware UTC clock for freshness classification (defaults to now).
        run_sync: Callable ``(fn, *args) -> awaitable`` that runs ``fn`` off the
            event loop (defaults to ``asyncio.to_thread``).

    Returns:
        :class:`RealtimeBoardData` (all-empty when realtime is disabled).
    """
    if not realtime_enabled:
        return RealtimeBoardData(
            trip_updates=None, alerts=[], feed_health={}, stale_modes=frozenset()
        )

    now = now or datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        raise TypeError("fetch_board_realtime requires an aware (UTC) `now`")
    now = now.astimezone(timezone.utc)

    modes = _distinct_modes(pins)

    # --- I/O phase ---------------------------------------------------------
    fetched: dict[tuple[str, str], object] = {}
    for mode in modes:
        for feed_type in (FEED_TRIP_UPDATES, FEED_SERVICE_ALERTS):
            try:
                fetched[(mode, feed_type)] = await _fetch_with_meta(
                    client, mode, feed_type
                )
            except Exception as exc:  # noqa: BLE001 - per-feed isolation
                fetched[(mode, feed_type)] = exc

    # --- Parse phase (offloaded) -------------------------------------------
    run_sync = run_sync or asyncio.to_thread
    return await run_sync(_parse_fetched, fetched, modes, now)


async def _fetch_with_meta(client, mode: str, feed_type: str):
    """Call the client's feed port and normalise to ``(feed, FeedMeta)``.

    Prefers ``fetch_feed_with_meta`` (the production port).  Otherwise calls
    ``fetch_feed`` and detects the return shape: a 2-tuple ``(feed, FeedMeta)``
    is used as-is (custom fakes), a bare feed is wrapped with an empty
    :class:`FeedMeta` (no header timestamp, no fetch time).
    """
    fetch_meta = getattr(client, "fetch_feed_with_meta", None)
    if fetch_meta is not None:
        return await fetch_meta(mode, feed_type)

    result = await client.fetch_feed(mode, feed_type)
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], FeedMeta):
        return result
    return result, FeedMeta(header_timestamp=None, fetched_at=None)


def _parse_fetched(
    fetched: dict[tuple[str, str], object],
    modes: list[str],
    now: datetime,
) -> RealtimeBoardData:
    """Pure sync: parse fetched feeds into a :class:`RealtimeBoardData`.

    Runs off the event loop (see :func:`fetch_board_realtime`).  Each
    ``(mode, feed_type)`` is parsed exactly once.
    """
    feed_health: dict[tuple[str, str], FeedHealth] = {}

    by_sequence: dict[tuple[str, int], TripStopUpdate] = {}
    by_stop_id: dict[tuple[str, str], tuple[TripStopUpdate, ...]] = {}
    trip_cancelled: dict[str, bool] = {}
    trip_skipped_count: dict[str, int] = {}
    any_trip_feed = False
    stale_modes: set[str] = set()
    alerts: list[ServiceAlert] = []

    for mode in modes:
        for feed_type in (FEED_TRIP_UPDATES, FEED_SERVICE_ALERTS):
            key = (mode, feed_type)
            result = fetched[key]
            if isinstance(result, BaseException):
                feed_health[key] = FeedHealth(FEED_FAILED)
                continue

            # One malformed feed (bad tuple shape, bad metadata, or a parser
            # exception) must not sink the others: isolate each entirely.
            try:
                feed, meta = result
                state, age = _classify_freshness(meta, now)
                feed_health[key] = FeedHealth(state, age)

                if feed_type == FEED_TRIP_UPDATES:
                    index = parse_trip_updates_index(feed)
                    by_sequence.update(index.by_sequence)
                    by_stop_id.update(index.by_stop_id)
                    trip_cancelled.update(index.trip_cancelled)
                    trip_skipped_count.update(index.trip_skipped_count)
                    any_trip_feed = True
                    if state == FEED_STALE:
                        stale_modes.add(mode)
                else:  # service alerts
                    # Alerts never affect a departure's realtime_state.
                    alerts.extend(parse_service_alerts(feed, feed_mode=mode))
            except Exception as exc:  # noqa: BLE001 - per-feed isolation
                _LOGGER.warning(
                    "feed parse failed for %s/%s: %s", mode, feed_type, exc
                )
                feed_health[key] = FeedHealth(FEED_FAILED)

    trip_updates: TripUpdateIndex | None = None
    if any_trip_feed:
        trip_updates = TripUpdateIndex(
            by_sequence=by_sequence,
            by_stop_id=by_stop_id,
            trip_cancelled=trip_cancelled,
            trip_skipped_count=trip_skipped_count,
        )

    return RealtimeBoardData(
        trip_updates=trip_updates,
        alerts=alerts,
        feed_health=feed_health,
        stale_modes=frozenset(stale_modes),
    )


def _classify_freshness(
    meta: FeedMeta, now: datetime
) -> tuple[str, timedelta | None]:
    """Classify a feed's freshness from its header (primary) or fetch time.

    Returns ``(state, age)`` where state is ``FEED_OK`` or ``FEED_STALE`` and
    age is the data age (None when unclassifiable).  A header timestamp more
    than ``_FEED_FUTURE_SKEW`` in the future is treated as clock skew → fresh.
    """
    ts = meta.header_timestamp
    if ts is not None:
        age = now - ts
        if age < -_FEED_FUTURE_SKEW:
            return FEED_OK, None  # future beyond skew → clock skew
        if age > _FEED_STALE_AFTER:
            return FEED_STALE, age
        return FEED_OK, age
    fetched_at = meta.fetched_at
    if fetched_at is not None:
        age = now - fetched_at
        if age > _FEED_STALE_AFTER:
            return FEED_STALE, age
        return FEED_OK, age
    return FEED_OK, None  # no signal → assume fresh (documented policy)


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


# ---------------------------------------------------------------------------
# Core aggregation (pure, synchronous)
# ---------------------------------------------------------------------------


def aggregate_board(
    db_path: Path,
    pins: list[PinData],
    now: datetime,
    *,
    local_tz: tzinfo,
    now_utc: datetime,
    realtime_enabled: bool = True,
    trip_updates: TripUpdateIndex | None = None,
    stale_modes: Iterable[str] = (),
    alerts: list[ServiceAlert] | None = None,
    feed_health: dict[tuple[str, str], FeedHealth] | None = None,
    invalid_pins: Iterable[str] = (),
    limit: int | None = None,
    horizon: timedelta | None = None,
    lookback: timedelta | None = None,
) -> BoardResult:
    """Merge ``pins`` into one time-sorted departure board.

    Steps (all pure):

    1. Skip disabled pins (``include`` False) and pins in ``invalid_pins``.
    2. Query each remaining pin's scheduled departures via
       :func:`~.gtfs.repository.next_departures` with a bounded lookback
       (``direction_stop_id`` is the pin's destination unless
       ``all_destinations``; route/platform filters honoured).
    3. Join realtime (delay / absolute-time / cancel / skip) onto each scheduled
       departure by trip id + stop sequence (see :ref:`join rule <join-rule>`);
       rows without a prediction stay ``realtime_state="scheduled"``.
    4. Apply the *effective-time* window: non-cancelled rows must land in
       ``[now, now + horizon]``; cancelled rows are retained across the lookback
       window (effective = scheduled) so a recent cancellation still shows.
    5. Dedupe by ``(trip_id, stop_sequence)``, then sort ascending by effective
       time with the deterministic tie-breaks, then cap at ``limit``.
    6. Match active service alerts to pins (selector AND/OR semantics, deduped by
       ``alert_id`` and capped).
    7. Compute the :func:`sync_index` fingerprint (pin-order + TZ independent).

    Generation snapshot: the schedule is resolved to ONE generation at the
    start, and every page in the pagination queries that pinned file (passed as
    ``resolved_db``).  If the pointer swaps mid-computation (or resolution
    fails), the aggregation restarts ONCE with the fresh snapshot, so a result
    never mixes rows from two generations.

    Args:
        db_path: GTFS generation anchor (resolved via ``store.resolve``); the
            schedule is pinned to ONE generation for the whole computation (see
            the generation-snapshot note above).
        pins: Pins in canonical display order (order index = list position).
        now: Local naive datetime (drives the schedule query + sort).
        local_tz: tzinfo for converting absolute RT times to local (and a naive
            ``now`` when one is passed aware).
        now_utc: REQUIRED aware-UTC datetime (alerts + freshness); a naive value
            raises ``TypeError``.
        realtime_enabled: False → all rows scheduled, realtime index ignored.
        trip_updates: Parsed realtime index (from ``fetch_board_realtime``).
        stale_modes: Modes whose trip-updates feed is stale → their rows stale.
        alerts: Parsed service alerts (unfiltered; active-window checked here).
        feed_health: Per-feed health (passed through to the result).
        invalid_pins: pin_keys considered unresolvable (skipped).
        limit: Cap the returned rows (None → uncapped).
        horizon: Override the default ``SCHEDULE_HORIZON``.
        lookback: Override the default ``SCHEDULE_LOOKBACK``.

    Returns:
        :class:`BoardResult`.
    """
    if now_utc.tzinfo is None:
        raise TypeError(
            "aggregate_board requires an aware (UTC) `now_utc`; got a naive "
            "datetime. Pass e.g. dt_util.utcnow() from the coordinator."
        )
    now_utc = now_utc.astimezone(timezone.utc)

    if now.tzinfo is not None:
        now = now.astimezone(local_tz).replace(tzinfo=None)

    horizon = horizon or SCHEDULE_HORIZON
    lookback = lookback or SCHEDULE_LOOKBACK
    stale_modes = frozenset(stale_modes)
    invalid = set(invalid_pins)
    feed_health = dict(feed_health) if feed_health else {}

    # Kept pins in canonical order (order index = position in this list).
    kept: list[tuple[int, PinData]] = []
    for idx, pin in enumerate(pins):
        if not pin.include or pin.pin_key in invalid:
            continue
        kept.append((idx, pin))

    # --- Build raw rows (pin order), then window + dedupe + sort ----------
    horizon_dt = now + horizon

    # Pin ONE generation snapshot for the whole pagination sequence.  If the
    # pointer swaps (or resolution fails) mid-computation, restart ONCE with the
    # fresh snapshot so the board never mixes rows from two generations.
    pinned = store.resolve_db(db_path)
    raw_rows: list[BoardDeparture] = []
    trips_by_pin: dict[str, set[str]] = {}
    for _attempt in range(2):
        try:
            raw_rows, trips_by_pin = _collect_departures(
                db_path, pinned, kept, now, horizon_dt, lookback,
                trip_updates, realtime_enabled, stale_modes, local_tz,
            )
            break  # success — one coherent snapshot
        except _GenerationSwapped as exc:
            pinned = exc.new_generation or store.resolve_db(db_path)
            raw_rows = []
            trips_by_pin = {}

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


def _collect_departures(
    db_path: Path,
    pinned: Path,
    kept: list[tuple[int, PinData]],
    now: datetime,
    horizon_dt: datetime,
    lookback: timedelta,
    trip_updates: TripUpdateIndex | None,
    realtime_enabled: bool,
    stale_modes: frozenset[str],
    local_tz: tzinfo,
) -> tuple[list[BoardDeparture], dict[str, set[str]]]:
    """Build all pins' in-window departure rows against ONE pinned generation.

    Raises :class:`_GenerationSwapped` when the generation pointer resolves to a
    different path mid-computation (checked before each page), so the caller can
    restart with a fresh, coherent snapshot instead of mixing two generations.
    """
    raw_rows: list[BoardDeparture] = []
    trips_by_pin: dict[str, set[str]] = {}

    for pin_index, pin in kept:
        destination = None if pin.all_destinations else pin.destination_stop_id
        pin_mode = route_type_to_mode(pin.route_type)
        pin_stale = pin_mode in stale_modes

        pin_trips: set[str] = set()
        # Paginate the pin's full bounded window (lookback..horizon) so a large
        # number of past departures can't hide upcoming rows; the display limit
        # is applied only after the effective-time filter (see aggregate_board).
        offset = 0
        while True:
            # Detect an activation swap: re-resolve and compare to the snapshot.
            try:
                current = store.resolve_db(db_path)
            except Exception:  # noqa: BLE001 - resolution error → restart
                current = None
            if current != pinned:
                raise _GenerationSwapped(current)

            page = next_departures(
                db_path,
                pin.boarding_stop_id,
                now,
                limit=_PER_PIN_PAGE,
                offset=offset,
                direction_stop_id=destination,
                route_id_filter=pin.route_id,
                platform_filter=pin.platform_filter,
                lookback_seconds=int(lookback.total_seconds()),
                resolved_db=pinned,
            )
            if not page:
                break

            past_horizon = False
            for row in page:
                # Rows are sorted by absolute scheduled time; stop once we pass
                # the horizon (nothing later can belong on the board).
                if row.scheduled_departure > horizon_dt:
                    past_horizon = True
                    break
                pin_trips.add(row.trip_id)
                dep = _build_departure(
                    pin, pin_index, row, trip_updates, realtime_enabled, pin_stale,
                    local_tz,
                )
                if _in_window(dep, now, horizon_dt, lookback):
                    raw_rows.append(dep)

            offset += len(page)
            if past_horizon or len(page) < _PER_PIN_PAGE:
                break
        trips_by_pin[pin.pin_key] = pin_trips

    return raw_rows, trips_by_pin


def _in_window(
    row: BoardDeparture, now: datetime, horizon_dt: datetime, lookback: timedelta
) -> bool:
    """Return True if a departure belongs on the board (effective-time window).

    Non-cancelled rows must be at or after ``now`` (already-departed services are
    dropped once their delay is applied) and before the horizon.  Cancelled rows
    are retained back through the lookback window (their effective time is their
    scheduled time), so a recently-cancelled service still surfaces.
    """
    eff = row.effective_departure
    if eff > horizon_dt:
        return False
    if row.is_cancelled:
        return eff >= now - lookback
    return eff >= now


def _lookup_update(
    trip_updates: TripUpdateIndex | None,
    trip_id: str,
    stop_sequence: int,
    actual_stop_id: str,
) -> TripStopUpdate | None:
    """Join a scheduled departure to its stop update (sequence-aware)."""
    if trip_updates is None:
        return None
    seq_key = (trip_id, stop_sequence)
    if seq_key in trip_updates.by_sequence:
        return trip_updates.by_sequence[seq_key]
    fallback = trip_updates.by_stop_id.get((trip_id, actual_stop_id))
    if fallback is not None and len(fallback) == 1:
        return fallback[0]
    return None


def _build_departure(
    pin: PinData,
    pin_index: int,
    row: DepartureRow,
    trip_updates: TripUpdateIndex | None,
    realtime_enabled: bool,
    pin_stale: bool,
    local_tz: tzinfo,
) -> BoardDeparture:
    """Join realtime onto one scheduled departure row (all RT gated on enabled)."""
    is_cancelled = False
    skipped_count = 0
    is_skipped = False
    delay = DELAY_UNKNOWN
    expected: datetime | None = None
    state = REALTIME_SCHEDULED

    if realtime_enabled and trip_updates is not None:
        is_cancelled = trip_updates.trip_cancelled.get(row.trip_id, False)
        skipped_count = trip_updates.trip_skipped_count.get(row.trip_id, 0)
        update = _lookup_update(
            trip_updates, row.trip_id, row.stop_sequence, row.actual_stop_id
        )
        if update is not None:
            is_skipped = update.is_skipped
            # A cancelled trip has no meaningful prediction: its effective time
            # is its scheduled time (see the windowing docstring).
            if not is_cancelled:
                delay, expected = _resolve_prediction(
                    update, row.scheduled_departure, local_tz
                )
                if expected is not None:
                    state = REALTIME_STALE if pin_stale else REALTIME_LIVE

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


def _resolve_prediction(
    update: TripStopUpdate, scheduled: datetime, local_tz: tzinfo
) -> tuple[int, datetime | None]:
    """Resolve a stop update to ``(delay_seconds, expected_departure)``.

    ``delay`` wins; otherwise an absolute ``time`` is converted from UTC to
    ``local_tz`` and the delay derived from it (may be negative).  ``(DELAY_UNKNOWN,
    None)`` when the update carries no prediction at all.
    """
    if update.delay_seconds is not None:
        delay = update.delay_seconds
        return delay, scheduled + timedelta(seconds=delay)
    if update.absolute_time is not None:
        local = update.absolute_time.astimezone(local_tz).replace(tzinfo=None)
        delay = int((local - scheduled).total_seconds())
        return delay, local
    return DELAY_UNKNOWN, None


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

    GTFS-RT semantics: populated fields WITHIN one selector are ANDed; separate
    selectors on an alert are ORed.  A selector with no route/stop/trip scope is
    agency-level (matches pins on its feed), and a truly empty selector matches
    everything.  Results are deduplicated by ``alert_id`` (match reasons merged
    across pins) and capped at :data:`MAX_BOARD_ALERTS` (deterministic drop order
    by ``alert_id``).  ``cause`` is never consulted.
    """
    merged: dict[str, dict[str, set[str]]] = {}
    meta: dict[str, tuple] = {}

    for alert in alerts:
        if not is_alert_active(alert, now_utc):
            continue
        pin_reasons: dict[str, set[str]] = {}
        for pin_key, pin in pins_by_key.items():
            kinds = _selector_kinds(
                alert, pin, trips_by_pin.get(pin_key, set())
            )
            if kinds:
                pin_reasons.setdefault(pin_key, set()).update(kinds)
        if pin_reasons:
            prev = merged.setdefault(alert.alert_id, {})
            for pin_key, kinds in pin_reasons.items():
                prev.setdefault(pin_key, set()).update(kinds)
            meta.setdefault(
                alert.alert_id,
                (
                    alert.header,
                    alert.description,
                    alert.cause,
                    alert.effect,
                    alert.active_periods[0].start if alert.active_periods else None,
                    alert.active_periods[0].end if alert.active_periods else None,
                ),
            )

    board_alerts: list[BoardAlert] = []
    for alert_id in sorted(merged):
        header, description, cause, effect, active_from, active_until = meta[alert_id]
        matches = tuple(
            AlertMatch(pin_key=pin_key, reasons=tuple(sorted(reasons)))
            for pin_key, reasons in sorted(merged[alert_id].items())
        )
        board_alerts.append(
            BoardAlert(
                alert_id=alert_id,
                header=header,
                description=description,
                cause=cause,
                effect=effect,
                active_from=active_from,
                active_until=active_until,
                matches=matches,
            )
        )

    return board_alerts[:MAX_BOARD_ALERTS]


def _selector_kinds(
    alert: ServiceAlert, pin: PinData, pin_trips: set[str]
) -> frozenset[str] | None:
    """Return the matching reason kinds across the alert's selectors (OR), or
    None when none match.  Within each selector the populated fields are ANDed."""
    pin_mode = route_type_to_mode(pin.route_type)
    if not alert.selectors:
        # An alert with no informed_entity selectors is unscoped → matches every
        # pin (agency-wide).
        return frozenset({"agency"})
    kinds: set[str] = set()
    for selector in alert.selectors:
        matched = _selector_match(selector, pin, pin_trips, pin_mode, alert.feed_mode)
        if matched:
            kinds.update(matched)
    return frozenset(kinds) if kinds else None


def _selector_match(
    selector: SelectorRecord,
    pin: PinData,
    pin_trips: set[str],
    pin_mode: str | None,
    feed_mode: str | None,
) -> tuple[str, ...] | None:
    """AND across a selector's populated fields; return the matching kinds.

    The agency/feed scope is part of the conjunction, never short-circuited: a
    selector naming an agency (or bound to a feed) additionally requires the pin
    to belong to that feed/mode, alongside every other populated field.
    """
    kinds: list[str] = []
    # Agency/feed scope — conjuncted with everything else.  In the
    # single-feed-per-mode model the alert's ``feed_mode`` IS the agency, so an
    # agency-scoped selector matches only pins on that feed.
    if selector.agency_id is not None:
        if feed_mode is not None and pin_mode != feed_mode:
            return None
        kinds.append("agency")
    if selector.route_type_id is not None:
        if pin.route_type != selector.route_type_id:
            return None
        kinds.append("route_type")
    if selector.route_id is not None:
        if pin.route_id != selector.route_id:
            return None
        kinds.append("route")
    if selector.direction_id is not None:
        if pin.direction_id != selector.direction_id:
            return None
        kinds.append("direction")
    if selector.stop_id is not None:
        if selector.stop_id not in (pin.boarding_stop_id, pin.destination_stop_id):
            return None
        kinds.append("stop")
    if selector.trip_id is not None:
        if selector.trip_id not in pin_trips:
            return None
        kinds.append("trip")

    if kinds:
        return tuple(kinds)

    # No populated fields at all → matches everything.
    return ("agency",)


# ---------------------------------------------------------------------------
# Sync index
# ---------------------------------------------------------------------------


def compute_sync_index(rows: list[BoardDeparture]) -> str:
    """Return a stable fingerprint over the board's rows (pin-order + TZ free).

    TripView ``Trip_GetSyncIndex`` semantics: identical when the board's rows
    are unchanged, different only when actual rows change.  The fingerprint
    covers each row's identity (``trip_id`` + ``stop_sequence``), its effective
    wall-clock time (ISO string — never ``.timestamp()``, so it is independent
    of the host timezone), and its cancel/skip status — but NOT the pin order or
    ``realtime_state``, so reordering pins (or a staleness transition) keeps the
    dashboard's scroll position stable.
    """
    canonical = sorted(
        (
            r.trip_id,
            r.stop_sequence,
            r.effective_departure.isoformat(),
            int(r.is_cancelled),
            int(r.is_skipped),
            r.skipped_stops_count,
        )
        for r in rows
    )
    raw = "|".join(
        f"{tid}:{seq}:{iso}:{cancelled}:{skipped}:{skipped_count}"
        for tid, seq, iso, cancelled, skipped, skipped_count in canonical
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
