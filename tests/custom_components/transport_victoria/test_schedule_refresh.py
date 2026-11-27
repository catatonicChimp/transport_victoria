"""Unit tests for the GTFS schedule refresh flow (generation model).

Covers: download→validate→import→atomic pointer publish; failure retention;
the process-global per-DB guard (concurrency + cancellation); union-of-bundles
rebuilds; and the reviewer's regression scenarios (schema-stale rebuild on an
unchanged ZIP, aged-window rebuild, honest failure reporting, held readers
across activation, unload/reload with pending retries).

The download/HEAD steps are monkeypatched to serve GTFS fixture ZIPs built in
memory; a minimal FakeHass supplies async_add_executor_job / async_create_task
backed by the real event loop.
"""

from __future__ import annotations

import asyncio
import csv
import io
import sqlite3
import threading
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.transport_victoria as tv
from custom_components.transport_victoria.const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_STOP_ID,
    DOMAIN,
    GTFS_SCHEDULE_URL,
    GTFS_SCHEMA_VERSION,
)
from custom_components.transport_victoria.gtfs import store
from custom_components.transport_victoria.gtfs.horizon import (
    compute_horizon,
    get_schema_version,
    imported_bundles,
    read_bundle_horizons,
)
from custom_components.transport_victoria.gtfs.repository import is_db_ready

from custom_components.transport_victoria import ScheduleManager

_SCHEMA_PATH = (
    Path(__file__).parents[3]
    / "custom_components"
    / "transport_victoria"
    / "gtfs"
    / "schema.sql"
)


# ---------------------------------------------------------------------------
# Fixture ZIP builders
# ---------------------------------------------------------------------------

def _inner_gtfs_bytes(route_type: int, horizon_days: int, rid: str, sid: str) -> bytes:
    """Build one bundle's google_transit.zip (single route, all-days service)."""
    today = date.today()
    start = (today - timedelta(days=1)).strftime("%Y%m%d")
    end = (today + timedelta(days=horizon_days)).strftime("%Y%m%d")
    cal = {
        "service_id": sid, "start_date": start, "end_date": end,
        "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1",
        "friday": "1", "saturday": "1", "sunday": "1",
    }

    def _csv(rows: list[dict]) -> bytes:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue().encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("agency.txt", _csv([{
            "agency_id": "1", "agency_name": "PTV", "agency_url": "",
            "agency_timezone": "Australia/Melbourne", "agency_lang": "en",
            "agency_phone": "",
        }]))
        z.writestr("routes.txt", _csv([{
            "route_id": rid, "agency_id": "1", "route_short_name": rid,
            "route_long_name": rid, "route_type": str(route_type),
            "route_color": "0099CC", "route_text_color": "FFFFFF",
        }]))
        z.writestr("calendar.txt", _csv([cal]))
        z.writestr("trips.txt", _csv([{
            "trip_id": f"T-{rid}", "route_id": rid, "service_id": sid,
            "shape_id": "", "trip_headsign": "Flinders Street",
            "direction_id": "0", "block_id": "",
        }]))
        z.writestr("stops.txt", _csv([{
            "stop_id": f"S-{rid}", "stop_name": "Station", "stop_lat": "-37.74",
            "stop_lon": "144.96", "stop_code": "1", "location_type": "",
            "parent_station": "", "platform_code": "", "wheelchair_boarding": "",
        }]))
        z.writestr("stop_times.txt", _csv([{
            "trip_id": f"T-{rid}", "stop_id": f"S-{rid}", "stop_sequence": "1",
            "arrival_time": "09:00:00", "departure_time": "09:00:00",
            "pickup_type": "0", "drop_off_type": "0", "timepoint": "1",
        }]))
    buf.seek(0)
    return buf.getvalue()


