"""Unit tests for gtfs/horizon.py.

These tests build tiny SQLite databases directly (schema only, flat files — the
generation store's resolver falls back to the flat anchor when no pointer
exists) and pass an explicit `today` where the API accepts one, so they are
independent of the wall-clock time of day.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from custom_components.transport_victoria.const import GTFS_SCHEMA_VERSION
from custom_components.transport_victoria.gtfs.horizon import (
    compute_horizon,
    compute_horizon_for_bundle,
    get_schema_version,
    has_service_on_date,
    imported_bundles,
    is_expired,
    last_import_age_days,
    read_bundle_horizons,
    refresh_due,
)

_SCHEMA_PATH = (
    Path(__file__).parents[3]
    / "custom_components"
    / "transport_victoria"
    / "gtfs"
    / "schema.sql"
)

# A fixed "today" (a Monday) so results don't depend on when the test runs.
TODAY = date(2026, 6, 1)

_ALL_DAYS = (1, 1, 1, 1, 1, 1, 1)  # monday..sunday all active


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _add_calendar(
    conn: sqlite3.Connection,
    service_id: str,
    start: date,
    end: date,
    days: tuple[int, ...] = _ALL_DAYS,
) -> None:
    conn.execute(
        "INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (service_id, *days, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")),
    )


def _add_dates(conn: sqlite3.Connection, service_id: str, d: date, exc_type: int) -> None:
    conn.execute(
        "INSERT INTO calendar_dates VALUES (?, ?, ?)",
        (service_id, d.strftime("%Y%m%d"), exc_type),
    )


def _add_imported_at(conn: sqlite3.Connection, imported: date) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO import_meta VALUES (?, ?)",
        ("imported_at", imported.isoformat()),
    )


def _add_bundle(conn: sqlite3.Connection, folder: str, ts: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO import_meta VALUES (?, ?)", (f"bundle_{folder}", ts)
    )


# ---------------------------------------------------------------------------
# compute_horizon
# ---------------------------------------------------------------------------

class TestComputeHorizon:
    def test_pure_calendar(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC1", TODAY - timedelta(days=10), TODAY + timedelta(days=20))
        conn.commit()
        conn.close()
        assert compute_horizon(db) == TODAY + timedelta(days=20)

    def test_calendar_dates_addition_extends(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC1", TODAY - timedelta(days=10), TODAY + timedelta(days=20))
        _add_dates(conn, "SVC1", TODAY + timedelta(days=40), 1)
        conn.commit()
        conn.close()
        assert compute_horizon(db) == TODAY + timedelta(days=40)

    def test_removal_does_not_extend(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC1", TODAY - timedelta(days=10), TODAY + timedelta(days=20))
        _add_dates(conn, "SVC1", TODAY + timedelta(days=40), 2)
        conn.commit()
        conn.close()
        assert compute_horizon(db) == TODAY + timedelta(days=20)

    def test_no_calendar_returns_none(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        conn.commit()
        conn.close()
        assert compute_horizon(db) is None

    def test_missing_db_returns_none(self, tmp_path: Path):
        assert compute_horizon(tmp_path / "nope.db") is None

    def test_no_trips_still_has_horizon(self, tmp_path: Path):
        # Global horizon is computed over calendar rows regardless of trips.
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC_UNUSED", TODAY - timedelta(days=5), TODAY + timedelta(days=15))
        conn.commit()
        conn.close()
        assert compute_horizon(db) == TODAY + timedelta(days=15)

    def test_malformed_date_returns_none_not_raise(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        conn.execute(
            "INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("SVC1", 1, 1, 1, 1, 1, 1, 1, "20260101", "not-a-date"),
        )
        conn.commit()
        conn.close()
        # Must not raise; malformed end_date → no usable horizon.
        assert compute_horizon(db) is None


# ---------------------------------------------------------------------------
# Per-bundle horizon (import-time provenance)
# ---------------------------------------------------------------------------

class TestPerBundleHorizon:
    def _build(self, tmp_path: Path, horizons: dict[str, int]) -> Path:
        """Build a flat DB with a _bundle_horizons provenance table."""
        db = tmp_path / "h.db"
        conn = _open_db(db)
        for folder, days in horizons.items():
            conn.execute(
                "INSERT INTO _bundle_horizons VALUES (?, ?)",
                (folder, (TODAY + timedelta(days=days)).strftime("%Y%m%d")),
            )
        conn.commit()
        conn.close()
        return db

    def test_bundle_horizon_from_provenance(self, tmp_path: Path):
        db = self._build(tmp_path, {"1": 30, "5": 5})
        # Folder 1 (rail) horizon +30, folder 5 (coach) +5 — distinct, exact.
        assert compute_horizon_for_bundle(db, "1") == TODAY + timedelta(days=30)
        assert compute_horizon_for_bundle(db, "5") == TODAY + timedelta(days=5)

    def test_overlapping_route_types_still_distinct(self, tmp_path: Path):
        # Folders 1 & 5 share route_types [2, 204]; folders 4, 6, 11 share
        # [3, 701]. Provenance keeps them distinct regardless.
        db = self._build(tmp_path, {"1": 30, "5": 3, "4": 30, "6": 2, "11": 30})
        assert compute_horizon_for_bundle(db, "1") == TODAY + timedelta(days=30)
        assert compute_horizon_for_bundle(db, "5") == TODAY + timedelta(days=3)
        assert compute_horizon_for_bundle(db, "6") == TODAY + timedelta(days=2)

    def test_missing_bundle_returns_none(self, tmp_path: Path):
        db = self._build(tmp_path, {"2": 30})
        assert compute_horizon_for_bundle(db, "not-a-folder") is None

    def test_read_bundle_horizons(self, tmp_path: Path):
        db = self._build(tmp_path, {"2": 30, "6": 5})
        horizons = read_bundle_horizons(db)
        assert horizons == {
            "2": TODAY + timedelta(days=30),
            "6": TODAY + timedelta(days=5),
        }

    def test_read_bundle_horizons_empty_when_missing(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        conn.commit()
        conn.close()
        assert read_bundle_horizons(db) == {}


# ---------------------------------------------------------------------------
# has_service_on_date
# ---------------------------------------------------------------------------

class TestHasServiceOnDate:
    def test_covered_by_calendar(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC1", TODAY - timedelta(days=5), TODAY + timedelta(days=5))
        conn.commit()
        conn.close()
        assert has_service_on_date(db, TODAY) is True

    def test_not_covered_future_only(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC1", TODAY + timedelta(days=1), TODAY + timedelta(days=30))
        conn.commit()
        conn.close()
        assert has_service_on_date(db, TODAY) is False

    def test_addition_covers(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_dates(conn, "SVC1", TODAY, 1)
        conn.commit()
        conn.close()
        assert has_service_on_date(db, TODAY) is True

    def test_removal_excludes(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC1", TODAY - timedelta(days=5), TODAY + timedelta(days=5))
        _add_dates(conn, "SVC1", TODAY, 2)
        conn.commit()
        conn.close()
        assert has_service_on_date(db, TODAY) is False

    def test_missing_db_false(self, tmp_path: Path):
        assert has_service_on_date(tmp_path / "nope.db", TODAY) is False


# ---------------------------------------------------------------------------
# is_expired
# ---------------------------------------------------------------------------

class TestIsExpired:
    def test_expired_when_horizon_past(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC1", TODAY - timedelta(days=30), TODAY - timedelta(days=1))
        conn.commit()
        conn.close()
        assert is_expired(db, TODAY) is True

    def test_not_expired_when_horizon_future(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC1", TODAY - timedelta(days=5), TODAY + timedelta(days=10))
        conn.commit()
        conn.close()
        assert is_expired(db, TODAY) is False

    def test_not_expired_when_missing(self, tmp_path: Path):
        assert is_expired(tmp_path / "nope.db", TODAY) is False


# ---------------------------------------------------------------------------
# get_schema_version (incl. forward-compat contract)
# ---------------------------------------------------------------------------

class TestGetSchemaVersion:
    def test_fresh_db_has_version(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        conn.commit()
        conn.close()
        assert get_schema_version(db) == GTFS_SCHEMA_VERSION

    def test_missing_db_zero(self, tmp_path: Path):
        assert get_schema_version(tmp_path / "nope.db") == 0

    def test_stale_version_read(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()
        assert get_schema_version(db) == 0

    def test_higher_version_accepted_forward_compat(self, tmp_path: Path):
        # Forward-compat contract: a DB with a HIGHER user_version than required
        # is accepted as-is (assumed to be a compatible superset), so
        # refresh_due does NOT flag it stale.
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC1", TODAY - timedelta(days=5), TODAY + timedelta(days=30))
        _add_imported_at(conn, TODAY)
        conn.execute(f"PRAGMA user_version = {GTFS_SCHEMA_VERSION + 5}")
        conn.commit()
        conn.close()
        assert get_schema_version(db) == GTFS_SCHEMA_VERSION + 5
        assert refresh_due(db, today=TODAY) is False


# ---------------------------------------------------------------------------
# last_import_age_days
# ---------------------------------------------------------------------------

class TestLastImportAgeDays:
    def test_recent_import(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_imported_at(conn, TODAY)
        conn.commit()
        conn.close()
        assert last_import_age_days(db, TODAY) == 0

    def test_old_import(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_imported_at(conn, TODAY - timedelta(days=10))
        conn.commit()
        conn.close()
        assert last_import_age_days(db, TODAY) == 10

    def test_no_import_meta_returns_none(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        conn.commit()
        conn.close()
        assert last_import_age_days(db, TODAY) is None


# ---------------------------------------------------------------------------
# imported_bundles
# ---------------------------------------------------------------------------

class TestImportedBundles:
    def test_empty(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        conn.commit()
        conn.close()
        assert imported_bundles(db) == set()

    def test_returns_bundles(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_bundle(conn, "2", TODAY.isoformat())
        _add_bundle(conn, "6", TODAY.isoformat())
        conn.commit()
        conn.close()
        assert imported_bundles(db) == {"2", "6"}


# ---------------------------------------------------------------------------
# refresh_due
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path: Path, horizon_days: int = 30, start_offset: int = -5) -> Path:
    db = tmp_path / "h.db"
    conn = _open_db(db)
    _add_calendar(
        conn, "SVC1",
        TODAY + timedelta(days=start_offset),
        TODAY + timedelta(days=horizon_days),
    )
    _add_imported_at(conn, TODAY)
    conn.commit()
    conn.close()
    return db


class TestRefreshDue:
    def test_missing_db_due(self, tmp_path: Path):
        assert refresh_due(tmp_path / "nope.db", today=TODAY) is True

    def test_fresh_schedule_not_due(self, tmp_path: Path):
        db = _fresh_db(tmp_path, horizon_days=30)
        assert refresh_due(db, today=TODAY) is False

    def test_due_when_within_margin(self, tmp_path: Path):
        db = _fresh_db(tmp_path, horizon_days=5)
        assert refresh_due(db, today=TODAY) is True

    def test_due_when_expired(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        _add_calendar(conn, "SVC1", TODAY - timedelta(days=30), TODAY - timedelta(days=1))
        _add_imported_at(conn, TODAY)
        conn.commit()
        conn.close()
        assert refresh_due(db, today=TODAY) is True

    def test_due_when_coverage_missing(self, tmp_path: Path):
        db = _fresh_db(tmp_path, horizon_days=30, start_offset=1)
        assert refresh_due(db, today=TODAY) is True

    def test_due_when_stale_version(self, tmp_path: Path):
        db = _fresh_db(tmp_path, horizon_days=30)
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()
        assert refresh_due(db, today=TODAY) is True

    def test_due_when_import_old(self, tmp_path: Path):
        db = _fresh_db(tmp_path, horizon_days=30)
        conn = sqlite3.connect(str(db))
        _add_imported_at(conn, TODAY - timedelta(days=10))
        conn.commit()
        conn.close()
        assert refresh_due(db, today=TODAY) is True

    def test_no_horizon_due(self, tmp_path: Path):
        db = tmp_path / "h.db"
        conn = _open_db(db)
        conn.commit()
        conn.close()
        assert refresh_due(db, today=TODAY) is True

    # -- per-bundle -----------------------------------------------------

    def _bundle_db(self, tmp_path: Path, metro_days: int, bus_days: int) -> Path:
        db = tmp_path / "h.db"
        conn = _open_db(db)
        # Global calendar coverage for today (so has_service_on_date passes).
        _add_calendar(conn, "ANY", TODAY - timedelta(days=5), TODAY + timedelta(days=30))
        # Per-bundle provenance (exact, import-time).
        conn.execute(
            "INSERT INTO _bundle_horizons VALUES (?, ?)",
            ("2", (TODAY + timedelta(days=metro_days)).strftime("%Y%m%d")),
        )
        conn.execute(
            "INSERT INTO _bundle_horizons VALUES (?, ?)",
            ("4", (TODAY + timedelta(days=bus_days)).strftime("%Y%m%d")),
        )
        _add_imported_at(conn, TODAY)
        _add_bundle(conn, "2", TODAY.isoformat())  # metro
        _add_bundle(conn, "4", TODAY.isoformat())  # bus
        conn.commit()
        conn.close()
        return db

    def test_healthy_bundle_not_due(self, tmp_path: Path):
        db = self._bundle_db(tmp_path, metro_days=30, bus_days=30)
        assert refresh_due(db, ["2"], today=TODAY) is False
        assert refresh_due(db, ["4"], today=TODAY) is False

    def test_expired_bundle_not_masked_by_healthy(self, tmp_path: Path):
        # Bus bundle expired, metro healthy → requesting metro only is fine,
        # but requesting bus (or the union) is due.
        db = self._bundle_db(tmp_path, metro_days=30, bus_days=-2)
        assert refresh_due(db, ["2"], today=TODAY) is False
        assert refresh_due(db, ["4"], today=TODAY) is True
        assert refresh_due(db, ["2", "4"], today=TODAY) is True

    def test_missing_bundle_due(self, tmp_path: Path):
        db = self._bundle_db(tmp_path, metro_days=30, bus_days=30)
        # Bundle "6" was never imported.
        assert refresh_due(db, ["6"], today=TODAY) is True
