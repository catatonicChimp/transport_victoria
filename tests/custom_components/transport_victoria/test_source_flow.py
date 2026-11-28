"""T18.1 source (hub) ownership contract — real Home Assistant harness tests.

Exercises the native config flow + options flow for the source entry against a
real HA runtime:

  * hub creation (realtime w/ key, and scheduled-only);
  * single-hub rule (a second Add Integration routes to the feature menu);
  * migration (feature entries w/ credential + no hub → auto-hub + association,
    unique ids preserved);
  * hub reconfigure changes credential → dependents reload, unique ids kept;
  * hub deletion cascade removes dependents.

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
from custom_components.transport_victoria import source as src  # noqa: E402
from custom_components.transport_victoria.const import (  # noqa: E402
    CONF_ALL_DESTINATIONS,
    CONF_API_KEY,
    CONF_DISPLAY_NAME,
    CONF_ENABLE_REALTIME,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_ROUTE_ID,
    CONF_SOURCE_ENTRY_ID,
    CONF_STOP_ID,
    DOMAIN,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_SOURCE,
    ENTRY_TYPE_WATCH,
    MODE_BUS,
    READINESS_READY,
    READINESS_SCHEDULE_FAILED,
    SOURCE_CHOICE_REALTIME,
    SOURCE_CHOICE_SCHEDULED,
)


@pytest.fixture(autouse=True)
def _enable_custom_integration(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _neutralise_network(monkeypatch):
    """No real download/import or key validation during flow tests."""
    monkeypatch.setattr(cf, "is_mode_ready", lambda *a, **k: True)
    monkeypatch.setattr(tv, "async_trigger_gtfs_refresh", _noop_refresh)
    monkeypatch.setattr(cf, "_validate_api_key", _noop_validate)



import pytest

@pytest.fixture
def source_hub(hass):
    """Create a source hub entry for testing the options menu."""
    entry = MockConfigEntry(domain="transport_victoria", title="Transport Victoria", data={"entry_type": "source"})
    entry.add_to_hass(hass)
    return entry


async def _noop_refresh(*args, **kwargs):
    return True


async def _noop_validate(hass, api_key):
    return {}


async def _start_flow(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _create_source(
    hass, *, realtime: bool, key: str | None = None, name: str = "Transport Victoria"
):
    """Drive the hub-creation flow to CREATE_ENTRY; return the created entry."""
    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    choice = SOURCE_CHOICE_REALTIME if realtime else SOURCE_CHOICE_SCHEDULED
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DISPLAY_NAME: name, CONF_ENABLE_REALTIME: choice},
    )
    if realtime:
        assert result["step_id"] == "source_key"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: key or "canary-key"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    return hass.config_entries.async_get_entry(result["result"].entry_id)


async def test_create_source_scheduled_only(hass):
    entry = await _create_source(hass, realtime=False)
    assert entry is not None
    assert entry.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_SOURCE
    assert entry.data.get(CONF_API_KEY, "") == ""
    assert src.get_source_api_key(hass) is None


async def test_create_source_realtime_with_key(hass):
    entry = await _create_source(hass, realtime=True, key="secret-key")
    assert entry.data[CONF_API_KEY] == "secret-key"
    assert src.get_source_api_key(hass) == "secret-key"


async def test_second_add_integration_routes_to_feature_menu(hass):
    await _create_source(hass, realtime=False)
    result = await _start_flow(hass)
    # With a hub present, Add Integration opens the feature menu (never a 2nd hub).
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "entry_type"


async def test_migration_creates_hub_and_associates(hass):
    """Feature entries w/ a credential + no hub → auto-hub + source_entry_id."""
    watch = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
            CONF_API_KEY: "carried-key",
            CONF_MODE: MODE_BUS,
            CONF_STOP_ID: "B_SUNSHINE",
        },
        unique_id="watch-unique-1",
        title="Sunshine",
    )
    watch.add_to_hass(hass)

    hub = await tv.async_migrate_entries_to_source(hass)

    assert hub is not None
    assert hub.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_SOURCE
    assert hub.data[CONF_API_KEY] == "carried-key"

    refreshed = hass.config_entries.async_get_entry(watch.entry_id)
    assert refreshed.data[CONF_SOURCE_ENTRY_ID] == hub.entry_id
    # Unique id preserved (not rewritten by the association).
    assert refreshed.unique_id == "watch-unique-1"


async def test_migration_scheduled_only_when_no_credential(hass):
    watch = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_MODE: MODE_BUS, CONF_STOP_ID: "B_SUNSHINE"},
        title="Keyless Watch",
    )
    watch.add_to_hass(hass)

    hub = await tv.async_migrate_entries_to_source(hass)
    assert hub.data.get(CONF_API_KEY, "") == ""
    refreshed = hass.config_entries.async_get_entry(watch.entry_id)
    assert refreshed.data[CONF_SOURCE_ENTRY_ID] == hub.entry_id


async def test_reconfigure_changes_credential_and_reloads_dependents(hass, monkeypatch):
    hub = await _create_source(hass, realtime=True, key="old-key")
    watch = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
            CONF_SOURCE_ENTRY_ID: hub.entry_id,
            CONF_MODE: MODE_BUS,
            CONF_STOP_ID: "B_SUNSHINE",
        },
        unique_id="watch-unique-2",
        title="Sunshine",
    )
    watch.add_to_hass(hass)

    # Reload spy: dependents must be reloaded on credential change.
    reloaded: list[str] = []
    orig_reload = hass.config_entries.async_reload

    async def _spy_reload(entry_id):
        reloaded.append(entry_id)
        await orig_reload(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", _spy_reload)

    # Open the hub options flow → reconfigure → new credential.
    result = await hass.config_entries.options.async_init(hub.entry_id)
    assert result["step_id"] == "source_menu"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_reconfigure"}
    )
    assert result["step_id"] == "source_reconfigure"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ENABLE_REALTIME: SOURCE_CHOICE_REALTIME, CONF_API_KEY: "new-key"},
    )

    hub_after = hass.config_entries.async_get_entry(hub.entry_id)
    assert hub_after.data[CONF_API_KEY] == "new-key"
    assert src.get_source_api_key(hass) == "new-key"
    # The dependent watch was reloaded with the new credential.
    assert watch.entry_id in reloaded
    # Unique id preserved across the reload.
    assert hass.config_entries.async_get_entry(watch.entry_id).unique_id == "watch-unique-2"


async def test_reconfigure_remove_key_is_scheduled_only(hass):
    hub = await _create_source(hass, realtime=True, key="old-key")
    result = await hass.config_entries.options.async_init(hub.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_reconfigure"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ENABLE_REALTIME: SOURCE_CHOICE_SCHEDULED}
    )
    assert hass.config_entries.async_get_entry(hub.entry_id).data.get(CONF_API_KEY, "") == ""
    assert src.get_source_api_key(hass) is None


async def test_hub_remove_cascades_dependents(hass):
    hub = await _create_source(hass, realtime=False)
    watch = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
            CONF_SOURCE_ENTRY_ID: hub.entry_id,
            CONF_MODE: MODE_BUS,
            CONF_STOP_ID: "B_SUNSHINE",
        },
        title="Sunshine",
    )
    watch.add_to_hass(hass)
    assert hass.config_entries.async_get_entry(watch.entry_id) is not None

    result = await hass.config_entries.options.async_init(hub.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_remove"}
    )
    assert result["step_id"] == "source_remove"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )

    # Both the hub and its dependent are gone.
    assert hass.config_entries.async_get_entry(hub.entry_id) is None
    assert hass.config_entries.async_get_entry(watch.entry_id) is None


async def test_second_source_creation_aborts_via_unique_id(hass):
    """F2 — the stable unique id makes a second source creation race-safe."""
    from custom_components.transport_victoria.const import SOURCE_UNIQUE_ID

    hub = await _create_source(hass, realtime=False)
    assert hub.unique_id == SOURCE_UNIQUE_ID

    # A second programmatic creation must abort (unique-id reservation).
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE,
            CONF_DISPLAY_NAME: "Second Hub",
            CONF_API_KEY: "",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    # Exactly one source entry remains.
    sources = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SOURCE
    ]
    assert len(sources) == 1


async def test_reconcile_strips_credentials_and_repairs_dangling(hass):
    """F3 — reconciliation strips copied credentials + repairs a dangling link."""
    hub = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE, CONF_API_KEY: "carried-key"},
        unique_id="transport_victoria_source",
        title="Transport Victoria",
    )
    hub.add_to_hass(hass)

    # A feature with a copied credential and a DANGLING source_entry_id.
    orphan = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
            CONF_API_KEY: "stale-copy",
            CONF_SOURCE_ENTRY_ID: "dead-hub",
            CONF_MODE: MODE_BUS,
            CONF_STOP_ID: "B_SUNSHINE",
        },
        unique_id="orphan-unique",
        title="Orphan",
    )
    orphan.add_to_hass(hass)

    result = await tv.async_migrate_entries_to_source(hass)

    assert result.entry_id == hub.entry_id
    fixed = hass.config_entries.async_get_entry(orphan.entry_id)
    assert fixed.data[CONF_SOURCE_ENTRY_ID] == hub.entry_id
    assert CONF_API_KEY not in fixed.data  # credential stripped (hub owns it)
    assert fixed.unique_id == "orphan-unique"  # unique id preserved


async def test_reconcile_idempotent(hass):
    """F3 — re-running reconciliation is a no-op once everything is consistent."""
    hub = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE, CONF_API_KEY: "k"},
        unique_id="transport_victoria_source",
        title="Transport Victoria",
    )
    hub.add_to_hass(hass)
    watch = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
            CONF_SOURCE_ENTRY_ID: hub.entry_id,
            CONF_MODE: MODE_BUS,
            CONF_STOP_ID: "B_SUNSHINE",
        },
        title="Watch",
    )
    watch.add_to_hass(hass)

    await tv.async_migrate_entries_to_source(hass)
    data_after_first = dict(hass.config_entries.async_get_entry(watch.entry_id).data)

    await tv.async_migrate_entries_to_source(hass)
    data_after_second = dict(hass.config_entries.async_get_entry(watch.entry_id).data)
    assert data_after_second == data_after_first


async def test_add_monitor_option_removed_from_source_menu(hass, source_hub):
    """Maintainer decision: features are added via Add Integration, not the options menu."""
    result = await hass.config_entries.options.async_init(source_hub.entry_id)
    assert result["type"] == FlowResultType.MENU
    assert set(result["menu_options"]) == {
        "source_reconfigure",
        "source_refresh",
        "source_remove",
    }
    assert "add_monitor" not in set(result["menu_options"])
async def test_force_refresh_surfaces_failure_then_success(hass, monkeypatch):
    """F1 — manual repair routes through the manager + surfaces the outcome."""
    hub = await _create_source(hass, realtime=False)

    outcome = {"ok": False}

    async def fake_trigger(hass, bundles, *, skip_download_if_exists=False, on_phase=None):
        return outcome["ok"]

    monkeypatch.setattr(tv, "async_trigger_gtfs_refresh", fake_trigger)

    result = await hass.config_entries.options.async_init(hub.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_refresh"}
    )
    assert result["step_id"] == "source_refresh"

    # Failure → surfaced error (not a silent success) + SCHEDULE_FAILED readiness.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm_refresh": True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "schedule_refresh_failed"}
    assert src.source_readiness(hass, hub.entry_id) == READINESS_SCHEDULE_FAILED

    # Success → READY surfaced in the outcome message.
    outcome["ok"] = True
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm_refresh": True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["refresh_outcome"] == "Schedule is ready."
    assert src.source_readiness(hass, hub.entry_id) == READINESS_READY


async def test_legacy_entries_migrate_and_are_fully_associated(hass):
    """Reproduction of the live defect: v0.1.0-era entries (own api_key, no
    source_entry_id, missing/varying modern fields) on an instance WITHOUT a
    hub → the migration creates the hub AND stamps + strips every feature."""
    legacy_watch = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
            CONF_MODE: "metro",
            CONF_STOP_ID: "14308",
            CONF_ALL_DESTINATIONS: True,
            CONF_API_KEY: "legacy-key",
            # legacy shape: no stop_name / watch_name (modern fields absent)
        },
        title="Coburg",
    )
    legacy_watch.add_to_hass(hass)
    tracker6 = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_ROUTE_TRACKER,
            CONF_MODE: "metro",
            CONF_ROUTE_ID: "R6",
            CONF_API_KEY: "legacy-key",
        },
        title="6 Tracker",
    )
    tracker6.add_to_hass(hass)
    tracker1 = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_ROUTE_TRACKER,
            CONF_MODE: "metro",
            CONF_ROUTE_ID: "R1",
            CONF_API_KEY: "legacy-key",
        },
        title="1 Tracker",
    )
    tracker1.add_to_hass(hass)

    hub = await tv.async_migrate_entries_to_source(hass)

    assert hub is not None
    assert hub.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_SOURCE
    assert hub.data[CONF_API_KEY] == "legacy-key"

    for entry in (legacy_watch, tracker6, tracker1):
        e = hass.config_entries.async_get_entry(entry.entry_id)
        assert e.data[CONF_SOURCE_ENTRY_ID] == hub.entry_id
        assert CONF_API_KEY not in e.data  # hub alone owns the credential

    # Idempotent: a second restart reconciles to a no-op.
    data_after_first = {
        eid: dict(hass.config_entries.async_get_entry(eid).data)
        for eid in (legacy_watch.entry_id, tracker6.entry_id, tracker1.entry_id)
    }
    await tv.async_migrate_entries_to_source(hass)
    for eid in data_after_first:
        assert dict(hass.config_entries.async_get_entry(eid).data) == data_after_first[eid]


async def test_feature_setup_stamps_association_defensively(hass):
    """A feature set up after the hub exists is stamped even without migration."""
    hub = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE, CONF_API_KEY: "k"},
        unique_id="transport_victoria_source",
        title="Transport Victoria",
    )
    hub.add_to_hass(hass)

    legacy = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_MODE: "metro", CONF_STOP_ID: "14308", CONF_API_KEY: "k"},
        title="Coburg",
    )
    legacy.add_to_hass(hass)

    await tv._ensure_feature_associated(hass, legacy)

    e = hass.config_entries.async_get_entry(legacy.entry_id)
    assert e.data[CONF_SOURCE_ENTRY_ID] == hub.entry_id
    assert CONF_API_KEY not in e.data