def _outer_zip_bytes(folders: list[tuple[str, int, int]]) -> bytes:
    """Build an outer PTV ZIP. folders = [(folder, route_type, horizon_days)]."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for folder, route_type, horizon_days in folders:
            inner = _inner_gtfs_bytes(route_type, horizon_days, f"R-{folder}", f"SVC-{folder}")
            z.writestr(f"{folder}/google_transit.zip", inner)
    return buf.getvalue()


def _make_zip_bytes(horizon_days: int = 30) -> bytes:
    return _outer_zip_bytes([("2", 400, horizon_days)])


# ---------------------------------------------------------------------------
# FakeHass
# ---------------------------------------------------------------------------

class FakeHass:
    """Minimal hass double backed by the real event loop.

    ``config.path`` ignores the subdirectory and returns tmp_path, so
    ``_db_path``/``_zip_path`` resolve to ``tmp_path/gtfs.db``/``gtfs.zip``.
    ``entries`` is a list of SimpleNamespace(entry_id, data=...) used by
    ``_bundles_for_all_entries``.
    """

    def __init__(self, tmp_path: Path, entries: list | None = None) -> None:
        self.data: dict = {}
        self.config = SimpleNamespace(path=lambda _name: str(tmp_path))
        self.executor_jobs: list[str] = []
        self._entries: list = entries or []
        self._config_entries = _ConfigEntries(self._entries)

    async def async_add_executor_job(self, fn, *args):
        self.executor_jobs.append(getattr(fn, "__name__", repr(fn)))
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)

    def async_create_task(self, coro):
        return asyncio.create_task(coro)

    @property
    def loop(self):
        return asyncio.get_running_loop()

    @property
    def config_entries(self):
        return self._config_entries


class _ConfigEntries:
    def __init__(self, entries: list) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list:
        return self._entries


def _watch_entry(entry_id: str, mode: str):
    return SimpleNamespace(entry_id=entry_id, data={CONF_MODE: mode, CONF_ENTRY_TYPE: "watch"})


# ---------------------------------------------------------------------------
# Downloader / HEAD fakes
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _resolve_offline(monkeypatch):
    """Never hit the CKAN endpoint from unit tests — use the fallback URL."""
    async def _resolve(hass, fallback=None):
        return GTFS_SCHEDULE_URL

    monkeypatch.setattr(tv, "resolve_gtfs_schedule_url", _resolve)


async def _always_changed(hass, url, stored_etag, stored_size) -> bool:
    return True


async def _never_changed(hass, url, stored_etag, stored_size) -> bool:
    return False


def _make_downloader(zip_bytes: bytes, *, fail: bool = False, sha: str = "sha0"):
    """Return an async fake for tv.download_zip serving fixed bytes."""

    async def download(hass, url, dest_path: Path, *, skip_if_exists: bool = False):
        if fail:
            raise RuntimeError("simulated download failure")
        if skip_if_exists and dest_path.exists():
            return False, sha, '"etag"', "1234"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(zip_bytes)
        return True, sha, '"etag"', "1234"

    return download


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "gtfs.db"


@pytest.fixture
def zip_path(tmp_path: Path) -> Path:
    return tmp_path / "gtfs.zip"


def _make_stale_db(db_path: Path) -> None:
    """Write a flat (legacy) schema-version-0 DB at db_path."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()


def _open_generation(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(store.resolve_db(db_path)))


def _generation_count(db_path: Path) -> int:
    gd = store.generations_dir(db_path)
    return len(list(gd.glob("*.db"))) if gd.is_dir() else 0


# ---------------------------------------------------------------------------
# End-to-end refresh
# ---------------------------------------------------------------------------

class TestRefresh:
    async def test_refresh_publishes_generation_and_pointer(self, tmp_path, db_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))

        hass = FakeHass(tmp_path)
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is True
        assert is_db_ready(db_path) is True
        assert get_schema_version(db_path) == GTFS_SCHEMA_VERSION
        assert compute_horizon(db_path) == date.today() + timedelta(days=30)
        # A generation file + a pointer exist.
        assert _generation_count(db_path) == 1
        assert store.read_pointer(db_path) is not None

    async def test_import_ran_in_executor(self, tmp_path, db_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))

        hass = FakeHass(tmp_path)
        await tv.async_trigger_gtfs_refresh(hass, ["2"])
        # Blocking import must have run via the executor, never on the loop.
        assert "_run_import" in hass.executor_jobs

    async def test_refresh_uses_ckan_resolved_url(self, tmp_path, db_path, monkeypatch):
        """Both the HEAD check and the download must use the resolved URL.

        The hardcoded constant is only a fallback — the portal moves resources.
        """
        live_url = "https://example.invalid/live/gtfs.zip"
        seen: dict[str, str] = {}

        async def resolve(hass, fallback=None):
            return live_url

        async def head(hass, url, stored_etag, stored_size):
            seen["head"] = url
            return True

        downloader = _make_downloader(_make_zip_bytes(30))

        async def download(hass, url, dest_path, *, skip_if_exists=False):
            seen["download"] = url
            return await downloader(
                hass, url, dest_path, skip_if_exists=skip_if_exists
            )

        monkeypatch.setattr(tv, "resolve_gtfs_schedule_url", resolve)
        monkeypatch.setattr(tv, "check_for_remote_changes", head)
        monkeypatch.setattr(tv, "download_zip", download)

        hass = FakeHass(tmp_path)
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])

        assert ok is True
        assert seen == {"head": live_url, "download": live_url}

    async def test_refresh_retries_pinned_url_when_resolved_url_fails(
        self, tmp_path, db_path, monkeypatch
    ):
        """Stale CKAN metadata pointing at a dead resource must not break setup."""
        live_url = "https://example.invalid/stale/gtfs.zip"
        seen: list[str] = []

        async def resolve(hass, fallback=None):
            return live_url

        good_download = _make_downloader(_make_zip_bytes(30))

        async def download(hass, url, dest_path, *, skip_if_exists=False):
            seen.append(url)
            if url == live_url:
                raise RuntimeError("HTTP 404")
            return await good_download(
                hass, url, dest_path, skip_if_exists=skip_if_exists
            )

        monkeypatch.setattr(tv, "resolve_gtfs_schedule_url", resolve)
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", download)

        hass = FakeHass(tmp_path)
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])

        assert ok is True
        assert seen == [live_url, GTFS_SCHEDULE_URL]

    async def test_healthy_unchanged_is_noop(self, tmp_path, db_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))
        hass = FakeHass(tmp_path)
        await tv.async_trigger_gtfs_refresh(hass, ["2"])

        # Now healthy + unchanged upstream → no import (still one generation).
        monkeypatch.setattr(tv, "check_for_remote_changes", _never_changed)
        import_calls = {"n": 0}
        real = tv.import_bundles_to_generation

        def counting(*a, **k):
            import_calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(tv, "import_bundles_to_generation", counting)
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is True
        assert import_calls["n"] == 0


