"""T18.2 keyless vertical slice — real Home Assistant harness tests.

One fixture walk proves the whole scheduled-only contract at the ENTITY level:

  (a) hub created scheduled-only (no credential);
  (b) schedule ready (offline metro fixture, no download);
  (c) a watch configured → departure sensors created + AVAILABLE with scheduled
      rows (``realtime_state="scheduled"``, ``scheduled_only=True``); the
      disruption binary sensor is NOT created; fleet trackers are absent;
  (d) hub reconfigure → add key → realtime turns on (stubbed trip-updates feed
      → ``delay_seconds=90``, ``realtime_state="live"``, ``scheduled_only`` gone);
  (e) hub reconfigure → remove key → back to scheduled-only with NO reauth storm
      (no ``ConfigEntryAuthFailed``, entry still LOADED, sensors still available);
  (f) reload (restart-equivalent) → entity identity preserved.

Run ONLY under the classic harness venv (homeassistant 2026.2.3 +
pytest-homeassistant-custom-component); skipped automatically in the light env.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.const import STATE_UNAVAILABLE  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

import custom_components.transport_victoria as tv  # noqa: E402
from custom_components.transport_victoria import config_flow as cf  # noqa: E402
from custom_components.transport_victoria import source as src  # noqa: E402
from custom_components.transport_victoria.const import (  # noqa: E402
    CONF_ALL_DESTINATIONS,
    CONF_API_KEY,
    CONF_ENABLE_REALTIME,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_SOURCE_ENTRY_ID,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_TRACK_VEHICLES,
    CONF_WATCH_NAME,
    DOMAIN,
    ENTRY_TYPE_SOURCE,
    ENTRY_TYPE_WATCH,
    FEED_SERVICE_ALERTS,
    FEED_TRIP_UPDATES,
    MODE_METRO,
    REALTIME_STATE_LIVE,
    REALTIME_STATE_SCHEDULED,
    SOURCE_CHOICE_REALTIME,
    SOURCE_CHOICE_SCHEDULED,
)
from custom_components.transport_victoria.realtime.client import PTVRealtimeClient  # noqa: E402
from tests import gtfs_fixtures as fx  # noqa: E402
from tests import realtime_fixtures as rfx  # noqa: E402
from tests.gtfs_builder import import_bundle  # noqa: E402

# Pinned Monday within the metro fixture's weekday calendar (see
# tests.gtfs_fixtures.metro_spec / tests.gtfs_builder.REFERENCE_DATE).
FROZEN_NAIVE = datetime(2026, 6, 1, 8, 0, 0)
FROZEN_UTC = datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _enable_custom_integration(enable_custom_integrations):
    yield


@pytest.fixture
def metro_db_path(tmp_path: Path) -> Path:
    return import_bundle(tmp_path, **fx.metro_spec())


async def _noop_refresh(*args, **kwargs):
    return True


async def _noop_validate(hass, api_key):
    return {}


def _freeze_time(monkeypatch) -> None:
    """Pin ``dt_util`` to a weekday with departures (deterministic schedule)."""
    monkeypatch.setattr(dt_util, "now", lambda *a, **k: FROZEN_NAIVE)
    monkeypatch.setattr(dt_util, "utcnow", lambda *a, **k: FROZEN_UTC)


def _stub_realtime(monkeypatch, fetch_calls: list) -> None:
    """Replace the realtime client fetch with offline fixtures (counting calls).

    trip-updates → a 90s delay on ``T_FRK_OUT_2`` at RICHMOND;
    service-alerts → empty feed.  ``fetch_calls`` records every (mode, feed) so
    tests can assert scheduled-only mode never issues a realtime request.
    """

    async def _fake_fetch(self, mode, feed_type):
        fetch_calls.append((mode, feed_type))
        if feed_type == FEED_TRIP_UPDATES:
            return rfx.BUILDERS["trip_update_delay"]()
        if feed_type == FEED_SERVICE_ALERTS:
            return rfx.BUILDERS["header_tick_0"]()
        return rfx.BUILDERS["header_tick_0"]()

    monkeypatch.setattr(PTVRealtimeClient, "fetch_feed", _fake_fetch)


@pytest.fixture
def slice_ctx(hass, metro_db_path: Path, monkeypatch):
    """(a)+(b)+(c): scheduled-only hub + a configured watch with live entities."""
    _freeze_time(monkeypatch)
    fetch_calls: list = []
    _stub_realtime(monkeypatch, fetch_calls)

    monkeypatch.setattr(tv, "async_trigger_gtfs_refresh", _noop_refresh)
    monkeypatch.setattr(tv, "_db_path", lambda _hass: metro_db_path)
    monkeypatch.setattr(cf, "_validate_api_key", _noop_validate)

    hub = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE, CONF_API_KEY: ""},
        unique_id="transport_victoria_source",
        title="Transport Victoria",
    )
    hub.add_to_hass(hass)
    watch = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
            CONF_SOURCE_ENTRY_ID: hub.entry_id,
            CONF_MODE: MODE_METRO,
            CONF_STOP_ID: "RICHMOND",
            CONF_STOP_NAME: "Richmond",
            CONF_ALL_DESTINATIONS: True,
            CONF_WATCH_NAME: "Richmond",
        },
        options={CONF_TRACK_VEHICLES: True},
        unique_id="watch-keyless-slice",
        title="Richmond",
    )

    return SimpleNamespace(
        hass=hass,
        hub=hub,
        watch=watch,
        fetch_calls=fetch_calls,
    )


async def _setup(hass, hub, watch) -> None:
    """Set up the hub first, then add + set up the watch (real creation order)."""
    await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    watch.add_to_hass(hass)
    await hass.config_entries.async_setup(watch.entry_id)
    await hass.async_block_till_done()


def _dep_entity_ids(hass, entry_id: str) -> list[str | None]:
    """Return the three departure sensor entity ids (slot 1..3)."""
    reg = er.async_get(hass)
    return [
        reg.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_departure_{slot}")
        for slot in (1, 2, 3)
    ]


def _dep_states(hass, entry_id: str) -> list:
    """Return the three departure sensor State objects (slot order)."""
    return [hass.states.get(eid) for eid in _dep_entity_ids(hass, entry_id)]


async def _reconfigure_key(hass, hub, key: str | None) -> None:
    """Drive the source options flow to add (str) or remove (None) the key.

    Adding a key is a two-step form: choose "realtime" (which re-renders the
    form with the api_key field), then supply the key.
    """
    result = await hass.config_entries.options.async_init(hub.entry_id)
    assert result["step_id"] == "source_menu"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_reconfigure"}
    )
    assert result["step_id"] == "source_reconfigure"
    if key:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_ENABLE_REALTIME: SOURCE_CHOICE_REALTIME}
        )
        assert result["step_id"] == "source_reconfigure"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_ENABLE_REALTIME: SOURCE_CHOICE_REALTIME, CONF_API_KEY: key},
        )
    else:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_ENABLE_REALTIME: SOURCE_CHOICE_SCHEDULED},
        )
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# The vertical slice (one walk)
# ---------------------------------------------------------------------------


async def test_keyless_vertical_slice(slice_ctx):
    """(a)→(f): scheduled-only → add key → realtime → remove key → reload."""
    hass = slice_ctx.hass
    hub = slice_ctx.hub
    watch = slice_ctx.watch

    await _setup(hass, hub, watch)

    # ---- (a)+(b): hub scheduled-only, schedule ready ----------------------
    assert src.get_source_api_key(hass) is None
    assert src.source_is_scheduled_only(hass) is True

    # ---- (c): departure sensors created + AVAILABLE with scheduled rows ----
    dep_ids = _dep_entity_ids(hass, watch.entry_id)
    assert all(eid is not None for eid in dep_ids)
    states = _dep_states(hass, watch.entry_id)
    for s in states:
        assert s is not None
        assert s.state not in (STATE_UNAVAILABLE, "unknown", None)
        assert s.attributes["realtime_state"] == REALTIME_STATE_SCHEDULED
        assert s.attributes["scheduled_only"] is True
        assert s.attributes["delay_seconds"] == 0  # watch: scheduled → delay 0
    # First slot is the 08:07 Frankston departure (scheduled == actual).
    assert states[0].attributes["scheduled_time"] == "2026-06-01T08:07:00"
    assert states[0].attributes["actual_time"] == "2026-06-01T08:07:00"

    # Disruption binary sensor is NOT created (scheduled-only); no fleet.
    assert not any(
        s.domain == "binary_sensor" for s in hass.states.async_all()
    )
    assert not any(
        s.domain == "device_tracker" for s in hass.states.async_all()
    )

    # Scheduled-only never issued a realtime request (client never built).
    assert slice_ctx.fetch_calls == []

    # ---- (d): add key → realtime turns on --------------------------------
    await _reconfigure_key(hass, hub, "canary-key")
    assert src.get_source_api_key(hass) == "canary-key"
    assert src.source_is_scheduled_only(hass) is False

    # The watch was reloaded with the new credential and fetched RT once.
    assert any(c[1] == FEED_TRIP_UPDATES for c in slice_ctx.fetch_calls)

    states = _dep_states(hass, watch.entry_id)
    assert states[0].attributes["realtime_state"] == REALTIME_STATE_LIVE
    assert states[0].attributes["delay_seconds"] == 90  # trip_update_delay
    assert states[0].attributes["actual_time"] == "2026-06-01T08:08:30"
    assert "scheduled_only" not in states[0].attributes

    # The disruption binary sensor is now created (credential present).
    assert any(
        s.domain == "binary_sensor" for s in hass.states.async_all()
    )

    # ---- (e): remove key → scheduled-only, NO reauth storm ---------------
    fetch_calls_after_rt = len(slice_ctx.fetch_calls)
    await _reconfigure_key(hass, hub, None)
    assert src.get_source_api_key(hass) is None
    assert src.source_is_scheduled_only(hass) is True

    # Entry still loaded (no ConfigEntryAuthFailed → no error/retry state).
    entry_after = hass.config_entries.async_get_entry(watch.entry_id)
    assert entry_after.state is ConfigEntryState.LOADED

    # No further realtime request after the key was removed (client disposed).
    assert len(slice_ctx.fetch_calls) == fetch_calls_after_rt

    states = _dep_states(hass, watch.entry_id)
    for s in states:
        assert s is not None
        assert s.state not in (STATE_UNAVAILABLE, "unknown", None)
        assert s.attributes["realtime_state"] == REALTIME_STATE_SCHEDULED
        assert s.attributes["scheduled_only"] is True

    # Disruption binary sensor: after the key is removed the entity lingers as
    # "unavailable" (HA preserves the entity_id from the keyed phase) — it is
    # never ON/OFF (never a false OFF).  A *fresh* scheduled-only setup does not
    # create it at all (asserted in the (c) phase above).
    _bs = [s for s in hass.states.async_all() if s.domain == "binary_sensor"]
    assert _bs and all(s.state == STATE_UNAVAILABLE for s in _bs)

    # ---- (f): reload (restart-equivalent) → identity preserved ------------
    registry = er.async_get(hass)

    def _identity() -> dict[str, str | None]:
        return {
            eid: registry.async_get(eid).unique_id
            for eid in _dep_entity_ids(hass, watch.entry_id)
            if eid is not None
        }

    identity_before = _identity()
    await hass.config_entries.async_reload(watch.entry_id)
    await hass.async_block_till_done()

    assert _identity() == identity_before
    # And still scheduled-only, still available.
    states = _dep_states(hass, watch.entry_id)
    assert all(
        s is not None
        and s.state not in (STATE_UNAVAILABLE, "unknown", None)
        and s.attributes["scheduled_only"] is True
        for s in states
    )


async def test_disruption_sensor_created_when_realtime_from_the_start(slice_ctx):
    """A keyed hub creates the disruption sensor (contrast with scheduled-only)."""
    hass = slice_ctx.hass
    hub = slice_ctx.hub
    watch = slice_ctx.watch

    # Seed the hub with a key BEFORE the watch is set up.
    hass.config_entries.async_update_entry(
        hub, data={CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE, CONF_API_KEY: "canary-key"}
    )
    await _setup(hass, hub, watch)

    assert src.source_is_scheduled_only(hass) is False
    assert any(
        s.domain == "binary_sensor" for s in hass.states.async_all()
    )
    states = _dep_states(hass, watch.entry_id)
    assert "scheduled_only" not in states[0].attributes
