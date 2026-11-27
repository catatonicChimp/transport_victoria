"""GTFS schedule horizon and DB-state computation.

The "service horizon" is the last calendar date for which the imported GTFS
schedule still has active service. It drives the refresh policy: when the
horizon approaches (or passes) today, the integration re-downloads the
schedule ZIP and re-imports it, rather than waiting for the local calendar to
expire and the departure sensors to silently fall to "unknown".

Horizon semantics (chosen after reading schema.sql and importer.py):

  * Computed from BOTH calendar.txt (start_date/end_date) and
    calendar_dates.txt (exception_type additions/removals).
  * The horizon is the maximum date with active service::

        max( MAX(calendar.end_date),
             MAX(calendar_dates.date WHERE exception_type = 1) )

  * We deliberately do NOT join against `trips` for the *global* horizon: the
    `trips` table is imported in full (only `stop_times` is windowed to the
    next 45 days), so filtering by trips adds a join with no correctness gain.
    Per-bundle horizons DO join through trips→routes to scope the horizon to a
    bundle's route_types — see :func:`compute_horizon_for_bundle` for the
    approximation this introduces.

  * exception_type = 2 (removals) subtract specific dates. Because the horizon
    is a MAX over end_dates/additions, removing a non-maximum date cannot
    shorten it. The rare case where the maximum end_date itself is removed is
    accepted as negligible (and the 7-day refresh margin absorbs it).

Database resolution (generation store): every function here takes a *logical*
``db_path`` (the anchor, e.g. ``<data>/gtfs.db``) and resolves it to the
current immutable generation via ``store.resolve_db`` (see gtfs/store.py).
All functions are synchronous; callers must run them in an executor thread.

Schema-version contract: a database whose ``user_version`` is BELOW
``GTFS_SCHEMA_VERSION`` is stale and forces a refresh. A database whose
``user_version`` is ABOVE it is accepted as-is — we assume a newer schema is a
compatible superset (documented backward-compat choice).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..const import (
    GTFS_REFRESH_MARGIN_DAYS,
    GTFS_REFRESH_MAX_AGE_DAYS,
    GTFS_SCHEMA_VERSION,
)
from . import store
from .time_utils import day_of_week_column, service_date_str

# The active-services predicate, mirrored from repository._ACTIVE_SERVICES_SQL,
# used by has_service_on_date to decide whether *any* service covers a date.
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


def _parse_date(s: str) -> date:
    """Parse a YYYYMMDD string into a date (GTFS calendar format)."""
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def compute_horizon_from_conn(conn: sqlite3.Connection) -> date | None:
    """Return the horizon from an open connection (used during staged import).

    Malformed date strings are ignored rather than raising — a malformed
    calendar is reported as "no usable horizon" (and therefore a validation
    failure at import time), never an unhandled ValueError.
    """
    try:
        max_cal = conn.execute("SELECT MAX(end_date) FROM calendar").fetchone()[0]
        max_add = conn.execute(
            "SELECT MAX(date) FROM calendar_dates WHERE exception_type = 1"
        ).fetchone()[0]
    except sqlite3.Error:
        return None

    candidates: list[date] = []
    for s in (max_cal, max_add):
        if not s:
            continue
        try:
            candidates.append(_parse_date(s))
        except ValueError:
            continue
    if not candidates:
        return None
    return max(candidates)


def compute_horizon(db_path: Path) -> date | None:
    """Return the last servable date in the current generation, or None.

    Returns None when no generation exists, it has no calendar data, or it is
    unreadable. Callers treat None as "no schedule" (and therefore stale).
    """
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return None
    try:
        conn = sqlite3.connect(str(resolved))
        try:
            return compute_horizon_from_conn(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def read_bundle_horizons(db_path: Path) -> dict[str, date]:
    """Return {bundle_folder: horizon_date} from the current generation.

    Reads the provenance table (``_bundle_horizons``) recorded at import time,
    which is exact per-bundle provenance (the importer knows which folder each
    calendar row came from) rather than route-type inference.
    """
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return {}
    try:
        conn = sqlite3.connect(str(resolved))
        try:
            rows = conn.execute(
                "SELECT bundle_folder, horizon_date FROM _bundle_horizons"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    result: dict[str, date] = {}
    for folder, h in rows:
        try:
            result[folder] = _parse_date(h)
        except ValueError:
            continue
    return result


def compute_horizon_for_bundle(db_path: Path, folder: str) -> date | None:
    """Return the horizon for one bundle folder, or None.

    Uses exact import-time provenance (the ``_bundle_horizons`` table), so a
    healthy bundle can never mask an expired one (and vice versa) — unlike the
    route-type inference this replaces, which was over-generous for overlapping
    route_types. Returns None when the folder has no recorded horizon (missing
    or not imported).
    """
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return None
    try:
        conn = sqlite3.connect(str(resolved))
        try:
            row = conn.execute(
                "SELECT horizon_date FROM _bundle_horizons WHERE bundle_folder = ?",
                (folder,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return _parse_date(row[0])
    except ValueError:
        return None


def has_service_on_date(db_path: Path, d: date) -> bool:
    """Return True if any service is active on the given date.

    Mirrors the coordinator's active-services predicate: a service covers the
    date if a calendar row spans it with the matching day-of-week column set,
    or a calendar_dates addition covers it, unless a removal subtracts it.
    """
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return False
    dow_col = day_of_week_column(d)
    service_date = service_date_str(d)
    sql = _ACTIVE_SERVICES_SQL.format(dow_col=dow_col)
    try:
        conn = sqlite3.connect(str(resolved))
        try:
            row = conn.execute(sql, {"service_date": service_date}).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def get_schema_version(db_path: Path) -> int:
    """Return the PRAGMA user_version of the current generation (0 if missing)."""
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return 0
    try:
        conn = sqlite3.connect(str(resolved))
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def imported_bundles(db_path: Path) -> set[str]:
    """Return the set of bundle folder numbers in the current generation."""
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return set()
    try:
        conn = sqlite3.connect(str(resolved))
        try:
            rows = conn.execute(
                "SELECT key FROM import_meta WHERE key LIKE 'bundle_%'"
            ).fetchall()
            return {row[0].removeprefix("bundle_") for row in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        return set()


def stored_zip_meta(db_path: Path) -> dict[str, str]:
    """Return stored ZIP metadata (zip_sha256/zip_etag/zip_size) from import_meta."""
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return {}
    try:
        conn = sqlite3.connect(str(resolved))
        try:
            rows = conn.execute(
                "SELECT key, value FROM import_meta"
                " WHERE key IN ('zip_sha256', 'zip_etag', 'zip_size')"
            ).fetchall()
            return {row[0]: row[1] for row in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def is_expired(db_path: Path, today: date | None = None) -> bool:
    """Return True if the schedule has data but its global horizon is in the past."""
    today = today or date.today()
    horizon = compute_horizon(db_path)
    return horizon is not None and horizon < today


def last_import_age_days(db_path: Path, today: date | None = None) -> int | None:
    """Return whole days since the last successful import, or None if unknown.

    Reads import_meta.imported_at (persisted in the generation itself, so it
    survives restarts). Returns None when there is no record — callers treat
    that as "due for refresh".
    """
    today = today or date.today()
    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return None
    try:
        conn = sqlite3.connect(str(resolved))
        try:
            row = conn.execute(
                "SELECT value FROM import_meta WHERE key = 'imported_at'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        imported_at = datetime.fromisoformat(row[0])
    except ValueError:
        return None
    if imported_at.tzinfo is not None:
        imported_at = imported_at.astimezone(timezone.utc).replace(tzinfo=None)
    return (today - imported_at.date()).days


def refresh_due(
    db_path: Path,
    bundle_folders: list[str] | None = None,
    today: date | None = None,
    margin_days: int = GTFS_REFRESH_MARGIN_DAYS,
    max_age_days: int = GTFS_REFRESH_MAX_AGE_DAYS,
) -> bool:
    """Return True if the schedule should be rebuilt now, from DB state alone.

    This is independent of any download/HEAD/SHA check: it only looks at what
    is on disk (schema version, per-bundle horizon, coverage, import age), so a
    schema-stale DB, an aged 45-day window, or a missing/expired bundle forces
    a rebuild even when the upstream ZIP is byte-for-byte unchanged.

    Policy:
      * no generation → rebuild (first import);
      * schema version below required → stale, rebuild (higher is accepted);
      * a requested bundle is missing → rebuild;
      * any bundle's horizon is missing, expired, or within `margin_days` → rebuild;
      * no service covers `today` → rebuild;
      * last import older than `max_age_days` → weekly minimum cadence rebuild.
    """
    today = today or date.today()

    resolved = store.resolve_db(db_path)
    if not resolved.exists():
        return True

    version = get_schema_version(db_path)
    if version < GTFS_SCHEMA_VERSION:
        return True

    if not bundle_folders:
        horizon = compute_horizon(db_path)
        if horizon is None or horizon < today:
            return True
        if today >= horizon - timedelta(days=margin_days):
            return True
    else:
        present = imported_bundles(db_path)
        for folder in bundle_folders:
            if folder not in present:
                return True
            h = compute_horizon_for_bundle(db_path, folder)
            if h is None or h < today:
                return True
            if today >= h - timedelta(days=margin_days):
                return True

    if not has_service_on_date(db_path, today):
        return True

    age = last_import_age_days(db_path, today)
    if age is None or age >= max_age_days:
        return True

    return False
