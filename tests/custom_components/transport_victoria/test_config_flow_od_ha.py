"""T18.4 TripView-style origin→destination flow — real Home Assistant harness tests.

Exercises the single four-step creation flow (origin → destination → line →
confirm) against a real HA runtime, for both the watch and board entry types:

  * board: the parent board + one pin subentry are created in ONE
    ``async_create_entry`` call (no flaky transient ``async_add_subentry``);
  * watch: a single watch entry with the derived mode/direction is created.

Run ONLY under the classic harness venv (homeassistant 2026.2.3 +
pytest-homeassistant-custom-component); skipped automatically in the light env.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from homeassistant.data_entry_flow import FlowResultType  # noqa: F401

    _REAL_HA = True
except ImportError:  # pragma: no cover - light env
    _REAL_HA = False

if not _REAL_HA:
    pytest.skip(
        "requires real homeassistant (classic harness venv)",
        allow_module_level=True,
    )

pytestmark = [pytest.mark.asyncio]

from homeassistant import config_entries  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402, F811
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

import custom_components.transport_victoria as tv  # noqa: E402
from custom_components.transport_victoria import config_flow as cf  # noqa: E402
from custom_components.transport_victoria.board import line_value  # noqa: E402
from custom_components.transport_victoria.const import (  # noqa: E402
    CONF_BOARD_NAME,
    CONF_DESTINATION_STOP_ID,
    CONF_DIRECTION,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_ROUTE_ID,
    CONF_STOP_ID,
    CONF_WATCH_NAME,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_SOURCE,
    ENTRY_TYPE_WATCH,
    MODE_BUS,
    SUBENTRY_TYPE_PIN,
)
from custom_components.transport_victoria.gtfs.repository import get_lines_between  # noqa: E402
from tests import gtfs_fixtures as fx  # noqa: E402
from tests.gtfs_builder import import_bundle  # noqa: E402


@pytest.fixture(autouse=True)
def _enable_custom_integration(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _neutralise_network(monkeypatch):
    """No real download/import or key validation during flow tests."""
    monkeypatch.setattr(cf, "is_mode_ready", lambda *a, **k: True)
    monkeypatch.setattr(tv, "async_trigger_gtfs_refresh", _noop_refresh)


async def _noop_refresh(*args, **kwargs):
    return True


@pytest.fixture
def bus_db_path(tmp_path: Path) -> Path:
    return import_bundle(tmp_path, **fx.bus_spec())


@pytest.fixture
def source_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE, "api_key": ""},
        unique_id="source-hub",
        title="Transport Victoria",
    )
    entry.add_to_hass(hass)
    return entry


async def _start_flow(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _drive_to_confirm(hass, flow_id: str, entry_type: str):
    """Walk entry_type → origin → destination → line selection."""
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"entry_type": entry_type}
    )
    assert result["step_id"] == "origin"

    result = await hass.config_entries.flow.async_configure(flow_id, {"query": "Sun"})
    assert result["step_id"] == "origin"
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_STOP_ID: "B_SUNSHINE"}
    )
    assert result["step_id"] == "dest"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"destination_choice": "specific", "dest_query": "Cit"}
    )
    assert result["step_id"] == "dest"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"destination_choice": "specific", CONF_DESTINATION_STOP_ID: "B_CITY"},
    )
    assert result["step_id"] == "lines"

    lines = get_lines_between(
        cf._db_path(hass.config.config_dir), "B_SUNSHINE", "B_CITY"
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"line_select": line_value(lines[0])}
    )
    assert result["step_id"] == "confirm"
    return result


async def test_full_walk_creates_board_with_pin(
    hass, source_entry, bus_db_path: Path, monkeypatch
):
    monkeypatch.setattr(cf, "_db_path", lambda _cfg: bus_db_path)
    result = await _start_flow(hass)
    assert result["step_id"] == "entry_type"

    result = await _drive_to_confirm(hass, result["flow_id"], ENTRY_TYPE_BOARD)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BOARD_NAME: "My Board"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = result["result"]
    assert entry.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_BOARD
    assert entry.data[CONF_BOARD_NAME] == "My Board"
    # Exactly one pin subentry, attached atomically (not via async_add_subentry).
    assert len(entry.subentries) == 1
    sub = next(iter(entry.subentries.values()))
    assert sub.subentry_type == SUBENTRY_TYPE_PIN
    assert sub.data[CONF_ROUTE_ID] == "R_BUS_220"
    assert sub.unique_id == sub.data["pin_key"]


async def test_full_walk_creates_watch_entry(
    hass, source_entry, bus_db_path: Path, monkeypatch
):
    monkeypatch.setattr(cf, "_db_path", lambda _cfg: bus_db_path)
    result = await _start_flow(hass)
    assert result["step_id"] == "entry_type"

    result = await _drive_to_confirm(hass, result["flow_id"], ENTRY_TYPE_WATCH)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_WATCH_NAME: "My Watch"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = result["result"]
    assert entry.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_WATCH
    assert entry.data[CONF_STOP_ID] == "B_SUNSHINE"
    assert entry.data[CONF_DESTINATION_STOP_ID] == "B_CITY"
    assert entry.data[CONF_MODE] == MODE_BUS
    assert entry.data[CONF_DIRECTION] == 0
    assert not entry.subentries
