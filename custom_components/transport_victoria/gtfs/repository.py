"""GTFS schedule repository.

All database access for departure queries and stop searches lives here.
Every method is a plain synchronous function — callers must wrap them in
hass.async_add_executor_job() because SQLite access blocks the event loop.

A new sqlite3.Connection is created per call. This avoids the
"SQLite objects created in a thread can only be used in that same thread"
error that occurs when sharing a connection across the HA executor pool.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..const import GTFS_SCHEMA_VERSION
from . import store
from .horizon import (
    compute_horizon,
    compute_horizon_for_bundle,
    get_schema_version,
    imported_bundles,
)
from .time_utils import (
    day_of_week_column,
    departure_seconds_to_datetime,
    service_date_str,
    service_day_for_time,
)

_LOGGER = logging.getLogger(__name__)


def _resolve(db_path: Path) -> Path:
    """Resolve a logical DB path (anchor) to the current generation file."""
    return store.resolve_db(db_path)


@dataclass(slots=True)
class DepartureRow:
    """A single scheduled departure, before realtime data is merged in."""

    trip_id: str
    route_id: str
    route_short_name: str
    route_long_name: str
    trip_headsign: str
    direction_id: int | None
    stop_sequence: int
    departure_seconds: int
    scheduled_departure: datetime  # absolute local datetime
    actual_stop_id: str            # specific platform stop_id that matched (for RT key)
    platform_code: str | None      # platform/track label from stops table
    route_color: str | None        # hex colour string from routes table (no '#')


@dataclass(slots=True)
class StopResult:
    """A stop returned from a name or distance search."""

    stop_id: str
    stop_name: str
    stop_lat: float | None
    stop_lon: float | None
    location_type: int
    parent_station: str | None
    distance_m: float | None = None  # only set by search_stops_by_distance


# ---------------------------------------------------------------------------
# Active services query (shared between both departure query branches)
# ---------------------------------------------------------------------------

_ACTIVE_SERVICES_SQL = """
    SELECT service_id FROM calendar
    WHERE start_date <= :service_date
      AND end_date   >= :service_date
      AND {dow_col}  = 1
    UNION
    SELECT service_id FROM calendar_dates
    WHERE date = :service_date AND exception_type = 1
    EXCEPT
    SELECT service_id FROM calendar_dates
    WHERE date = :service_date AND exception_type = 2
"""

# ---------------------------------------------------------------------------
# Departure query
# ---------------------------------------------------------------------------

_DEPARTURE_SQL = """
    SELECT
        st.trip_id,
        t.route_id,
        r.route_short_name,
        r.route_long_name,
        t.trip_headsign,
        t.direction_id,
        st.stop_sequence,
        st.departure_seconds,
        st.stop_id          AS actual_stop_id,
        s.platform_code,
        r.route_color
    FROM stop_times st
    JOIN trips t ON t.trip_id = st.trip_id
    JOIN routes r ON r.route_id = t.route_id
    JOIN stops s ON s.stop_id = st.stop_id
    WHERE st.stop_id IN (
        -- The chosen stop plus all sibling platforms sharing the same parent station,
        -- so selecting any one platform still returns all departures from that station.
        SELECT stop_id FROM stops
        WHERE stop_id = :stop_id
        UNION
        SELECT s2.stop_id FROM stops s2
        JOIN stops s1 ON s1.stop_id = :stop_id
        WHERE s2.parent_station = s1.parent_station
          AND s1.parent_station IS NOT NULL
          AND (s2.location_type = 0 OR s2.location_type IS NULL)
    )
      AND st.departure_seconds >= :now_seconds
      AND t.service_id IN ({active_services_sql}){extra_clauses}{direction_clause}
    ORDER BY st.departure_seconds
    LIMIT :limit
"""

# Destination-eligibility clause (T9): keep only trips that call at the
# requested destination stop at a LATER stop_sequence than this boarding
# occurrence.  Pushed into the SQL so LIMIT/pagination applies AFTER eligibility
# (a destination-filtered pin whose first N route rows don't reach the
# destination no longer produces an empty page that hides later valid rows).
_DESTINATION_CLAUSE_SQL = """
      AND EXISTS (
          SELECT 1 FROM stop_times sd
          WHERE sd.trip_id = st.trip_id
            AND sd.stop_id = :direction_stop_id
            AND sd.stop_sequence > st.stop_sequence
      )
