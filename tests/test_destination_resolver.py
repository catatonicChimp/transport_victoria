"""T7 destination resolver tests, built on the T6 offline fixture catalogue.

Covers:
- terminal candidates per (route, direction) with headsign/via disambiguation
  (train, tram loop, bus short-workings + duplicate "City" headsigns, V/Line);
- direction derivation at an intermediate stop for both directions;
- loop ambiguity returning two candidates with correct via hints;
- impossible boarding/destination pairs rejected cleanly;
- the T8 multi-select line resolver (get_lines_between).
"""

from __future__ import annotations

from pathlib import Path

from custom_components.transport_victoria.gtfs.repository import (
    get_lines_between,
    get_terminals_for_route,
)
from custom_components.transport_victoria.routing.destination import (
    destination_label,
    destination_value,
    resolve_destination,
)


class TestTerminalCandidates:
    def test_train_terminals(self, metro_db: Path):
        cands = get_terminals_for_route(metro_db, "R_METRO_FRANKSTON")
        assert [(c.direction_id, c.terminal_stop_name, c.via_hint) for c in cands] == [
            (0, "Frankston", None),
            (1, "Flinders Street", None),
        ]

    def test_tram_terminals_include_loop(self, tram_db: Path):
        cands = get_terminals_for_route(tram_db, "R_TRAM_96")
        # East Brunswick (through service), plus the loop's two "directions":
        # the plain terminus and the self-returning leg via Carlisle Street.
        assert [(c.terminal_stop_name, c.via_hint) for c in cands] == [
            ("East Brunswick", None),
            ("St Kilda Beach", None),
            ("St Kilda Beach", "Carlisle Street"),
        ]

    def test_bus_short_workings_and_via_labels(self, bus_db: Path):
        cands = get_terminals_for_route(bus_db, "R_BUS_220")
        assert [(c.terminal_stop_name, c.via_hint) for c in cands] == [
            ("Braybrook", None),
            ("City", "Footscray"),
            ("City", "Kensington"),
            ("Footscray", None),
            ("Sunshine", "Braybrook"),
            ("Sunshine", "Footscray"),
        ]
        # Distinct selector values even for the duplicate "City" headsigns.
        values = [destination_value(c) for c in cands]
        assert len(values) == len(set(values))

    def test_vline_terminals(self, vline_db: Path):
        cands = get_terminals_for_route(vline_db, "R_VLINE_GEELONG")
        assert [(c.direction_id, c.terminal_stop_name) for c in cands] == [
            (0, "Geelong"),
            (1, "Southern Cross"),
        ]


class TestDirectionDerivation:
    def test_both_directions_at_intermediate_stop(self, vline_db: Path):
        outbound = resolve_destination(
            vline_db, "R_VLINE_GEELONG", "V_WERRIBEE", "V_GEELONG"
        )
        assert [c.direction_id for c in outbound.candidates] == [0]
        assert outbound.resolved is not None
        assert outbound.resolved.terminal_stop_name == "Geelong"

        inbound = resolve_destination(
            vline_db, "R_VLINE_GEELONG", "V_WERRIBEE", "V_SC"
        )
        assert [c.direction_id for c in inbound.candidates] == [1]
        assert inbound.resolved is not None
        assert inbound.resolved.terminal_stop_name == "Southern Cross"

    def test_loop_ambiguity_returns_two_candidates(self, tram_db: Path):
        res = resolve_destination(tram_db, "R_TRAM_96", "T96_P1", "T96_TERM")
        assert res.ambiguous
        assert [c.terminal_stop_name for c in res.candidates] == [
            "St Kilda Beach",
            "St Kilda Beach",
        ]
        assert {c.via_hint for c in res.candidates} == {None, "Carlisle Street"}

    def test_impossible_pair_rejected(self, metro_db: Path):
        res = resolve_destination(
            metro_db, "R_METRO_FRANKSTON", "RICHMOND", "WERRIBEE"
        )
        assert res.candidates == []
        assert res.reason == "no_direct_service"

    def test_all_destinations_returns_every_terminal(self, bus_db: Path):
        res = resolve_destination(bus_db, "R_BUS_220", "B_SUNSHINE", None)
        assert len(res.candidates) == 6  # same as get_terminals_for_route


class TestLabels:
    def test_label_includes_via_only_when_needed(self, bus_db: Path):
        cands = get_terminals_for_route(bus_db, "R_BUS_220")
        by_name = {destination_label(c) for c in cands}
        assert "Footscray" in by_name
        assert "City via Footscray" in by_name
        assert "City via Kensington" in by_name
        # A plain terminus has no "via" suffix.
        assert "Braybrook" in by_name and "Braybrook via" not in " ".join(by_name)


class TestLinesBetween:
    def test_distinct_routes_for_od(self, metro_db: Path):
        lines = get_lines_between(metro_db, "FLINDERS", "RICHMOND")
        assert [(l.route_id, l.direction_id, l.via_hint) for l in lines] == [
            ("R_METRO_FRANKSTON", 0, None),
            ("R_METRO_WERRIBEE", 0, None),
        ]

    def test_single_route_direction(self, vline_db: Path):
        lines = get_lines_between(vline_db, "V_SC", "V_GEELONG")
        assert [(l.route_id, l.direction_id) for l in lines] == [
            ("R_VLINE_GEELONG", 0)
        ]

    def test_no_lines_for_impossible_pair(self, vline_db: Path):
        # A stop cannot serve as its own origin→destination in order.
        assert get_lines_between(vline_db, "V_GEELONG", "V_GEELONG") == []
