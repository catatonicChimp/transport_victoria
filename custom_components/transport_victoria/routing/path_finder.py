"""Route validation for specific-destination watches.

Checks whether a direct service exists between an origin and destination stop
by querying the GTFS stop_times table for trips that serve both stops with the
destination coming after the origin in stop_sequence.

This is a one-time validation called during config flow — not used at runtime.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ..gtfs import store

_LOGGER = logging.getLogger(__name__)


def validate_route(
    db_path: Path,
    origin_stop_id: str,
    destination_stop_id: str,
    route_types: list[int],
) -> bool:
    """Return True if at least one trip serves origin → destination in order.

    Checks stop_sequence so a round-trip that visits origin after destination
    does not produce a false positive.

    The ``db_path`` argument is the logical anchor; it is resolved to the
    current immutable generation (via the same pointer mechanism as the
    repository) and opened READ-ONLY, so a resolution miss never silently
    creates an empty DB at the legacy path. A missing generation (e.g. a fresh
    install before the first import) is reported as a clean validation failure.

    Args:
        db_path: Logical DB anchor path (e.g. ``<data>/gtfs.db``).
        origin_stop_id: GTFS stop_id of the departure stop.
        destination_stop_id: GTFS stop_id of the arrival stop.
        route_types: List of GTFS route_type values to restrict the search.

    Returns:
        True if a matching service was found, False otherwise.
    """
    if not route_types:
        return False

    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        _LOGGER.warning(
            "No GTFS schedule generation available for route validation (%s)", resolved
        )
        return False

    rt_placeholders = ", ".join("?" * len(route_types))
    sql = f"""
        SELECT 1
        FROM stop_times st_origin
        JOIN stop_times st_dest
          ON st_dest.trip_id = st_origin.trip_id
         AND st_dest.stop_id = ?
         AND st_dest.stop_sequence > st_origin.stop_sequence
        JOIN trips t ON t.trip_id = st_origin.trip_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE st_origin.stop_id = ?
          AND r.route_type IN ({rt_placeholders})
        LIMIT 1
    """
    params = [destination_stop_id, origin_stop_id] + list(route_types)

    # Open read-only so a resolution miss never creates a file. The connect is
    # inside the try-block so a disappearance/permission race returns a clean
    # False rather than raising.
    uri = resolved.resolve().as_uri() + "?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        row = conn.execute(sql, params).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        _LOGGER.error("Route validation query failed: %s", exc)
        return False
    finally:
        if conn is not None:
            conn.close()
