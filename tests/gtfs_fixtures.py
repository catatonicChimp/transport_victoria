"""Named, offline GTFS fixture catalogue for T7-T13.

Each function returns a plain keyword spec accepted by
:func:`tests.gtfs_builder.build_generation` / :func:`import_bundle`, or (for the
multi-bundle scenario) a ``{folder: spec}`` mapping.  The specs use pinned
dates from :data:`tests.gtfs_builder.REFERENCE_DATE`; pytest fixtures in
``tests/conftest.py`` import them into real generation DBs once per session.

The contract (scenario → key facts) is documented in
``tests/fixtures/README.md``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .gtfs_builder import (
    REFERENCE_DATE as REF,
    ROUTE_TYPE_BUS,
    ROUTE_TYPE_TRAM,
    ROUTE_TYPE_TRAIN,
    ROUTE_TYPE_VLINE,
)

# --- small row helpers -----------------------------------------------------

def _calendar(
    service_id: str,
    start: date = REF,
    end: date = REF + timedelta(days=30),
    **days: int,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "service_id": service_id,
        "start_date": start.strftime("%Y%m%d"),
        "end_date": end.strftime("%Y%m%d"),
        "monday": 1, "tuesday": 1, "wednesday": 1, "thursday": 1,
        "friday": 1, "saturday": 1, "sunday": 1,
    }
    row.update(days)
    return row


def _stop(
    stop_id: str,
    name: str,
    lat: float,
    lon: float,
    *,
    location_type: int | None = None,
    parent: str = "",
    platform: str = "",
) -> dict[str, Any]:
    return {
        "stop_id": stop_id,
        "stop_name": name,
        "stop_lat": lat,
        "stop_lon": lon,
        "stop_code": stop_id,
        "location_type": location_type,
        "parent_station": parent,
        "platform_code": platform,
        "wheelchair_boarding": 0,
    }


def _trip(
    trip_id: str,
    route_id: str,
    service_id: str,
    headsign: str,
    direction: int,
    shape_id: str = "",
) -> dict[str, Any]:
    return {
        "trip_id": trip_id,
        "route_id": route_id,
        "service_id": service_id,
        "shape_id": shape_id,
        "trip_headsign": headsign,
        "direction_id": direction,
        "block_id": "",
    }


def _st(
    trip_id: str,
    stop_id: str,
    sequence: int,
    departure: str,
    arrival: str | None = None,
) -> dict[str, Any]:
    """A stop_time.  ``departure=""`` makes it an arrival-only terminus row."""
    return {
        "trip_id": trip_id,
        "stop_id": stop_id,
        "stop_sequence": sequence,
        "arrival_time": departure if arrival is None else arrival,
        "departure_time": departure,
    }


def _shape(shape_id: str, points: list[tuple[float, float]]) -> list[dict[str, Any]]:
    return [
        {
            "shape_id": shape_id,
            "shape_pt_lat": lat,
            "shape_pt_lon": lon,
            "shape_pt_sequence": idx + 1,
            "shape_dist_traveled": idx * 1000,
        }
        for idx, (lat, lon) in enumerate(points)
    ]


# --- 2a. metro: two routes, shared stop, both directions, post-midnight -----

def metro_spec() -> dict[str, Any]:
    """Frankston + Werribee lines sharing Richmond, plus a 25:10 service."""
    stops = [
        _stop("FLINDERS", "Flinders Street", -37.8183, 144.9671),
        _stop("RICHMOND", "Richmond", -37.8236, 145.0025),
        _stop("CAULFIELD", "Caulfield", -37.8774, 145.0452),
        _stop("FRANKSTON", "Frankston", -38.1435, 145.1230),
        _stop("NEWPORT", "Newport", -37.8420, 144.8840),
        _stop("ALTONA", "Altona", -37.8680, 144.8300),
        _stop("WERRIBEE", "Werribee", -37.8990, 144.6600),
    ]
    routes = [
        {
            "route_id": "R_METRO_FRANKSTON", "route_short_name": "Frankston",
            "route_long_name": "Frankston Line", "route_type": ROUTE_TYPE_TRAIN,
            "route_color": "0099CC",
        },
        {
            "route_id": "R_METRO_WERRIBEE", "route_short_name": "Werribee",
            "route_long_name": "Werribee Line", "route_type": ROUTE_TYPE_TRAIN,
            "route_color": "0099CC",
        },
    ]
    trips = [
        _trip("T_FRK_OUT_1", "R_METRO_FRANKSTON", "SVC_METRO_WEEKDAY",
              "Frankston", 0, "shape_frk_out"),
        _trip("T_FRK_OUT_2", "R_METRO_FRANKSTON", "SVC_METRO_WEEKDAY",
              "Frankston", 0, "shape_frk_out"),
        _trip("T_FRK_OUT_3", "R_METRO_FRANKSTON", "SVC_METRO_WEEKDAY",
              "Frankston", 0, "shape_frk_out"),
        _trip("T_FRK_LATE", "R_METRO_FRANKSTON", "SVC_METRO_WEEKDAY",
              "Frankston", 0, "shape_frk_out"),
        _trip("T_FRK_IN_1", "R_METRO_FRANKSTON", "SVC_METRO_WEEKDAY",
              "Flinders Street", 1, "shape_frk_in"),
        _trip("T_WRB_OUT_1", "R_METRO_WERRIBEE", "SVC_METRO_WEEKDAY",
              "Werribee", 0, "shape_wrb_out"),
        _trip("T_WRB_OUT_2", "R_METRO_WERRIBEE", "SVC_METRO_WEEKDAY",
              "Werribee", 0, "shape_wrb_out"),
        _trip("T_WRB_OUT_3", "R_METRO_WERRIBEE", "SVC_METRO_WEEKDAY",
              "Werribee", 0, "shape_wrb_out"),
        _trip("T_WRB_IN_1", "R_METRO_WERRIBEE", "SVC_METRO_WEEKDAY",
              "Flinders Street", 1, "shape_wrb_in"),
    ]
    stop_times = [
        # Frankston outbound (all share RICHMOND).
        _st("T_FRK_OUT_1", "FLINDERS", 1, "07:00:00"),
        _st("T_FRK_OUT_1", "RICHMOND", 2, "07:07:00"),
        _st("T_FRK_OUT_1", "CAULFIELD", 3, "07:20:00"),
        _st("T_FRK_OUT_1", "FRANKSTON", 4, "08:05:00"),
        _st("T_FRK_OUT_2", "FLINDERS", 1, "08:00:00"),
        _st("T_FRK_OUT_2", "RICHMOND", 2, "08:07:00"),
        _st("T_FRK_OUT_2", "CAULFIELD", 3, "08:20:00"),
        _st("T_FRK_OUT_2", "FRANKSTON", 4, "09:05:00"),
        _st("T_FRK_OUT_3", "FLINDERS", 1, "09:00:00"),
        _st("T_FRK_OUT_3", "RICHMOND", 2, "09:07:00"),
        _st("T_FRK_OUT_3", "CAULFIELD", 3, "09:20:00"),
        _st("T_FRK_OUT_3", "FRANKSTON", 4, "10:05:00"),
        # The post-midnight service: departs 25:10 (01:10 next calendar day).
        _st("T_FRK_LATE", "FLINDERS", 1, "24:55:00"),
        _st("T_FRK_LATE", "RICHMOND", 2, "25:10:00"),
        _st("T_FRK_LATE", "CAULFIELD", 3, "25:25:00"),
        _st("T_FRK_LATE", "FRANKSTON", 4, "26:10:00"),
        # Frankston inbound.
        _st("T_FRK_IN_1", "FRANKSTON", 1, "07:30:00"),
        _st("T_FRK_IN_1", "CAULFIELD", 2, "08:15:00"),
        _st("T_FRK_IN_1", "RICHMOND", 3, "08:28:00"),
        _st("T_FRK_IN_1", "FLINDERS", 4, "08:35:00"),
        # Werribee outbound.
        _st("T_WRB_OUT_1", "FLINDERS", 1, "07:15:00"),
        _st("T_WRB_OUT_1", "RICHMOND", 2, "07:22:00"),
        _st("T_WRB_OUT_1", "NEWPORT", 3, "07:35:00"),
        _st("T_WRB_OUT_1", "ALTONA", 4, "07:42:00"),
        _st("T_WRB_OUT_1", "WERRIBEE", 5, "07:55:00"),
        _st("T_WRB_OUT_2", "FLINDERS", 1, "08:15:00"),
        _st("T_WRB_OUT_2", "RICHMOND", 2, "08:22:00"),
        _st("T_WRB_OUT_2", "NEWPORT", 3, "08:35:00"),
        _st("T_WRB_OUT_2", "ALTONA", 4, "08:42:00"),
        _st("T_WRB_OUT_2", "WERRIBEE", 5, "08:55:00"),
        _st("T_WRB_OUT_3", "FLINDERS", 1, "09:15:00"),
        _st("T_WRB_OUT_3", "RICHMOND", 2, "09:22:00"),
        _st("T_WRB_OUT_3", "NEWPORT", 3, "09:35:00"),
        _st("T_WRB_OUT_3", "ALTONA", 4, "09:42:00"),
        _st("T_WRB_OUT_3", "WERRIBEE", 5, "09:55:00"),
        # Werribee inbound.
        _st("T_WRB_IN_1", "WERRIBEE", 1, "07:05:00"),
        _st("T_WRB_IN_1", "ALTONA", 2, "07:18:00"),
        _st("T_WRB_IN_1", "NEWPORT", 3, "07:25:00"),
        _st("T_WRB_IN_1", "RICHMOND", 4, "07:50:00"),
        _st("T_WRB_IN_1", "FLINDERS", 5, "08:00:00"),
    ]
    shapes: list[dict[str, Any]] = []
    shapes += _shape("shape_frk_out", [(-37.8183, 144.9671), (-38.1435, 145.1230)])
    shapes += _shape("shape_frk_in", [(-38.1435, 145.1230), (-37.8183, 144.9671)])
    shapes += _shape("shape_wrb_out", [(-37.8183, 144.9671), (-37.8990, 144.6600)])
    shapes += _shape("shape_wrb_in", [(-37.8990, 144.6600), (-37.8183, 144.9671)])
    return {
        "routes": routes,
        "trips": trips,
        "stops": stops,
        "stop_times": stop_times,
        "calendar": [_calendar("SVC_METRO_WEEKDAY", REF - timedelta(days=30),
                               REF + timedelta(days=60), saturday=0, sunday=0)],
        "shapes": shapes,
    }


# --- 2b. tram: parent station + platforms, ambiguous loop + clean pattern ---

def tram_spec() -> dict[str, Any]:
    """Route 96 with a station/parent hierarchy and a first==last loop trip."""
    stops = [
        _stop("ST_KILDA", "St Kilda Beach Station", -37.8673, 144.9741,
              location_type=1, platform=""),
        _stop("T96_P1", "St Kilda Beach Station", -37.8673, 144.9741,
              location_type=0, parent="ST_KILDA", platform="1"),
        _stop("T96_P2", "St Kilda Beach Station", -37.8673, 144.9741,
              location_type=0, parent="ST_KILDA", platform="2"),
        _stop("T96_ENTRANCE", "St Kilda Beach Station Entrance", -37.8674, 144.9742,
              location_type=2, parent="ST_KILDA", platform="E"),
        _stop("T96_TERM", "St Kilda Beach", -37.8670, 144.9730),
        _stop("T96_MID", "Carlisle Street", -37.8640, 144.9800),
        _stop("T96_EB", "East Brunswick", -37.7660, 144.9800),
    ]
    routes = [{
        "route_id": "R_TRAM_96", "route_short_name": "96",
        "route_long_name": "East Brunswick - St Kilda Beach",
        "route_type": ROUTE_TYPE_TRAM, "route_color": "78BE20",
    }]
    trips = [
        _trip("T_TRAM_P1", "R_TRAM_96", "SVC_TRAM_WEEKDAY",
              "East Brunswick", 0, "shape_tram_out"),
        _trip("T_TRAM_P2", "R_TRAM_96", "SVC_TRAM_WEEKDAY",
              "East Brunswick", 0, "shape_tram_out"),
        _trip("T_TRAM_CLEAN", "R_TRAM_96", "SVC_TRAM_WEEKDAY",
              "East Brunswick", 0, "shape_tram_out"),
        # First and last stop are identical → ambiguous destination.
        _trip("T_TRAM_LOOP", "R_TRAM_96", "SVC_TRAM_WEEKDAY", "", 0,
              "shape_tram_loop"),
    ]
    stop_times = [
        _st("T_TRAM_P1", "T96_P1", 1, "08:10:00"),
        _st("T_TRAM_P1", "T96_MID", 2, "08:20:00"),
        _st("T_TRAM_P1", "T96_EB", 3, "09:00:00"),
        _st("T_TRAM_P2", "T96_P2", 1, "08:20:00"),
        _st("T_TRAM_P2", "T96_MID", 2, "08:30:00"),
        _st("T_TRAM_P2", "T96_EB", 3, "09:10:00"),
        _st("T_TRAM_CLEAN", "T96_TERM", 1, "09:00:00"),
        _st("T_TRAM_CLEAN", "T96_MID", 2, "09:10:00"),
        _st("T_TRAM_CLEAN", "T96_EB", 3, "09:45:00"),
        _st("T_TRAM_LOOP", "T96_TERM", 1, "10:00:00"),
        _st("T_TRAM_LOOP", "T96_P1", 2, "10:05:00"),
        _st("T_TRAM_LOOP", "T96_MID", 3, "10:10:00"),
        _st("T_TRAM_LOOP", "T96_TERM", 4, "10:40:00"),
    ]
    shapes: list[dict[str, Any]] = []
    shapes += _shape("shape_tram_out", [(-37.8673, 144.9741), (-37.7660, 144.9800)])
    shapes += _shape("shape_tram_loop", [
        (-37.8673, 144.9741), (-37.8640, 144.9800), (-37.8673, 144.9741),
    ])
    return {
        "routes": routes,
        "trips": trips,
        "stops": stops,
        "stop_times": stop_times,
        "calendar": [_calendar("SVC_TRAM_WEEKDAY", REF - timedelta(days=30),
                               REF + timedelta(days=60), saturday=0, sunday=0)],
        "shapes": shapes,
    }


# --- 2c. bus: short-working patterns, duplicate headsigns, night service ----

def bus_spec() -> dict[str, Any]:
    """Route 220 with short workings, duplicate "City" headsigns, night trips."""
    stops = [
        _stop("B_SUNSHINE", "Sunshine", -37.7870, 144.8320),
        _stop("B_BRAYBROOK", "Braybrook", -37.7860, 144.8550),
        _stop("B_KENSINGTON", "Kensington", -37.7940, 144.9200),
        _stop("B_FOOTSCRAY", "Footscray", -37.8010, 144.9000),
        _stop("B_CITY", "City", -37.8180, 144.9670),
    ]
    routes = [{
        "route_id": "R_BUS_220", "route_short_name": "220",
        "route_long_name": "Sunshine - City", "route_type": ROUTE_TYPE_BUS,
        "route_color": "FF8200",
    }]
    trips = [
        _trip("T_BUS_LONG", "R_BUS_220", "SVC_BUS_WEEKDAY", "City", 0,
              "shape_bus_via_footscray"),
        _trip("T_BUS_SHORT", "R_BUS_220", "SVC_BUS_WEEKDAY", "Footscray", 0,
              "shape_bus_short_footscray"),
        _trip("T_BUS_SHORT2", "R_BUS_220", "SVC_BUS_WEEKDAY", "Braybrook", 0,
              "shape_bus_short_braybrook"),
        _trip("T_BUS_VIA_A", "R_BUS_220", "SVC_BUS_WEEKDAY", "City", 0,
              "shape_bus_via_footscray"),
        _trip("T_BUS_VIA_B", "R_BUS_220", "SVC_BUS_WEEKDAY", "City", 0,
              "shape_bus_via_kensington"),
        _trip("T_BUS_IN", "R_BUS_220", "SVC_BUS_WEEKDAY", "Sunshine", 1,
              "shape_bus_in"),
        _trip("T_NIGHT_0030", "R_BUS_220", "SVC_BUS_NIGHT", "Sunshine", 1),
        _trip("T_NIGHT_0345", "R_BUS_220", "SVC_BUS_NIGHT", "Sunshine", 1),
        _trip("T_NIGHT_0400", "R_BUS_220", "SVC_BUS_NIGHT", "Sunshine", 1),
    ]
    stop_times = [
        _st("T_BUS_LONG", "B_SUNSHINE", 1, "08:00:00"),
        _st("T_BUS_LONG", "B_BRAYBROOK", 2, "08:10:00"),
        _st("T_BUS_LONG", "B_FOOTSCRAY", 3, "08:25:00"),
        _st("T_BUS_LONG", "B_CITY", 4, "08:50:00"),
        # Short working #1: subset terminating at Footscray.
        _st("T_BUS_SHORT", "B_SUNSHINE", 1, "08:30:00"),
        _st("T_BUS_SHORT", "B_BRAYBROOK", 2, "08:40:00"),
        _st("T_BUS_SHORT", "B_FOOTSCRAY", 3, "08:55:00"),
        # Short working #2: even shorter, terminating at Braybrook.
        _st("T_BUS_SHORT2", "B_SUNSHINE", 1, "08:45:00"),
        _st("T_BUS_SHORT2", "B_BRAYBROOK", 2, "08:55:00"),
        # Duplicate "City" headsign via Footscray ...
        _st("T_BUS_VIA_A", "B_SUNSHINE", 1, "09:00:00"),
        _st("T_BUS_VIA_A", "B_BRAYBROOK", 2, "09:10:00"),
        _st("T_BUS_VIA_A", "B_FOOTSCRAY", 3, "09:25:00"),
        _st("T_BUS_VIA_A", "B_CITY", 4, "09:50:00"),
        # ... and via Kensington (same headsign, different intermediate stop).
        _st("T_BUS_VIA_B", "B_SUNSHINE", 1, "09:15:00"),
        _st("T_BUS_VIA_B", "B_KENSINGTON", 2, "09:30:00"),
        _st("T_BUS_VIA_B", "B_CITY", 3, "09:45:00"),
        # Inbound ends at Sunshine (arrival only, so it is not a "departure").
        _st("T_BUS_IN", "B_CITY", 1, "07:30:00"),
        _st("T_BUS_IN", "B_FOOTSCRAY", 2, "07:50:00"),
        _st("T_BUS_IN", "B_BRAYBROOK", 3, "08:05:00"),
        _st("T_BUS_IN", "B_SUNSHINE", 4, "", arrival="08:20:00"),
        # Night service: midnight-clock times on a daily calendar row.
        _st("T_NIGHT_0030", "B_CITY", 1, "00:30:00"),
        _st("T_NIGHT_0030", "B_FOOTSCRAY", 2, "00:45:00"),
        _st("T_NIGHT_0030", "B_SUNSHINE", 3, "01:05:00"),
        _st("T_NIGHT_0345", "B_CITY", 1, "03:45:00"),
        _st("T_NIGHT_0345", "B_FOOTSCRAY", 2, "03:55:00"),
        _st("T_NIGHT_0345", "B_SUNSHINE", 3, "04:15:00"),
        _st("T_NIGHT_0400", "B_CITY", 1, "04:00:00"),
        _st("T_NIGHT_0400", "B_FOOTSCRAY", 2, "04:10:00"),
        _st("T_NIGHT_0400", "B_SUNSHINE", 3, "04:30:00"),
    ]
    shapes: list[dict[str, Any]] = []
    shapes += _shape("shape_bus_via_footscray", [
        (-37.7870, 144.8320), (-37.8010, 144.9000), (-37.8180, 144.9670),
    ])
    shapes += _shape("shape_bus_via_kensington", [
        (-37.7870, 144.8320), (-37.7940, 144.9200), (-37.8180, 144.9670),
    ])
    shapes += _shape("shape_bus_short_footscray", [
        (-37.7870, 144.8320), (-37.8010, 144.9000),
    ])
    shapes += _shape("shape_bus_short_braybrook", [
        (-37.7870, 144.8320), (-37.7860, 144.8550),
    ])
    shapes += _shape("shape_bus_in", [
        (-37.8180, 144.9670), (-37.7870, 144.8320),
    ])
    return {
        "routes": routes,
        "trips": trips,
        "stops": stops,
        "stop_times": stop_times,
        "calendar": [
            _calendar("SVC_BUS_WEEKDAY", REF - timedelta(days=30),
                      REF + timedelta(days=30), saturday=0, sunday=0),
            # Daily calendar row: the night trips have midnight-clock times.
            _calendar("SVC_BUS_NIGHT", REF - timedelta(days=30),
                      REF + timedelta(days=30)),
        ],
        "shapes": shapes,
    }


# --- 2d. V/Line: one long route, two directions ----------------------------

def vline_spec() -> dict[str, Any]:
    stops = [
        _stop("V_SC", "Southern Cross", -37.8183, 144.9520),
        _stop("V_FOOTSCRAY", "Footscray", -37.8010, 144.9000),
        _stop("V_WERRIBEE", "Werribee", -37.8990, 144.6600),
        _stop("V_GEELONG", "Geelong", -38.1460, 144.3540),
    ]
    routes = [{
        "route_id": "R_VLINE_GEELONG", "route_short_name": "Geelong",
        "route_long_name": "Geelong Line", "route_type": ROUTE_TYPE_VLINE,
        "route_color": "8B5CF6",
    }]
    trips = [
        _trip("V_OUT_0700", "R_VLINE_GEELONG", "SVC_VLINE_DAILY", "Geelong", 0),
        _trip("V_OUT_1200", "R_VLINE_GEELONG", "SVC_VLINE_DAILY", "Geelong", 0),
        _trip("V_OUT_1700", "R_VLINE_GEELONG", "SVC_VLINE_DAILY", "Geelong", 0),
        _trip("V_IN_0630", "R_VLINE_GEELONG", "SVC_VLINE_DAILY",
              "Southern Cross", 1),
    ]
    stop_times = [
        _st("V_OUT_0700", "V_SC", 1, "07:00:00"),
        _st("V_OUT_0700", "V_FOOTSCRAY", 2, "07:07:00"),
        _st("V_OUT_0700", "V_WERRIBEE", 3, "07:25:00"),
        _st("V_OUT_0700", "V_GEELONG", 4, "08:05:00"),
        _st("V_OUT_1200", "V_SC", 1, "12:00:00"),
        _st("V_OUT_1200", "V_FOOTSCRAY", 2, "12:07:00"),
        _st("V_OUT_1200", "V_WERRIBEE", 3, "12:25:00"),
        _st("V_OUT_1200", "V_GEELONG", 4, "13:05:00"),
        _st("V_OUT_1700", "V_SC", 1, "17:00:00"),
        _st("V_OUT_1700", "V_FOOTSCRAY", 2, "17:07:00"),
        _st("V_OUT_1700", "V_WERRIBEE", 3, "17:25:00"),
        _st("V_OUT_1700", "V_GEELONG", 4, "18:05:00"),
        _st("V_IN_0630", "V_GEELONG", 1, "06:30:00"),
        _st("V_IN_0630", "V_WERRIBEE", 2, "07:10:00"),
        _st("V_IN_0630", "V_FOOTSCRAY", 3, "07:28:00"),
        _st("V_IN_0630", "V_SC", 4, "07:35:00"),
    ]
    return {
        "routes": routes,
        "trips": trips,
        "stops": stops,
        "stop_times": stop_times,
        "calendar": [_calendar("SVC_VLINE_DAILY", REF - timedelta(days=30),
                               REF + timedelta(days=60))],
    }


# --- 2e. calendar-expiry scenarios -----------------------------------------

def _minimal_spec(
    *,
    service_id: str,
    calendar: list[dict[str, Any]],
    calendar_dates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "routes": [{
            "route_id": f"R_{service_id}", "route_short_name": service_id,
            "route_long_name": f"{service_id} Line",
            "route_type": ROUTE_TYPE_TRAIN,
        }],
        "trips": [_trip(f"T_{service_id}", f"R_{service_id}", service_id,
                        "Expiry", 0)],
        "stops": [
            _stop("EXP_A", "Expiry Origin", -37.80, 144.90),
            _stop("EXP_B", "Expiry Terminus", -37.90, 145.00),
        ],
        "stop_times": [
            _st(f"T_{service_id}", "EXP_A", 1, "08:00:00"),
            _st(f"T_{service_id}", "EXP_B", 2, "08:20:00"),
        ],
        "calendar": calendar,
        "calendar_dates": calendar_dates,
    }


def expired_calendar_spec() -> dict[str, Any]:
    """Schedule valid at import (REF) but expired by ``EXPIRY_EVAL_TODAY``."""
    return _minimal_spec(
        service_id="SVC_EXPIRED",
        calendar=[_calendar("SVC_EXPIRED", REF - timedelta(days=30),
                            REF + timedelta(days=9))],
        calendar_dates=[],
    )


def dates_only_extension_spec() -> dict[str, Any]:
    """calendar.txt ends tomorrow; calendar_dates add service past the margin.

    Imported at REF (horizon REF+1 is still future), evaluated at
    ``EXTENSION_EVAL_TODAY`` where only the exception additions carry the
    horizon — schedule_state must stay "ready" because the horizon uses the
    max addition date, not the (stale) calendar end_date.
    """
    return _minimal_spec(
        service_id="SVC_EXTENDED",
        calendar=[_calendar("SVC_EXTENDED", REF - timedelta(days=30),
                            REF + timedelta(days=1))],
        calendar_dates=[
            {"service_id": "SVC_EXTENDED",
             "date": (REF + timedelta(days=20)).strftime("%Y%m%d"),
             "exception_type": 1},
            {"service_id": "SVC_EXTENDED",
             "date": (REF + timedelta(days=44)).strftime("%Y%m%d"),
             "exception_type": 1},
        ],
    )


def removed_today_spec() -> dict[str, Any]:
    """A removal exception (type 2) subtracts the pinned service day."""
    return _minimal_spec(
        service_id="SVC_REMOVED",
        calendar=[_calendar("SVC_REMOVED", REF - timedelta(days=30),
                            REF + timedelta(days=60))],
        calendar_dates=[
            {"service_id": "SVC_REMOVED", "date": REF.strftime("%Y%m%d"),
             "exception_type": 2},
        ],
    )


def no_service_spec() -> dict[str, Any]:
    """Removals only: no calendar rows and no additions → no servable date.

    The importer must refuse this bundle (per-bundle horizon is None) rather
    than publish an empty schedule.
    """
    return _minimal_spec(
        service_id="SVC_NONE",
        calendar=[],
        calendar_dates=[
            {"service_id": "SVC_NONE", "date": REF.strftime("%Y%m%d"),
             "exception_type": 2},
        ],
    )


# --- 2f. two bundles → one generation (provenance + pointer) --------------

def two_bundle_specs() -> dict[str, dict[str, Any]]:
    """folders "2" and "5" with overlapping calendar ranges, distinct horizons."""
    def _bundle(route_id, route_type, short, horizon_days):
        return {
            "routes": [{
                "route_id": route_id, "route_short_name": short,
                "route_long_name": f"{short} Board", "route_type": route_type,
            }],
            "trips": [_trip(f"T_{route_id}", route_id, f"SVC_{route_id}",
                            short, 0)],
            "stops": [
                _stop(f"{route_id}_A", f"{short} Origin", -37.80, 144.90),
                _stop(f"{route_id}_B", f"{short} Terminus", -37.90, 145.00),
            ],
            "stop_times": [
                _st(f"T_{route_id}", f"{route_id}_A", 1, "08:00:00"),
                _st(f"T_{route_id}", f"{route_id}_B", 2, "08:20:00"),
            ],
            "calendar": [_calendar(
                f"SVC_{route_id}", REF - timedelta(days=20),
                REF + timedelta(days=horizon_days),
            )],
        }

    return {
        "2": _bundle("R_BOARD_TRAIN", ROUTE_TYPE_TRAIN, "Train",
                     horizon_days=30),
        "5": _bundle("R_BOARD_TRAM", ROUTE_TYPE_TRAM, "Tram",
                     horizon_days=5),
    }
