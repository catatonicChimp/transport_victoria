"""Dynamic, route-scoped vehicle fleet tracking (T16).

Pure logic with **no** ``homeassistant`` imports: the light-env unit tests
import this module directly, and the ``device_tracker`` platform wires it into
a :class:`~.device_tracker.FleetTrackerCoordinator` at runtime.

The maintainer's v0.1.0 tracker latched onto a single trip.  This module is the
replacement: it reconciles the *active* vehicle-positions feed for a watch or
board against the set of routes that entry watches, producing a dynamic set of
per-vehicle trackers that appear as vehicles start serving a watched route and
disappear when they finish.

Key decisions (see the T16 report for rationale):

* **Primary key is ``vehicle.id``** from the feed — stable within a feed, unlike
  ``trip_id`` which changes per trip (and per day).  ``parse_vehicle_positions``
  still keys by ``trip_id`` for the legacy route tracker; the fleet uses
  :func:`~.realtime.vehicle_positions.parse_vehicle_positions_by_vehicle`.

* **Route matching precedence** (see :func:`resolve_vehicle_route`):
  1. the feed's own trip descriptor ``route_id`` (primary),
  2. the GTFS schedule ``trips`` table (``trip_id`` → ``route_id``),
  3. the trip-updates trip descriptor ``route_id`` (fallback, when both feeds
     are parsed).  A vehicle whose resolved route is not in the watched set is
     dropped (regional-bus vehicles ride the combined "bus" feed and are only
     kept when their route actually matches a watched route).

* **Lifecycle** (:func:`update_fleet`): a vehicle is ADDED the first poll it
  appears on a watched route and REMOVED after ``GRACE_ABSENT_SECONDS`` of
  absence (the realtime feed TTL is 30 s, so 90 s covers three consecutive
  misses before a trip's end removes its vehicle id from the feed).

* **Cap** (:data:`MAX_VEHICLES_PER_ENTRY`): hard ceiling on simultaneously
  tracked vehicles; when exceeded, the overflow is dropped by a deterministic
  stable sort on ``vehicle_id`` (no home-zone computation — a board/watch has no
  single reference point to rank by distance).

* **Staleness** (:func:`feed_is_stale`): mirrors T13's header-timestamp
  classification.  A *stale* feed is still a successful fetch (HTTP 200 with
  old data), so the coordinator keeps trackers ``available`` with their
  last-known positions and logs the staleness once — it does NOT mark
  per-vehicle entities unavailable, and removal stays governed by the per-vehicle
  absence grace rather than the feed header age.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .realtime.vehicle_positions import VehiclePosition

_LOGGER = logging.getLogger(__name__)

# A vehicle is removed once it has been absent from the feed for this long.
# The realtime feed TTL is 30 s; three consecutive misses (at the default 30 s
# poll) covers transient drops while a trip's vehicle id vanishes at trip end.
GRACE_ABSENT_SECONDS = 90

# Hard cap on simultaneously-tracked vehicles per entry.  Overflow is dropped
# by a deterministic stable sort on vehicle_id.
MAX_VEHICLES_PER_ENTRY = 30

# Feed staleness threshold — matches T13/T9's ``_FEED_STALE_AFTER`` (3 minutes
# from the header timestamp).  A header more than this old classifies the feed
# "stale" (still usable, but logged).
FEED_STALE_AFTER_SECONDS = 180

# A header timestamp more than this far in the FUTURE is clock skew → fresh.
FEED_FUTURE_SKEW_SECONDS = 300

# ``current_status`` (VehicleStopStatus) → motion label used as the tracker's
# state.  ``INCOMING_AT`` (about to arrive) is treated as stationary so the
# state is a coarse moving/stationary signal.
_STATUS_MOTION: dict[str, str] = {
    "IN_TRANSIT_TO": "moving",
    "STOPPED_AT": "stationary",
    "INCOMING_AT": "stationary",
}


@dataclass(slots=True, frozen=True)
class FleetVehicle:
    """One tracked vehicle, keyed by the feed's ``vehicle.id``."""

    vehicle_id: str
    route_id: str
    trip_id: str
    latitude: float
    longitude: float
    bearing: float | None
    speed_kmh: float | None
    vehicle_label: str | None
    current_status: str | None
    occupancy_status: str | None
    direction_id: int | None
    last_seen: datetime


@dataclass(slots=True)
class FleetUpdate:
    """The outcome of one reconcile: the current fleet + transition ids."""

    vehicles: dict[str, FleetVehicle]
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


def vehicle_state(current_status: str | None) -> str | None:
    """Return ``"moving"`` / ``"stationary"`` for a feed-declared status, else None."""
    if not current_status:
        return None
    return _STATUS_MOTION.get(current_status)