# ---------------------------------------------------------------------------
# Failure retention
# ---------------------------------------------------------------------------

class TestFailureRetention:
    async def test_download_failure_returns_false(self, tmp_path, db_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30), fail=True))
        hass = FakeHass(tmp_path)
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is False
        assert store.read_pointer(db_path) is None

    async def test_import_failure_keeps_old_generation_and_pointer(
        self, tmp_path, db_path, monkeypatch
    ):
        # Establish a good generation.
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30), sha="good"))
        hass = FakeHass(tmp_path)
        await tv.async_trigger_gtfs_refresh(hass, ["2"])
        old_gen = store.resolve_db(db_path)
        old_horizon = compute_horizon(db_path)

        # Now a zip that imports successfully but is rejected for expired horizon.
        monkeypatch.setattr(
            tv, "download_zip", _make_downloader(_make_zip_bytes(-10), sha="expired")
        )
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is False
        # Old generation + pointer untouched and still usable.
        assert store.resolve_db(db_path) == old_gen
        assert compute_horizon(db_path) == old_horizon
        assert is_db_ready(db_path) is True

    async def test_missing_bundle_is_error_not_success(self, tmp_path, db_path, monkeypatch):
        # Request a bundle folder that isn't in the ZIP.
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))
        hass = FakeHass(tmp_path)
        ok = await tv.async_trigger_gtfs_refresh(hass, ["6"])
        assert ok is False
        assert store.read_pointer(db_path) is None


# ---------------------------------------------------------------------------
# Reviewer scenarios (P1-1)
# ---------------------------------------------------------------------------

