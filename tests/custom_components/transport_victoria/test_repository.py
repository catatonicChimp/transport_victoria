"""Unit tests for gtfs/repository.py using a real SQLite DB."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.transport_victoria.gtfs.repository import (
    is_db_ready,
    is_mode_ready,
    next_departures,
    search_stops_by_distance,
    search_stops_by_name,
)

METRO_ROUTE_TYPES = [400]


# ---------------------------------------------------------------------------
# is_db_ready / is_mode_ready
# ---------------------------------------------------------------------------

class TestDbReady:
    def test_ready_when_db_has_stops(self, gtfs_db: Path):
        assert is_db_ready(gtfs_db) is True

    def test_not_ready_when_missing(self, tmp_path: Path):
        assert is_db_ready(tmp_path / "nonexistent.db") is False

    def test_mode_ready_when_bundle_imported(self, gtfs_db: Path):
        assert is_mode_ready(gtfs_db, ["2"]) is True

    def test_mode_not_ready_for_unknown_bundle(self, gtfs_db: Path):
        assert is_mode_ready(gtfs_db, ["99"]) is False

    def test_mode_not_ready_when_db_missing(self, tmp_path: Path):
        assert is_mode_ready(tmp_path / "nope.db", ["2"]) is False


# ---------------------------------------------------------------------------
# search_stops_by_name
# ---------------------------------------------------------------------------

class TestSearchStopsByName:
    def test_finds_stop(self, gtfs_db: Path):
        results = search_stops_by_name(gtfs_db, "Coburg", METRO_ROUTE_TYPES)
        assert len(results) == 1
        assert results[0].stop_name == "Coburg Station"

    def test_carries_route_type(self, gtfs_db: Path):
        """The route_type (metro train) is joined onto each result for labels."""
        results = search_stops_by_name(gtfs_db, "Coburg", METRO_ROUTE_TYPES)
        assert results[0].route_type == 400

    def test_deduplicates_same_name(self, gtfs_db: Path):
        # Two platforms with same name — should return only one result
        results = search_stops_by_name(gtfs_db, "Coburg", METRO_ROUTE_TYPES)
        assert len(results) == 1

    def test_case_insensitive(self, gtfs_db: Path):
        results = search_stops_by_name(gtfs_db, "coburg", METRO_ROUTE_TYPES)
        assert len(results) == 1

    def test_no_results_for_unknown(self, gtfs_db: Path):
        results = search_stops_by_name(gtfs_db, "Zzzunknown", METRO_ROUTE_TYPES)
        assert results == []

    def test_wrong_route_type_returns_nothing(self, gtfs_db: Path):
        results = search_stops_by_name(gtfs_db, "Coburg", [0])  # tram type
        assert results == []

    def test_empty_route_types_returns_nothing(self, gtfs_db: Path):
        results = search_stops_by_name(gtfs_db, "Coburg", [])
        assert results == []

    def test_query_too_broad_respects_limit(self, gtfs_db: Path):
        results = search_stops_by_name(gtfs_db, "F", METRO_ROUTE_TYPES, limit=1)
        assert len(results) <= 1


# ---------------------------------------------------------------------------
# Ranked stop-search over the materialized stop_search table
# ---------------------------------------------------------------------------


class TestStopSearchRanking:
    @pytest.fixture
    def ranking_db(self, tmp_path: Path) -> Path:
        from tests import gtfs_fixtures as fx
        from tests.gtfs_builder import import_bundle

        routes = [{
            "route_id": "R_METRO", "route_short_name": "Metro",
            "route_long_name": "Metro Line", "route_type": 400,
        }]
        stops = [
            fx._stop("S_PARLIAMENT", "Parliament", -37.80, 144.97),
            fx._stop("S_PARL_STATION", "Parliament Station", -37.81, 144.97),
            fx._stop("S_NORTH_MELB", "North Melbourne", -37.82, 144.96),
            fx._stop("S_FLINDERS", "Flinders Street", -37.83, 144.95),
        ]
        trips = [fx._trip("T1", "R_METRO", "SVC", "City", 0)]
        stop_times = [
            fx._st("T1", "S_PARLIAMENT", 1, "08:00:00"),
            fx._st("T1", "S_PARL_STATION", 2, "08:05:00"),
            fx._st("T1", "S_NORTH_MELB", 3, "08:10:00"),
            fx._st("T1", "S_FLINDERS", 4, "08:15:00"),
        ]
        return import_bundle(
            tmp_path, folder="2", routes=routes, trips=trips, stops=stops,
            stop_times=stop_times, calendar=[fx._calendar("SVC")],
        )

    def test_exact_beats_prefix(self, ranking_db: Path):
        names = [
            r.stop_name
            for r in search_stops_by_name(ranking_db, "parliament", METRO_ROUTE_TYPES, limit=10)
        ]
        assert names[0] == "Parliament"        # exact match wins
        assert "Parliament Station" in names    # prefix follows

    def test_prefix_matches(self, ranking_db: Path):
        names = [
            r.stop_name
            for r in search_stops_by_name(ranking_db, "parl", METRO_ROUTE_TYPES, limit=10)
        ]
        assert set(names) == {"Parliament", "Parliament Station"}

    def test_word_boundary_match(self, ranking_db: Path):
        names = [
            r.stop_name
            for r in search_stops_by_name(ranking_db, "station", METRO_ROUTE_TYPES, limit=10)
        ]
        assert names == ["Parliament Station"]

    def test_substring_match(self, ranking_db: Path):
        names = [
            r.stop_name
            for r in search_stops_by_name(ranking_db, "inders", METRO_ROUTE_TYPES, limit=10)
        ]
        assert names == ["Flinders Street"]

    def test_short_query_returns_empty(self, ranking_db: Path):
        assert search_stops_by_name(ranking_db, "p", METRO_ROUTE_TYPES) == []

    def test_route_type_filter(self, ranking_db: Path):
        # A route type the fixture does not serve → no results.
        assert search_stops_by_name(ranking_db, "parliament", [0]) == []


# ---------------------------------------------------------------------------
# search_stops_by_distance
# ---------------------------------------------------------------------------

class TestSearchStopsByDistance:
    def test_finds_nearby_stop(self, gtfs_db: Path):
        # Coburg Station at approx -37.74, 144.96
        results = search_stops_by_distance(
            gtfs_db, -37.74, 144.96, METRO_ROUTE_TYPES, radius_m=100
        )
        assert any(r.stop_name == "Coburg Station" for r in results)

    def test_deduplicates_same_name(self, gtfs_db: Path):
        results = search_stops_by_distance(
            gtfs_db, -37.74, 144.96, METRO_ROUTE_TYPES, radius_m=500
        )
        names = [r.stop_name for r in results]
        assert names.count("Coburg Station") == 1

    def test_distance_populated(self, gtfs_db: Path):
        results = search_stops_by_distance(
            gtfs_db, -37.74, 144.96, METRO_ROUTE_TYPES, radius_m=200
        )
        assert all(r.distance_m is not None for r in results)

    def test_sorted_by_distance(self, gtfs_db: Path):
        results = search_stops_by_distance(
            gtfs_db, -37.74, 144.96, METRO_ROUTE_TYPES, radius_m=100_000
        )
        distances = [r.distance_m for r in results if r.distance_m is not None]
        assert distances == sorted(distances)

    def test_nothing_outside_radius(self, gtfs_db: Path):
        # Middle of the ocean
        results = search_stops_by_distance(
            gtfs_db, 0.0, 0.0, METRO_ROUTE_TYPES, radius_m=500
        )
        assert results == []


# ---------------------------------------------------------------------------
# next_departures
# ---------------------------------------------------------------------------

class TestNextDepartures:
    def test_returns_upcoming_departures(self, gtfs_db: Path):
        rows = next_departures(gtfs_db, "PLAT1", datetime.now(), limit=5)
        assert len(rows) >= 1

    def test_departures_in_future(self, gtfs_db: Path):
        rows = next_departures(gtfs_db, "PLAT1", datetime.now(), limit=5)
        now = datetime.now()
        for row in rows:
            assert row.scheduled_departure >= now - timedelta(seconds=5)

    def test_sorted_by_departure_time(self, gtfs_db: Path):
        rows = next_departures(gtfs_db, "PLAT1", datetime.now(), limit=5)
        times = [r.departure_seconds for r in rows]
        assert times == sorted(times)

    def test_limit_respected(self, gtfs_db: Path):
        rows = next_departures(gtfs_db, "PLAT1", datetime.now(), limit=1)
        assert len(rows) <= 1

    def test_no_results_for_unknown_stop(self, gtfs_db: Path):
        rows = next_departures(gtfs_db, "UNKNOWNSTOP", datetime.now(), limit=5)
        assert rows == []

    def test_destination_filter_works(self, gtfs_db: Path):
        # Both T1 and T2 call at FLST after PLAT1
        rows = next_departures(
            gtfs_db, "PLAT1", datetime.now(), limit=5, direction_stop_id="FLST"
        )
        assert len(rows) >= 1

    def test_destination_filter_excludes_invalid(self, gtfs_db: Path):
        # PLAT1 never goes to itself via a later stop
        rows = next_departures(
            gtfs_db, "PLAT1", datetime.now(), limit=5, direction_stop_id="PLAT1"
        )
        # Either empty or only rows where PLAT1 appears again later (it doesn't in our fixture)
        assert rows == []
