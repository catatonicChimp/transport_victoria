"""Parse GTFS Realtime trip-update feeds into delay/skip indexes.

The trip-updates feed carries delay, cancellation, skip and (optionally)
absolute-time predictions keyed by trip descriptor + stop.  This module exposes
two surfaces:

* :func:`parse_trip_updates_index` — a **sequence-aware** index for board
  consumers (T9).  Loop trips that call at the same stop twice are disambiguated
  by ``stop_sequence``; updates carrying no sequence fall back to a
  ``(trip_id, stop_id)`` secondary index.
* :func:`parse_trip_updates` — the legacy flat ``"<trip_id>:<stop_id>"`` map the
  watch coordinator consumes.  Unchanged in shape; it is rebuilt from the same
  parsed data so the two surfaces never drift.

Prediction semantics (T9):

* ``delay`` (relative seconds) and ``time`` (absolute epoch seconds) are
  alternatives within an arrival/departure; ``delay`` is preferred, then
  ``time``.  Departure is preferred over arrival.
* A stop update with **neither** a delay nor an absolute time carries *no*
  prediction — the caller must NOT read it as "on time / live" (see the
  ``TripStopUpdate`` fields, all ``None`` in that case).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

# ScheduleRelationship enum values from the GTFS Realtime spec
_SR_CANCELED = 3
# StopTimeUpdate.ScheduleRelationship.SKIPPED
_STU_SKIPPED = 1


@dataclass(slots=True, frozen=True)
class TripStopUpdate:
    """One parsed ``stop_time_update`` (sequence-aware, richer than StopDelay).

    ``delay_seconds`` and ``absolute_time`` are both ``None`` when the update
    carries no prediction at all (a scheduled/cancelled trip with no published
    time) — distinct from a genuine ``delay_seconds == 0`` ("on time").
    ``absolute_time`` is an aware UTC datetime converted from the protobuf epoch
    ``departure.time``/``arrival.time`` field; the caller converts it to local.
    """

    delay_seconds: int | None
    absolute_time: datetime | None
    is_skipped: bool
    # Trip-descriptor provenance (NOT part of the join key — see module note).
    start_date: str | None = None
    start_time: str | None = None


@dataclass(slots=True, frozen=True)
class StopDelay:
    """Realtime delay information for one trip at one stop (legacy coordinator
    surface).  ``delay_seconds`` is 0 when no delay field was present (the
    coordinator cannot represent "no prediction")."""

    delay_seconds: int
    is_cancelled: bool
    # T9: this specific stop_time_update is SKIPPED (the trip will not serve
    # this stop).  Distinct from trip-level cancellation.
    is_skipped: bool = False


@dataclass(slots=True, frozen=True)
class TripUpdateIndex:
    """Parsed trip-updates feed, indexed for board aggregation (T9).

    * ``by_sequence`` maps ``(trip_id, stop_sequence)`` → update for updates
      that carry a ``stop_sequence`` (the primary, unambiguous join key).
    * ``by_stop_id`` maps ``(trip_id, stop_id)`` → a tuple of updates that
      carry a ``stop_id`` but NO ``stop_sequence`` (the fallback; a tuple so a
      multi-occurrence stop is recognisably ambiguous).
    * ``trip_cancelled`` / ``trip_skipped_count`` carry trip-level facts,
      including trips whose update has *no* stop_time_updates at all (which the
      per-stop maps cannot represent).
    """

    by_sequence: dict[tuple[str, int], TripStopUpdate]
    by_stop_id: dict[tuple[str, str], tuple[TripStopUpdate, ...]]
    trip_cancelled: dict[str, bool]
    trip_skipped_count: dict[str, int]


def parse_trip_updates_index(feed: object) -> TripUpdateIndex:
    """Parse a TripUpdates FeedMessage into a sequence-aware index.

    Args:
        feed: A decoded ``gtfs_realtime_pb2.FeedMessage`` instance.

    Returns:
        A :class:`TripUpdateIndex` (empty when the feed is malformed/empty).
    """
    by_sequence: dict[tuple[str, int], TripStopUpdate] = {}
    by_stop_id: dict[tuple[str, str], tuple[TripStopUpdate, ...]] = {}
    trip_cancelled: dict[str, bool] = {}
    trip_skipped_count: dict[str, int] = {}

    try:
        entities = feed.entity  # type: ignore[attr-defined]
    except AttributeError:
        _LOGGER.warning(
            "parse_trip_updates_index received unexpected feed type: %s",
            type(feed),
        )
        return TripUpdateIndex(by_sequence, by_stop_id, trip_cancelled, trip_skipped_count)

    for entity in entities:
        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update
        trip_id: str = trip_update.trip.trip_id

        if not trip_id:
            continue

        is_cancelled = trip_update.trip.schedule_relationship == _SR_CANCELED
        if is_cancelled:
            trip_cancelled[trip_id] = True

        start_date = _string_field(trip_update.trip, "start_date")
        start_time = _string_field(trip_update.trip, "start_time")

        for stu in trip_update.stop_time_update:
            stop_id: str = stu.stop_id
            stop_sequence: int = stu.stop_sequence
            has_seq = _has_int_field(stu, "stop_sequence")

            is_skipped = stu.schedule_relationship == _STU_SKIPPED
            if is_skipped:
                trip_skipped_count[trip_id] = trip_skipped_count.get(trip_id, 0) + 1

            delay, absolute = _prediction(stu)

            update = TripStopUpdate(
                delay_seconds=delay,
                absolute_time=absolute,
                is_skipped=is_skipped,
                start_date=start_date,
                start_time=start_time,
            )

            if has_seq:
                # Primary key: unambiguous even for a loop trip.  Presence-only,
                # so an explicit stop_sequence == 0 still keys here.
                by_sequence[(trip_id, stop_sequence)] = update
            elif stop_id and not has_seq:
                # Secondary fallback: keyed by stop_id, may be ambiguous.
                by_stop_id[(trip_id, stop_id)] = by_stop_id.get(
                    (trip_id, stop_id), ()
                ) + (update,)
            # Updates with neither a sequence nor a stop_id are dropped.

    _LOGGER.debug(
        "Parsed %d sequence + %d stop-id trip updates",
        len(by_sequence), len(by_stop_id),
    )
    return TripUpdateIndex(by_sequence, by_stop_id, trip_cancelled, trip_skipped_count)


def _prediction(stu: object) -> tuple[int | None, datetime | None]:
    """Return ``(delay_seconds | None, absolute_time | None)`` for a stop update.

    Departure is preferred over arrival; within an event, ``delay`` is preferred
    over ``time``.  ``(None, None)`` means no prediction is published.
    """
    for event_name in ("departure", "arrival"):
        if not _has_msg_field(stu, event_name):
            continue
        event = getattr(stu, event_name)
        if _has_int_field(event, "delay"):
            return int(event.delay), None
        if _has_int_field(event, "time"):
            return None, datetime.fromtimestamp(int(event.time), tz=timezone.utc)
    return None, None


def _has_msg_field(obj: object, name: str) -> bool:
    has = getattr(obj, "HasField", None)
    if has is None:
        return False
    try:
        return bool(has(name))
    except (ValueError, TypeError):
        return False


def _has_int_field(obj: object, name: str) -> bool:
    """True if a scalar protobuf field is explicitly set (key presence, not
    truthiness — so ``direction_id == 0`` and ``stop_sequence == 0`` count)."""
    return _has_msg_field(obj, name)


def _string_field(obj: object, name: str) -> str | None:
    """Return a string field's value, or None when absent/empty."""
    try:
        value = getattr(obj, name)
    except AttributeError:
        return None
    return value or None