class TestRebuildDecision:
    async def test_schema_stale_db_rebuilds_on_unchanged_zip(self, tmp_path, db_path, monkeypatch):
        # A version-0 DB, upstream unchanged → must still rebuild (no false success).
        _make_stale_db(db_path)
        monkeypatch.setattr(tv, "check_for_remote_changes", _never_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))

        import_calls = {"n": 0}
        real = tv.import_bundles_to_generation

        def counting(*a, **k):
            import_calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(tv, "import_bundles_to_generation", counting)

        hass = FakeHass(tmp_path)
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is True
        assert import_calls["n"] == 1
        assert get_schema_version(db_path) == GTFS_SCHEMA_VERSION

    async def test_aged_window_rebuilds_on_unchanged_zip(self, tmp_path, db_path, monkeypatch):
        # Import normally, then age the imported_at so the 45-day window is stale.
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))
        hass = FakeHass(tmp_path)
        await tv.async_trigger_gtfs_refresh(hass, ["2"])

        # Rewind imported_at by 10 days (past the weekly max age).
        conn = _open_generation(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO import_meta VALUES ('imported_at', ?)",
            [(date.today() - timedelta(days=10)).isoformat()],
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(tv, "check_for_remote_changes", _never_changed)
        import_calls = {"n": 0}
        real = tv.import_bundles_to_generation

        def counting(*a, **k):
            import_calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(tv, "import_bundles_to_generation", counting)
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is True
        assert import_calls["n"] == 1  # rebuilt despite unchanged ZIP

    async def test_failed_import_then_retry_same_zip_rebuilds(self, tmp_path, db_path, monkeypatch):
        # Import a good short-horizon zip, then a transient import failure of a NEW
        # zip, then retry with the IDENTICAL cached bytes — the rebuild must run.
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(5), sha="shaA"))
        hass = FakeHass(tmp_path)
        assert await tv.async_trigger_gtfs_refresh(hass, ["2"]) is True
        assert compute_horizon(db_path) == date.today() + timedelta(days=5)

        # New upstream (shaB, longer horizon). First import attempt fails transiently.
        monkeypatch.setattr(
            tv, "download_zip", _make_downloader(_make_zip_bytes(60), sha="shaB")
        )
        real = tv.import_bundles_to_generation
        calls = {"n": 0}

        def flaky_import(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient import failure")
            return real(*a, **k)

        monkeypatch.setattr(tv, "import_bundles_to_generation", flaky_import)
        assert await tv.async_trigger_gtfs_refresh(hass, ["2"]) is False

        # Retry with the IDENTICAL cached bytes (shaB) — the DB's stored sha (shaA)
        # still differs from the cached zip, so it must rebuild (not falsely skip).
        monkeypatch.setattr(tv, "import_bundles_to_generation", real)
        assert await tv.async_trigger_gtfs_refresh(hass, ["2"]) is True
        assert compute_horizon(db_path) == date.today() + timedelta(days=60)

    async def test_healthy_db_different_sha_rebuilds(self, tmp_path, db_path, monkeypatch):
        # Branch isolation: a HEALTHY DB (far from the expiry margin, so
        # must_rebuild is False) with an OLD stored sha rebuilds purely because the
        # cached download carries a DIFFERENT sha — not because the horizon is due.
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30), sha="shaA"))
        hass = FakeHass(tmp_path)
        assert await tv.async_trigger_gtfs_refresh(hass, ["2"]) is True
        # DB is healthy: horizon +30 (not within 7-day margin), imported today.
        assert compute_horizon(db_path) == date.today() + timedelta(days=30)

        import_calls = {"n": 0}
        real = tv.import_bundles_to_generation

        def counting(*a, **k):
            import_calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(tv, "import_bundles_to_generation", counting)
        # Same horizon (30) but a different sha → rebuild must still happen.
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30), sha="shaB"))
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is True
        assert import_calls["n"] == 1  # rebuilt due to sha mismatch, not horizon

    async def test_expired_unchanged_feed_reports_failure(self, tmp_path, db_path, monkeypatch):
        # Import a short-horizon zip, then advance the service date past its
        # horizon and keep the upstream unchanged → refresh must report False.
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(5)))
        hass = FakeHass(tmp_path)
        await tv.async_trigger_gtfs_refresh(hass, ["2"])

        future_today = date.today() + timedelta(days=10)
        monkeypatch.setattr(tv, "_service_today", lambda hass: future_today)
        monkeypatch.setattr(tv, "check_for_remote_changes", _never_changed)

        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is False  # unchanged expired feed → honest failure


# ---------------------------------------------------------------------------
# Union-of-bundles (P1-4)
# ---------------------------------------------------------------------------

class TestUnionBundles:
    async def test_startup_refresh_builds_union(self, tmp_path, db_path, monkeypatch):
        # Two live entries: metro (folder 2) + regional bus (folder 6).
        entries = [_watch_entry("a", "metro"), _watch_entry("b", "regional-bus")]
        zip_bytes = _outer_zip_bytes([("2", 400, 30), ("6", 701, 30)])
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(zip_bytes))

        hass = FakeHass(tmp_path, entries=entries)
        # Trigger with only metro's bundle — the union (2 + 6) must be built.
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is True

        conn = _open_generation(db_path)
        imported = {r[0].removeprefix("bundle_") for r in conn.execute(
            "SELECT key FROM import_meta WHERE key LIKE 'bundle_%'"
        )}
        conn.close()
        assert imported == {"2", "6"}

    async def test_missing_second_bundle_triggers_refresh(self, tmp_path, db_path, monkeypatch):
        entries = [_watch_entry("a", "metro"), _watch_entry("b", "regional-bus")]
        # ZIP only contains metro (folder 2); regional bus (6) is missing.
        zip_bytes = _outer_zip_bytes([("2", 400, 30)])
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(zip_bytes))

        hass = FakeHass(tmp_path, entries=entries)
        # Union is ["2", "6"] but "6" is missing from the ZIP → import fails.
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is False
        assert store.read_pointer(db_path) is None


# ---------------------------------------------------------------------------
# Concurrency guard (P1-3)
# ---------------------------------------------------------------------------

