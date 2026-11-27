"""T11 watch→board import compatibility-mapper unit tests (light env, pure).

Covers the compatibility rules in
``custom_components.transport_victoria.watch_import`` against the T6 fixtures:
journeys / trackers stay as-is, missing/NULL keys map to explicit None with a
note, stale references (route / stop / destination) are flagged (never dropped),
and the derived pins use the T8 derivation path.  No homeassistant imports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.transport_victoria.const import (
    CONF_ALL_DESTINATIONS,
    CONF_DESTINATION_STOP_ID,
    CONF_DESTINATION_STOP_NAME,
    CONF_DIRECTION,
    CONF_ENTRY_TYPE,
    CONF_JOURNEY_MODE,
    CONF_MODE,
    CONF_PLATFORM_FILTER,
    CONF_ROUTE_ID_FILTER,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_WATCH_NAME,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_VEHICLE_TRACKER,
    ENTRY_TYPE_WATCH,
    MODE_BUS,
)
from custom_components.transport_victoria.watch_import import (
    REASON_JOURNEY,
    REASON_MISSING_STOP,
    REASON_NO_ROUTE,
    REASON_TRACKER,
    STALE_DESTINATION,
    STALE_ROUTE,
    STALE_STOP,
    WatchMapping,
    map_watch_to_pin,
)

from tests import gtfs_fixtures as fx
from tests.gtfs_builder import ROUTE_TYPE_BUS, import_bundle


def _bus_watch(overrides: dict | None = None) -> dict:
    """A minimal compatible bus watch (all destinations, one line)."""
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
        CONF_MODE: MODE_BUS,
        CONF_STOP_ID: "B_SUNSHINE",
        CONF_STOP_NAME: "Sunshine",
        CONF_ALL_DESTINATIONS: True,
        CONF_ROUTE_ID_FILTER: "R_BUS_220",
        CONF_WATCH_NAME: "Sunshine watch",
    }
    if overrides:
        data.update(overrides)
    return data


@pytest.fixture
def two_route_bus_db(tmp_path: Path) -> Path:
    """A bus generation with TWO routes sharing one stop (all-lines ambiguity)."""
    routes = [
        {"route_id": "R_BUS_A", "route_short_name": "A", "route_long_name": "A Line",
         "route_type": ROUTE_TYPE_BUS},
        {"route_id": "R_BUS_B", "route_short_name": "B", "route_long_name": "B Line",
         "route_type": ROUTE_TYPE_BUS},
    ]
    stops = [
        fx._stop("S_HUB", "Hub", -37.0, 144.0),
        fx._stop("S_A", "A End", -37.1, 144.1),
        fx._stop("S_B", "B End", -37.2, 144.2),
    ]
    trips = [
        fx._trip("T_A", "R_BUS_A", "SVC", "A End", 0),
        fx._trip("T_B", "R_BUS_B", "SVC", "B End", 0),
    ]
    stop_times = [
        fx._st("T_A", "S_HUB", 1, "08:00:00"),
        fx._st("T_A", "S_A", 2, "09:00:00"),
        fx._st("T_B", "S_HUB", 1, "08:30:00"),
        fx._st("T_B", "S_B", 2, "09:30:00"),
    ]
    return import_bundle(
        tmp_path,
        folder="4",
        routes=routes,
        trips=trips,
        stops=stops,
        stop_times=stop_times,
        calendar=[fx._calendar("SVC")],
    )


# ---------------------------------------------------------------------------
# Compatible mappings (T8 derivation path)
# ---------------------------------------------------------------------------


def test_all_destinations_watch_maps_to_pin(bus_db: Path):
    mapping = map_watch_to_pin(bus_db, _bus_watch())
    assert mapping.compatible is True
    assert mapping.reason is None
    assert mapping.stale is False
    pin = mapping.pin
    assert pin is not None
    assert pin.route_id == "R_BUS_220"
    assert pin.route_type == ROUTE_TYPE_BUS
    assert pin.boarding_stop_id == "B_SUNSHINE"
    assert pin.all_destinations is True
    assert pin.destination_stop_id is None
    assert pin.direction_id is None
    # Labels/identity come from the T8 path (matches manual creation).
    assert pin.label_from == "220 from Sunshine"
    assert pin.label_to == "All destinations"


def test_specific_destination_watch_maps_to_pin(bus_db: Path):
    mapping = map_watch_to_pin(
        bus_db,
        _bus_watch({
            CONF_ALL_DESTINATIONS: False,
            CONF_DESTINATION_STOP_ID: "B_CITY",
            CONF_DESTINATION_STOP_NAME: "City",
            CONF_DIRECTION: 0,
        }),
    )
    assert mapping.compatible is True
    pin = mapping.pin
    assert pin is not None
    assert pin.all_destinations is False
    assert pin.destination_stop_id == "B_CITY"
    assert pin.direction_id == 0
    # Resolver picks a deterministic via (Footscray sorts first).
    assert pin.via_hint == "Footscray"


def test_no_line_filter_single_route_resolves(bus_db: Path):
    mapping = map_watch_to_pin(
        bus_db, _bus_watch({CONF_ROUTE_ID_FILTER: None})
    )
    assert mapping.compatible is True
    assert mapping.pin is not None
    assert mapping.pin.route_id == "R_BUS_220"


def test_no_line_filter_multiple_routes_incompatible(two_route_bus_db: Path):
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
        CONF_MODE: MODE_BUS,
        CONF_STOP_ID: "S_HUB",
        CONF_STOP_NAME: "Hub",
        CONF_ALL_DESTINATIONS: True,
        CONF_ROUTE_ID_FILTER: None,
    }
    mapping = map_watch_to_pin(two_route_bus_db, data)
    assert mapping.compatible is False
    assert mapping.reason == REASON_NO_ROUTE
    assert mapping.pin is None


# ---------------------------------------------------------------------------
# Incompatibility rules
# ---------------------------------------------------------------------------


def test_transfer_journey_incompatible(bus_db: Path):
    mapping = map_watch_to_pin(bus_db, _bus_watch({CONF_JOURNEY_MODE: True}))
    assert mapping.compatible is False
    assert mapping.reason == REASON_JOURNEY
    assert mapping.pin is None


def test_vehicle_tracker_incompatible(bus_db: Path):
    mapping = map_watch_to_pin(
        bus_db, _bus_watch({CONF_ENTRY_TYPE: ENTRY_TYPE_VEHICLE_TRACKER})
    )
    assert mapping.compatible is False
    assert mapping.reason == REASON_TRACKER


def test_route_tracker_incompatible(bus_db: Path):
    mapping = map_watch_to_pin(
        bus_db, _bus_watch({CONF_ENTRY_TYPE: ENTRY_TYPE_ROUTE_TRACKER})
    )
    assert mapping.compatible is False
    assert mapping.reason == REASON_TRACKER


def test_missing_stop_incompatible(bus_db: Path):
    mapping = map_watch_to_pin(bus_db, _bus_watch({CONF_STOP_ID: None}))
    assert mapping.compatible is False
    assert mapping.reason == REASON_MISSING_STOP


# ---------------------------------------------------------------------------
# Stale references (flagged, never dropped)
# ---------------------------------------------------------------------------


def test_stale_route_is_flagged_not_dropped(bus_db: Path):
    mapping = map_watch_to_pin(bus_db, _bus_watch({CONF_ROUTE_ID_FILTER: "R_GONE"}))
    assert mapping.compatible is True
    assert mapping.stale is True
    assert STALE_ROUTE in mapping.stale_fields
    assert STALE_STOP not in mapping.stale_fields
    # Still imported: pin carries the (stale) route id.
    assert mapping.pin is not None
    assert mapping.pin.route_id == "R_GONE"


def test_stale_stop_is_flagged_not_dropped(bus_db: Path):
    mapping = map_watch_to_pin(bus_db, _bus_watch({CONF_STOP_ID: "S_GONE"}))
    assert mapping.compatible is True
    assert mapping.stale is True
    assert STALE_STOP in mapping.stale_fields
    assert mapping.pin is not None
    assert mapping.pin.boarding_stop_id == "S_GONE"


def test_stale_destination_is_flagged_not_dropped(bus_db: Path):
    mapping = map_watch_to_pin(
        bus_db,
        _bus_watch({
            CONF_ALL_DESTINATIONS: False,
            CONF_DESTINATION_STOP_ID: "V_GEELONG",
            CONF_DESTINATION_STOP_NAME: "Geelong",
        }),
    )
    assert mapping.compatible is True
    assert mapping.stale is True
    assert STALE_DESTINATION in mapping.stale_fields
    assert mapping.pin is not None
    assert mapping.pin.destination_stop_id == "V_GEELONG"


# ---------------------------------------------------------------------------
# Missing / NULL keys → explicit None + note
# ---------------------------------------------------------------------------


def test_missing_optional_keys_record_notes(bus_db: Path):
    mapping = map_watch_to_pin(bus_db, _bus_watch())
    assert mapping.compatible is True
    joined = "\n".join(mapping.notes)
    assert "platform_filter missing" in joined
    assert "colour_override" in joined
    # Explicit None (not silently dropped) in the derived pin.
    assert mapping.pin is not None
    assert mapping.pin.platform_filter is None
    assert mapping.pin.colour_override is None


def test_missing_destination_id_falls_back_to_all_destinations(bus_db: Path):
    # Specific destination flag set, but no destination id stored (rule b).
    mapping = map_watch_to_pin(
        bus_db,
        _bus_watch({CONF_ALL_DESTINATIONS: False}),
    )
    assert mapping.compatible is True
    assert mapping.pin is not None
    assert mapping.pin.all_destinations is True
    joined = "\n".join(mapping.notes)
    assert "destination_stop_id missing" in joined


def test_platform_filter_preserved(bus_db: Path):
    mapping = map_watch_to_pin(bus_db, _bus_watch({CONF_PLATFORM_FILTER: "1"}))
    assert mapping.compatible is True
    assert mapping.pin is not None
    assert mapping.pin.platform_filter == "1"


def test_mapping_result_type(bus_db: Path):
    assert isinstance(map_watch_to_pin(bus_db, _bus_watch()), WatchMapping)


# ---------------------------------------------------------------------------
# Consumer-union: the imported pin's bundles cover the watch's mode
# ---------------------------------------------------------------------------


def test_imported_pin_bundles_cover_watch_mode(bus_db: Path):
    """A board of imported pins drives the same bundle union as the watches."""
    from custom_components.transport_victoria.board import board_bundles

    mapping = map_watch_to_pin(bus_db, _bus_watch())
    assert mapping.pin is not None
    # Bus route_type 3 → folders {"4", "11"} (what ``_board_bundles_for_entry``
    # contributes to the shared schedule consumer union).
    assert board_bundles([mapping.pin]) == frozenset({"4", "11"})
