"""Query-semantics tests against offline builder-built generation DBs.

Covers the T7-T10 contract surfaces T6 must lock down:

* next-N departures ordering, route/destination/platform filtering;
* two patterns sharing one physical stop (both routes returned);
* short-working bus trips and duplicate headsigns (the "via" disambiguation
  input);
* post-midnight (``25:10:00``) and night-service (``00:30``-``04:00``) rows;
* per-bundle provenance (``_bundle_horizons``) and pointer publication for a
  two-bundle generation with overlapping calendar ranges;
* calendar-expiry semantics (expired, calendar_dates-only extension, removal
  gap today, no-service bundle rejection).

All queries pass a pinned ``now``/``today``; nothing depends on the wall clock.
The fixture catalogue is documented in ``tests/fixtures/README.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import zipfile

import pytest

from custom_components.transport_victoria.gtfs import store
from custom_components.transport_victoria.gtfs.horizon import (
    compute_horizon,
    compute_horizon_for_bundle,
    has_service_on_date,
    imported_bundles,
    is_expired,
    read_bundle_horizons,
    refresh_due,
)
from custom_components.transport_victoria.gtfs.repository import (
    get_all_routes,
    get_arrival_at_stop,
    get_service_type_baselines,
    next_departures,
    schedule_state,
)

from tests.gtfs_builder import (
    REFERENCE_DATE as REF,
    build_bundle,
    build_generation,
)
from tests import gtfs_fixtures as fx

NOW_0800 = datetime(2026, 6, 1, 8, 0, 0)      # pinned Monday morning
NEXT_DAY_0020 = datetime(2026, 6, 2, 0, 20, 0)


# ---------------------------------------------------------------------------
# Builder contract: flat CSV set → directory or zip
# ---------------------------------------------------------------------------

class TestBuilder:
    def test_build_bundle_writes_gtfs_csvs_to_directory_and_zip(self, tmp_path: Path):
        spec = fx.metro_spec()

        flat_dir = build_bundle(tmp_path / "flat", **spec)
        assert (flat_dir / "agency.txt").is_file()
        assert (flat_dir / "shapes.txt").is_file()
        stop_times = (flat_dir / "stop_times.txt").read_text(encoding="utf-8")
        assert "25:10:00" in stop_times  # post-midnight time preserved verbatim

        flat_zip = build_bundle(tmp_path / "flat.zip", **spec)
        with zipfile.ZipFile(flat_zip) as zf:
            assert "agency.txt" in zf.namelist()
            assert "25:10:00" in zf.read("stop_times.txt").decode("utf-8")


# ---------------------------------------------------------------------------
# 2a. metro — shared stop, ordering, route/destination filters, post-midnight
# ---------------------------------------------------------------------------

class TestMetroSharedStop:
    def test_two_routes_share_one_stop_sorted_by_time(self, metro_db: Path):
        rows = next_departures(metro_db, "RICHMOND", NOW_0800, limit=10)

        # Both routes appear at the SAME stop_id, interleaved by departure time.
        assert [r.trip_id for r in rows] == [
            "T_FRK_OUT_2", "T_WRB_OUT_2", "T_FRK_IN_1",
            "T_FRK_OUT_3", "T_WRB_OUT_3", "T_FRK_LATE",
        ]
        assert {r.route_id for r in rows[:3]} == {
            "R_METRO_FRANKSTON", "R_METRO_WERRIBEE",
        }
        times = [r.departure_seconds for r in rows]
        assert times == sorted(times)
        assert rows[0].actual_stop_id == "RICHMOND"
        assert rows[0].trip_headsign == "Frankston"
        assert rows[1].trip_headsign == "Werribee"

    def test_route_filter_returns_only_that_route(self, metro_db: Path):
        rows = next_departures(
            metro_db, "RICHMOND", NOW_0800, limit=10,
            route_id_filter="R_METRO_WERRIBEE",
        )
        assert [r.trip_id for r in rows] == ["T_WRB_OUT_2", "T_WRB_OUT_3"]
        assert all(r.route_id == "R_METRO_WERRIBEE" for r in rows)

    def test_direction_filter_by_destination(self, metro_db: Path):
        # Outbound: only Frankston-bound trips serve FRANKSTON *after* Richmond.
        outbound = next_departures(
            metro_db, "RICHMOND", NOW_0800, limit=10,
            direction_stop_id="FRANKSTON",
        )
        assert [r.trip_id for r in outbound] == [
            "T_FRK_OUT_2", "T_FRK_OUT_3", "T_FRK_LATE",
        ]
        assert all(r.direction_id == 0 for r in outbound)
        assert all(r.trip_headsign == "Frankston" for r in outbound)

        # Inbound: only trips continuing to Flinders Street after Richmond.
        inbound = next_departures(
            metro_db, "RICHMOND", NOW_0800, limit=10,
            direction_stop_id="FLINDERS",
        )
        assert [r.trip_id for r in inbound] == ["T_FRK_IN_1"]
        assert inbound[0].direction_id == 1

    def test_post_midnight_2510_service_is_next_service_day(self, metro_db: Path):
        # 00:20 Tuesday is still Monday's service day (03:00 cutover), so the
        # 25:10 Monday trip appears as 01:10 Tuesday.
        rows = next_departures(metro_db, "RICHMOND", NEXT_DAY_0020, limit=10)
        assert [r.trip_id for r in rows] == ["T_FRK_LATE"]
        assert rows[0].departure_seconds == 25 * 3600 + 10 * 60
        assert rows[0].scheduled_departure == datetime(2026, 6, 2, 1, 10)

        # Once 25:10 has passed, it is no longer returned.
        later = NEXT_DAY_0020 + timedelta(hours=1, minutes=1)  # 01:21
        assert next_departures(metro_db, "RICHMOND", later, limit=10) == []


# ---------------------------------------------------------------------------
# 2b. tram — parent station/platform filtering + ambiguous loop pattern
# ---------------------------------------------------------------------------

class TestTramPlatforms:
    def test_sibling_platform_query_expands_to_all_platforms(self, tram_db: Path):
        # The departure query expands *from a platform stop* to all sibling
        # platforms sharing its parent_station (it does not expand downwards
        # from the station parent; documented in the T6 report for T10).
        rows = next_departures(tram_db, "T96_P1", NOW_0800, limit=10)
        assert [r.trip_id for r in rows] == [
            "T_TRAM_P1", "T_TRAM_P2", "T_TRAM_LOOP",
        ]
        assert {r.actual_stop_id for r in rows} == {"T96_P1", "T96_P2"}
        assert rows[0].platform_code == "1"
        assert rows[1].platform_code == "2"

    def test_platform_filter_keeps_only_that_platform(self, tram_db: Path):
        p1 = next_departures(
            tram_db, "T96_P1", NOW_0800, limit=10, platform_filter="1",
        )
        assert [r.trip_id for r in p1] == ["T_TRAM_P1", "T_TRAM_LOOP"]
        assert all(r.actual_stop_id == "T96_P1" for r in p1)

        p2 = next_departures(
            tram_db, "T96_P1", NOW_0800, limit=10, platform_filter="2",
        )
        assert [r.trip_id for r in p2] == ["T_TRAM_P2"]
        assert p2[0].platform_code == "2"

    def test_ambiguous_loop_trip_starts_and_ends_at_same_stop(self, tram_db: Path):
        # At 09:30 the loop trip has two future rows at its origin/terminus:
        # the same trip/stops pair is the ambiguity T7/T8 must resolve.
        rows = next_departures(
            tram_db, "T96_TERM", datetime(2026, 6, 1, 9, 30), limit=10,
        )
        assert [r.trip_id for r in rows] == ["T_TRAM_LOOP", "T_TRAM_LOOP"]
        assert [r.stop_sequence for r in rows] == [1, 4]
        assert [r.actual_stop_id for r in rows] == ["T96_TERM", "T96_TERM"]

        # After the T7 fix, the destination filter keys the origin sequence by
        # (trip_id, stop_sequence), so the loop's seq-1 boarding (10:00) is kept
        # because the same trip reaches T96_TERM again at seq 4 (10:40); the
        # seq-4 row has no later T96_TERM occurrence and is dropped.
        ambiguous = next_departures(
            tram_db, "T96_TERM", datetime(2026, 6, 1, 9, 30), limit=10,
            direction_stop_id="T96_TERM",
        )
        assert [r.trip_id for r in ambiguous] == ["T_TRAM_LOOP"]
        assert [r.stop_sequence for r in ambiguous] == [1]


# ---------------------------------------------------------------------------
# 2c. bus — short workings, duplicate headsigns, night service
# ---------------------------------------------------------------------------

class TestBusPatterns:
    def test_short_working_trips_are_returned(self, bus_db: Path):
        rows = next_departures(bus_db, "B_SUNSHINE", datetime(2026, 6, 1, 7, 30),
                               limit=10)
        assert [r.trip_id for r in rows] == [
            "T_BUS_LONG", "T_BUS_SHORT", "T_BUS_SHORT2",
            "T_BUS_VIA_A", "T_BUS_VIA_B",
        ]
        assert [r.trip_headsign for r in rows] == [
            "City", "Footscray", "Braybrook", "City", "City",
        ]
        # The inbound trip *arrives* at Sunshine (no departure row).
        assert "T_BUS_IN" not in {r.trip_id for r in rows}

        counts = get_service_type_baselines(
            bus_db, ["T_BUS_LONG", "T_BUS_SHORT", "T_BUS_SHORT2"]
        )
        assert counts["T_BUS_LONG"][0] == 4
        assert counts["T_BUS_SHORT"][0] == 3
        assert counts["T_BUS_SHORT2"][0] == 2

    def test_duplicate_headsign_differs_only_by_via_stop(self, bus_db: Path):
        rows = next_departures(bus_db, "B_SUNSHINE", datetime(2026, 6, 1, 8, 50),
                               limit=10)
        assert [r.trip_id for r in rows] == ["T_BUS_VIA_A", "T_BUS_VIA_B"]
        assert {r.trip_headsign for r in rows} == {"City"}

        # The only difference T8 can use for a "via" label is the intermediate
        # stop sequence; both surfaces come from existing query methods.
        assert get_arrival_at_stop(
            bus_db, "T_BUS_VIA_A", "B_FOOTSCRAY", REF
        ) == datetime(2026, 6, 1, 9, 25)
        assert get_arrival_at_stop(
            bus_db, "T_BUS_VIA_B", "B_KENSINGTON", REF
        ) == datetime(2026, 6, 1, 9, 30)
        assert get_arrival_at_stop(
            bus_db, "T_BUS_VIA_B", "B_FOOTSCRAY", REF
        ) is None

    def test_night_service_after_cutover(self, bus_db: Path):
        # 03:30 Monday: the daily night calendar's 00:30 trip is past, 03:45
        # and 04:00 are upcoming.
        rows = next_departures(bus_db, "B_CITY", datetime(2026, 6, 1, 3, 30),
                               limit=2)
        assert [r.trip_id for r in rows] == ["T_NIGHT_0345", "T_NIGHT_0400"]
        assert [r.departure_seconds for r in rows] == [3 * 3600 + 45 * 60,
                                                      4 * 3600]

        wider = next_departures(bus_db, "B_CITY", datetime(2026, 6, 1, 3, 30),
                                limit=10)
        assert "T_NIGHT_0030" not in {r.trip_id for r in wider}

    def test_midnight_clock_times_before_cutover_belong_to_previous_day(self, bus_db: Path):
        # 00:15 rolls back to Sunday's service day, so a *Monday* 00:30 row is
        # not returned.  Feeds must publish 24:30 under the previous day instead
        # (see the metro 25:10 fixture, which does it correctly).
        rows = next_departures(bus_db, "B_CITY", datetime(2026, 6, 1, 0, 15),
                               limit=10)
        assert rows == []


# ---------------------------------------------------------------------------
# 2d. V/Line — one long route, two directions
# ---------------------------------------------------------------------------

class TestVLineDirections:
    def test_both_directions_at_an_intermediate_stop(self, vline_db: Path):
        rows = next_departures(vline_db, "V_WERRIBEE", datetime(2026, 6, 1, 7, 0),
                               limit=10)
        assert [r.trip_id for r in rows[:2]] == ["V_IN_0630", "V_OUT_0700"]
        assert [r.direction_id for r in rows[:2]] == [1, 0]
        assert rows[0].trip_headsign == "Southern Cross"
        assert rows[1].trip_headsign == "Geelong"

    def test_destination_filter_selects_one_direction(self, vline_db: Path):
        rows = next_departures(
            vline_db, "V_WERRIBEE", datetime(2026, 6, 1, 7, 0), limit=10,
            direction_stop_id="V_GEELONG",
        )
        assert rows
        assert all(r.direction_id == 0 for r in rows)
        assert all(r.trip_headsign == "Geelong" for r in rows)


# ---------------------------------------------------------------------------
# Destination terminus derivation (T7 gap probe)
# ---------------------------------------------------------------------------

class TestTerminusDerivation:
    """T7 needs terminal stops per (route, direction).

    ``repository.py`` has NO public query for this.  Rather than adding one in
    T6, these tests prove the existing surfaces expose enough to derive it:

    * ``next_departures(..., direction_stop_id=...)`` proves a candidate stop is
      served *after* the origin (i.e. a valid terminus for that direction);
    * ``DepartureRow.trip_headsign``/``direction_id`` confirm the pairing.

    The missing method is flagged in the T6 report: a
    ``get_terminals_for_route(db, route_id) -> {direction_id: [stop_id, ...]}``
    (or equivalent trip→stop-sequence query) is still required by T7.
    """

    def test_candidate_terminus_matches_one_direction_only(self, metro_db: Path):
        now = NOW_0800
        frankston = next_departures(
            metro_db, "RICHMOND", now, limit=10, direction_stop_id="FRANKSTON",
        )
        flinders = next_departures(
            metro_db, "RICHMOND", now, limit=10, direction_stop_id="FLINDERS",
        )
        assert {r.direction_id for r in frankston} == {0}
        assert {r.direction_id for r in flinders} == {1}
        assert {r.trip_headsign for r in frankston} == {"Frankston"}
        assert {r.trip_headsign for r in flinders} == {"Flinders Street"}


# ---------------------------------------------------------------------------
# 2f. two bundles → one generation: provenance + pointer
# ---------------------------------------------------------------------------

class TestTwoBundleProvenance:
    def test_distinct_horizons_for_overlapping_folders(self, two_bundle_db: Path):
        horizons = read_bundle_horizons(two_bundle_db)
        assert horizons == {
            "2": REF + timedelta(days=30),
            "5": REF + timedelta(days=5),
        }
        # Both calendars overlap (start REF-20); provenance must stay per-bundle.
        assert compute_horizon(two_bundle_db) == REF + timedelta(days=30)
        assert compute_horizon_for_bundle(two_bundle_db, "5") == REF + timedelta(days=5)

    def test_pointer_and_both_bundles_published(self, two_bundle_db: Path):
        assert imported_bundles(two_bundle_db) == {"2", "5"}
        pointer = store.read_pointer(two_bundle_db)
        assert pointer is not None
        assert store.resolve_db(two_bundle_db).is_file()
        assert pointer["generation"] == store.resolve_db(two_bundle_db).name

    def test_per_bundle_schedule_state_is_scoped(self, two_bundle_db: Path):
        # At REF+10 bundle 2 is healthy, bundle 5 is expired; worst wins.
        eval_day = REF + timedelta(days=10)
        assert schedule_state(two_bundle_db, ["2"], today=eval_day) == "ready"
        assert schedule_state(two_bundle_db, ["5"], today=eval_day) == "expired"
        assert schedule_state(two_bundle_db, ["2", "5"], today=eval_day) == "expired"

    def test_both_route_types_queryable(self, two_bundle_db: Path):
        trains = get_all_routes(two_bundle_db, [1])
        trams = get_all_routes(two_bundle_db, [0])
        assert [r.route_id for r in trains] == ["R_BOARD_TRAIN"]
        assert [r.route_id for r in trams] == ["R_BOARD_TRAM"]


# ---------------------------------------------------------------------------
# 2e. calendar-expiry scenarios
# ---------------------------------------------------------------------------

class TestCalendarExpiry:
    def test_expired_calendar_state(self, expired_db: Path):
        horizon = REF + timedelta(days=9)
        assert compute_horizon(expired_db) == horizon
        assert schedule_state(expired_db, ["2"], today=REF) == "ready"
        assert schedule_state(expired_db, ["2"], today=horizon + timedelta(days=1)) == "expired"
        assert is_expired(expired_db, today=horizon + timedelta(days=1)) is True

    def test_dates_only_extension_keeps_state_ready(self, extension_db: Path):
        horizon = REF + timedelta(days=44)
        assert compute_horizon(extension_db) == horizon
        assert compute_horizon_for_bundle(extension_db, "2") == horizon
        # Evaluated 10 days after the calendar end_date (REF+1) but before the
        # last exception addition: horizon comes from calendar_dates → ready.
        assert schedule_state(extension_db, ["2"], today=REF + timedelta(days=10)) == "ready"
        assert schedule_state(extension_db, ["2"], today=horizon + timedelta(days=1)) == "expired"

    def test_removal_gap_today_has_no_departures(self, removed_today_db: Path):
        assert has_service_on_date(removed_today_db, REF) is False
        assert has_service_on_date(removed_today_db, REF + timedelta(days=1)) is True
        # Horizon is far in the future, so the *bundle* is ready, but "no
        # service today" forces a refresh (T2 policy) ...
        assert schedule_state(removed_today_db, ["2"], today=REF) == "ready"
        assert refresh_due(removed_today_db, ["2"], today=REF) is True
        # ... and the departure query's active-services EXCEPT yields no rows.
        assert next_departures(
            removed_today_db, "EXP_A", datetime(2026, 6, 1, 8, 30), limit=5,
        ) == []
        # The next day the service is back.
        assert next_departures(
            removed_today_db, "EXP_A", datetime(2026, 6, 2, 7, 30), limit=5,
        )[0].trip_id == "T_SVC_REMOVED"

    def test_no_service_bundle_is_refused(self, tmp_path: Path):
        with pytest.raises(ValueError):
            build_generation(tmp_path, {"2": fx.no_service_spec()})
        assert store.read_pointer(tmp_path / "gtfs.db") is None
        assert schedule_state(tmp_path / "gtfs.db", ["2"], today=REF) == "missing"