class TestConcurrencyGuard:
    async def test_concurrent_refresh_single_import(self, tmp_path, db_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))
        import_calls = {"n": 0}
        real = tv.import_bundles_to_generation

        def counting(*a, **k):
            import_calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(tv, "import_bundles_to_generation", counting)

        hass = FakeHass(tmp_path)
        results = await asyncio.gather(
            tv.async_trigger_gtfs_refresh(hass, ["2"]),
            tv.async_trigger_gtfs_refresh(hass, ["2"]),
        )
        assert results == [True, True]
        assert import_calls["n"] == 1
        # Final generation passes integrity_check.
        conn = _open_generation(db_path)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.close()

    async def test_waiter_cancellation_shielded(self, tmp_path, db_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        import_calls = {"n": 0}
        real = tv.import_bundles_to_generation

        def counting(*a, **k):
            import_calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(tv, "import_bundles_to_generation", counting)

        started = asyncio.Event()
        gate = asyncio.Event()

        async def slow_download(hass, url, dest_path: Path, *, skip_if_exists: bool = False):
            started.set()
            await gate.wait()  # keep the refresh in flight until released
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(_make_zip_bytes(30))
            return True, "sha0", '"etag"', "1234"

        monkeypatch.setattr(tv, "download_zip", slow_download)

        hass = FakeHass(tmp_path)
        outer = asyncio.create_task(tv.async_trigger_gtfs_refresh(hass, ["2"]))
        await started.wait()  # inner refresh is now blocked inside download
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

        # A second caller dedups onto the still-running (shielded) inner task.
        second = asyncio.create_task(tv.async_trigger_gtfs_refresh(hass, ["2"]))
        await asyncio.sleep(0.02)
        gate.set()
        ok = await second
        assert ok is True
        assert import_calls["n"] == 1
        assert _generation_count(db_path) == 1


# ---------------------------------------------------------------------------
# Held reader across activation (P1-2)
# ---------------------------------------------------------------------------

class TestHeldReaderAcrossActivation:
    async def test_held_reader_sees_old_generation(self, tmp_path, db_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30), sha="sha1"))
        hass = FakeHass(tmp_path)
        await tv.async_trigger_gtfs_refresh(hass, ["2"])
        gen1 = store.resolve_db(db_path)

        # Hold a reader open on generation 1.
        reader = sqlite3.connect(str(gen1))
        before_stops = reader.execute("SELECT COUNT(*) FROM stops").fetchone()[0]
        before_horizon = reader.execute(
            "SELECT MAX(end_date) FROM calendar"
        ).fetchone()[0]

        # Refresh to a new generation (different sha → different content).
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(60), sha="sha2"))
        await tv.async_trigger_gtfs_refresh(hass, ["2"])
        gen2 = store.resolve_db(db_path)
        assert gen2 != gen1

        # The held reader still passes integrity_check and sees OLD content (both
        # the stop count and the OLD horizon — it must not see the new data).
        assert reader.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert reader.execute("SELECT COUNT(*) FROM stops").fetchone()[0] == before_stops
        assert reader.execute("SELECT MAX(end_date) FROM calendar").fetchone()[0] == before_horizon
        reader.close()

        # New opens see the new generation (horizon advanced).
        assert compute_horizon(db_path) == date.today() + timedelta(days=60)


# ---------------------------------------------------------------------------
# Per-bundle provenance + publication validation (F4/F6)
# ---------------------------------------------------------------------------

