"""T8 trip-group board data-model unit tests (light env, stub style).

Covers the pure logic in ``custom_components.transport_victoria.board``: pin
identity, label helpers, resolver-backed pin derivation (against the T6 bus/
metro fixtures), schedule-invalidated validation, consumer-union bundle
bookkeeping, and ordering.  No homeassistant imports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.transport_victoria import board
from custom_components.transport_victoria.board import (
    PinData,
    PinValidation,
    all_destination_pins,
    board_bundles,
    default_label_from,
    default_label_to,
    line_pins,
    move_subentry,
    normalize_order,
    pin_bundles,
    pin_key,
    validate_pin,
)
from custom_components.transport_victoria.const import (
    CONF_INCLUDE,
    CONF_PIN_KEY,
    ROUTE_TYPE_BUNDLE_MAP,
    board_entity_unique_id,
)

# ---------------------------------------------------------------------------
# Pin identity
# ---------------------------------------------------------------------------


def test_pin_key_is_deterministic():
    a = pin_key("R1", "S1", "D1", 0)
    b = pin_key("R1", "S1", "D1", 0)
    assert a == b


def test_pin_key_distinguishes_fields():
    base = ("R1", "S1", "D1", 0)
    keys = {
        pin_key(*base),
        pin_key("R2", "S1", "D1", 0),
        pin_key("R1", "S2", "D1", 0),
        pin_key("R1", "S1", "D2", 0),
        pin_key("R1", "S1", "D1", 1),
        pin_key("R1", "S1", "D1", None),
    }
    assert len(keys) == 6


def test_pin_key_all_destinations_uses_wildcard():
    # destination None ("all destinations") and direction None are stable.
    a = pin_key("R1", "S1", None, None)
    b = pin_key("R1", "S1", None, None)
    assert a == b
    assert a != pin_key("R1", "S1", "D1", None)


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------


def test_default_label_from_prefers_short_name():
    assert default_label_from("220", "Sunshine - City", "Sunshine") == "220 from Sunshine"
    assert default_label_from("", "Frankston Line", "Richmond") == "Frankston Line from Richmond"


def test_default_label_to_all_destinations():
    assert default_label_to(True, None, None, None, None) == "All destinations"


def test_default_label_to_includes_via_hint():
    assert default_label_to(False, "B_CITY", "City", "Footscray", 0) == "City via Footscray"
    assert default_label_to(False, "B_CITY", "City", None, 0) == "City"


# ---------------------------------------------------------------------------
# Pin derivation (resolver-backed, against fixtures)
# ---------------------------------------------------------------------------


def test_line_pins_derives_via_candidates(bus_db: Path):
    pins = line_pins(bus_db, "B_SUNSHINE", "Sunshine", "B_CITY")
    assert len(pins) == 2
    for pin in pins:
        assert pin.route_id == "R_BUS_220"
        assert pin.direction_id == 0
        assert pin.destination_stop_id == "B_CITY"
        assert pin.all_destinations is False
        assert pin.label_from == "220 from Sunshine"
    vias = {pin.label_to for pin in pins}
    assert vias == {"City via Footscray", "City via Kensington"}
    # Distinct stable keys per via.
    assert len({pin.pin_key for pin in pins}) == 2


def test_all_destination_pins_one_per_route(metro_db: Path):
    pins = all_destination_pins(metro_db, "RICHMOND", "Richmond", [1])
    assert {p.route_id for p in pins} == {"R_METRO_FRANKSTON", "R_METRO_WERRIBEE"}
    for pin in pins:
        assert pin.all_destinations is True
        assert pin.direction_id is None
        assert pin.destination_stop_id is None


def test_line_pins_empty_for_impossible_pair(bus_db: Path):
    # Geelong is not in the bus fixture — no line serves Sunshine → Geelong.
    assert line_pins(bus_db, "B_SUNSHINE", "Sunshine", "V_GEELONG") == []


# ---------------------------------------------------------------------------
# Schedule-invalidated validation
# ---------------------------------------------------------------------------


def _bus_city_pin() -> PinData:
    return PinData(
        pin_key=pin_key("R_BUS_220", "B_SUNSHINE", "B_CITY", 0),
        route_type=3,
        route_id="R_BUS_220",
        boarding_stop_id="B_SUNSHINE",
        boarding_stop_name="Sunshine",
        route_short_name="220",
        all_destinations=False,
        destination_stop_id="B_CITY",
        destination_stop_name="City",
        direction_id=0,
    )


def test_validate_pin_ok(bus_db: Path):
    result = validate_pin(bus_db, _bus_city_pin())
    assert isinstance(result, PinValidation)
    assert result.ok is True
    assert result.reason is None


def test_validate_pin_impossible(bus_db: Path):
    pin = PinData(
        pin_key=pin_key("R_BUS_220", "B_SUNSHINE", "V_GEELONG", 0),
        route_type=3,
        route_id="R_BUS_220",
        boarding_stop_id="B_SUNSHINE",
        boarding_stop_name="Sunshine",
        all_destinations=False,
        destination_stop_id="V_GEELONG",
        direction_id=0,
    )
    result = validate_pin(bus_db, pin)
    assert result.ok is False
    assert result.reason == "no_direct_service"


def test_validate_pin_all_destinations_ok(bus_db: Path):
    pin = PinData(
        pin_key=pin_key("R_BUS_220", "B_SUNSHINE", None, None),
        route_type=3,
        route_id="R_BUS_220",
        boarding_stop_id="B_SUNSHINE",
        boarding_stop_name="Sunshine",
        all_destinations=True,
    )
    result = validate_pin(bus_db, pin)
    assert result.ok is True


def test_validate_pin_direction_changed(bus_db: Path):
    # Sunshine → City is direction 0 in the fixture; store direction 1 (wrong).
    pin = _bus_city_pin()
    pin.direction_id = 1
    result = validate_pin(bus_db, pin)
    assert result.ok is False
    assert result.reason == "direction_changed"
    # Repair path: a candidate direction is offered.
    assert result.direction_id == 0


# ---------------------------------------------------------------------------
# Consumer-union bundle bookkeeping
# ---------------------------------------------------------------------------


def test_pin_bundles_maps_route_type():
    bus_pin = PinData(pin_key="k", route_type=3, route_id="R", boarding_stop_id="S",
                      boarding_stop_name="S")
    assert pin_bundles(bus_pin) == frozenset({"4", "11"})
    assert pin_bundles(bus_pin) == ROUTE_TYPE_BUNDLE_MAP[3]


def test_pin_bundles_unknown_route_type_empty():
    pin = PinData(pin_key="k", route_type=999, route_id="R", boarding_stop_id="S",
                  boarding_stop_name="S")
    assert pin_bundles(pin) == frozenset()


def test_board_bundles_is_union():
    metro = PinData(pin_key="m", route_type=400, route_id="M", boarding_stop_id="S",
                    boarding_stop_name="S")
    bus = PinData(pin_key="b", route_type=3, route_id="B", boarding_stop_id="S",
                  boarding_stop_name="S")
    assert board_bundles([metro, bus]) == frozenset({"2", "4", "11"})


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_normalize_order_preserves_and_appends():
    assert normalize_order(["a", "b"], ["a", "b", "c"]) == ["a", "b", "c"]
    assert normalize_order(["b", "a"], ["a", "b"]) == ["b", "a"]
    # A pin missing from the stored order is appended (never dropped).
    assert normalize_order(["a"], ["a", "b", "c"]) == ["a", "b", "c"]
    # Stale ids are dropped.
    assert normalize_order(["a", "gone"], ["a", "b"]) == ["a", "b"]


def test_move_subentry():
    order = ["a", "b", "c"]
    assert move_subentry(order, "b", -1) == ["b", "a", "c"]
    assert move_subentry(order, "b", +1) == ["a", "c", "b"]
    # Boundaries are no-ops (returns an equal list, not the same object).
    assert move_subentry(order, "a", -1) == ["a", "b", "c"]
    assert move_subentry(order, "c", +1) == ["a", "b", "c"]
    assert move_subentry(order, "missing", +1) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Serialization round-trip + entity unique-id contract
# ---------------------------------------------------------------------------


def test_pin_data_round_trip():
    pin = _bus_city_pin()
    pin.include = False
    data = pin.to_dict()
    assert data[CONF_INCLUDE] is False
    assert data[CONF_PIN_KEY] == pin.pin_key
    restored = PinData.from_dict(data)
    assert restored == pin


def test_pin_data_from_dict_tolerates_missing_keys():
    restored = PinData.from_dict({})
    assert restored.route_id == ""
    assert restored.route_type == 0
    assert restored.include is True


def test_board_entity_unique_id_contract():
    assert board_entity_unique_id("parent1", "departure", 2) == "parent1:departure:2"
    assert board_entity_unique_id("parent1", "departure", 1) != (
        board_entity_unique_id("parent1", "departure", 2)
    )
