"""Declarative, offline GTFS fixture builder (test-only).

The builder has two layers:

1. :func:`build_bundle` writes a flat GTFS CSV set (``agency.txt`` ...
   ``shapes.txt``) into either a directory or a ``.zip`` file from plain
   Python dicts.  Missing optional columns get GTFS-typed defaults, so a spec
   only states the facts a test cares about (ids stay strings, times stay
   ``HH:MM:SS`` strings — including post-midnight ``25:10:00`` values).
2. :func:`build_outer_zip` / :func:`build_generation` / :func:`import_bundle`
   wrap one or more specs in the PTV nested layout
   (``<folder>/google_transit.zip``) and run the *production* importer
   (`gtfs.importer.import_bundles_to_generation`) to build a real generation
   DB + pointer.

Everything is deterministic: bundle dates are pinned in :data:`REFERENCE_DATE`
and passed as ``today`` to the importer, so neither the generated schedule nor
the accepted/rejected boundary depends on the wall clock.  Tests then pass a
pinned ``now``/``today`` to the repository/horizon query surfaces.

No network access and no binary SQLite is committed — fixtures are constructed
at test time (see ``tests/gtfs_fixtures.py`` for the named catalogue).
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from custom_components.transport_victoria.gtfs.importer import (
    import_bundles_to_generation,
)

# A pinned Monday inside the fixture calendars.  Import validation (`today`
# argument) and query `now`/`today` arguments are both anchored here, so the
# suite is independent of the real clock.
REFERENCE_DATE = date(2026, 6, 1)
REFERENCE_NOW = datetime(2026, 6, 1, 8, 0, 0)

# PTV route_type values used by the integration constants: 0 tram, 1 train
# (metro), 2 V/Line, 3 bus, 4 ferry.  (`schema.sql` documents the same set.)
ROUTE_TYPE_TRAM = 0
ROUTE_TYPE_TRAIN = 1
ROUTE_TYPE_VLINE = 2
ROUTE_TYPE_BUS = 3

# ---------------------------------------------------------------------------
# File schemas / per-row defaults
# ---------------------------------------------------------------------------

_AGENCY_COLUMNS = [
    "agency_id", "agency_name", "agency_url",
    "agency_timezone", "agency_lang", "agency_phone",
]
_ROUTE_COLUMNS = [
    "route_id", "agency_id", "route_short_name", "route_long_name",
    "route_type", "route_color", "route_text_color",
]
_TRIP_COLUMNS = [
    "trip_id", "route_id", "service_id", "shape_id",
    "trip_headsign", "direction_id", "block_id",
]
_STOP_COLUMNS = [
    "stop_id", "stop_name", "stop_lat", "stop_lon", "stop_code",
    "location_type", "parent_station", "platform_code",
    "wheelchair_boarding",
]
_STOP_TIME_COLUMNS = [
    "trip_id", "stop_id", "stop_sequence", "arrival_time",
    "departure_time", "pickup_type", "drop_off_type", "timepoint",
]
_CALENDAR_COLUMNS = [
    "service_id", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "start_date", "end_date",
]
_CALENDAR_DATE_COLUMNS = ["service_id", "date", "exception_type"]
_SHAPE_COLUMNS = [
    "shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence",
    "shape_dist_traveled",
]

_AGENCY_DEFAULTS: dict[str, Any] = {
    "agency_id": "1",
    "agency_name": "PTV Fixture Agency",
    "agency_url": "https://example.invalid",
    "agency_timezone": "Australia/Melbourne",
    "agency_lang": "en",
    "agency_phone": "",
}
_ROUTE_DEFAULTS: dict[str, Any] = {
    "agency_id": "1",
    "route_short_name": "",
    "route_long_name": "",
    "route_type": ROUTE_TYPE_BUS,
    "route_color": "0099CC",
    "route_text_color": "FFFFFF",
}
_TRIP_DEFAULTS: dict[str, Any] = {
    "shape_id": "",
    "trip_headsign": "",
    "direction_id": 0,
    "block_id": "",
}
_STOP_DEFAULTS: dict[str, Any] = {
    "stop_lat": "",
    "stop_lon": "",
    "stop_code": "",
    "location_type": "",
    "parent_station": "",
    "platform_code": "",
    "wheelchair_boarding": "",
}
_STOP_TIME_DEFAULTS: dict[str, Any] = {
    "pickup_type": 0,
    "drop_off_type": 0,
    "timepoint": 1,
}
_CALENDAR_DEFAULTS: dict[str, Any] = {
    "monday": 1, "tuesday": 1, "wednesday": 1, "thursday": 1,
    "friday": 1, "saturday": 1, "sunday": 1,
}
_CALENDAR_DATE_DEFAULTS: dict[str, Any] = {"exception_type": 1}
_SHAPE_DEFAULTS: dict[str, Any] = {"shape_dist_traveled": ""}


def _stringify(value: Any) -> str:
    """Render a GTFS cell the way the importer expects (strings, '' if None)."""
    if value is None:
        return ""
    return str(value)


def _normalise(
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    defaults: Mapping[str, Any] | None = None,
    *,
    auto_sequence: str | None = None,
) -> list[dict[str, str]]:
    """Merge defaults, fill absent columns, and render every cell as text."""
    out: list[dict[str, str]] = []
    for idx, raw in enumerate(rows):
        row: dict[str, str] = {}
        for col in columns:
            if col in raw:
                value = raw[col]
            elif defaults is not None and col in defaults:
                value = defaults[col]
            elif auto_sequence is not None and col == auto_sequence:
                value = idx + 1
            elif col == "arrival_time" and "departure_time" in raw:
                # Sensible default: dwell-free stop.
                value = raw["departure_time"]
            else:
                value = ""
            row[col] = _stringify(value)
        out.append(row)
    return out


def _csv_bytes(rows: Sequence[Mapping[str, str]], columns: Sequence[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _flat_files(
    *,
    agency: Sequence[Mapping[str, Any]] | None,
    routes: Sequence[Mapping[str, Any]],
    trips: Sequence[Mapping[str, Any]],
    stops: Sequence[Mapping[str, Any]],
    stop_times: Sequence[Mapping[str, Any]],
    calendar: Sequence[Mapping[str, Any]],
    calendar_dates: Sequence[Mapping[str, Any]],
    shapes: Sequence[Mapping[str, Any]],
) -> dict[str, bytes]:
    """Return {filename: csv bytes} for one bundle."""
    agency_rows = _normalise(agency or [_AGENCY_DEFAULTS], _AGENCY_COLUMNS)
    route_rows = _normalise(routes, _ROUTE_COLUMNS, _ROUTE_DEFAULTS)
    trip_rows = _normalise(trips, _TRIP_COLUMNS, _TRIP_DEFAULTS)
    stop_rows = _normalise(stops, _STOP_COLUMNS, _STOP_DEFAULTS)
    stop_time_rows = _normalise(
        stop_times, _STOP_TIME_COLUMNS, _STOP_TIME_DEFAULTS,
        auto_sequence="stop_sequence",
    )
    calendar_rows = _normalise(calendar, _CALENDAR_COLUMNS, _CALENDAR_DEFAULTS)
    calendar_date_rows = _normalise(
        calendar_dates, _CALENDAR_DATE_COLUMNS, _CALENDAR_DATE_DEFAULTS
    )
    shape_rows = _normalise(shapes, _SHAPE_COLUMNS, _SHAPE_DEFAULTS)

    files = {
        "agency.txt": _csv_bytes(agency_rows, _AGENCY_COLUMNS),
        "routes.txt": _csv_bytes(route_rows, _ROUTE_COLUMNS),
        "trips.txt": _csv_bytes(trip_rows, _TRIP_COLUMNS),
        "stops.txt": _csv_bytes(stop_rows, _STOP_COLUMNS),
        "stop_times.txt": _csv_bytes(stop_time_rows, _STOP_TIME_COLUMNS),
        # The importer tolerates a missing calendar.txt (calendar_dates-only
        # feeds), so only write files that actually carry rows.
        "shapes.txt": _csv_bytes(shape_rows, _SHAPE_COLUMNS),
    }
    if calendar_rows:
        files["calendar.txt"] = _csv_bytes(calendar_rows, _CALENDAR_COLUMNS)
    if calendar_date_rows:
        files["calendar_dates.txt"] = _csv_bytes(
            calendar_date_rows, _CALENDAR_DATE_COLUMNS
        )
    return files


# ---------------------------------------------------------------------------
# Public builder API
# ---------------------------------------------------------------------------

def build_bundle(
    dest: str | Path,
    *,
    routes: Sequence[Mapping[str, Any]],
    trips: Sequence[Mapping[str, Any]],
    stops: Sequence[Mapping[str, Any]],
    stop_times: Sequence[Mapping[str, Any]],
    calendar: Sequence[Mapping[str, Any]] = (),
    calendar_dates: Sequence[Mapping[str, Any]] = (),
    shapes: Sequence[Mapping[str, Any]] = (),
    agency: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """Write one flat GTFS CSV bundle into ``dest`` (a directory or ``.zip``).

    ``routes``/``trips``/``stops``/``stop_times`` are required; the calendar and
    shapes files are optional.  Values are plain Python/GTFS-typed: ids as
    strings, times as ``HH:MM:SS`` strings, calendar dates as ``YYYYMMDD``.

    Returns the destination path.
    """
    path = Path(dest)
    files = _flat_files(
        agency=agency,
        routes=routes,
        trips=trips,
        stops=stops,
        stop_times=stop_times,
        calendar=calendar,
        calendar_dates=calendar_dates,
        shapes=shapes,
    )
    if path.suffix == ".zip":
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
            for name, data in files.items():
                zf.writestr(name, data)
    else:
        path.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (path / name).write_bytes(data)
    return path


def _bundle_bytes(**spec: Any) -> bytes:
    """Build one bundle in memory as an inner ``google_transit.zip`` payload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        files = _flat_files(
            agency=spec.get("agency"),
            routes=spec["routes"],
            trips=spec["trips"],
            stops=spec["stops"],
            stop_times=spec["stop_times"],
            calendar=spec.get("calendar", ()),
            calendar_dates=spec.get("calendar_dates", ()),
            shapes=spec.get("shapes", ()),
        )
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def build_outer_zip(
    dest: str | Path,
    bundles: Mapping[str, Mapping[str, Any]],
) -> Path:
    """Write the PTV nested layout: ``<folder>/google_transit.zip`` per bundle.

    ``bundles`` maps a folder label (``"2"``, ``"5"`` ...) to the same keyword
    spec accepted by :func:`build_bundle`.
    """
    path = Path(dest)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for folder, spec in bundles.items():
            zf.writestr(f"{folder}/google_transit.zip", _bundle_bytes(**spec))
    return path


def build_generation(
    tmp_path: str | Path,
    bundles: Mapping[str, Mapping[str, Any]],
    *,
    today: date = REFERENCE_DATE,
    sha: str = "fixture-sha",
    anchor: str | Path | None = None,
    zip_name: str = "gtfs-fixture.zip",
) -> Path:
    """Build ``bundles`` into one real generation DB and return the DB anchor."""
    tmp = Path(tmp_path)
    anchor_path = Path(anchor) if anchor is not None else tmp / "gtfs.db"
    zip_path = build_outer_zip(tmp / zip_name, bundles)
    import_bundles_to_generation(
        anchor_path,
        zip_path,
        sha,
        list(bundles),
        today=today,
    )
    return anchor_path


def import_bundle(
    tmp_path: str | Path,
    *,
    folder: str = "2",
    today: date = REFERENCE_DATE,
    sha: str = "fixture-sha",
    anchor: str | Path | None = None,
    **spec: Any,
) -> Path:
    """Build a single-bundle generation from one inline spec and return the anchor."""
    return build_generation(
        tmp_path,
        {folder: spec},
        today=today,
        sha=sha,
        anchor=anchor,
    )