class TestProvenance:
    async def test_per_bundle_provenance_recorded(self, tmp_path, db_path, monkeypatch):
        zip_bytes = _outer_zip_bytes([("1", 2, 30), ("5", 204, 3)])
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(zip_bytes))
        hass = FakeHass(tmp_path)
        ok = await tv.async_trigger_gtfs_refresh(hass, ["1", "5"])
        assert ok is True
        h = read_bundle_horizons(db_path)
        assert h["1"] == date.today() + timedelta(days=30)
        assert h["5"] == date.today() + timedelta(days=3)

    async def test_mixed_expired_bundle_rejected(self, tmp_path, db_path, monkeypatch):
        # Healthy bundle 1 + expired bundle 5 → publication rejected, incumbent kept.
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_outer_zip_bytes([("1", 2, 30)]), sha="sha1"))
        hass = FakeHass(tmp_path)
        assert await tv.async_trigger_gtfs_refresh(hass, ["1"]) is True
        incumbent = store.resolve_db(db_path)

        monkeypatch.setattr(
            tv, "download_zip",
            _make_downloader(_outer_zip_bytes([("1", 2, 30), ("5", 204, -3)]), sha="sha2"),
        )
        ok = await tv.async_trigger_gtfs_refresh(hass, ["1", "5"])
        assert ok is False
        assert store.resolve_db(db_path) == incumbent

    async def test_incumbent_healthy_bundle_not_discarded(self, tmp_path, db_path, monkeypatch):
        # A REQUIRED bundle that is healthy in the incumbent and would become
        # unhealthy (expired) in the new generation is still protected.
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_outer_zip_bytes([("1", 2, 30)]), sha="sha1"))
        hass = FakeHass(tmp_path)
        assert await tv.async_trigger_gtfs_refresh(hass, ["1"]) is True
        incumbent = store.resolve_db(db_path)

        # Rebuild the SAME bundle 1 but with an expired horizon → rejected.
        monkeypatch.setattr(
            tv, "download_zip",
            _make_downloader(_outer_zip_bytes([("1", 2, -3)]), sha="sha2"),
        )
        ok = await tv.async_trigger_gtfs_refresh(hass, ["1"])
        assert ok is False
        assert store.resolve_db(db_path) == incumbent

    async def test_non_required_incumbent_bundle_dropped(self, tmp_path, db_path, monkeypatch):
        # A healthy incumbent bundle NOT in the requested set may be dropped
        # (intentional removal) — publication succeeds with the smaller set.
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_outer_zip_bytes([("1", 2, 30)]), sha="sha1"))
        hass = FakeHass(tmp_path)
        assert await tv.async_trigger_gtfs_refresh(hass, ["1"]) is True

        # Refresh trains only (bundle 2) — bundle 1 is dropped, not gated.
        monkeypatch.setattr(
            tv, "download_zip",
            _make_downloader(_outer_zip_bytes([("2", 400, 30)]), sha="sha2"),
        )
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is True
        horizons = read_bundle_horizons(db_path)
        assert "2" in horizons
        assert "1" not in horizons

    async def test_removed_bundle_does_not_block_refresh(self, tmp_path, db_path, monkeypatch):
        # train (1) + bus (4) imported healthy → remove bus → refresh trains only
        # → publication succeeds with a trains-only generation and a refreshed
        # train horizon.
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(
            tv, "download_zip",
            _make_downloader(_outer_zip_bytes([("1", 2, 5), ("4", 3, 30)]), sha="sha1"),
        )
        hass = FakeHass(tmp_path)
        assert await tv.async_trigger_gtfs_refresh(hass, ["1", "4"]) is True
        old_train_horizon = read_bundle_horizons(db_path)["1"]
        assert old_train_horizon == date.today() + timedelta(days=5)

        # Bus entry removed; refresh trains (bundle 1) only with a longer horizon.
        monkeypatch.setattr(
            tv, "download_zip",
            _make_downloader(_outer_zip_bytes([("1", 2, 40)]), sha="sha2"),
        )
        ok = await tv.async_trigger_gtfs_refresh(hass, ["1"])
        assert ok is True
        horizons = read_bundle_horizons(db_path)
        assert horizons["1"] == date.today() + timedelta(days=40)  # refreshed
        assert "4" not in horizons  # bus dropped


# ---------------------------------------------------------------------------
# Concurrency: different bundle requirements (F3)
# ---------------------------------------------------------------------------

class TestDifferentBundleRequirements:
    async def test_direct_caller_different_bundles_triggers_followup(self, tmp_path, db_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        started = asyncio.Event()
        gate = asyncio.Event()
        zip_all = _outer_zip_bytes([("2", 400, 30), ("6", 701, 30)])

        async def slow_download(hass, url, dest_path: Path, *, skip_if_exists: bool = False):
            started.set()
            await gate.wait()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(zip_all)
            return True, "sha0", '"etag"', "1234"

        monkeypatch.setattr(tv, "download_zip", slow_download)

        hass = FakeHass(tmp_path)
        # A manager-style refresh builds bundle 2 (in flight).
        mgr = asyncio.create_task(tv.async_trigger_gtfs_refresh(hass, ["2"]))
        await started.wait()
        # A direct (config-flow) caller needs bundle 6 — not covered by {2}.
        direct = asyncio.create_task(tv.async_trigger_gtfs_refresh(hass, ["6"]))
        await asyncio.sleep(0.02)
        gate.set()
        ok1 = await mgr
        ok2 = await direct
        assert ok1 is True
        assert ok2 is True
        # After both resolve, the DB contains BOTH bundles.
        assert imported_bundles(db_path) >= {"2", "6"}


# ---------------------------------------------------------------------------
# Cancellation during executor import (tightening)
# ---------------------------------------------------------------------------

class TestCancellationDuringImport:
    async def test_waiter_cancellation_during_import(self, tmp_path, db_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))
        real = tv.import_bundles_to_generation
        import_calls = {"n": 0}
        import_started = threading.Event()
        release = threading.Event()

        def gated_import(*a, **k):
            import_calls["n"] += 1
            import_started.set()
            release.wait(timeout=10)  # block inside the executor thread
            return real(*a, **k)

        monkeypatch.setattr(tv, "import_bundles_to_generation", gated_import)

        hass = FakeHass(tmp_path)
        outer = asyncio.create_task(tv.async_trigger_gtfs_refresh(hass, ["2"]))
        await asyncio.to_thread(import_started.wait)  # import is now in-flight
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

        # Release the shielded import; it must finish and publish exactly once.
        release.set()
        for _ in range(200):
            if _generation_count(db_path) >= 1:
                break
            await asyncio.sleep(0.01)

        # A subsequent caller succeeds without a hidden replacement import.
        ok = await tv.async_trigger_gtfs_refresh(hass, ["2"])
        assert ok is True
        assert import_calls["n"] == 1  # exactly one import, no replacement
        assert _generation_count(db_path) == 1