def parse_trip_updates(feed: object) -> dict[str, StopDelay]:
    """Parse a TripUpdates FeedMessage into the legacy flat delay map.

    Keyed ``"<trip_id>:<stop_id>"`` for O(1) coordinator lookups.  Updates
    without a ``stop_id`` (sequence-only) are omitted; for a loop trip the last
    occurrence of a stop wins (the legacy coordinator does not handle loops).
    Each :class:`StopDelay` carries the stop-level ``is_skipped`` flag (T9).

    Args:
        feed: A decoded ``gtfs_realtime_pb2.FeedMessage`` instance.

    Returns:
        Dict mapping "trip_id:stop_id" → StopDelay.
    """
    result: dict[str, StopDelay] = {}

    try:
        entities = feed.entity  # type: ignore[attr-defined]
    except AttributeError:
        _LOGGER.warning(
            "parse_trip_updates received unexpected feed type: %s", type(feed)
        )
        return result

    for entity in entities:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        trip_id = trip_update.trip.trip_id
        if not trip_id:
            continue
        is_cancelled = trip_update.trip.schedule_relationship == _SR_CANCELED
        for stu in trip_update.stop_time_update:
            stop_id = stu.stop_id
            if not stop_id:
                continue
            result[f"{trip_id}:{stop_id}"] = StopDelay(
                delay_seconds=_legacy_delay(stu),
                is_cancelled=is_cancelled,
                is_skipped=stu.schedule_relationship == _STU_SKIPPED,
            )

    return result


def _legacy_delay(stu: object) -> int:
    """Historical delay precedence for the legacy flat map.

    The watch coordinator's :class:`StopDelay` has no absolute-time field, so the
    legacy map is DELIBERATELY divergent from the board index (:func:`_prediction`,
    which prefers an absolute ``time`` over an arrival ``delay``).  It restores the
    pre-T9 behaviour: ``departure.delay`` → ``arrival.delay`` → ``0``, so a stop
    whose departure carries only an absolute time but whose arrival carries a
    delay still reports that arrival delay (never a misleading 0).
    """
    for event_name in ("departure", "arrival"):
        if not _has_msg_field(stu, event_name):
            continue
        event = getattr(stu, event_name)
        if _has_int_field(event, "delay"):
            return int(event.delay)
    return 0
