"""T8 board subentry config-flow tests (real Home Assistant harness).

These exercise the native ``ConfigSubentryFlow`` pin flow, reconfigure, and the
board options reorder/remove paths against a real HA runtime.  Run ONLY under
the classic harness venv (homeassistant 2026.2.3 +
pytest-homeassistant-custom-component):

    .venv-classic/bin/python -m pytest \\
        tests/custom_components/transport_victoria/test_board_subentry_flow.py -q

They are skipped automatically in the light env (which stubs homeassistant).
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    # ``homeassistant.data_entry_flow`` exists only in the real HA package (the
    # light-env conftest stubs ``homeassistant`` but not this submodule).
    from homeassistant.data_entry_flow import FlowResultType  # noqa: F401

    _REAL_HA = True
except ImportError:  # pragma: no cover - light env
    _REAL_HA = False

if not _REAL_HA:
    # Skip the whole module in the light env (homeassistant is stubbed there).
    pytest.skip(
        "requires real homeassistant (classic harness venv)",
        allow_module_level=True,
    )

pytestmark = [pytest.mark.asyncio]

from homeassistant import config_entries  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402, F811
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.transport_victoria import config_flow as cf  # noqa: E402
from custom_components.transport_victoria.board import line_value  # noqa: E402
from custom_components.transport_victoria.const import (  # noqa: E402
    CONF_BOARDING_STOP_ID,
    CONF_BOARD_NAME,
    CONF_COLOUR_OVERRIDE,
    CONF_DEPARTURE_COUNT,
    CONF_DESTINATION_STOP_ID,
    CONF_ENTRY_TYPE,
    CONF_INCLUDE,
    CONF_LABEL_FROM,
    CONF_LABEL_TO,
    CONF_PIN_KEY,
    CONF_PLATFORM_FILTER,
    CONF_REALTIME_ENABLED,
    CONF_ROUTE_ID,
    CONF_SHOW_RUN_NUMBERS,
    CONF_SHOW_VEHICLE_TYPE,
    CONF_STOP_ID,
    CONF_SUBENTRY_ORDER,
    CONF_TRACK_VEHICLES,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    SUBENTRY_TYPE_PIN,
)
from tests import gtfs_fixtures as fx  # noqa: E402
from tests.gtfs_builder import import_bundle  # noqa: E402


@pytest.fixture(autouse=True)
def _enable_custom_integration(enable_custom_integrations):
    yield


@pytest.fixture
def bus_db_path(tmp_path: Path) -> Path:
    return import_bundle(tmp_path, **fx.bus_spec())


@pytest.fixture
def board_entry(hass, bus_db_path: Path, monkeypatch):
    # Point the flow's DB anchor at the offline bus fixture (skip download).
    monkeypatch.setattr(cf, "_db_path", lambda _cfg: bus_db_path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD, CONF_BOARD_NAME: "Test Board"},
        options={CONF_DEPARTURE_COUNT: 5, CONF_SUBENTRY_ORDER: []},
        title="Test Board",
    )
    entry.add_to_hass(hass)
    return entry


async def _start_pin_flow(hass, entry_id: str):
    return await hass.config_entries.subentries.async_init(
        (entry_id, SUBENTRY_TYPE_PIN),
        context={"source": config_entries.SOURCE_USER},
    )


async def _select_sunshine(hass, flow_id: str):
    result = await hass.config_entries.subentries.async_configure(
        flow_id, {"query": "Sun"}
    )
    assert result["step_id"] == "origin"
    result = await hass.config_entries.subentries.async_configure(
        flow_id, {CONF_STOP_ID: "B_SUNSHINE"}
    )
    return result


async def _add_all_destinations_pin(hass, entry_id: str) -> str:
    """Drive the add-pin flow to create one "all destinations" pin; return id."""
    result = await _start_pin_flow(hass, entry_id)
    result = await _select_sunshine(hass, result["flow_id"])
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"destination_choice": "__all__"}
    )
    assert result["step_id"] == "options"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_LABEL_FROM: "A", CONF_LABEL_TO: "All", CONF_INCLUDE: True}
    )
    # Options now advances to the "Add another line?" menu, not straight to create.
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "add_another"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_get_entry(entry_id)
    return next(iter(entry.subentries.keys()))


async def _add_city_pin(hass, entry_id: str) -> str:
    """Drive the add-pin flow to create one specific-destination pin."""
    result = await _start_pin_flow(hass, entry_id)
    result = await _select_sunshine(hass, result["flow_id"])
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"destination_choice": "specific"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"destination_choice": "specific", "dest_query": "Cit"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"destination_choice": "specific", CONF_DESTINATION_STOP_ID: "B_CITY"},
    )
    assert result["step_id"] == "select_lines"
    from custom_components.transport_victoria.gtfs.repository import get_lines_between

    lines = get_lines_between(cf._db_path(hass.config.config_dir), "B_SUNSHINE", "B_CITY")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"line_select": line_value(lines[0])}
    )
    assert result["step_id"] == "options"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_LABEL_FROM: "B", CONF_LABEL_TO: "City", CONF_INCLUDE: True}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "add_another"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_get_entry(entry_id)
    return next(
        sub_id
        for sub_id, sub in entry.subentries.items()
        if sub.data.get(CONF_DESTINATION_STOP_ID) == "B_CITY"
    )


async def test_add_pin_all_destinations_creates_subentry(hass, board_entry):
    result = await _start_pin_flow(hass, board_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "origin"

    result = await _select_sunshine(hass, result["flow_id"])
    assert result["step_id"] == "to"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"destination_choice": "__all__"}
    )
    # Single route at Sunshine → auto-advances to options.
    assert result["step_id"] == "options"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_LABEL_FROM: "220 from Sunshine",
            CONF_LABEL_TO: "All destinations",
            CONF_INCLUDE: True,
        },
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "add_another"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_get_entry(board_entry.entry_id)
    assert entry is not None
    assert len(entry.subentries) == 1
    sub = next(iter(entry.subentries.values()))
    assert sub.subentry_type == SUBENTRY_TYPE_PIN
    assert sub.data[CONF_ROUTE_ID] == "R_BUS_220"
    assert sub.data[CONF_BOARDING_STOP_ID] == "B_SUNSHINE"
    assert sub.data[CONF_DESTINATION_STOP_ID] is None
    assert sub.data[CONF_PIN_KEY] == sub.unique_id
    assert sub.data[CONF_INCLUDE] is True


async def test_add_pin_specific_destination_derives_direction(hass, board_entry):
    result = await _start_pin_flow(hass, board_entry.entry_id)
    result = await _select_sunshine(hass, result["flow_id"])
    assert result["step_id"] == "to"

    # Choose a specific destination: search + select "City".
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"destination_choice": "specific"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"destination_choice": "specific", "dest_query": "Cit"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"destination_choice": "specific", CONF_DESTINATION_STOP_ID: "B_CITY"},
    )
    # Two "City" routings (via Footscray / via Kensington) → line select list.
    assert result["step_id"] == "select_lines"

    # Resolve the first line candidate directly and submit its selector value.
    from custom_components.transport_victoria.gtfs.repository import get_lines_between

    lines = get_lines_between(
        cf._db_path(hass.config.config_dir), "B_SUNSHINE", "B_CITY"
    )
    chosen = line_value(lines[0])
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"line_select": chosen}
    )
    assert result["step_id"] == "options"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_LABEL_FROM: "220 from Sunshine",
            CONF_LABEL_TO: "City",
            CONF_INCLUDE: True,
        },
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "add_another"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_get_entry(board_entry.entry_id)
    sub = next(iter(entry.subentries.values()))
    assert sub.data[CONF_DESTINATION_STOP_ID] == "B_CITY"
    assert sub.data["direction"] == 0


async def test_reconfigure_pin_edits_data(hass, board_entry):
    subentry_id = await _add_all_destinations_pin(hass, board_entry.entry_id)

    result = await hass.config_entries.subentries.async_init(
        (board_entry.entry_id, SUBENTRY_TYPE_PIN),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": subentry_id,
        },
    )
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_LABEL_FROM: "Custom From",
            CONF_LABEL_TO: "Custom To",
            CONF_INCLUDE: False,
            CONF_PLATFORM_FILTER: "1",
            CONF_COLOUR_OVERRIDE: "FF0000",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    entry = hass.config_entries.async_get_entry(board_entry.entry_id)
    sub = entry.subentries[subentry_id]
    assert sub.data[CONF_LABEL_FROM] == "Custom From"
    assert sub.data[CONF_LABEL_TO] == "Custom To"
    assert sub.data[CONF_INCLUDE] is False
    assert sub.data[CONF_PLATFORM_FILTER] == "1"
    assert sub.data[CONF_COLOUR_OVERRIDE] == "FF0000"
    # Identity is unchanged by an edit.
    assert sub.data[CONF_PIN_KEY] == sub.unique_id


async def test_board_options_reorder_writes_order(hass, board_entry):
    first = await _add_all_destinations_pin(hass, board_entry.entry_id)
    second = await _add_city_pin(hass, board_entry.entry_id)
    assert first != second

    result = await hass.config_entries.options.async_init(board_entry.entry_id)
    # Single-screen board options (a form with a dropdown, not a two-hop menu).
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "board_menu"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"board_action": "reorder"}
    )
    assert result["step_id"] == "board_reorder"

    # Move the first pin down → applied immediately, reorder step re-renders.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"pin_select": first, "reorder_action": "down"}
    )
    assert result["step_id"] == "board_reorder"

    # Done → order flips to [second, first].
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"pin_select": first, "reorder_action": "done"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_get_entry(board_entry.entry_id)
    assert entry.options[CONF_SUBENTRY_ORDER] == [second, first]


async def test_board_options_remove_deletes_subentry(hass, board_entry):
    first = await _add_all_destinations_pin(hass, board_entry.entry_id)
    await _add_city_pin(hass, board_entry.entry_id)

    result = await hass.config_entries.options.async_init(board_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"board_action": "remove"}
    )
    assert result["step_id"] == "board_remove"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"pin_select": first}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_get_entry(board_entry.entry_id)
    assert first not in entry.subentries
    assert first not in entry.options[CONF_SUBENTRY_ORDER]
    assert len(entry.subentries) == 1


async def test_board_options_settings_track_vehicles(hass, board_entry):
    """The board settings screen exposes and persists the fleet-tracking toggle."""
    result = await hass.config_entries.options.async_init(board_entry.entry_id)
    assert result["step_id"] == "board_menu"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"board_action": "settings"}
    )
    assert result["step_id"] == "board_settings"
    # The toggle is present in the rendered form schema.
    schema = result["data_schema"].schema
    assert any(
        getattr(key, "schema", None) == CONF_TRACK_VEHICLES for key in schema
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_BOARD_NAME: "Test Board",
            CONF_DEPARTURE_COUNT: 5,
            CONF_REALTIME_ENABLED: True,
            CONF_SHOW_RUN_NUMBERS: True,
            CONF_SHOW_VEHICLE_TYPE: True,
            CONF_TRACK_VEHICLES: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_get_entry(board_entry.entry_id)
    assert entry.options[CONF_TRACK_VEHICLES] is True