# ---------------------------------------------------------------------------
# Manager lifecycle (P2-6)
# ---------------------------------------------------------------------------

class TestManagerLifecycle:
    async def test_unload_cancels_pending_retry(self, tmp_path, db_path, zip_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30), fail=True))

        hass = FakeHass(tmp_path)
        manager = ScheduleManager(hass, db_path, zip_path)
        manager.register_consumer("entry-a")

        # A failing refresh schedules a retry.
        assert await manager.refresh(["2"]) is False
        assert len(manager._retry_tasks) == 1

        # Unload the last consumer → shutdown cancels the retry.
        await manager.consumer_unloaded("entry-a")
        assert manager.stopped is True
        assert manager._retry_tasks == {}

    async def test_no_import_after_shutdown(self, tmp_path, db_path, zip_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))

        hass = FakeHass(tmp_path)
        manager = ScheduleManager(hass, db_path, zip_path)
        manager.register_consumer("entry-a")
        await manager.consumer_unloaded("entry-a")

        # A refresh after shutdown must not start an import.
        ok = await manager.refresh(["2"])
        assert ok is False
        assert _generation_count(db_path) == 0

    async def test_fresh_manager_after_unload_no_clash(self, tmp_path, db_path, zip_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))

        hass = FakeHass(tmp_path)
        m1 = ScheduleManager(hass, db_path, zip_path)
        m1.register_consumer("entry-a")
        await m1.consumer_unloaded("entry-a")
        assert m1.stopped

        # A fresh manager (same DB path) reuses the process-global guard.
        m2 = ScheduleManager(hass, db_path, zip_path)
        m2.register_consumer("entry-a")
        ok = await m2.refresh(["2"])
        assert ok is True
        assert _generation_count(db_path) == 1

    async def test_consumer_registration_idempotent(self, tmp_path, db_path, zip_path):
        hass = FakeHass(tmp_path)
        manager = ScheduleManager(hass, db_path, zip_path)
        manager.register_consumer("entry-a")
        # A setup retry re-registers the same entry_id — must not double-count.
        manager.register_consumer("entry-a")
        assert manager._consumers == {"entry-a"}
        assert manager._cancel_check is not None  # tick started exactly once

        # A failed first refresh rolls back the registration (setup failure).
        await manager.consumer_unloaded("entry-a")
        assert manager._consumers == set()
        assert manager.stopped is True

    async def test_setup_retry_rollback_then_success_then_unload(self, tmp_path, db_path, zip_path):
        hass = FakeHass(tmp_path)
        manager = ScheduleManager(hass, db_path, zip_path)
        # First setup attempt registers.
        manager.register_consumer("entry-a")
        assert manager._consumers == {"entry-a"}
        # First refresh fails → roll back.
        await manager.consumer_unloaded("entry-a")
        assert manager.stopped is True

        # HA retries setup: a fresh manager (replacing the stopped one) registers.
        m2 = ScheduleManager(hass, db_path, zip_path)
        m2.register_consumer("entry-a")
        assert m2._consumers == {"entry-a"}
        assert m2.stopped is False
        # Success, then unload → fully stopped, no dangling consumers.
        await m2.consumer_unloaded("entry-a")
        assert m2._consumers == set()
        assert m2.stopped is True

    async def test_ensure_fresh_skips_when_healthy(self, tmp_path, db_path, zip_path, monkeypatch):
        monkeypatch.setattr(tv, "check_for_remote_changes", _always_changed)
        monkeypatch.setattr(tv, "download_zip", _make_downloader(_make_zip_bytes(30)))
        hass = FakeHass(tmp_path)
        await tv.async_trigger_gtfs_refresh(hass, ["2"])

        download_calls = {"n": 0}

        async def counting_download(hass, url, dest_path: Path, *, skip_if_exists: bool = False):
            download_calls["n"] += 1
            return True, "sha0", '"etag"', "1234"

        monkeypatch.setattr(tv, "download_zip", counting_download)

        manager = ScheduleManager(hass, db_path, zip_path)
        triggered = await manager.ensure_fresh(["2"])
        assert triggered is False
        assert download_calls["n"] == 0