def resolve_vehicle_route(
    position: VehiclePosition,
    route_by_trip_id: dict[str, str],
    trip_update_routes: dict[str, str],
) -> str | None:
    """Resolve a vehicle position's route id, with documented precedence.

    1. ``position.route_id`` — the vehicle-positions feed's own trip descriptor
       (present for most metro/bus/vline vehicles).
    2. ``route_by_trip_id[position.trip_id]`` — the imported GTFS schedule
       ``trips`` table (``trip_id`` → ``route_id``), the reliable fallback when
       the feed omits the descriptor.
    3. ``trip_update_routes[position.trip_id]`` — the trip-updates feed's trip
       descriptor ``route_id`` (only populated when trip-updates are parsed).

    Returns ``None`` when the route cannot be determined (the caller drops the
    vehicle rather than guessing).
    """
    if position.route_id:
        return position.route_id
    if not position.trip_id:
        return None
    route = route_by_trip_id.get(position.trip_id)
    if route:
        return route
    return trip_update_routes.get(position.trip_id)


def feed_is_stale(
    header_timestamp: datetime | None,
    fetched_at: datetime | None,
    now: datetime,
) -> bool:
    """Classify a vehicle-positions feed as stale (header primary, fetch fallback).

    Mirrors T13: a header timestamp more than :data:`FEED_STALE_AFTER_SECONDS`
    old is stale; a header far in the future is clock skew → fresh; when the
    header is absent, fall back to the fetch time.  ``now`` must be aware UTC.
    """
    if header_timestamp is not None:
        age = now - header_timestamp
        if age < -timedelta(seconds=FEED_FUTURE_SKEW_SECONDS):
            return False
        return age > timedelta(seconds=FEED_STALE_AFTER_SECONDS)
    if fetched_at is not None:
        return (now - fetched_at) > timedelta(seconds=FEED_STALE_AFTER_SECONDS)
    return False


def update_fleet(
    tracked: dict[str, FleetVehicle],
    positions: list[VehiclePosition],
    watched_routes: frozenset[str],
    route_by_trip_id: dict[str, str],
    trip_update_routes: dict[str, str],
    now: datetime,
    *,
    grace_seconds: int = GRACE_ABSENT_SECONDS,
    max_vehicles: int = MAX_VEHICLES_PER_ENTRY,
) -> FleetUpdate:
    """Reconcile the current feed against the tracked fleet → :class:`FleetUpdate`.

    Pure and deterministic:

    * Only vehicles whose resolved route is in ``watched_routes`` are kept.
    * A vehicle present this poll is upserted (``last_seen = now``) and reported
      in ``added`` the first time it is seen.
    * A tracked vehicle absent this poll is retained with its last-known state
      until it has been absent ``grace_seconds``; then it is reported ``removed``.
    * When more than ``max_vehicles`` survive, the overflow (deterministic stable
      sort on ``vehicle_id``) is dropped — newly-added overflow vehicles are
      simply not reported as added, already-tracked overflow vehicles are removed.

    Args:
        tracked: The previous fleet (``vehicle_id`` → :class:`FleetVehicle`).
        positions: Parsed active positions (one per vehicle id).
        watched_routes: The set of route ids this entry watches.
        route_by_trip_id: ``trip_id`` → ``route_id`` from the GTFS trips table.
        trip_update_routes: ``trip_id`` → ``route_id`` from trip-updates.
        now: Aware UTC datetime for ``last_seen`` / grace computations.
        grace_seconds: Absence threshold before removal.
        max_vehicles: Hard cap on tracked vehicles.
    """
    added: list[str] = []
    next_tracked: dict[str, FleetVehicle] = {}

    for pos in positions:
        route = resolve_vehicle_route(pos, route_by_trip_id, trip_update_routes)
        if route is None or route not in watched_routes:
            continue
        vehicle_id = pos.vehicle_id
        if not vehicle_id:
            continue
        vehicle = FleetVehicle(
            vehicle_id=vehicle_id,
            route_id=route,
            trip_id=pos.trip_id,
            latitude=pos.latitude,
            longitude=pos.longitude,
            bearing=pos.bearing,
            speed_kmh=pos.speed_kmh,
            vehicle_label=pos.vehicle_label,
            current_status=pos.current_status,
            occupancy_status=pos.occupancy_status,
            direction_id=pos.direction_id,
            last_seen=now,
        )
        if vehicle_id not in tracked:
            added.append(vehicle_id)
        next_tracked[vehicle_id] = vehicle

    removed: list[str] = []
    for vehicle_id, vehicle in tracked.items():
        if vehicle_id in next_tracked:
            continue
        if (now - vehicle.last_seen).total_seconds() >= grace_seconds:
            removed.append(vehicle_id)
        else:
            # Still within the grace window — retain last-known state.
            next_tracked[vehicle_id] = vehicle

    # Cap — deterministic drop order (stable sort by vehicle_id).
    if len(next_tracked) > max_vehicles:
        ordered = sorted(next_tracked)
        for vehicle_id in ordered[max_vehicles:]:
            next_tracked.pop(vehicle_id, None)
            if vehicle_id in added:
                added.remove(vehicle_id)
            elif vehicle_id in tracked:
                removed.append(vehicle_id)

    return FleetUpdate(
        vehicles=next_tracked,
        added=tuple(sorted(added)),
        removed=tuple(sorted(removed)),
    )