"""


def next_departures(
    db_path: Path,
    stop_id: str,
    now: datetime,
    limit: int = 5,
    direction_stop_id: str | None = None,
    route_id_filter: str | None = None,
    platform_filter: str | None = None,
    lookback_seconds: int = 0,
    offset: int = 0,
    resolved_db: Path | None = None,
    trips_direction_filter: int | None = None,
) -> list[DepartureRow]:
    """Return the next scheduled departures from a stop.

    Handles post-midnight trips by running two query branches:
    - Branch A: today's service date, departure_seconds >= now_seconds
    - Branch B: yesterday's service date, departure_seconds >= 86400 + now_seconds
      (catches late-night trips published under yesterday's calendar)

    Both branches are merged and sorted by the ABSOLUTE scheduled datetime
    (each row carries its own service date, so a 25:10 post-midnight row and a
    10:00 same-day row order by real wall-clock time, not their raw
    service-day offsets), then the first ``limit`` rows after ``offset`` are
    returned.  ``offset`` enables pagination so a caller can walk the full
    window without a small ``limit`` hiding later rows behind many earlier ones.

    Destination eligibility (``direction_stop_id``) is enforced IN SQL (an
    ``EXISTS`` on ``stop_times`` for the destination at a later ``stop_sequence``
    than this boarding occurrence), so ``LIMIT``/pagination apply AFTER the
    filter: a destination-filtered pin whose first page of raw route rows do not
    reach the destination still pages forward correctly.

    A *direction* filter (``trips_direction_filter``) is distinct and orthogonal:
    it binds ``trips.direction_id = :trips_direction_filter`` directly, so a
    pin scoped to a direction returns ONLY that direction's trips.  It is used by
    the board's Opposite-direction mode (T13), where the destination filter is
    intentionally disabled but the flipped ``direction_id`` must still select one
    direction rather than returning every service at the boarding stop.

    Args:
        db_path: Path to the SQLite database (the logical anchor).
        stop_id: The GTFS stop_id to query.
        now: Current local datetime (naive or aware).
        limit: Maximum number of departures to return.
        direction_stop_id: If set, only return trips that also serve this stop
            after the origin (used for specific-destination filtering).
        route_id_filter: If set, only return departures for this route_id.
        platform_filter: If set, only return departures from this platform_code.
        lookback_seconds: Lower the departure_seconds bound by this many seconds
            (0 = no lookback).  Lets a board also fetch trips whose *scheduled*
            departure is in the recent past but which a realtime delay has
            pushed into the future.  The service-date anchor is unchanged, so
            the 00:00–03:00 cutover is still resolved against ``now``.
        offset: Skip this many rows (by absolute scheduled time) before
            returning ``limit`` rows.
        resolved_db: A pre-resolved generation file to query (bypasses pointer
            resolution).  The board aggregator pins one snapshot for a whole
            pagination sequence via this parameter.
        trips_direction_filter: If set, only return trips whose ``direction_id``
            equals this value (None = no direction filter).

    Returns:
        List of DepartureRow, sorted by absolute scheduled departure time,
        length <= limit.
    """
    service_date, now_seconds = service_day_for_time(now)
    lower_bound = now_seconds - lookback_seconds
    fetch_limit = offset + limit

    resolved = resolved_db if resolved_db is not None else _resolve(db_path)

    rows_a = _query_branch(
        resolved, stop_id, service_date, lower_bound, fetch_limit,
        route_id_filter=route_id_filter, platform_filter=platform_filter,
        direction_stop_id=direction_stop_id,
        trips_direction_filter=trips_direction_filter,
    )

    # Second branch for post-midnight: query yesterday, offset now_seconds
    yesterday = date.fromordinal(service_date.toordinal() - 1)
    rows_b = _query_branch(
        resolved, stop_id, yesterday, lower_bound + 86400, fetch_limit,
        route_id_filter=route_id_filter, platform_filter=platform_filter,
        direction_stop_id=direction_stop_id,
        trips_direction_filter=trips_direction_filter,
    )

    all_rows = sorted(rows_a + rows_b, key=lambda r: r.scheduled_departure)

    return all_rows[offset:offset + limit]


def _query_branch(
    db_path: Path,
    stop_id: str,
    service_date: date,
    now_seconds: int,
    limit: int,
    route_id_filter: str | None = None,
    platform_filter: str | None = None,
    direction_stop_id: str | None = None,
    trips_direction_filter: int | None = None,
) -> list[DepartureRow]:
    """Run one branch of the departure query for a single service date.

    ``db_path`` is the RESOLVED generation file (not the anchor).
    """
    dow_col = day_of_week_column(service_date)
    service_date_s = service_date_str(service_date)

    active_services_sql = _ACTIVE_SERVICES_SQL.format(dow_col=dow_col)

    # Build optional filter clauses
    extra_clauses = ""
    direction_clause = ""
    if route_id_filter:
        extra_clauses += "\n      AND t.route_id = :route_id_filter"
    if platform_filter:
        extra_clauses += "\n      AND s.platform_code = :platform_filter"
    if direction_stop_id:
        direction_clause = _DESTINATION_CLAUSE_SQL
    if trips_direction_filter is not None:
        extra_clauses += "\n      AND t.direction_id = :trips_direction_filter"

    sql = _DEPARTURE_SQL.format(
        active_services_sql=active_services_sql,
        extra_clauses=extra_clauses,
        direction_clause=direction_clause,
    )

    params: dict[str, object] = {
        "stop_id": stop_id,
        "now_seconds": now_seconds,
        "service_date": service_date_s,
        "limit": limit,
    }
    if route_id_filter:
        params["route_id_filter"] = route_id_filter
    if platform_filter:
        params["platform_filter"] = platform_filter
    if direction_stop_id:
        params["direction_stop_id"] = direction_stop_id
    if trips_direction_filter is not None:
        params["trips_direction_filter"] = trips_direction_filter

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(sql, params)
        rows = []
        for row in cursor.fetchall():
            dep_secs = row["departure_seconds"]
            rows.append(
                DepartureRow(
                    trip_id=row["trip_id"],
                    route_id=row["route_id"],
                    route_short_name=row["route_short_name"] or "",
                    route_long_name=row["route_long_name"] or "",
                    trip_headsign=row["trip_headsign"] or "",
                    direction_id=row["direction_id"],
                    stop_sequence=row["stop_sequence"],
                    departure_seconds=dep_secs,
                    scheduled_departure=departure_seconds_to_datetime(
                        service_date, dep_secs
                    ),
                    actual_stop_id=row["actual_stop_id"],
                    platform_code=row["platform_code"] or None,
                    route_color=row["route_color"] or None,
                )
            )
        return rows
    except sqlite3.Error as exc:
        _LOGGER.error("Departure query failed: %s", exc)
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stop search
# ---------------------------------------------------------------------------

def search_stops_by_name(
    db_path: Path,
    query: str,
    route_types: list[int],
    limit: int = 10,
) -> list[StopResult]:
    """Search for stops by name, filtered to stops served by the given route types.

    Only returns platform-level stops (location_type = 0 or NULL) so we don't
    surface station buildings as selectable options. Stops with the same name
    (e.g. multiple platforms of the same station) are collapsed into one result
    using the lowest stop_id as the representative.

    Args:
        db_path: Path to the SQLite database.
        query: Partial stop name to search (case-insensitive, prefix match).
        route_types: List of GTFS route_type ints to filter by.
        limit: Maximum results to return.

    Returns:
        List of StopResult sorted by name, deduplicated by name.
    """
    if not route_types:
        return []

    rt_placeholders = ", ".join("?" * len(route_types))
    sql = f"""
        SELECT MIN(s.stop_id) AS stop_id, s.stop_name,
               MIN(s.stop_lat) AS stop_lat, MIN(s.stop_lon) AS stop_lon,
               MIN(s.location_type) AS location_type,
               MIN(s.parent_station) AS parent_station
        FROM stops s
        JOIN stop_times st ON st.stop_id = s.stop_id
        JOIN trips t ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE s.stop_name LIKE ? ESCAPE '\\'
          AND (s.location_type = 0 OR s.location_type IS NULL)
          AND r.route_type IN ({rt_placeholders})
        GROUP BY lower(s.stop_name)
        ORDER BY s.stop_name COLLATE NOCASE
        LIMIT ?
    """

    like_query = _escape_like(query) + "%"
    params = [like_query] + route_types + [limit]

    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        conn.row_factory = sqlite3.Row
        results = []
        for row in conn.execute(sql, params):
            results.append(
                StopResult(
                    stop_id=row["stop_id"],
                    stop_name=row["stop_name"],
                    stop_lat=row["stop_lat"],
                    stop_lon=row["stop_lon"],
                    location_type=row["location_type"],
                    parent_station=row["parent_station"],
                )
            )
        return results
    except sqlite3.Error as exc:
        _LOGGER.error("Stop name search failed: %s", exc)
        return []
    finally:
        conn.close()


def search_stops_by_distance(
    db_path: Path,
    lat: float,
    lon: float,
    route_types: list[int],
    radius_m: float = 2000.0,
    limit: int = 10,
) -> list[StopResult]:
    """Find stops near a lat/lon point, sorted by distance.

    Uses a bounding-box pre-filter in SQL (fast), then computes haversine
    distance in Python to get accurate ordering and radius filtering.

    Args:
        db_path: Path to the SQLite database.
        lat: WGS84 latitude of the reference point (e.g. HA home zone).
        lon: WGS84 longitude.
        route_types: List of GTFS route_type ints to filter by.
        radius_m: Maximum distance in metres.
        limit: Maximum results to return.

    Returns:
        List of StopResult sorted by distance_m ascending.
    """
    import math

    if not route_types:
        return []

    # Degrees of latitude / longitude per metre (approximate)
    deg_per_m_lat = 1.0 / 111_320.0
    deg_per_m_lon = 1.0 / (111_320.0 * math.cos(math.radians(lat)))

    lat_margin = radius_m * deg_per_m_lat
    lon_margin = radius_m * deg_per_m_lon

    rt_placeholders = ", ".join("?" * len(route_types))
    sql = f"""
        SELECT MIN(s.stop_id) AS stop_id, s.stop_name,
               MIN(s.stop_lat) AS stop_lat, MIN(s.stop_lon) AS stop_lon,
               MIN(s.location_type) AS location_type,
               MIN(s.parent_station) AS parent_station
        FROM stops s
        JOIN stop_times st ON st.stop_id = s.stop_id
        JOIN trips t ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE (s.location_type = 0 OR s.location_type IS NULL)
          AND s.stop_lat BETWEEN ? AND ?
          AND s.stop_lon BETWEEN ? AND ?
          AND r.route_type IN ({rt_placeholders})
        GROUP BY lower(s.stop_name)
    """

    params = [
        lat - lat_margin, lat + lat_margin,
        lon - lon_margin, lon + lon_margin,
    ] + route_types

    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        conn.row_factory = sqlite3.Row
        candidates = []
        for row in conn.execute(sql, params):
            slat, slon = row["stop_lat"], row["stop_lon"]
            if slat is None or slon is None:
                continue
            dist = _haversine_m(lat, lon, slat, slon)
            if dist <= radius_m:
                candidates.append(
                    StopResult(
                        stop_id=row["stop_id"],
                        stop_name=row["stop_name"],
                        stop_lat=slat,
                        stop_lon=slon,
                        location_type=row["location_type"],
                        parent_station=row["parent_station"],
                        distance_m=dist,
                    )
                )

        candidates.sort(key=lambda r: r.distance_m or 0)
        return candidates[:limit]
    except sqlite3.Error as exc:
        _LOGGER.error("Stop distance search failed: %s", exc)
        return []
    finally:
        conn.close()


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two WGS84 points."""
    import math

    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _escape_like(value: str) -> str:
    """Escape special characters in a LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(slots=True)
class RouteResult:
    """A route served by a stop, or from a mode-wide route search."""

    route_id: str
    route_short_name: str
    route_long_name: str
    route_color: str | None = None  # hex colour string from routes table (no '#')
    route_type: int | None = None   # GTFS route_type (for bundle mapping, T8)


def get_routes_for_stop(
    db_path: Path,
    stop_id: str,
    route_types: list[int],
) -> list[RouteResult]:
    """Return distinct routes served by a stop and its sibling platforms.

    Used in the config flow to offer a line/route filter after stop selection.
    """
    if not route_types:
        return []

    rt_placeholders = ", ".join("?" * len(route_types))
    sql = f"""
        SELECT DISTINCT r.route_id, r.route_short_name, r.route_long_name, r.route_color,
               r.route_type
        FROM stop_times st
        JOIN trips t ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE st.stop_id IN (
            SELECT stop_id FROM stops WHERE stop_id = ?
            UNION
            SELECT s2.stop_id FROM stops s2
            JOIN stops s1 ON s1.stop_id = ?
            WHERE s2.parent_station = s1.parent_station
              AND s1.parent_station IS NOT NULL
              AND (s2.location_type = 0 OR s2.location_type IS NULL)
        )
          AND r.route_type IN ({rt_placeholders})
        ORDER BY r.route_short_name COLLATE NOCASE
    """
    params: list[object] = [stop_id, stop_id] + list(route_types)

    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        conn.row_factory = sqlite3.Row
        results = []
        for row in conn.execute(sql, params):
            results.append(
                RouteResult(
                    route_id=row["route_id"],
                    route_short_name=row["route_short_name"] or "",
                    route_long_name=row["route_long_name"] or "",
                    route_color=row["route_color"] or None,
                    route_type=row["route_type"],
                )
            )
        return results
    except sqlite3.Error as exc:
        _LOGGER.error("get_routes_for_stop failed: %s", exc)
        return []
    finally:
        conn.close()


def get_platforms_for_stop(db_path: Path, stop_id: str) -> list[str]:
    """Return distinct non-null platform_codes for a stop and its siblings."""
    sql = """
        SELECT DISTINCT s.platform_code
        FROM stops s
        WHERE s.platform_code IS NOT NULL
          AND s.stop_id IN (
              SELECT stop_id FROM stops WHERE stop_id = ?
              UNION
              SELECT s2.stop_id FROM stops s2
              JOIN stops s1 ON s1.stop_id = ?
              WHERE s2.parent_station = s1.parent_station
                AND s1.parent_station IS NOT NULL
                AND (s2.location_type = 0 OR s2.location_type IS NULL)
          )
        ORDER BY s.platform_code
    """
    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        platforms = []
        for row in conn.execute(sql, [stop_id, stop_id]):
            platforms.append(str(row[0]))
        return platforms
    except sqlite3.Error as exc:
        _LOGGER.error("get_platforms_for_stop failed: %s", exc)
        return []
    finally:
        conn.close()


def get_service_type_baselines(
    db_path: Path,
    trip_ids: list[str],
) -> dict[str, tuple[int, int]]:
    """Return {trip_id: (trip_stop_count, baseline_max)} for service-type classification.

    The baseline is the maximum stop count among all trips that share the same
    (route_id, direction_id, terminal_stop_id) group.  This prevents short-working
    trips (e.g. a Coburg-terminating train) from being incorrectly classified as
    express simply because a longer Craigieburn service exists on the same route.
    """
    if not trip_ids:
        return {}
    tp = ", ".join("?" * len(trip_ids))
    sql = f"""
        WITH
        -- Route/direction for each target trip
        target AS (
            SELECT trip_id, route_id, direction_id
            FROM trips
            WHERE trip_id IN ({tp})
        ),
        -- Stop count and last sequence number for each target trip
        target_stats AS (
            SELECT trip_id, COUNT(*) AS stop_count, MAX(stop_sequence) AS max_seq
            FROM stop_times
            WHERE trip_id IN ({tp})
            GROUP BY trip_id
        ),
        -- Terminal stop (last stop in sequence) for each target trip
        target_terminal AS (
            SELECT st.trip_id, st.stop_id AS terminal_stop_id
            FROM stop_times st
            JOIN target_stats ts ON ts.trip_id = st.trip_id
                                 AND st.stop_sequence = ts.max_seq
        ),
        -- All trips on the same routes (to build group baselines)
        route_trips AS (
            SELECT t.trip_id, t.route_id, t.direction_id
            FROM trips t
            WHERE t.route_id IN (SELECT DISTINCT route_id FROM target)
        ),
        -- Stop count + terminal for every trip on those routes
        route_trip_stats AS (
            SELECT trip_id, COUNT(*) AS stop_count, MAX(stop_sequence) AS max_seq
            FROM stop_times
            WHERE trip_id IN (SELECT trip_id FROM route_trips)
            GROUP BY trip_id
        ),
        route_trip_terminal AS (
            SELECT st.trip_id, st.stop_id AS terminal_stop_id
            FROM stop_times st
            JOIN route_trip_stats rts ON rts.trip_id = st.trip_id
                                      AND st.stop_sequence = rts.max_seq
        ),
        -- Maximum stop count per (route, direction, terminal) group
        group_baseline AS (
            SELECT rt.route_id, rt.direction_id, rtt.terminal_stop_id,
                   MAX(rts.stop_count) AS max_count
            FROM route_trips rt
            JOIN route_trip_stats rts ON rts.trip_id = rt.trip_id
            JOIN route_trip_terminal rtt ON rtt.trip_id = rt.trip_id
            GROUP BY rt.route_id, rt.direction_id, rtt.terminal_stop_id
        )
        SELECT tgt.trip_id, ts.stop_count, gb.max_count
        FROM target tgt
        JOIN target_stats ts ON ts.trip_id = tgt.trip_id
        JOIN target_terminal tt ON tt.trip_id = tgt.trip_id
        JOIN group_baseline gb ON gb.route_id = tgt.route_id
                               AND gb.direction_id = tgt.direction_id
                               AND gb.terminal_stop_id = tt.terminal_stop_id
    """
    params = trip_ids + trip_ids
    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        result: dict[str, tuple[int, int]] = {}
        for row in conn.execute(sql, params):
            result[row[0]] = (row[1], row[2])
        return result
    except sqlite3.Error as exc:
        _LOGGER.error("get_service_type_baselines failed: %s", exc)
        return {}
    finally:
        conn.close()


def get_arrival_at_stop(
    db_path: Path,
    trip_id: str,
    stop_id: str,
    service_date: date,
) -> datetime | None:
    """Return the scheduled arrival datetime at a stop for a given trip.

    Used to compute transfer windows in journey mode.
    """
    sql = """
        SELECT arrival_seconds FROM stop_times
        WHERE trip_id = ? AND stop_id = ?
        LIMIT 1
    """
    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        row = conn.execute(sql, [trip_id, stop_id]).fetchone()
        if row is None:
            return None
        return departure_seconds_to_datetime(service_date, row[0])
    except sqlite3.Error as exc:
        _LOGGER.error("get_arrival_at_stop failed: %s", exc)
        return None
    finally:
        conn.close()


def is_db_ready(db_path: Path) -> bool:
    """Return True if a generation exists and has data in the stops table."""
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return False
    try:
        conn = sqlite3.connect(str(resolved))
        try:
            row = conn.execute("SELECT COUNT(*) FROM stops").fetchone()
            return (row[0] or 0) > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def is_mode_ready(db_path: Path, bundle_folders: list[str]) -> bool:
    """Return True if all requested bundle folders have been imported."""
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return False
    try:
        conn = sqlite3.connect(str(resolved))
        try:
            placeholders = ", ".join("?" * len(bundle_folders))
            keys = [f"bundle_{f}" for f in bundle_folders]
            row = conn.execute(
                f"SELECT COUNT(*) FROM import_meta WHERE key IN ({placeholders})",
                keys,
            ).fetchone()
            return (row[0] or 0) >= len(bundle_folders)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def schedule_state(
    db_path: Path,
    bundle_folders: list[str] | None = None,
    today: date | None = None,
) -> str:
    """Return a coarse schedule-health state for the coordinator.

    One of (worst-first):
      * "missing" — no generation, a required bundle not imported, or no
        calendar coverage at all;
      * "stale"   — generation present but below the required schema version;
      * "expired" — a bundle (or the whole schedule, when no bundles given)
        has data but its horizon is in the past;
      * "ready"   — usable.

    When ``bundle_folders`` is given, each bundle is evaluated independently
    (horizon scoped to that bundle's route_types, plus a missing-bundle check)
    and the WORST state wins, so a healthy bundle cannot mask an expired one.
    """
    today = today or date.today()
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return "missing"
    if get_schema_version(db_path) < GTFS_SCHEMA_VERSION:
        return "stale"

    if bundle_folders:
        present = imported_bundles(db_path)
        for folder in bundle_folders:
            if folder not in present:
                return "missing"
            horizon = compute_horizon_for_bundle(db_path, folder)
            if horizon is None:
                return "missing"
            if horizon < today:
                return "expired"
        return "ready"

    horizon = compute_horizon(db_path)
    if horizon is None:
        return "missing"
    if horizon < today:
        return "expired"
    return "ready"


def get_all_routes(
    db_path: Path,
    route_types: list[int],
    limit: int = 100,
) -> list[RouteResult]:
    """Return all routes for the given route types, ordered by route_short_name.

    Used by the route tracker config flow to populate the initial route picker.
    """
    if not route_types:
        return []

    rt_placeholders = ", ".join("?" * len(route_types))
    sql = f"""
        SELECT DISTINCT r.route_id, r.route_short_name, r.route_long_name, r.route_color,
               r.route_type
        FROM routes r
        WHERE r.route_type IN ({rt_placeholders})
          AND (r.route_long_name NOT LIKE 'Replacement%' OR r.route_long_name IS NULL)
          AND (r.route_short_name NOT LIKE 'Replacement%' OR r.route_short_name IS NULL)
        ORDER BY
            CASE WHEN CAST(r.route_short_name AS INTEGER) > 0
                 THEN CAST(r.route_short_name AS INTEGER)
                 ELSE 9999999 END,
            r.route_short_name COLLATE NOCASE
        LIMIT ?
    """
    params: list[int | str] = [*route_types, limit]

    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        conn.row_factory = sqlite3.Row
        results = []
        for row in conn.execute(sql, params):
            results.append(
                RouteResult(
                    route_id=row["route_id"],
                    route_short_name=row["route_short_name"] or "",
                    route_long_name=row["route_long_name"] or "",
                    route_color=row["route_color"] or None,
                    route_type=row["route_type"],
                )
            )
        return results
    except sqlite3.Error as exc:
        _LOGGER.error("get_all_routes failed: %s", exc)
        return []
    finally:
        conn.close()


def search_routes_by_name(
    db_path: Path,
    query: str,
    route_types: list[int],
    limit: int = 20,
) -> list[RouteResult]:
    """Search routes by short or long name (case-insensitive prefix match).

    Used by the route tracker config flow search box.
    """
    if not route_types:
        return []

    rt_placeholders = ", ".join("?" * len(route_types))
    like_query = _escape_like(query) + "%"
    sql = f"""
        SELECT DISTINCT r.route_id, r.route_short_name, r.route_long_name, r.route_color,
               r.route_type
        FROM routes r
        WHERE (r.route_short_name LIKE ? ESCAPE '\\'
            OR r.route_long_name LIKE ? ESCAPE '\\')
          AND r.route_type IN ({rt_placeholders})
          AND (r.route_long_name NOT LIKE 'Replacement%' OR r.route_long_name IS NULL)
          AND (r.route_short_name NOT LIKE 'Replacement%' OR r.route_short_name IS NULL)
        ORDER BY
            CASE WHEN CAST(r.route_short_name AS INTEGER) > 0
                 THEN CAST(r.route_short_name AS INTEGER)
                 ELSE 9999999 END,
            r.route_short_name COLLATE NOCASE
        LIMIT ?
    """
    params: list[int | str] = [like_query, like_query, *route_types, limit]

    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        conn.row_factory = sqlite3.Row
        results = []
        for row in conn.execute(sql, params):
            results.append(
                RouteResult(
                    route_id=row["route_id"],
                    route_short_name=row["route_short_name"] or "",
                    route_long_name=row["route_long_name"] or "",
                    route_color=row["route_color"] or None,
                    route_type=row["route_type"],
                )
            )
        return results
    except sqlite3.Error as exc:
        _LOGGER.error("search_routes_by_name failed: %s", exc)
        return []
    finally:
        conn.close()


def get_stop_lat_lon(
    db_path: Path,
    stop_id: str,
) -> tuple[float, float] | None:
    """Return the (lat, lon) of a stop, or None if not found.

    Used during route tracker config flow to store the reference stop's
    coordinates in the config entry (avoids a DB lookup on every poll).
    """
    sql = "SELECT stop_lat, stop_lon FROM stops WHERE stop_id = ? LIMIT 1"
    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        row = conn.execute(sql, [stop_id]).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        return (float(row[0]), float(row[1]))
    except sqlite3.Error as exc:
        _LOGGER.error("get_stop_lat_lon failed: %s", exc)
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Destination / terminus derivation (T7)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Destination:
    """A terminal destination candidate derived from actual trip patterns.

    ``via_hint`` names a disambiguating intermediate stop (set when a terminus
    is reached by more than one routing, or when a loop trip's terminus is also
    its origin so its turn-around stop distinguishes the "direction").  Empty
    ``via_hint`` means the terminal name alone is unambiguous.
    """

    direction_id: int | None
    terminal_stop_id: str
    terminal_stop_name: str
    headsign: str | None = None
    via_hint: str | None = None
    pattern_trip_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LineCandidate:
    """A route+direction candidate serving an origin→destination stop pair.

    Used by the T8 group flow's multi-select (TripView-style) resolver.
    """

    route_id: str
    route_short_name: str
    route_long_name: str
    direction_id: int | None
    terminal_stop_id: str
    terminal_stop_name: str
    via_hint: str | None = None
    route_color: str | None = None
    route_type: int | None = None   # GTFS route_type (for bundle mapping, T8)


def _destination_sort_key(d: Destination) -> tuple:
    """Deterministic ordering for destination candidates."""
    return (
        d.direction_id if d.direction_id is not None else -1,
        (d.terminal_stop_name or "").lower(),
        (d.via_hint or "").lower(),
        d.terminal_stop_id,
    )


def _line_sort_key(c: LineCandidate) -> tuple:
    """Deterministic ordering for line candidates."""
    return (
        (c.route_short_name or c.route_long_name or c.route_id).lower(),
        c.direction_id if c.direction_id is not None else -1,
        (c.terminal_stop_name or "").lower(),
        (c.via_hint or "").lower(),
        c.terminal_stop_id,
    )


def route_ids_for_trips(db_path: Path, trip_ids: list[str]) -> dict[str, str]:
    """Return ``trip_id`` → ``route_id`` for the given trips (T16 fleet lookup).

    Used by the vehicle-fleet tracker to resolve a vehicle's route when the
    realtime feed omits the trip descriptor's ``route_id``.  Runs against the
    current generation (like every other query here); unknown trips are simply
    absent from the result.  Callers wrap this in an executor.
    """
    if not trip_ids:
        return {}
    placeholders = ", ".join("?" * len(trip_ids))
    sql = f"SELECT trip_id, route_id FROM trips WHERE trip_id IN ({placeholders})"
    result: dict[str, str] = {}
    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        for trip_id, route_id in conn.execute(sql, list(trip_ids)):
            result[trip_id] = route_id
    except sqlite3.Error as exc:
        _LOGGER.error("route_ids_for_trips failed: %s", exc)
    finally:
        conn.close()
    return result


def routes_for_stop(db_path: Path, stop_id: str) -> set[str]:
    """Return the distinct ``route_id`` values serving ``stop_id`` (T17 fleet scope).

    Used by the vehicle-fleet tracker to derive a watched route scope from a
    watch's stop when the entry carries no explicit ``route_id_filter``.  Runs
    against the current generation (like every other query here); an unknown or
    route-less stop simply yields an empty set.  Callers wrap this in an
    executor.
    """
    if not stop_id:
        return set()
    sql = """
        SELECT DISTINCT t.route_id
        FROM stop_times st
        JOIN trips t ON t.trip_id = st.trip_id
        WHERE st.stop_id = ?
    """
    result: set[str] = set()
    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        for (route_id,) in conn.execute(sql, (stop_id,)):
            result.add(route_id)
    except sqlite3.Error as exc:
        _LOGGER.error("routes_for_stop failed: %s", exc)
    finally:
        conn.close()
    return result


def load_trip_shapes(
    db_path: Path,
    *,
    route_id: str | None = None,
    trip_ids: list[str] | None = None,
) -> list[dict]:
    """Load trips with their ordered ``(stop_id, stop_name)`` stop sequence.

    Returns a list of dicts with keys: ``trip_id``, ``route_id``,
    ``direction_id``, ``headsign``, ``route_short_name``, ``route_long_name``,
    ``route_color`` and ``stops`` (a list of ``(stop_id, stop_name)`` tuples in
    ``stop_sequence`` order).  Exactly one of ``route_id`` / ``trip_ids`` selects
    which trips are loaded.
    """
    if route_id is not None:
        where = "t.route_id = ?"
        params: list[object] = [route_id]
    elif trip_ids:
        placeholders = ", ".join("?" * len(trip_ids))
        where = f"t.trip_id IN ({placeholders})"
        params = list(trip_ids)
    else:
        return []

    sql = f"""
        SELECT t.trip_id, t.route_id, t.direction_id, t.trip_headsign,
               r.route_short_name, r.route_long_name, r.route_color, r.route_type,
               st.stop_id, st.stop_sequence, s.stop_name
        FROM trips t
        JOIN routes r ON r.route_id = t.route_id
        JOIN stop_times st ON st.trip_id = t.trip_id
        JOIN stops s ON s.stop_id = st.stop_id
        WHERE {where}
        ORDER BY t.trip_id, st.stop_sequence
    """
    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        trips: dict[str, dict] = {}
        for row in conn.execute(sql, params):
            tid = row[0]
            entry = trips.get(tid)
            if entry is None:
                entry = {
                    "trip_id": tid,
                    "route_id": row[1],
                    "direction_id": row[2],
                    "headsign": row[3] or "",
                    "route_short_name": row[4] or "",
                    "route_long_name": row[5] or "",
                    "route_color": row[6] or None,
                    "route_type": row[7],
                    "stops": [],
                }
                trips[tid] = entry
            entry["stops"].append((row[8], row[10] or row[8]))
        return list(trips.values())
    except sqlite3.Error as exc:
        _LOGGER.error("load_trip_shapes failed: %s", exc)
        return []
    finally:
        conn.close()


def reaching_trip_ids(
    db_path: Path,
    origin_stop_id: str,
    destination_stop_id: str,
    *,
    route_id: str | None = None,
    route_types: list[int] | None = None,
) -> list[str]:
    """Return trip_ids that serve ``origin`` then ``destination`` in order.

    Sequence-aware: the destination must be served at a later ``stop_sequence``
    than the origin occurrence (so a loop trip passing the origin again later is
    not mistaken for a reversed service).  Optionally restricted to one route or
    a set of route_types.
    """
    clauses: list[str] = []
    params: list[object] = [destination_stop_id, origin_stop_id]
    if route_id is not None:
        clauses.append("t.route_id = ?")
        params.append(route_id)
    if route_types:
        rt = ", ".join("?" * len(route_types))
        clauses.append(f"r.route_type IN ({rt})")
        params.extend(route_types)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""

    sql = f"""
        SELECT DISTINCT so.trip_id
        FROM stop_times so
        JOIN trips t ON t.trip_id = so.trip_id
        JOIN routes r ON r.route_id = t.route_id
        JOIN stop_times sd ON sd.trip_id = so.trip_id AND sd.stop_id = ?
        WHERE so.stop_id = ?{where}
          AND sd.stop_sequence > so.stop_sequence
    """
    conn = sqlite3.connect(str(_resolve(db_path)))
    try:
        return [row[0] for row in conn.execute(sql, params)]
    except sqlite3.Error as exc:
        _LOGGER.error("reaching_trip_ids failed: %s", exc)
        return []
    finally:
        conn.close()


def _most_common_headsign(headsigns: dict[str, int]) -> str | None:
    nonempty = {h: c for h, c in headsigns.items() if h}
    if not nonempty:
        return None
    return max(nonempty, key=lambda h: nonempty[h])


def build_destination_candidates(trips: list[dict]) -> list[Destination]:
    """Collapse ordered trips into distinct destination (terminus) candidates.

    Candidates are keyed by ``(direction_id, terminal_stop_id)``.  Trips whose
    *intermediate* stop signature matches collapse into one candidate; when a
    terminus is reached by more than one distinct intermediate signature (e.g.
    "City" via Footscray vs via Kensington) each signature becomes its own
    candidate with a ``via_hint`` naming the penultimate stop.  A loop trip
    (first stop == last stop) additionally yields a second candidate carrying the
    loop's turn-around stop as ``via_hint``, surfacing the two "directions" of the
    loop as separate, labelled choices.
    """
    groups: dict[tuple, dict] = {}
    for tr in trips:
        stops = tr["stops"]
        if not stops:
            continue
        terminal = stops[-1]
        intermediate = stops[1:-1]
        key = (tr["direction_id"], terminal[0])
        g = groups.get(key)
        if g is None:
            g = {
                "direction_id": tr["direction_id"],
                "terminal_stop_id": terminal[0],
                "terminal_stop_name": terminal[1],
                "headsigns": {},
                "sigs": {},
                "loop_turnarounds": set(),
            }
            groups[key] = g
        g["headsigns"][tr["headsign"]] = g["headsigns"].get(tr["headsign"], 0) + 1
        sig = tuple(s[0] for s in intermediate)
        rec = g["sigs"].setdefault(sig, {"trip_ids": [], "via": None})
        rec["trip_ids"].append(tr["trip_id"])
        if sig:
            rec["via"] = intermediate[-1][1]
        if len(stops) >= 2 and stops[0][0] == terminal[0]:
            g["loop_turnarounds"].add(stops[-2][1])

    candidates: list[Destination] = []
    for (direction_id, term_id), g in groups.items():
        headsign = _most_common_headsign(g["headsigns"])
        sigs = g["sigs"]
        if len(sigs) == 1:
            rec = next(iter(sigs.values()))
            candidates.append(
                Destination(
                    direction_id=direction_id,
                    terminal_stop_id=term_id,
                    terminal_stop_name=g["terminal_stop_name"],
                    headsign=headsign,
                    via_hint=None,
                    pattern_trip_ids=rec["trip_ids"],
                )
            )
        else:
            for rec in sigs.values():
                candidates.append(
                    Destination(
                        direction_id=direction_id,
                        terminal_stop_id=term_id,
                        terminal_stop_name=g["terminal_stop_name"],
                        headsign=headsign,
                        via_hint=rec["via"],
                        pattern_trip_ids=rec["trip_ids"],
                    )
                )
        for turn_around in sorted(g["loop_turnarounds"]):
            candidates.append(
                Destination(
                    direction_id=direction_id,
                    terminal_stop_id=term_id,
                    terminal_stop_name=g["terminal_stop_name"],
                    headsign=headsign,
                    via_hint=turn_around,
                    pattern_trip_ids=[],
                )
            )

    candidates.sort(key=_destination_sort_key)
    return candidates


def collapse_direction_candidates(
    trips: list[dict],
    destination_stop_id: str,
) -> list[Destination]:
    """Derive direction candidates for a chosen destination from trip evidence.

    Unlike :func:`build_destination_candidates` this collapses to ONE candidate
    per ``direction_id`` (multiple patterns / intermediate-vs-terminus reachings
    of the same destination share a direction).  A loop trip (first == last)
    adds a via-labelled second candidate for its self-returning direction.
    """
    groups: dict[int | None, dict] = {}
    for tr in trips:
        stops = tr["stops"]
        if not stops:
            continue
        terminal = stops[-1]
        dir_id = tr["direction_id"]
        g = groups.get(dir_id)
        if g is None:
            g = {"headsigns": {}, "terminals": {}, "loop_turnarounds": set()}
            groups[dir_id] = g
        g["headsigns"][tr["headsign"]] = g["headsigns"].get(tr["headsign"], 0) + 1
        name, count = g["terminals"].get(terminal[0], (terminal[1], 0))
        g["terminals"][terminal[0]] = (name, count + 1)
        if len(stops) >= 2 and stops[0][0] == terminal[0]:
            g["loop_turnarounds"].add(stops[-2][1])

    candidates: list[Destination] = []
    for dir_id in sorted(groups, key=lambda d: d if d is not None else -1):
        g = groups[dir_id]
        headsign = _most_common_headsign(g["headsigns"])
        if any(tid == destination_stop_id for tid in g["terminals"]):
            terminal_stop_id = destination_stop_id
            terminal_stop_name = g["terminals"][destination_stop_id][0]
        else:
            terminal_stop_id, (terminal_stop_name, _count) = max(
                g["terminals"].items(), key=lambda kv: kv[1][1]
            )
        candidates.append(
            Destination(
                direction_id=dir_id,
                terminal_stop_id=terminal_stop_id,
                terminal_stop_name=terminal_stop_name,
                headsign=headsign,
                via_hint=None,
                pattern_trip_ids=[],
            )
        )
        for turn_around in sorted(g["loop_turnarounds"]):
            candidates.append(
                Destination(
                    direction_id=dir_id,
                    terminal_stop_id=terminal_stop_id,
                    terminal_stop_name=terminal_stop_name,
                    headsign=headsign,
                    via_hint=turn_around,
                    pattern_trip_ids=[],
                )
            )

    candidates.sort(key=_destination_sort_key)
    return candidates


def _collapse_duplicate_labels(candidates: list[Destination]) -> list[Destination]:
    """Collapse candidates whose rendered label text is identical.

    Distinct trip patterns often reach the same terminus with the same penultimate
    stop (e.g. City Loop vs direct Upfield services both arrive via Gowrie), so
    ``(direction_id, terminal_stop_id, via_hint)`` alone is NOT a unique UI key:
    several candidates then render the same label.  Keeping one candidate per
    final label text guarantees the selector can never offer two identically
    labelled options.

    Input is expected in :func:`_destination_sort_key` order (as returned by
    :func:`build_destination_candidates`).  The first occurrence wins ties; when
    patterns differ in evidence the candidate backed by the most trips wins, so
    the retained candidate is the most representative pattern.  Its position in
    the ordering is unchanged.
    """
    # Deferred import: routing.destination imports this module at import time.
    from ..routing.destination import destination_label

    kept: dict[str, Destination] = {}
    for candidate in candidates:
        label = destination_label(candidate)
        existing = kept.get(label)
        if existing is None or len(candidate.pattern_trip_ids) > len(
            existing.pattern_trip_ids
        ):
            kept[label] = candidate
    return list(kept.values())


def get_terminals_for_route(db_path: Path, route_id: str) -> list[Destination]:
    """Return distinct destination candidates (termini) for a route.

    Derived from the ordered ``stop_sequences`` of the route's trips, with
    headsign/via disambiguation (see :func:`build_destination_candidates`),
    then collapsed to one candidate per final label text so the UI never shows
    two identically labelled destinations (see
    :func:`_collapse_duplicate_labels`).  Deterministically ordered by
    (direction, terminal name, via, stop_id).
    """
    trips = load_trip_shapes(db_path, route_id=route_id)
    if not trips:
        return []
    return _collapse_duplicate_labels(build_destination_candidates(trips))


def get_lines_between(
    db_path: Path,
    origin_stop_id: str,
    destination_stop_id: str,
    route_types: list[int] | None = None,
) -> list[LineCandidate]:
    """Return distinct (route, direction, via) candidates serving a stop pair.

    T8 plumbing: the group flow uses this to list every route+direction that
    carries a passenger from ``origin_stop_id`` to ``destination_stop_id`` in
    order, collapsing multiple patterns to a terminus into a single candidate
    (with a ``via_hint`` when a terminus has distinct intermediate routings).
    """
    trip_ids = reaching_trip_ids(
        db_path, origin_stop_id, destination_stop_id, route_types=route_types
    )
    if not trip_ids:
        return []
    trips = load_trip_shapes(db_path, trip_ids=trip_ids)

    groups: dict[tuple, dict] = {}
    for tr in trips:
        stops = tr["stops"]
        if not stops:
            continue
        terminal = stops[-1]
        intermediate = stops[1:-1]
        key = (tr["route_id"], tr["direction_id"], terminal[0])
        g = groups.get(key)
        if g is None:
            g = {
                "route_id": tr["route_id"],
                "route_short_name": tr["route_short_name"],
                "route_long_name": tr["route_long_name"],
                "route_color": tr["route_color"],
                "route_type": tr["route_type"],
                "direction_id": tr["direction_id"],
                "terminal_stop_id": terminal[0],
                "terminal_stop_name": terminal[1],
                "sigs": {},
            }
            groups[key] = g
        sig = tuple(s[0] for s in intermediate)
        rec = g["sigs"].setdefault(sig, {"via": None})
        if sig:
            rec["via"] = intermediate[-1][1]

    candidates: list[LineCandidate] = []
    for _key, g in groups.items():
        sigs = g["sigs"]
        if len(sigs) == 1:
            candidates.append(
                LineCandidate(
                    route_id=g["route_id"],
                    route_short_name=g["route_short_name"],
                    route_long_name=g["route_long_name"],
                    direction_id=g["direction_id"],
                    terminal_stop_id=g["terminal_stop_id"],
                    terminal_stop_name=g["terminal_stop_name"],
                    via_hint=None,
                    route_color=g["route_color"],
                    route_type=g["route_type"],
                )
            )
        else:
            for rec in sigs.values():
                candidates.append(
                    LineCandidate(
                        route_id=g["route_id"],
                        route_short_name=g["route_short_name"],
                        route_long_name=g["route_long_name"],
                        direction_id=g["direction_id"],
                        terminal_stop_id=g["terminal_stop_id"],
                        terminal_stop_name=g["terminal_stop_name"],
                        via_hint=rec["via"],
                        route_color=g["route_color"],
                        route_type=g["route_type"],
                    )
                )

    candidates.sort(key=_line_sort_key)
    return candidates