# ---------------------------------------------------------------------------
# Real _async_setup_watch lifecycle (transactional consumer registration)
# ---------------------------------------------------------------------------

class _SetupConfigEntries:
    def __init__(self, forward_raises: bool) -> None:
        self._forward_raises = forward_raises

    def async_entries(self, domain: str) -> list:
        return []

    async def async_forward_entry_setups(self, entry, platforms):
        if self._forward_raises:
            raise RuntimeError("platform forward failed")
        return None


class _SetupHass:
    """A hass double sufficient to run the real `_async_setup_watch`."""

    def __init__(self, tmp_path: Path, forward_raises: bool) -> None:
        self.data: dict = {DOMAIN: {}}
        self.config = SimpleNamespace(path=lambda _name: str(tmp_path))
        self._ce = _SetupConfigEntries(forward_raises)

    async def async_add_executor_job(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)

    def async_create_task(self, coro):
        return asyncio.create_task(coro)

    @property
    def loop(self):
        return asyncio.get_running_loop()

    @property
    def config_entries(self):
        return self._ce


def _setup_entry(entry_id: str = "e1", mode: str = "metro"):
    return SimpleNamespace(
        entry_id=entry_id,
        data={CONF_API_KEY: "k", CONF_MODE: mode, CONF_STOP_ID: "S1"},
        options={},
        runtime_data=None,
        add_update_listener=lambda cb: (lambda: None),
    )


class TestRealSetupWatch:
    async def test_forward_failure_rolls_back_registration(self, tmp_path, monkeypatch):
        async def _noop_ensure_fresh(self, bundle_folders, today=None):
            return False

        async def _noop_first_refresh(self):
            return None

        monkeypatch.setattr(tv.ScheduleManager, "ensure_fresh", _noop_ensure_fresh)
        monkeypatch.setattr(
            tv.PTVDataUpdateCoordinator, "async_config_entry_first_refresh", _noop_first_refresh
        )

        hass = _SetupHass(tmp_path, forward_raises=True)
        entry = _setup_entry()
        with pytest.raises(RuntimeError):
            await tv._async_setup_watch(hass, entry)

        manager = hass.data[DOMAIN][tv._SCHEDULE_MANAGER_KEY]
        assert manager._consumers == set()
        assert manager.stopped is True

    async def test_cancel_mid_setup_rolls_back_registration(self, tmp_path, monkeypatch):
        async def _noop_ensure_fresh(self, bundle_folders, today=None):
            return False

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_first_refresh(self):
            started.set()
            await release.wait()
            return None

        monkeypatch.setattr(tv.ScheduleManager, "ensure_fresh", _noop_ensure_fresh)
        monkeypatch.setattr(
            tv.PTVDataUpdateCoordinator, "async_config_entry_first_refresh", _slow_first_refresh
        )

        hass = _SetupHass(tmp_path, forward_raises=False)
        entry = _setup_entry()
        task = asyncio.create_task(tv._async_setup_watch(hass, entry))
        await started.wait()  # reached the (blocked) first refresh
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        manager = hass.data[DOMAIN][tv._SCHEDULE_MANAGER_KEY]
        assert manager._consumers == set()
        assert manager.stopped is True

    async def test_successful_setup_then_unload_stops(self, tmp_path, monkeypatch):
        async def _noop_ensure_fresh(self, bundle_folders, today=None):
            return False

        async def _noop_first_refresh(self):
            return None

        monkeypatch.setattr(tv.ScheduleManager, "ensure_fresh", _noop_ensure_fresh)
        monkeypatch.setattr(
            tv.PTVDataUpdateCoordinator, "async_config_entry_first_refresh", _noop_first_refresh
        )

        hass = _SetupHass(tmp_path, forward_raises=False)
        entry = _setup_entry()
        assert await tv._async_setup_watch(hass, entry) is True

        manager = hass.data[DOMAIN][tv._SCHEDULE_MANAGER_KEY]
        assert manager._consumers == {"e1"}
        assert manager.stopped is False

        # Unload the entry → consumer released → manager stopped.
        await manager.consumer_unloaded("e1")
        assert manager._consumers == set()
        assert manager.stopped is True
