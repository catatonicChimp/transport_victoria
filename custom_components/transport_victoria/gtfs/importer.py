"""GTFS ZIP importer.

Reads a PTV GTFS ZIP file and imports all tables into a SQLite database.
Uses stdlib csv + sqlite3.executemany — no pandas, no ORM — so it runs
quickly on low-power hardware and adds no extra HA dependencies.

Generation architecture (see gtfs/store.py):

  * Every import writes a NEW, uniquely-named *generation* file into a
    ``generations/`` subdirectory, rather than overwriting the live DB.  The
    generation is checkpointed out of WAL and closed before publication, so it
    has no ``-wal``/``-shm`` sidecars and is immutable once published.
  * Publication is an atomic pointer update (``current.json``) — see
    :func:`store.publish_pointer`.  A failed import leaves the previous pointer
    and generation untouched (the new generation file is simply discarded).
  * All requested bundles are imported into the one generation (no partial
    in-place writes), and every requested bundle must exist in the ZIP or the
    import fails before publication.

Typical import time for the ~229 MB PTV ZIP: under 2 minutes on a Pi 4.

Optimisations applied:
  - shapes.txt is skipped (11 M geometry rows with no value for departure queries)
  - stop_times.txt is filtered to trips active within the next 45 days, reducing
    the bus bundle from ~18 M rows to a much smaller working set
  - calendar / calendar_dates / trips are imported before stop_times so the
    valid-trip filter can be computed from data already in the database
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
import zipfile
from collections.abc import Callable
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..const import GTFS_SCHEMA_VERSION
from . import store
from .horizon import (
    compute_horizon_from_conn,
    imported_bundles,
    read_bundle_horizons,
    stored_zip_meta,
)
from .time_utils import gtfs_time_to_seconds

_LOGGER = logging.getLogger(__name__)

# Chunk size for executemany batches — keeps memory use bounded
_BATCH_SIZE = 5000

# Only import stop_times for trips that run within this many days from today
_TRIP_WINDOW_DAYS = 45

# Core tables that must be non-empty in a successfully imported database.
# calendar is excluded because a frequency-based feed may use calendar_dates
# exclusively; _assert_db_integrity checks calendar OR calendar_dates below.
_REQUIRED_NONEMPTY_TABLES = ("agency", "routes", "trips", "stops", "stop_times")

# A real GTFS bundle is far larger than this; guards against a truncated file.
_MIN_ZIP_BYTES = 64


def _optional_int(value: str) -> int | None:
    return int(value) if value.strip() else None


def _optional_float(value: str) -> float | None:
    return float(value) if value.strip() else None


# GTFS files we import and their target tables, in processing order.
# calendar / calendar_dates / trips must come before stop_times so that
# _compute_valid_trip_ids() has data to query when stop_times is processed.
# shapes.txt is intentionally excluded — see module docstring.
_GTFS_FILES: dict[str, str] = {
    "agency.txt": "agency",
    "routes.txt": "routes",
    "calendar.txt": "calendar",
    "calendar_dates.txt": "calendar_dates",
    "trips.txt": "trips",
    "stops.txt": "stops",
    "stop_times.txt": "stop_times",
}

# Column definitions per table: (csv_column, db_column, type_converter)
# type_converter is called on the raw string value. None means pass through as-is.
# departure_seconds / arrival_seconds are computed from the time string columns.
_TABLE_COLUMNS: dict[str, list[tuple[str, str, Callable[[str], Any] | None]]] = {
    "agency": [
        ("agency_id", "agency_id", None),
        ("agency_name", "agency_name", None),
        ("agency_url", "agency_url", None),
        ("agency_timezone", "agency_timezone", None),
        ("agency_lang", "agency_lang", None),
        ("agency_phone", "agency_phone", None),
    ],
    "routes": [
        ("route_id", "route_id", None),
        ("agency_id", "agency_id", None),
        ("route_short_name", "route_short_name", None),
        ("route_long_name", "route_long_name", None),
        ("route_type", "route_type", int),
        ("route_color", "route_color", None),
        ("route_text_color", "route_text_color", None),
    ],
    "trips": [
        ("trip_id", "trip_id", None),
        ("route_id", "route_id", None),
        ("service_id", "service_id", None),
        ("shape_id", "shape_id", None),
        ("trip_headsign", "trip_headsign", None),
        ("direction_id", "direction_id", _optional_int),
        ("block_id", "block_id", None),
    ],
    "stops": [
        ("stop_id", "stop_id", None),
        ("stop_name", "stop_name", None),
        ("stop_lat", "stop_lat", _optional_float),
        ("stop_lon", "stop_lon", _optional_float),
        ("stop_code", "stop_code", None),
        ("location_type", "location_type", _optional_int),
        ("parent_station", "parent_station", None),
        ("platform_code", "platform_code", None),
        ("wheelchair_boarding", "wheelchair_boarding", _optional_int),
    ],
    # stop_times has special handling — see _import_stop_times()
    "stop_times": [],
    "calendar": [
        ("service_id", "service_id", None),
        ("monday", "monday", int),
        ("tuesday", "tuesday", int),
        ("wednesday", "wednesday", int),
        ("thursday", "thursday", int),
        ("friday", "friday", int),
        ("saturday", "saturday", int),
        ("sunday", "sunday", int),
        ("start_date", "start_date", None),
        ("end_date", "end_date", None),
    ],
    "calendar_dates": [
        ("service_id", "service_id", None),
        ("date", "date", None),
        ("exception_type", "exception_type", int),
    ],
}


def _compute_valid_trip_ids(
    conn: sqlite3.Connection,
    window_days: int = _TRIP_WINDOW_DAYS,
    today: date | None = None,
) -> frozenset[str]:
    """Return trip_ids whose service runs within the next window_days days.

    Queries calendar and calendar_dates from the already-imported data in conn.
    Errs on the side of inclusion: a service is included if it has any active
    dates in the window (individual exception removals are not subtracted here).
    This keeps the logic simple while still cutting the majority of stale rows.

    ``today`` is the service-local date (HA timezone); it defaults to the host
    date only for CLI/test convenience.
    """
    today = today or date.today()
    end = today + timedelta(days=window_days)
    today_str = today.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    sql = """
        SELECT DISTINCT t.trip_id
        FROM trips t
        WHERE t.service_id IN (
            -- Regular calendar services whose date range overlaps the window
            -- and has at least one scheduled day of the week
            SELECT service_id FROM calendar
             WHERE start_date <= ? AND end_date >= ?
               AND (monday=1 OR tuesday=1 OR wednesday=1 OR thursday=1
                    OR friday=1 OR saturday=1 OR sunday=1)
            UNION
            -- Exception-added service dates within the window
            SELECT service_id FROM calendar_dates
             WHERE date >= ? AND date <= ? AND exception_type = 1
        )
    """
    try:
        rows = conn.execute(sql, [end_str, today_str, today_str, end_str]).fetchall()
        return frozenset(row[0] for row in rows)
    except sqlite3.Error as exc:
        _LOGGER.warning(
            "Could not compute valid trip IDs (calendar not yet imported?): %s — "
            "importing all stop_times without filter",
            exc,
        )
        return frozenset()


def _import_gtfs_files(
    conn: sqlite3.Connection,
    zf: zipfile.ZipFile,
    bundle_label: str,
    today: date | None = None,
) -> None:
    """Import all known GTFS txt files from an open ZipFile into conn.

    calendar / calendar_dates / trips are processed before stop_times (see
    _GTFS_FILES ordering) so that _compute_valid_trip_ids() has data to query
    when stop_times is reached.
    """
    files = list(_GTFS_FILES.items())
    prefix = f"[{bundle_label}] " if bundle_label else ""
    trip_id_filter: frozenset[str] | None = None

    for idx, (filename, table) in enumerate(files, start=1):
        reader = _open_csv(zf, filename)
        if reader is None:
            continue

        _LOGGER.info("%sImporting %s [%d/%d]", prefix, filename, idx, len(files))
        t0 = datetime.now(tz=timezone.utc)

        if table == "stop_times":
            if trip_id_filter is None:
                trip_id_filter = _compute_valid_trip_ids(conn, today=today)
                _LOGGER.info(
                    "%sTrip filter: %d trips active in next %d days",
                    prefix,
                    len(trip_id_filter),
                    _TRIP_WINDOW_DAYS,
                )
            count = _import_stop_times(conn, reader, trip_id_filter)
        else:
            count = _import_generic(conn, reader, table, _TABLE_COLUMNS[table])

        elapsed_s = (datetime.now(tz=timezone.utc) - t0).total_seconds()
        _LOGGER.info(
            "%s%s done — %d rows in %.1fs", prefix, table, count, elapsed_s
        )
        conn.commit()


def _load_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes from schema.sql."""
    schema_path = Path(__file__).parent / "schema.sql"
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def _open_csv(zf: zipfile.ZipFile, filename: str) -> csv.DictReader[str] | None:
    """Open a file from a ZIP as a DictReader, handling UTF-8 BOM."""
    try:
        raw = zf.open(filename)
    except KeyError:
        _LOGGER.debug("GTFS file not present in ZIP: %s", filename)
        return None
    return csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def _import_generic(
    conn: sqlite3.Connection,
    reader: csv.DictReader[str],
    table: str,
    col_defs: list[tuple[str, str, Callable[[str], Any] | None]],
) -> int:
    """Import rows from a DictReader into a table using batched executemany."""
    db_cols = [col[1] for col in col_defs]
    placeholders = ", ".join("?" * len(db_cols))
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(db_cols)}) VALUES ({placeholders})"

    batch: list[tuple[Any, ...]] = []
    total = 0

    for row in reader:
        values: list[Any] = []
        for csv_col, _db_col, converter in col_defs:
            raw = row.get(csv_col, "") or ""
            values.append(converter(raw) if converter and raw.strip() else (raw or None))
        batch.append(tuple(values))

        if len(batch) >= _BATCH_SIZE:
            conn.executemany(sql, batch)
            total += len(batch)
            batch = []

    if batch:
        conn.executemany(sql, batch)
        total += len(batch)

    return total


