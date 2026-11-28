"""Unit tests for gtfs/importer.py (generation model)."""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from custom_components.transport_victoria.gtfs import store
from custom_components.transport_victoria.gtfs.importer import (
    _compute_valid_trip_ids,
    _import_stop_times,
    get_imported_bundles,
    get_stored_zip_meta,
    import_bundles_to_generation,
    import_mode_bundles,
)

_SCHEMA_PATH = Path(__file__).parents[3] / "custom_components" / "transport_victoria" / "gtfs" / "schema.sql"


def _make_db(path: Path) -> sqlite3.Connection:
    """Create a fresh DB from schema.sql and return an open connection."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _make_gtfs_zip(tmp_path: Path, folder: str = "2", horizon_days: int = 30) -> Path:
    """Build a minimal nested PTV GTFS ZIP at tmp_path/gtfs.zip."""
    today = date.today()
    yesterday = (today - timedelta(days=1)).strftime("%Y%m%d")
    tomorrow = (today + timedelta(days=horizon_days)).strftime("%Y%m%d")

    cal_row = {"service_id": "SVC1", "start_date": yesterday, "end_date": tomorrow,
               "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1",
               "friday": "1", "saturday": "1", "sunday": "1"}

    def _csv(rows: list[dict]) -> bytes:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue().encode("utf-8")

    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w") as inner:
        inner.writestr("agency.txt", _csv([
            {"agency_id": "1", "agency_name": "Metro", "agency_url": "",
             "agency_timezone": "Australia/Melbourne", "agency_lang": "en", "agency_phone": ""},
        ]))
        inner.writestr("routes.txt", _csv([
            {"route_id": "R1", "agency_id": "1", "route_short_name": "Craigieburn",
             "route_long_name": "Craigieburn Line", "route_type": "400",
             "route_color": "0099CC", "route_text_color": "FFFFFF"},
        ]))
        inner.writestr("calendar.txt", _csv([cal_row]))
        inner.writestr("trips.txt", _csv([
            {"trip_id": "T1", "route_id": "R1", "service_id": "SVC1",
             "shape_id": "", "trip_headsign": "Flinders Street", "direction_id": "0", "block_id": ""},
        ]))
        inner.writestr("stops.txt", _csv([
            {"stop_id": "S1", "stop_name": "Test Station", "stop_lat": "-37.74",
             "stop_lon": "144.96", "stop_code": "1", "location_type": "",
             "parent_station": "", "platform_code": "", "wheelchair_boarding": ""},
        ]))
        inner.writestr("stop_times.txt", _csv([
            {"trip_id": "T1", "stop_id": "S1", "stop_sequence": "1",
             "arrival_time": "09:00:00", "departure_time": "09:00:00",
             "pickup_type": "0", "drop_off_type": "0", "timepoint": "1"},
        ]))
    inner_buf.seek(0)

    outer_path = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(outer_path, "w") as outer:
        outer.writestr(f"{folder}/google_transit.zip", inner_buf.getvalue())
    return outer_path


def _open_generation(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(store.resolve_db(db_path)))


# ---------------------------------------------------------------------------
# _compute_valid_trip_ids
# ---------------------------------------------------------------------------

class TestComputeValidTripIds:
    def test_includes_active_trips(self, tmp_path: Path):
        conn = _make_db(tmp_path / "db.db")
        today = date.today()
        cal = {"monday": 1, "tuesday": 1, "wednesday": 1, "thursday": 1,
               "friday": 1, "saturday": 1, "sunday": 1}
        conn.execute(
            "INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("SVC1", *list(cal.values()),
             (today - timedelta(1)).strftime("%Y%m%d"),
             (today + timedelta(30)).strftime("%Y%m%d")),
        )
        conn.execute("INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("T1", "R1", "SVC1", None, "Test", 0, None))
        conn.commit()
        result = _compute_valid_trip_ids(conn, window_days=45, today=today)
        assert "T1" in result
        conn.close()

    def test_excludes_expired_trips(self, tmp_path: Path):
        conn = _make_db(tmp_path / "db.db")
        today = date.today()
        past = (today - timedelta(100)).strftime("%Y%m%d")
        past_end = (today - timedelta(60)).strftime("%Y%m%d")
        conn.execute(
            "INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("SVC_OLD", 1, 1, 1, 1, 1, 1, 1, past, past_end),
        )
        conn.execute("INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("T_OLD", "R1", "SVC_OLD", None, "Old", 0, None))
        conn.commit()
        result = _compute_valid_trip_ids(conn, window_days=45, today=today)
        assert "T_OLD" not in result
        conn.close()

    def test_empty_calendar_returns_empty(self, tmp_path: Path):
        conn = _make_db(tmp_path / "db.db")
        result = _compute_valid_trip_ids(conn)
        assert result == frozenset()
        conn.close()


# ---------------------------------------------------------------------------
# import_bundles_to_generation / import_mode_bundles
# ---------------------------------------------------------------------------

class TestImportBundlesToGeneration:
    def test_basic_import(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        ptr = import_bundles_to_generation(db_path, zip_path, "abc123", ["2"])
        assert ptr["generation"].startswith("gtfs-")
        conn = _open_generation(db_path)
        row = conn.execute("SELECT COUNT(*) FROM stops").fetchone()
        conn.close()
        assert row[0] > 0
        assert store.pointer_path(db_path).exists()

    def test_stop_search_built_at_import(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_bundles_to_generation(db_path, zip_path, "abc", ["2"])
        conn = _open_generation(db_path)
        row = conn.execute(
            "SELECT stop_name, name_norm, route_type, route_types, trip_count, "
            "token_count FROM stop_search"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "Test Station"
        assert row[1] == "test station"  # normalized
        assert row[2] == 400             # representative route type
        assert row[3] == "400"           # CSV
        assert row[4] == 1               # one stop_time in the fixture
        assert row[5] == 2               # "Test Station" → 2 tokens

    def test_stop_search_rebuilt_on_reimport(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_bundles_to_generation(db_path, zip_path, "sha1", ["2"])
        import_bundles_to_generation(db_path, zip_path, "sha2", ["2"])
        conn = _open_generation(db_path)
        count = conn.execute("SELECT COUNT(*) FROM stop_search").fetchone()[0]
        conn.close()
        assert count == 1  # rebuilt (not duplicated) in the new generation

    def test_legacy_wrapper_order(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        # Legacy signature: (zip_path, db_path, sha, bundles).
        import_mode_bundles(zip_path, db_path, "abc123", ["2"])
        conn = _open_generation(db_path)
        row = conn.execute("SELECT COUNT(*) FROM stops").fetchone()
        conn.close()
        assert row[0] > 0

    def test_two_imports_two_generations(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_bundles_to_generation(db_path, zip_path, "sha1", ["2"])
        first = store.resolve_db(db_path)
        import_bundles_to_generation(db_path, zip_path, "sha2", ["2"])
        second = store.resolve_db(db_path)
        # A fresh generation each time; pointer now names the newest.
        assert first != second
        assert second.exists()
        assert "2" in get_imported_bundles(db_path)

    def test_stores_etag_and_size(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_bundles_to_generation(
            db_path, zip_path, "abc", ["2"],
            zip_etag='"etag123"', zip_size="12345",
        )
        meta = get_stored_zip_meta(db_path)
        assert meta.get("zip_etag") == '"etag123"'
        assert meta.get("zip_size") == "12345"
        assert meta.get("zip_sha256") == "abc"

    def test_stop_times_filtered_to_active_trips(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_bundles_to_generation(db_path, zip_path, "abc", ["2"])
        conn = _open_generation(db_path)
        count = conn.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0]
        conn.close()
        assert count == 1

    def test_missing_bundle_folder_raises(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path, folder="2")
        db_path = tmp_path / "gtfs.db"
        with pytest.raises(ValueError):
            import_bundles_to_generation(db_path, zip_path, "abc", ["6"])
        # No pointer published on failure.
        assert store.read_pointer(db_path) is None

    def test_expired_schedule_rejected(self, tmp_path: Path):
        # A zip whose calendar already ends in the past must be rejected.
        zip_path = _make_gtfs_zip(tmp_path, folder="2", horizon_days=-10)
        db_path = tmp_path / "gtfs.db"
        with pytest.raises(ValueError):
            import_bundles_to_generation(db_path, zip_path, "abc", ["2"])
        assert store.read_pointer(db_path) is None

    def test_failed_import_preserves_previous_generation(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_bundles_to_generation(db_path, zip_path, "good", ["2"])
        previous = store.resolve_db(db_path)
        previous_horizon = _stops_count(previous)

        # Now import a zip missing the requested bundle → fails, previous intact.
        bad_zip = _make_gtfs_zip(tmp_path, folder="2")
        bad_zip = _make_gtfs_zip(tmp_path, folder="9")  # no folder 9 in zip? it's folder 9
        with pytest.raises(ValueError):
            import_bundles_to_generation(db_path, bad_zip, "bad", ["2"])
        assert store.resolve_db(db_path) == previous
        assert _stops_count(previous) == previous_horizon


def _stops_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# get_imported_bundles / get_stored_zip_meta
# ---------------------------------------------------------------------------

class TestMetaHelpers:
    def test_empty_when_no_db(self, tmp_path: Path):
        assert get_imported_bundles(tmp_path / "nope.db") == set()
        assert get_stored_zip_meta(tmp_path / "nope.db") == {}

    def test_returns_imported_bundles(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_bundles_to_generation(db_path, zip_path, "sha", ["2"])
        assert "2" in get_imported_bundles(db_path)

    def test_sha256_stored(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_bundles_to_generation(db_path, zip_path, "deadbeef", ["2"])
        meta = get_stored_zip_meta(db_path)
        assert meta["zip_sha256"] == "deadbeef"


# ---------------------------------------------------------------------------
# Build-registry lifecycle (guaranteed unregistration)
# ---------------------------------------------------------------------------

class TestBuildRegistry:
    def test_connect_failure_unregisters_build(self, tmp_path: Path, monkeypatch):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"

        def failing_connect(*a, **k):
            raise sqlite3.OperationalError("simulated connect failure")

        monkeypatch.setattr(sqlite3, "connect", failing_connect)
        with pytest.raises(sqlite3.OperationalError):
            import_bundles_to_generation(db_path, zip_path, "sha", ["2"])

        # Registration is fully rolled back; no dangling registered build.
        assert store._active_builds == set()

    def test_rename_failure_unregisters_and_keeps_incumbent(self, tmp_path: Path, monkeypatch):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_bundles_to_generation(db_path, zip_path, "sha1", ["2"])
        incumbent = store.resolve_db(db_path)
        assert incumbent.exists()

        def failing_rename(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(os, "rename", failing_rename)
        with pytest.raises(OSError):
            import_bundles_to_generation(db_path, zip_path, "sha2", ["2"])

        # Registration rolled back and the live generation is untouched.
        assert store._active_builds == set()
        assert store.resolve_db(db_path) == incumbent
        assert incumbent.exists()
