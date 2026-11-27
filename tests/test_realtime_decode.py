"""Decode tests for committed GTFS Realtime protobuf fixtures.

Bytes are loaded from ``tests/fixtures/realtime/*.pb`` (built programmatically
by ``tests/realtime_fixtures.py``) and decoded through the production
``realtime.client._decode_feed`` — the same function the client runs in an
executor thread.  Assertions cover both the raw protobuf facts and the
downstream parser surfaces T7-T10 consume.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from google.transit import gtfs_realtime_pb2 as rt

from custom_components.transport_victoria.realtime.client import _decode_feed
from custom_components.transport_victoria.realtime.service_alerts import parse_alerts
from custom_components.transport_victoria.realtime.trip_updates import (
    parse_trip_updates,
)
from custom_components.transport_victoria.realtime.vehicle_positions import (
    parse_vehicle_positions,
)

from tests.realtime_fixtures import (
    ALERT_END,
    ALERT_START,
    BASE_TIMESTAMP,
    ROUTE_FRANKSTON,
    SCENARIOS,
    SHARED_STOP,
    TRIP_FRANKSTON,
    fixture_path,
    load_bytes,
)


def _decode(name: str):
    return _decode_feed(load_bytes(name))


class TestFixtureInventory:
    def test_every_committed_fixture_exists_and_decodes(self):
        for name in SCENARIOS:
            path = fixture_path(name)
            assert path.is_file(), name
            assert path.stat().st_size > 0, name

            feed = _decode(name)
            assert isinstance(feed, rt.FeedMessage), name
            assert feed.header.gtfs_realtime_version == "2.0", name
            assert feed.header.timestamp > 0, name

    def test_committed_bytes_are_byte_identical_across_runs(self):
        # Reading twice yields the same bytes (no rebuild per run).
        assert load_bytes("trip_update_delay") == load_bytes("trip_update_delay")


class TestTripUpdateDelay:
    def test_delay_and_descriptor_facts(self):
        feed = _decode("trip_update_delay")
        assert feed.header.timestamp == BASE_TIMESTAMP
        assert len(feed.entity) == 1

        tu = feed.entity[0].trip_update
        assert tu.trip.trip_id == TRIP_FRANKSTON
        assert tu.trip.route_id == ROUTE_FRANKSTON
        assert tu.trip.direction_id == 0
        assert tu.trip.start_date == "20260601"

        shared, terminus = tu.stop_time_update
        assert shared.stop_id == SHARED_STOP
        assert shared.stop_sequence == 2
        assert shared.departure.delay == 90
        assert shared.arrival.delay == 100

        assert terminus.stop_id == "FRANKSTON"
        assert terminus.arrival.delay == 240
        assert not terminus.HasField("departure")

        parsed = parse_trip_updates(feed)
        assert parsed[f"{TRIP_FRANKSTON}:{SHARED_STOP}"].delay_seconds == 90
        assert parsed[f"{TRIP_FRANKSTON}:FRANKSTON"].delay_seconds == 240
        assert parsed[f"{TRIP_FRANKSTON}:{SHARED_STOP}"].is_cancelled is False


class TestTripUpdateCanceled:
    def test_canceled_flag_and_parser(self):
        feed = _decode("trip_update_canceled")
        tu = feed.entity[0].trip_update
        assert tu.trip.trip_id == "T_FRK_OUT_3"
        assert tu.trip.schedule_relationship == rt.TripDescriptor.CANCELED

        parsed = parse_trip_updates(feed)
        delay = parsed["T_FRK_OUT_3:RICHMOND"]
        assert delay.is_cancelled is True
        assert delay.delay_seconds == 0


class TestTripUpdateSkipped:
    def test_skipped_stop_time_update(self):
        feed = _decode("trip_update_skipped")
        tu = feed.entity[0].trip_update
        skipped, later = tu.stop_time_update

        assert skipped.stop_id == SHARED_STOP
        assert (
            skipped.schedule_relationship
            == rt.TripUpdate.StopTimeUpdate.SKIPPED
        )
        assert later.stop_id == "NEWPORT"
        assert later.departure.delay == 60

        # Current parse_trip_updates surfaces delay but not the SKIPPED
        # relationship (flagged in the T6 report; T8/T9 must handle skips).
        parsed = parse_trip_updates(feed)
        assert parsed[f"{tu.trip.trip_id}:{SHARED_STOP}"].delay_seconds == 0
        assert parsed[f"{tu.trip.trip_id}:NEWPORT"].delay_seconds == 60
        assert parsed[f"{tu.trip.trip_id}:{SHARED_STOP}"].is_cancelled is False


class TestTripUpdateMultipleEntities:
    def test_multiple_entities_in_one_feed(self):
        feed = _decode("trip_update_multi")
        assert [e.id for e in feed.entity] == [
            "tu-multi-a", "tu-multi-b", "tu-multi-c",
        ]

        parsed = parse_trip_updates(feed)
        assert parsed["T_MULTI_A:RICHMOND"].delay_seconds == 30
        assert parsed["T_MULTI_B:NEWPORT"].delay_seconds == -45
        # Canceled entity has no stop updates, so no keys are emitted.
        assert not any(k.startswith("T_MULTI_C:") for k in parsed)


class TestServiceAlert:
    def test_informed_entities_period_and_text(self):
        feed = _decode("alert_service")
        alert = feed.entity[0].alert
        assert feed.entity[0].id == "alert-1"

        informed = alert.informed_entity
        assert [ie.route_id for ie in informed if ie.route_id] == [
            ROUTE_FRANKSTON
        ]
        assert [ie.stop_id for ie in informed if ie.stop_id] == [SHARED_STOP]
        assert [ie.trip.trip_id for ie in informed if ie.trip.trip_id] == [
            TRIP_FRANKSTON
        ]

        period = alert.active_period[0]
        assert period.start == ALERT_START
        assert period.end == ALERT_END

        assert alert.header_text.translation[0].text == "Frankston line disruption"
        assert alert.header_text.translation[0].language == "en"
        assert alert.description_text.translation[0].text.startswith(
            "Buses replace trains"
        )

    def test_parser_output_and_filters(self):
        feed = _decode("alert_service")
        alerts = parse_alerts(feed)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.alert_id == "alert-1"
        assert alert.header == "Frankston line disruption"
        assert alert.description.startswith("Buses replace trains")
        assert alert.affected_route_ids == [ROUTE_FRANKSTON]
        assert alert.affected_stop_ids == [SHARED_STOP]
        # effect 3 matches both the spec enum and the production map in use.
        assert alert.effect == "SIGNIFICANT_DELAYS"
        assert alert.active_from == datetime.fromtimestamp(
            ALERT_START, tz=timezone.utc
        )
        assert alert.active_until == datetime.fromtimestamp(
            ALERT_END, tz=timezone.utc
        )

        assert len(parse_alerts(feed, route_id=ROUTE_FRANKSTON)) == 1
        assert len(parse_alerts(feed, stop_id=SHARED_STOP)) == 1
        assert parse_alerts(feed, route_id="R_UNRELATED") == []


class TestVehiclePosition:
    def test_trip_descriptor_position_and_status(self):
        feed = _decode("vehicle_position")
        veh = feed.entity[0].vehicle
        assert veh.trip.trip_id == TRIP_FRANKSTON
        assert veh.trip.route_id == ROUTE_FRANKSTON
        assert veh.trip.start_date == "20260601"
        assert veh.trip.direction_id == 0
        assert veh.position.latitude == pytest.approx(-37.85)
        assert veh.position.longitude == pytest.approx(145.02)
        assert veh.vehicle.id == "V123"
        assert veh.stop_id == "CAULFIELD"
        assert veh.current_status == rt.VehiclePosition.STOPPED_AT

        positions = parse_vehicle_positions(feed)
        pos = positions[TRIP_FRANKSTON]
        assert pos.route_id == ROUTE_FRANKSTON
        assert pos.latitude == pytest.approx(-37.85)
        assert pos.longitude == pytest.approx(145.02)
        assert pos.bearing == pytest.approx(180.0)
        assert pos.speed == pytest.approx(12.5)
        assert pos.speed_kmh == pytest.approx(45.0)
        assert pos.timestamp == datetime.fromtimestamp(
            BASE_TIMESTAMP, tz=timezone.utc
        )
        assert pos.vehicle_id == "V123"
        assert pos.vehicle_label == "123"
        assert pos.current_stop_id == "CAULFIELD"
        assert pos.current_status == "STOPPED_AT"


class TestHeaderTimestamp:
    def test_header_timestamp_increments_by_one_minute(self):
        first = _decode("header_tick_0")
        second = _decode("header_tick_1")
        assert first.header.timestamp == BASE_TIMESTAMP
        assert second.header.timestamp == BASE_TIMESTAMP + 60
        assert len(first.entity) == 0 and len(second.entity) == 0