def _import_stop_times(
    conn: sqlite3.Connection,
    reader: csv.DictReader[str],
    trip_id_filter: frozenset[str] | None = None,
) -> int:
    """Import stop_times with computed arrival_seconds and departure_seconds.

    stop_times is the largest file. We convert HH:MM:SS time strings to
    integer seconds during import so that departure queries are fast indexed
    integer comparisons rather than string operations.

    If trip_id_filter is provided, only rows whose trip_id is in the set are
    imported. This is used to skip trips that don't run in the next 45 days,
    significantly reducing DB size for the bus bundles.
    """
    sql = """
        INSERT OR REPLACE INTO stop_times
            (trip_id, stop_id, stop_sequence,
             arrival_seconds, departure_seconds,
             pickup_type, drop_off_type, timepoint)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    batch: list[tuple[Any, ...]] = []
    total = 0
    skipped = 0

    for row in reader:
        trip_id = row.get("trip_id") or None

        # Filter to active trips when a filter set is provided
        if trip_id_filter is not None and trip_id not in trip_id_filter:
            skipped += 1
            continue

        arrival_raw = row.get("arrival_time", "").strip()
        departure_raw = row.get("departure_time", "").strip()

        try:
            arrival_seconds = gtfs_time_to_seconds(arrival_raw) if arrival_raw else None
        except ValueError:
            arrival_seconds = None

        try:
            departure_seconds = gtfs_time_to_seconds(departure_raw) if departure_raw else None
        except ValueError:
            departure_seconds = None

        batch.append((
            trip_id,
            row.get("stop_id") or None,
            int(row.get("stop_sequence", 0) or 0),
            arrival_seconds,
            departure_seconds,
            int(row.get("pickup_type", 0) or 0),
            int(row.get("drop_off_type", 0) or 0),
            int(row.get("timepoint", 1) or 1),
        ))

        if len(batch) >= _BATCH_SIZE:
            conn.executemany(sql, batch)
            total += len(batch)
            batch = []

    if batch:
        conn.executemany(sql, batch)
        total += len(batch)

    if skipped:
        _LOGGER.debug("stop_times: skipped %d rows (trip outside 45-day window)", skipped)

    return total


def validate_gtfs_zip(zip_path: Path) -> None:
    """Validate the outer GTFS ZIP before import. Raises ValueError if invalid.

    Checks the file exists, has a sane size, is a real (non-truncated) ZIP,
    and contains at least one entry. Structural validation of the *contents*
    (required core tables present and non-empty) happens post-import in
    `_assert_db_integrity`, so a structurally-fine ZIP that yields an empty
    database is still rejected before the atomic swap.
    """
    if not zip_path.exists():
        raise ValueError(f"GTFS ZIP not found: {zip_path}")
    if zip_path.stat().st_size < _MIN_ZIP_BYTES:
        raise ValueError(
            f"GTFS ZIP is too small ({zip_path.stat().st_size} bytes) — "
            "download may be truncated"
        )
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            if not names:
                raise ValueError(f"GTFS ZIP is empty: {zip_path}")
            bad_entry = zf.testzip()
            if bad_entry is not None:
                raise ValueError(f"GTFS ZIP entry {bad_entry!r} is corrupt/truncated")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Not a valid ZIP: {exc}") from exc


def _assert_db_integrity(conn: sqlite3.Connection) -> None:
    """Raise ValueError if the staged DB is corrupt or missing core data.

    Runs PRAGMA integrity_check and verifies the core tables are non-empty.
    Called on the staging connection before the atomic swap so a bad import
    never replaces the live database.
    """
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if row is None or row[0] != "ok":
        raise ValueError(f"Staged GTFS DB failed integrity check: {row}")

    for table in _REQUIRED_NONEMPTY_TABLES:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if not count:
            raise ValueError(
                f"Staged GTFS DB has no rows in required table '{table}'"
            )

    # Schedule coverage must come from calendar.txt and/or calendar_dates.txt.
    cal = conn.execute("SELECT COUNT(*) FROM calendar").fetchone()[0]
    cal_dates = conn.execute("SELECT COUNT(*) FROM calendar_dates").fetchone()[0]
    if not cal and not cal_dates:
        raise ValueError("Staged GTFS DB has no calendar or calendar_dates rows")


def _finalize_generation(conn: sqlite3.Connection, gen_path: Path) -> None:
    """Checkpoint the generation out of WAL and close it (no sidecars).

    A generation must be a single self-contained, immutable file before
    publication so a reader can never consume stale ``-wal``/``-shm`` pages
    against it. Switching journal_mode off WAL performs the checkpoint and
    leaves the database header in rollback-journal mode.
    """
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()
    gen_path.with_name(gen_path.name + "-wal").unlink(missing_ok=True)
    gen_path.with_name(gen_path.name + "-shm").unlink(missing_ok=True)


def _parse_yyyymmdd(s: str) -> date:
    """Parse a YYYYMMDD string into a date."""
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def _bundle_horizon_from_zip(zf: zipfile.ZipFile) -> date | None:
    """Return the max service date in one bundle's calendar + calendar_dates.

    Reads the bundle's ``calendar.txt`` end_date and ``calendar_dates.txt``
    addition dates directly, so the per-bundle horizon is exact provenance (not
    route-type inference). Returns None if the bundle has no servable dates.
    """
    candidates: list[date] = []

    reader = _open_csv(zf, "calendar.txt")
    if reader is not None:
        for row in reader:
            end = (row.get("end_date") or "").strip()
            if end:
                try:
                    candidates.append(_parse_yyyymmdd(end))
                except ValueError:
                    continue

    reader = _open_csv(zf, "calendar_dates.txt")
    if reader is not None:
        for row in reader:
            if (row.get("exception_type") or "").strip() == "1":
                d = (row.get("date") or "").strip()
                if d:
                    try:
                        candidates.append(_parse_yyyymmdd(d))
                    except ValueError:
                        continue

    if not candidates:
        return None
    return max(candidates)


def import_bundles_to_generation(
    anchor: Path,
    zip_path: Path,
    zip_sha256: str,
    bundle_folders: list[str],
    *,
    zip_etag: str | None = None,
    zip_size: str | None = None,
    today: date | None = None,
) -> dict:
    """Import every requested bundle into a NEW immutable generation and publish.

    The previous live generation is left untouched until the new one is fully
    built, validated, and renamed into a discoverable generation name; only then
    is the pointer swapped (atomically). The build happens under a
    NON-discoverable ``generations/build/`` path so a reader/GC can never
    observe an in-progress or crashed build.

    Validation before publication:
      * a missing requested bundle folder, a corrupt/truncated ZIP, or an empty
        core table aborts the import (core-table non-emptiness is checked
        aggregate across the whole generation);
      * the GLOBAL horizon must be non-None and in the future;
      * EVERY requested bundle's own horizon (recorded provenance) must be
        non-None and in the future (per-bundle horizon validation);
      * a publish that would discard a currently-healthy *requested* bundle
        (incumbent horizon > today, new horizon missing/expired) is refused —
        healthy incumbent bundles NOT in the requested set may be dropped
        (intentional removal).

    Args:
        anchor: Logical DB path (e.g. ``<data>/gtfs.db``). The generation is
            written to ``<data>/generations/`` and published via ``current.json``.
        zip_path: Downloaded outer GTFS ZIP.
        zip_sha256: SHA-256 of the outer ZIP (stored in import_meta).
        bundle_folders: Folder numbers to import, e.g. ``["2", "5"]``.
        zip_etag / zip_size: download metadata for future HEAD checks.
        today: service-local date used for the 45-day stop_times window and the
            horizon validation.

    Returns:
        The pointer dict published (``{generation, sha, horizon, written_at}``).
    """
    today = today or date.today()
    validate_gtfs_zip(zip_path)

    gen_path = store.new_generation_path(anchor)  # final, discoverable name
    build_path = store.new_build_path(anchor)  # non-discoverable until finalized
    # The build is registered for its WHOLE lifetime — from before connect until
    # after the rename into the discoverable name — and unregistered exactly once
    # in the finally, so a connect, build, validate, finalize, cleanup, or rename
    # error can never leak the registration.
    store.register_active_build(build_path)
    try:
        return _build_and_publish_generation(
            anchor, zip_path, zip_sha256, bundle_folders,
            gen_path, build_path, today, zip_etag, zip_size,
        )
    finally:
        store.unregister_active_build(build_path)


def _build_and_publish_generation(
    anchor: Path,
    zip_path: Path,
    zip_sha256: str,
    bundle_folders: list[str],
    gen_path: Path,
    build_path: Path,
    today: date,
    zip_etag: str | None,
    zip_size: str | None,
) -> dict:
    """Build one generation from the ZIP and publish it (called under the active-build guard)."""
    _LOGGER.info(
        "Importing GTFS bundles %s into generation %s", bundle_folders, gen_path.name
    )
    start = datetime.now(tz=timezone.utc)

    folder_horizons: dict[str, date] = {}

    conn = sqlite3.connect(str(build_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -64000")

        _load_schema(conn)
        # Stamp the schema version so open-time checks can distinguish a current
        # DB from a stale one left behind by an older build.
        conn.execute(f"PRAGMA user_version = {GTFS_SCHEMA_VERSION}")

        with zipfile.ZipFile(zip_path, "r") as outer_zf:
            all_inner = {
                n.split("/")[0]: n
                for n in outer_zf.namelist()
                if n.endswith("/google_transit.zip")
            }

            if all_inner:
                for idx, folder in enumerate(bundle_folders, start=1):
                    inner_name = all_inner.get(folder)
                    if inner_name is None:
                        # A requested bundle must exist — never record it as imported.
                        raise ValueError(
                            f"Bundle folder {folder} not found in GTFS ZIP — refusing import"
                        )
                    _LOGGER.info(
                        "Importing bundle %d/%d: folder %s", idx, len(bundle_folders), folder
                    )
                    inner_zf = zipfile.ZipFile(io.BytesIO(outer_zf.read(inner_name)))
                    _import_gtfs_files(conn, inner_zf, bundle_label=folder, today=today)

                    # Record exact per-bundle provenance (max service date).
                    folder_horizon = _bundle_horizon_from_zip(inner_zf)
                    if folder_horizon is None:
                        raise ValueError(
                            f"Bundle folder {folder} has no calendar service dates — "
                            "refusing import"
                        )
                    folder_horizons[folder] = folder_horizon
                    conn.execute(
                        "INSERT OR REPLACE INTO _bundle_horizons "
                        "(bundle_folder, horizon_date) VALUES (?, ?)",
                        (folder, folder_horizon.strftime("%Y%m%d")),
                    )
            else:
                # Flat GTFS — treat the whole ZIP as one bundle
                _LOGGER.info("Flat GTFS format detected — importing all files")
                _import_gtfs_files(conn, outer_zf, bundle_label="", today=today)
                flat_horizon = _bundle_horizon_from_zip(outer_zf)
                if flat_horizon is not None:
                    folder_horizons[""] = flat_horizon
                    conn.execute(
                        "INSERT OR REPLACE INTO _bundle_horizons "
                        "(bundle_folder, horizon_date) VALUES (?, ?)",
                        ("", flat_horizon.strftime("%Y%m%d")),
                    )

        # Record which bundles are now imported, plus download metadata.
        # imported_at is the SERVICE-LOCAL date so age comparisons line up with
        # the coordinator's service day (no UTC/host-TZ one-day drift).
        imported_at = today.isoformat()
        written_at = datetime.now(tz=timezone.utc).isoformat()
        meta_rows: list[tuple[str, str]] = [
            ("zip_sha256", zip_sha256),
            ("imported_at", imported_at),
        ]
        if zip_etag:
            meta_rows.append(("zip_etag", zip_etag))
        if zip_size:
            meta_rows.append(("zip_size", zip_size))
        for folder in bundle_folders:
            meta_rows.append((f"bundle_{folder}", imported_at))

        conn.executemany(
            "INSERT OR REPLACE INTO import_meta (key, value) VALUES (?, ?)", meta_rows
        )
        conn.commit()

        # --- Validate before publication: never publish a bad generation. ---
        _assert_db_integrity(conn)
        horizon = compute_horizon_from_conn(conn)
        if horizon is None or horizon <= today:
            raise ValueError(
                f"Staged GTFS schedule horizon {horizon} does not extend beyond "
                f"{today} — refusing to publish"
            )

        # Per-bundle validation: every requested bundle must have a future horizon.
        for folder in bundle_folders:
            h = folder_horizons.get(folder)
            if h is None or h <= today:
                raise ValueError(
                    f"Bundle folder {folder} horizon {h} does not extend beyond "
                    f"{today} — refusing to publish"
                )

        # Defense against discarding healthy incumbent data. The gate protects
        # ONLY bundles in the requested/union set (the publish-time requirement
        # set): a REQUIRED bundle that is currently healthy in the incumbent must
        # not be replaced by a missing/expired bundle. A healthy incumbent bundle
        # that is NOT in the requested set may be dropped — that is intentional
        # removal (the mode was removed), logged rather than rejected.
        requested_set = set(bundle_folders)
        incumbent_horizons = read_bundle_horizons(anchor)
        for folder, inc_h in incumbent_horizons.items():
            if folder not in requested_set:
                if inc_h > today:
                    _LOGGER.info(
                        "Bundle %s dropped (not required by any watch)", folder
                    )
                continue
            if inc_h > today:
                new_h = folder_horizons.get(folder)
                if new_h is None or new_h <= today:
                    raise ValueError(
                        f"Refusing to publish: bundle {folder} is healthy in the "
                        "incumbent generation but would become unhealthy"
                    )

    except Exception:
        conn.close()
        # Best-effort cleanup of the build file and its sidecars; suppress OSError
        # so cleanup can't mask (or throw past) the original error.
        with suppress(OSError):
            build_path.unlink(missing_ok=True)
            build_path.with_name(build_path.name + "-wal").unlink(missing_ok=True)
            build_path.with_name(build_path.name + "-shm").unlink(missing_ok=True)
        raise
    else:
        _finalize_generation(conn, build_path)

    # Rename the finalized build into its discoverable generation name, then
    # publish the pointer and GC old generations.
    store.finalize_generation(build_path, gen_path)
    ptr = store.publish_pointer(
        anchor,
        gen_path,
        sha=zip_sha256,
        horizon=horizon.strftime("%Y%m%d"),
        written_at=written_at,
    )
    store.gc_generations(anchor)

    elapsed = (datetime.now(tz=timezone.utc) - start).total_seconds()
    _LOGGER.info(
        "GTFS import complete: generation %s in %.1fs", gen_path.name, elapsed
    )
    return ptr


# Backward-compatible alias — the historical import entry point now writes a
# generation. Its signature is (zip_path, db_path, zip_sha256, bundle_folders, ...)
# and delegates to import_bundles_to_generation with the logical anchor first.
def import_mode_bundles(
    zip_path: Path,
    db_path: Path,
    zip_sha256: str,
    bundle_folders: list[str],
    *,
    force: bool = False,  # no longer meaningful — a generation is always rebuilt
    zip_etag: str | None = None,
    zip_size: str | None = None,
) -> None:
    """Legacy entry point: import bundles into a new generation and publish."""
    import_bundles_to_generation(
        db_path,
        zip_path,
        zip_sha256,
        bundle_folders,
        zip_etag=zip_etag,
        zip_size=zip_size,
    )


def get_imported_bundles(db_path: Path) -> set[str]:
    """Return the set of bundle folder numbers in the current generation."""
    return imported_bundles(db_path)


def get_stored_zip_meta(db_path: Path) -> dict[str, str]:
    """Return stored ZIP metadata from the current generation's import_meta."""
    return stored_zip_meta(db_path)


# ---------------------------------------------------------------------------
# Legacy helper — kept for backward compat; delegates to get_stored_zip_meta
# ---------------------------------------------------------------------------

def get_stored_sha256(db_path: Path) -> str | None:
    """Return the SHA-256 of the last successfully imported ZIP, or None."""
    return get_stored_zip_meta(db_path).get("zip_sha256")


# ---------------------------------------------------------------------------
# Legacy helper — imports all bundles; kept for CLI/test use
# ---------------------------------------------------------------------------

def import_zip(zip_path: Path, db_path: Path, zip_sha256: str) -> None:
    """Import all bundles from the ZIP (used by weekly refresh for all modes)."""
    with zipfile.ZipFile(zip_path, "r") as outer_zf:
        all_folders = sorted(
            n.split("/")[0]
            for n in outer_zf.namelist()
            if n.endswith("/google_transit.zip")
        )
    folders = all_folders if all_folders else [""]
    import_bundles_to_generation(db_path, zip_path, zip_sha256, folders)

