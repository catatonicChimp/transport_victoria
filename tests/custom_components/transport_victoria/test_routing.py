"""Unit tests for routing/path_finder.py (generation resolution + read-only)."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, timedelta
from pathlib import Path

from custom_components.transport_victoria.gtfs.importer import import_bundles_to_generation
from custom_components.transport_victoria.routing.path_finder import validate_route


def _make_zip_with_route(tmp_path: Path, origin: str, dest: str) -> Path:
    """Build a minimal nested PTV ZIP with one trip serving origin → dest."""
    today = date.today()
    start = (today - timedelta(days=1)).strftime("%Y%m%d")
    end = (today + timedelta(days=30)).strftime("%Y%m%d")

    def _csv(rows: list[dict]) -> bytes:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue().encode("utf-8")

    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("agency.txt", _csv([{
            "agency_id": "1", "agency_name": "PTV", "agency_url": "",
            "agency_timezone": "Australia/Melbourne", "agency_lang": "en",
            "agency_phone": "",
        }]))
        z.writestr("routes.txt", _csv([{
            "route_id": "R1", "agency_id": "1", "route_short_name": "R1",
            "route_long_name": "R1", "route_type": "400",
            "route_color": "0099CC", "route_text_color": "FFFFFF",
        }]))
        z.writestr("calendar.txt", _csv([{
            "service_id": "SVC1", "start_date": start, "end_date": end,
            "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1",
            "friday": "1", "saturday": "1", "sunday": "1",
        }]))
        z.writestr("trips.txt", _csv([{
            "trip_id": "T1", "route_id": "R1", "service_id": "SVC1",
            "shape_id": "", "trip_headsign": dest, "direction_id": "0", "block_id": "",
        }]))
        z.writestr("stops.txt", _csv([
            {"stop_id": origin, "stop_name": "Origin", "stop_lat": "-37.74",
             "stop_lon": "144.96", "stop_code": "", "location_type": "",
             "parent_station": "", "platform_code": "", "wheelchair_boarding": ""},
            {"stop_id": dest, "stop_name": "Dest", "stop_lat": "-37.81",
             "stop_lon": "144.97", "stop_code": "", "location_type": "",
             "parent_station": "", "platform_code": "", "wheelchair_boarding": ""},
        ]))
        z.writestr("stop_times.txt", _csv([
            {"trip_id": "T1", "stop_id": origin, "stop_sequence": "1",
             "arrival_time": "09:00:00", "departure_time": "09:00:00",
             "pickup_type": "0", "drop_off_type": "0", "timepoint": "1"},
            {"trip_id": "T1", "stop_id": dest, "stop_sequence": "2",
             "arrival_time": "09:30:00", "departure_time": "09:30:00",
             "pickup_type": "0", "drop_off_type": "0", "timepoint": "1"},
        ]))
    inner.seek(0)

    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("2/google_transit.zip", inner.getvalue())
    path = tmp_path / "gtfs.zip"
    path.write_bytes(outer.getvalue())
    return path


class TestValidateRoute:
    def test_resolves_generation_and_returns_true(self, tmp_path: Path):
        anchor = tmp_path / "gtfs.db"
        zip_path = _make_zip_with_route(tmp_path, "ORIGIN", "DEST")
        import_bundles_to_generation(anchor, zip_path, "sha", ["2"])

        # Generation-only installation: no legacy flat DB at the anchor.
        assert not anchor.exists()
        assert validate_route(anchor, "ORIGIN", "DEST", [400]) is True
        # Wrong route_type → no match (not a false negative from an empty DB).
        assert validate_route(anchor, "ORIGIN", "DEST", [0]) is False
        # Reverse order → no match (destination before origin).
        assert validate_route(anchor, "DEST", "ORIGIN", [400]) is False
        # Read-only open never created a file at the anchor.
        assert not anchor.exists()

    def test_no_generation_returns_false_cleanly(self, tmp_path: Path):
        anchor = tmp_path / "gtfs.db"
        assert validate_route(anchor, "A", "B", [400]) is False
        # A resolution miss must not create an empty DB.
        assert not anchor.exists()
