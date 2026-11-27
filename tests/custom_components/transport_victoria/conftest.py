"""Shared pytest fixtures for PTV integration tests."""

from __future__ import annotations

import io
import sqlite3
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helper: build a minimal in-memory SQLite DB from our schema
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).parents[3] / "custom_components" / "transport_victoria" / "gtfs" / "schema.sql"


def _create_db(tmp_path: Path) -> Path:
    """Create a minimal GTFS SQLite DB at tmp_path/gtfs.db and return its path."""
    db_path = tmp_path / "gtfs.db"
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")

    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema_sql)

    # Anchor the fixture to the GTFS *service day*, which rolls back to
    # yesterday between 00:00 and 03:00 local. Using the raw wall-clock date
    # makes these tests time-of-day flaky (departure queries resolve to
    # yesterday's service date while the calendar only enabled today's weekday).
    from datetime import datetime

    from custom_components.transport_victoria.gtfs.time_utils import service_day_for_time

    now = datetime.now()
    service_date, now_seconds = service_day_for_time(now)

    start = service_date - timedelta(days=10)
    end = service_date + timedelta(days=30)
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    today_str = service_date.strftime("%Y%m%d")

    # Agency
    conn.execute(
        "INSERT INTO agency VALUES (?, ?, ?, ?, ?, ?)",
        ("1", "Metro Trains Melbourne", "https://metrotrains.com.au", "Australia/Melbourne", "en", None),
    )

    # Route
    conn.execute(
        "INSERT INTO routes VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("R1", "1", "Craigieburn", "Craigieburn Line", 400, "0099CC", "FFFFFF"),
    )

    # Service active every day of the week, spanning the service date broadly,
    # so the calendar covers whichever service date the departure query resolves
    # to (including the 00:00–03:00 cutover where "today" is yesterday).
    conn.execute(
        "INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("SVC1", 1, 1, 1, 1, 1, 1, 1, start_str, end_str),
    )

    # Trips
    conn.execute(
        "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("T1", "R1", "SVC1", None, "Flinders Street", 0, None),
    )
    conn.execute(
        "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("T2", "R1", "SVC1", None, "Flinders Street", 0, None),
    )

    # Stops — two platforms at the same parent station
    conn.execute(
        "INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("PLAT1", "Coburg Station", -37.74, 144.96, "1", None, "COBURG", "1", 0),
    )
    conn.execute(
        "INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("PLAT2", "Coburg Station", -37.74, 144.96, "2", None, "COBURG", "2", 0),
    )
    conn.execute(
        "INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("FLST", "Flinders Street", -37.818, 144.967, "3", None, None, None, 0),
    )

    # Stop times — two trips from PLAT1, then stop at FLST
    from custom_components.transport_victoria.gtfs.time_utils import gtfs_time_to_seconds

    # Departures anchored to the service day (see above) so they are always in
    # the future relative to the departure query, at any wall-clock time.
    dep1 = now_seconds + 600   # +10 min
    dep2 = now_seconds + 1800  # +30 min

    conn.executemany(
        "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("T1", "PLAT1", 1, dep1, dep1, 0, 0, 1),
            ("T1", "FLST",  2, dep1 + 1200, dep1 + 1200, 0, 0, 1),
            ("T2", "PLAT1", 1, dep2, dep2, 0, 0, 1),
            ("T2", "FLST",  2, dep2 + 1200, dep2 + 1200, 0, 0, 1),
        ],
    )

    # import_meta
    conn.execute(
        "INSERT INTO import_meta VALUES (?, ?)", ("bundle_2", today_str)
    )

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def gtfs_db(tmp_path: Path) -> Path:
    """Return path to a minimal GTFS SQLite database."""
    return _create_db(tmp_path)
