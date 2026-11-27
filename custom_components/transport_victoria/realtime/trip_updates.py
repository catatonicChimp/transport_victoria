"""Parse GTFS Realtime trip-update feeds into per-stop delay maps.

The trip-updates feed carries delay and cancellation information keyed by
trip_id + stop_id. This module converts raw FeedMessage objects into a simple
dict so the coordinator can merge realtime data into scheduled departures
without touching protobuf objects outside this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# ScheduleRelationship enum values from the GTFS Realtime spec
_SR_CANCELED = 3
# StopTimeUpdate.ScheduleRelationship.SKIPPED
_STU_SKIPPED = 1


@dataclass(slots=True, frozen=True)
class StopDelay:
    """Realtime delay information for one trip at one stop."""

    delay_seconds: int
    is_cancelled: bool
    # T9: this specific stop_time_update is SKIPPED (the trip will not serve
    # this stop).  Distinct from trip-level cancellation.
    is_skipped: bool = False


@dataclass(slots=True, frozen=True)
class TripUpdateIndex:
    """Parsed trip-updates feed, indexed for board aggregation (T9).

    ``stop_delays`` is the flat ``"<trip_id>:<stop_id>"`` delay map the
    coordinator already consumes.  ``trip_cancelled`` carries trip-level
    cancellation (including trips with *no* stop_time_updates, which the flat
    map cannot represent) and ``trip_skipped_count`` the per-trip number of
    ``SKIPPED`` stop updates — both surfaced so a board can flag a trip that
    skips its boarding stop while still applying delays to the stops it serves.
    """

    stop_delays: dict[str, StopDelay]
    trip_cancelled: dict[str, bool]
    trip_skipped_count: dict[str, int]


def parse_trip_updates_index(feed: object) -> TripUpdateIndex:
    """Parse a TripUpdates FeedMessage into a full index (delays + cancel/skip).

    See :class:`TripUpdateIndex` for the shape.  The flat delay map is available
    as ``.stop_delays`` (identical to :func:`parse_trip_updates`'s return).

    Args:
        feed: A decoded ``gtfs_realtime_pb2.FeedMessage`` instance.

    Returns:
        A :class:`TripUpdateIndex` (empty when the feed is malformed/empty).
    """
    stop_delays: dict[str, StopDelay] = {}
    trip_cancelled: dict[str, bool] = {}
    trip_skipped_count: dict[str, int] = {}

    try:
        entities = feed.entity  # type: ignore[attr-defined]
    except AttributeError:
        _LOGGER.warning(
            "parse_trip_updates_index received unexpected feed type: %s",
            type(feed),
        )
        return TripUpdateIndex(stop_delays, trip_cancelled, trip_skipped_count)

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

        for stu in trip_update.stop_time_update:
            stop_id: str = stu.stop_id
            if not stop_id:
                continue

            is_skipped = stu.schedule_relationship == _STU_SKIPPED
            if is_skipped:
                trip_skipped_count[trip_id] = trip_skipped_count.get(trip_id, 0) + 1

            delay = 0
            if stu.HasField("departure") and stu.departure.HasField("delay"):
                delay = stu.departure.delay
            elif stu.HasField("arrival") and stu.arrival.HasField("delay"):
                delay = stu.arrival.delay

            key = f"{trip_id}:{stop_id}"
            stop_delays[key] = StopDelay(
                delay_seconds=delay,
                is_cancelled=is_cancelled,
                is_skipped=is_skipped,
            )

    _LOGGER.debug("Parsed %d stop-level trip updates", len(stop_delays))
    return TripUpdateIndex(stop_delays, trip_cancelled, trip_skipped_count)


def parse_trip_updates(feed: object) -> dict[str, StopDelay]:
    """Parse a TripUpdates FeedMessage into a delay lookup dict.

    The returned dict is keyed by ``"<trip_id>:<stop_id>"`` so the coordinator
    can do an O(1) lookup for each scheduled departure.  Each :class:`StopDelay`
    now also carries the stop-level ``is_skipped`` flag (T9), default ``False``
    for feeds that carry no SKIPPED relationship.

    When a stop_time_update carries both arrival and departure delays, the
    departure delay is preferred (it's what passengers care about). If only
    arrival is present, that value is used instead.

    Args:
        feed: A decoded ``gtfs_realtime_pb2.FeedMessage`` instance.

    Returns:
        Dict mapping "trip_id:stop_id" → StopDelay.
    """
    return parse_trip_updates_index(feed).stop_delays
