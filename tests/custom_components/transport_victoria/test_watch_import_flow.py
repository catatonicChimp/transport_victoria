"""T11 watch→board import config-flow tests (real Home Assistant harness).

Drives the native config flow's "import from existing watches" path: two
compatible watches + one incompatible route tracker + one transfer journey →
the board is created with two pins, the tracker/journey are reported as skipped,
and every source entry's data is left byte-for-byte unchanged.  Run ONLY under
the classic harness venv (homeassistant + pytest-homeassistant-custom-component):

    .venv-classic/bin/python -m pytest \\
        tests/custom_components/transport_victoria/test_watch_import_flow.py -q

Skipped automatically in the light env (which stubs homeassistant).
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

from custom_components.transport_victoria import config_flow as cf  # noqa: E402
from custom_components.transport_victoria.const import (  # noqa: E402
    CONF_ALL_DESTINATIONS,
    CONF_API_KEY,
    CONF_BOARD_NAME,
    CONF_DESTINATION_STOP_ID,
    CONF_DESTINATION_STOP_NAME,
    CONF_DIRECTION,
    CONF_ENTRY_TYPE,
    CONF_JOURNEY_DESTINATION_STOP_ID,
    CONF_JOURNEY_MODE,
    CONF_MODE,
    CONF_ROUTE_ID,
    CONF_ROUTE_ID_FILTER,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_TRANSFER_STOP_ID,
    CONF_WATCH_NAME,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_WATCH,
    MODE_BUS,
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


async def _noop_refresh(*args, **kwargs):
    return True


def _watch_data(name: str, overrides: dict | None = None) -> dict:
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
        CONF_MODE: MODE_BUS,
        CONF_API_KEY: "test-key",
        CONF_STOP_ID: "B_SUNSHINE",
        CONF_STOP_NAME: "Sunshine",
        CONF_ALL_DESTINATIONS: True,
        CONF_ROUTE_ID_FILTER: "R_BUS_220",
        CONF_WATCH_NAME: name,
    }
    if overrides:
        data.update(overrides)
    return data


async def _start_flow(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_import_two_watches_skips_tracker_and_journey(
    hass, bus_db_path: Path, monkeypatch
):
    monkeypatch.setattr(cf, "_db_path", lambda _cfg: bus_db_path)
    monkeypatch.setattr(cf, "async_trigger_gtfs_refresh", _noop_refresh)

    # Two compatible watches + one route tracker + one transfer journey.
    watch1 = MockConfigEntry(
        domain=DOMAIN, title="Sunshine All", data=_watch_data("Sunshine All")
    )
    watch1.add_to_hass(hass)
    watch2 = MockConfigEntry(
        domain=DOMAIN,
        title="Sunshine → City",
        data=_watch_data(
            "Sunshine → City",
            {
                CONF_ALL_DESTINATIONS: False,
                CONF_DESTINATION_STOP_ID: "B_CITY",
                CONF_DESTINATION_STOP_NAME: "City",
                CONF_DIRECTION: 0,
            },
        ),
    )
    watch2.add_to_hass(hass)
    tracker = MockConfigEntry(
        domain=DOMAIN,
        title="220 Tracker",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_ROUTE_TRACKER,
            CONF_MODE: MODE_BUS,
            CONF_API_KEY: "test-key",
            CONF_ROUTE_ID: "R_BUS_220",
            CONF_WATCH_NAME: "220 Tracker",
        },
    )
    tracker.add_to_hass(hass)
    journey = MockConfigEntry(
        domain=DOMAIN,
        title="Sunshine Journey",
        data=_watch_data(
            "Sunshine Journey",
            {
                CONF_JOURNEY_MODE: True,
                CONF_TRANSFER_STOP_ID: "B_FOOTSCRAY",
                CONF_JOURNEY_DESTINATION_STOP_ID: "B_CITY",
            },
        ),
    )
    journey.add_to_hass(hass)

    # Snapshot every source entry's data (must be byte-for-byte unchanged).
    source_ids = [watch1.entry_id, watch2.entry_id, tracker.entry_id, journey.entry_id]
    before = {
        eid: dict(hass.config_entries.async_get_entry(eid).data) for eid in source_ids
    }

    # 1. entry_type → import.
    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "entry_type"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "import"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "import"

    # 2. select both watches + a board name.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "watch_ids": [watch1.entry_id, watch2.entry_id],
            CONF_BOARD_NAME: "Imported Board",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "import_summary"

    # 3. confirm → create the board with two pins.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    board = hass.config_entries.async_get_entry(result["result"].entry_id)
    assert board is not None
    assert board.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_BOARD
    assert board.data[CONF_BOARD_NAME] == "Imported Board"

    # Two pins, one per compatible watch.
    assert len(board.subentries) == 2
    pins = list(board.subentries.values())
    assert all(s.subentry_type == SUBENTRY_TYPE_PIN for s in pins)
    route_ids = {s.data[CONF_ROUTE_ID] for s in pins}
    # One all-destinations pin + one specific-destination pin, both on 220.
    assert route_ids == {"R_BUS_220"}
    all_dest = {s.data[CONF_ALL_DESTINATIONS] for s in pins}
    assert all_dest == {True, False}
    # Pin identity == unique_id (T8 contract).
    for s in pins:
        assert s.unique_id == s.data["pin_key"]

    # Source entries untouched: data identical, still present, still watches.
    for eid in source_ids:
        after = dict(hass.config_entries.async_get_entry(eid).data)
        assert after == before[eid]
    assert hass.config_entries.async_get_entry(watch1.entry_id) is not None
    assert hass.config_entries.async_get_entry(tracker.entry_id) is not None
    assert hass.config_entries.async_get_entry(journey.entry_id) is not None


async def test_import_empty_selection_shows_error(hass, bus_db_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "_db_path", lambda _cfg: bus_db_path)
    monkeypatch.setattr(cf, "async_trigger_gtfs_refresh", _noop_refresh)

    watch = MockConfigEntry(
        domain=DOMAIN, title="Sunshine", data=_watch_data("Sunshine")
    )
    watch.add_to_hass(hass)

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "import"}
    )
    assert result["step_id"] == "import"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"watch_ids": [], CONF_BOARD_NAME: "Board"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "import"
    assert result["errors"] == {"watch_ids": "select_at_least_one"}


async def test_import_all_journeys_aborts(hass, bus_db_path: Path, monkeypatch):
    """No compatible watches (only journeys) → the flow aborts with a reason."""
    monkeypatch.setattr(cf, "_db_path", lambda _cfg: bus_db_path)
    monkeypatch.setattr(cf, "async_trigger_gtfs_refresh", _noop_refresh)

    journey = MockConfigEntry(
        domain=DOMAIN,
        title="Sunshine Journey",
        data=_watch_data(
            "Sunshine Journey",
            {
                CONF_JOURNEY_MODE: True,
                CONF_TRANSFER_STOP_ID: "B_FOOTSCRAY",
                CONF_JOURNEY_DESTINATION_STOP_ID: "B_CITY",
            },
        ),
    )
    journey.add_to_hass(hass)

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "import"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_compatible_watches"
