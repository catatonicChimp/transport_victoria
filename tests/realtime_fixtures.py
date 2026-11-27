"""Google-protobuf GTFS Realtime fixture feeds (test-only, offline).

Two jobs:

* :data:`BUILDERS` construct each scenario programmatically with
  ``google.transit.gtfs_realtime_pb2``.  ``python -m tests.realtime_fixtures
  --write`` regenerates the committed ``tests/fixtures/realtime/*.pb`` files.
* :func:`load_bytes` returns the committed serialized bytes, so decode tests
  never rebuild proto classes per run and always decode exactly the bytes a
  reviewer can audit in the repository.

All timestamps/dates are pinned constants (no wall clock).  Alert active
periods span 2020..2033 so the production ``parse_alerts`` "is active now"
filter is stable for the life of the fixture.

Provenance (library versions used to build the committed bytes) is recorded in
``tests/fixtures/realtime/README.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from google.transit import gtfs_realtime_pb2 as rt

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "realtime"

# Pinned header timestamp: 2025-06-15T14:26:40Z.
BASE_TIMESTAMP = 1_750_000_000
# Alert window wide enough that ``parse_alerts``' real-clock active check is
# stable: 2020-09-13T12:26:40Z .. 2033-05-18T03:33:20Z.
ALERT_START = 1_600_000_000
ALERT_END = 2_000_000_000

# Pinned service identifiers matching the schedule fixtures.
TRIP_FRANKSTON = "T_FRK_OUT_2"
ROUTE_FRANKSTON = "R_METRO_FRANKSTON"
SHARED_STOP = "RICHMOND"
SERVICE_DATE = "20260601"


def _feed(*, timestamp: int = BASE_TIMESTAMP) -> rt.FeedMessage:
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = rt.FeedHeader.FULL_DATASET
    feed.header.timestamp = timestamp
    return feed


def _trip_update_entity(feed: rt.FeedMessage, entity_id: str) -> rt.TripUpdate:
    entity = feed.entity.add()
    entity.id = entity_id
    return entity.trip_update


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------

def trip_update_delay() -> rt.FeedMessage:
    """One trip with a departure delay and an arrival-only delay."""
    feed = _feed()
    tu = _trip_update_entity(feed, "tu-delay-1")
    tu.trip.trip_id = TRIP_FRANKSTON
    tu.trip.route_id = ROUTE_FRANKSTON
    tu.trip.direction_id = 0
    tu.trip.start_date = SERVICE_DATE
    tu.trip.start_time = "08:00:00"

    shared = tu.stop_time_update.add()
    shared.stop_id = SHARED_STOP
    shared.stop_sequence = 2
    shared.arrival.delay = 100
    shared.departure.delay = 90  # parser prefers departure

    terminus = tu.stop_time_update.add()
    terminus.stop_id = "FRANKSTON"
    terminus.stop_sequence = 4
    terminus.arrival.delay = 240  # arrival-only → parser falls back to it
    return feed


def trip_update_canceled() -> rt.FeedMessage:
    """A canceled trip (TripDescriptor.schedule_relationship = CANCELED)."""
    feed = _feed()
    tu = _trip_update_entity(feed, "tu-canceled-1")
    tu.trip.trip_id = "T_FRK_OUT_3"
    tu.trip.route_id = ROUTE_FRANKSTON
    tu.trip.schedule_relationship = rt.TripDescriptor.CANCELED

    stu = tu.stop_time_update.add()
    stu.stop_id = SHARED_STOP
    stu.stop_sequence = 2
    stu.departure.delay = 0
    return feed


def trip_update_skipped() -> rt.FeedMessage:
    """A trip whose shared stop is SKIPPED; the next stop still has a delay."""
    feed = _feed()
    tu = _trip_update_entity(feed, "tu-skipped-1")
    tu.trip.trip_id = "T_WRB_OUT_2"
    tu.trip.route_id = "R_METRO_WERRIBEE"

    skipped = tu.stop_time_update.add()
    skipped.stop_id = SHARED_STOP
    skipped.stop_sequence = 2
    skipped.schedule_relationship = rt.TripUpdate.StopTimeUpdate.SKIPPED
    skipped.departure.delay = 0

    later = tu.stop_time_update.add()
    later.stop_id = "NEWPORT"
    later.stop_sequence = 3
    later.departure.delay = 60
    return feed


def trip_update_multi() -> rt.FeedMessage:
    """Three entities, including a canceled trip, in one feed."""
    feed = _feed()

    a = _trip_update_entity(feed, "tu-multi-a")
    a.trip.trip_id = "T_MULTI_A"
    a.trip.route_id = ROUTE_FRANKSTON
    stu = a.stop_time_update.add()
    stu.stop_id = SHARED_STOP
    stu.departure.delay = 30

    b = _trip_update_entity(feed, "tu-multi-b")
    b.trip.trip_id = "T_MULTI_B"
    b.trip.route_id = "R_METRO_WERRIBEE"
    stu = b.stop_time_update.add()
    stu.stop_id = "NEWPORT"
    stu.departure.delay = -45  # early

    c = _trip_update_entity(feed, "tu-multi-c")
    c.trip.trip_id = "T_MULTI_C"
    c.trip.route_id = ROUTE_FRANKSTON
    c.trip.schedule_relationship = rt.TripDescriptor.CANCELED
    return feed


def alert_service() -> rt.FeedMessage:
    """Service alert scoped to a route, a stop AND a trip."""
    feed = _feed()
    entity = feed.entity.add()
    entity.id = "alert-1"
    alert = entity.alert

    alert.informed_entity.add().route_id = ROUTE_FRANKSTON
    alert.informed_entity.add().stop_id = SHARED_STOP
    alert.informed_entity.add().trip.trip_id = TRIP_FRANKSTON

    period = alert.active_period.add()
    period.start = ALERT_START
    period.end = ALERT_END

    header = alert.header_text.translation.add()
    header.text = "Frankston line disruption"
    header.language = "en"
    header_fr = alert.header_text.translation.add()
    header_fr.text = "Perturbation ligne Frankston"
    header_fr.language = "fr"

    description = alert.description_text.translation.add()
    description.text = "Buses replace trains between Caulfield and Frankston."
    description.language = "en-AU"

    # cause is left at the protobuf default (0): an unset cause reads 0, which
    # is not a valid enum value and maps to "UNKNOWN_CAUSE" (the maps are now
    # derived from the bound enum — see T13).  The cause/effect mapping is
    # asserted in test_realtime_decode.py::TestCauseEffectMaps.
    alert.effect = rt.Alert.SIGNIFICANT_DELAYS
    return feed


def vehicle_position() -> rt.FeedMessage:
    """Vehicle position with a full trip descriptor and STOPPED_AT status."""
    feed = _feed()
    entity = feed.entity.add()
    entity.id = "vp-1"
    veh = entity.vehicle

    veh.trip.trip_id = TRIP_FRANKSTON
    veh.trip.route_id = ROUTE_FRANKSTON
    veh.trip.direction_id = 0
    veh.trip.start_date = SERVICE_DATE
    veh.trip.start_time = "08:00:00"

    veh.position.latitude = -37.85
    veh.position.longitude = 145.02
    veh.position.bearing = 180.0
    veh.position.speed = 12.5

    veh.vehicle.id = "V123"
    veh.vehicle.label = "123"
    veh.stop_id = "CAULFIELD"
    veh.current_status = rt.VehiclePosition.STOPPED_AT
    veh.timestamp = BASE_TIMESTAMP
    return feed


def vehicle_positions_fleet() -> rt.FeedMessage:
    """Four vehicles for the fleet tracker (T16).

    * ``V_A`` / ``V_B`` are on the watched route ``R_METRO_FRANKSTON`` (the feed
      descriptor carries ``route_id``), so they match directly.
    * ``V_C`` is on ``R_METRO_WERRIBEE`` (not watched) and is dropped.
    * ``V_D`` is a "stale" trip: the descriptor carries NO ``route_id``, so the
      resolver must fall back to the GTFS trips-table lookup (``T_FRK_OUT_3``
      maps to ``R_METRO_FRANKSTON``) and match.
    """
    feed = _feed()

    def _add(
        entity_id: str,
        trip_id: str,
        route_id: str,
        vehicle_id: str,
        label: str,
        lat: float,
        lon: float,
        status: int,
    ) -> rt.VehiclePosition:
        entity = feed.entity.add()
        entity.id = entity_id
        veh = entity.vehicle
        veh.trip.trip_id = trip_id
        if route_id:
            veh.trip.route_id = route_id
        veh.trip.direction_id = 0
        veh.position.latitude = lat
        veh.position.longitude = lon
        veh.position.bearing = 90.0
        veh.position.speed = 11.1
        veh.vehicle.id = vehicle_id
        veh.vehicle.label = label
        veh.current_status = status
        veh.timestamp = BASE_TIMESTAMP
        return veh

    _add("vp-fleet-1", "T_FRK_OUT_1", ROUTE_FRANKSTON, "V_A", "A",
         -37.85, 145.02, rt.VehiclePosition.IN_TRANSIT_TO)
    _add("vp-fleet-2", "T_FRK_OUT_2", ROUTE_FRANKSTON, "V_B", "B",
         -37.83, 145.01, rt.VehiclePosition.STOPPED_AT)
    _add("vp-fleet-3", "T_WRB_OUT_1", "R_METRO_WERRIBEE", "V_C", "C",
         -37.84, 144.90, rt.VehiclePosition.IN_TRANSIT_TO)
    _add("vp-fleet-4", "T_FRK_OUT_3", "", "V_D", "D",
         -37.86, 145.03, rt.VehiclePosition.IN_TRANSIT_TO)
    return feed


def header_tick_0() -> rt.FeedMessage:
    """Empty feed at the base header timestamp."""
    return _feed(timestamp=BASE_TIMESTAMP)


def header_tick_1() -> rt.FeedMessage:
    """Empty feed one minute later (header.timestamp incremented)."""
    return _feed(timestamp=BASE_TIMESTAMP + 60)


BUILDERS: dict[str, Callable[[], rt.FeedMessage]] = {
    "trip_update_delay": trip_update_delay,
    "trip_update_canceled": trip_update_canceled,
    "trip_update_skipped": trip_update_skipped,
    "trip_update_multi": trip_update_multi,
    "alert_service": alert_service,
    "vehicle_position": vehicle_position,
    "header_tick_0": header_tick_0,
    "header_tick_1": header_tick_1,
}

SCENARIOS = tuple(BUILDERS)


def fixture_path(name: str) -> Path:
    return FIXTURES_DIR / f"{name}.pb"


def load_bytes(name: str) -> bytes:
    """Return the committed serialized bytes for a scenario."""
    return fixture_path(name).read_bytes()


def build_bytes(name: str) -> bytes:
    """Serialize a scenario from the programmatic builder (regeneration path)."""
    return BUILDERS[name]().SerializeToString()


def write_all(dest: Path = FIXTURES_DIR) -> list[Path]:
    """(Re)write every committed ``.pb`` fixture deterministically."""
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name in SCENARIOS:
        path = dest / f"{name}.pb"
        path.write_bytes(build_bytes(name))
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--write" not in args:
        print("usage: python -m tests.realtime_fixtures --write", file=sys.stderr)
        return 2
    for path in write_all():
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover - regeneration entry point
    raise SystemExit(main())
