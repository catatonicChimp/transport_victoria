"""T18.1 source (hub) ownership/readiness contract — light-env tests.

Covers the pure helpers in ``source.py``, the config-flow routing (single-hub
rule, hub creation, source-key validation, source options), and the scheduled-
only gating without the real HA runtime.  The real-runtime flow/migration/cascade
journeys live in ``test_source_flow.py`` (classic harness).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_components.transport_victoria as tv
from custom_components.transport_victoria import config_flow as cf
from custom_components.transport_victoria import source as src
from custom_components.transport_victoria.const import (
    CONF_API_KEY,
    CONF_DISPLAY_NAME,
    CONF_ENABLE_REALTIME,
    CONF_ENTRY_TYPE,
    CONF_REALTIME_ENABLED,
    CONF_SOURCE_ENTRY_ID,
    DOMAIN,
    ENTRY_TYPE_SOURCE,
    ENTRY_TYPE_WATCH,
    READINESS_SCHEDULE_FAILED,
    SOURCE_CHOICE_REALTIME,
    SOURCE_CHOICE_SCHEDULED,
)


# ---------------------------------------------------------------------------
# Fake hass / entries
# ---------------------------------------------------------------------------


def _entry(entry_id: str, data: dict) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=entry_id,
        data=data,
        options={},
        title=entry_id,
        disabled_by=None,
    )


def _hass(entries: list | None = None, *, disabled_by=None) -> SimpleNamespace:
    entries = entries or []

    def async_entries(domain):
        return entries

    def async_get_entry(sid):
        return next((e for e in entries if e.entry_id == sid), None)

    return SimpleNamespace(
        data={DOMAIN: {}},
        config_entries=SimpleNamespace(
            async_entries=async_entries,
            async_get_entry=async_get_entry,
        ),
    )


def _source_data(key: str | None) -> dict:
    data = {CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE}
    if key is not None:
        data[CONF_API_KEY] = key
    return data


# ---------------------------------------------------------------------------
# source.py pure helpers
# ---------------------------------------------------------------------------


class TestSourceLookups:
    def test_get_source_entry_returns_hub(self):
        hub = _entry("hub1", _source_data("k"))
        hass = _hass([hub, _entry("w1", {CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH})])
        assert src.get_source_entry(hass).entry_id == "hub1"

    def test_get_source_entry_none_when_no_hub(self):
        hass = _hass([_entry("w1", {CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH})])
        assert src.get_source_entry(hass) is None

    def test_get_source_api_key(self):
        hass = _hass([_entry("hub1", _source_data("secret"))])
        assert src.get_source_api_key(hass) == "secret"

    def test_get_source_api_key_none_scheduled_only(self):
        hass = _hass([_entry("hub1", _source_data(None))])
        assert src.get_source_api_key(hass) is None

    def test_get_source_api_key_none_no_hub(self):
        assert src.get_source_api_key(_hass([])) is None

    def test_source_is_scheduled_only(self):
        hass = _hass([_entry("hub1", _source_data(None))])
        assert src.source_is_scheduled_only(hass) is True
        hass = _hass([_entry("hub1", _source_data("k"))])
        assert src.source_is_scheduled_only(hass) is False
        # No hub at all → not "scheduled-only".
        assert src.source_is_scheduled_only(_hass([])) is False

    def test_source_disabled(self):
        assert src.source_disabled(_hass([])) is False
        hub = _entry("hub1", _source_data("k"))
        hub.disabled_by = "user"
        assert src.source_disabled(_hass([hub])) is True

    def test_all_mode_bundles_is_union(self):
        bundles = src.all_mode_bundles()
        assert "2" in bundles and "3" in bundles
        assert len(bundles) == len(set(bundles))


class TestEffectiveRealtime:
    def _feature(self, options: dict | None = None, data: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(entry_id="w1", options=options or {}, data=data or {})

    def test_enabled_with_credential_and_ready(self):
        hass = _hass([_entry("hub1", _source_data("k"))])
        hass.data[DOMAIN]["hub1"] = {"readiness": "ready"}
        assert src.effective_realtime_enabled(hass, self._feature()) is True

    def test_disabled_without_credential(self):
        hass = _hass([_entry("hub1", _source_data(None))])
        assert src.effective_realtime_enabled(hass, self._feature()) is False

    def test_disabled_when_feature_requests_off(self):
        hass = _hass([_entry("hub1", _source_data("k"))])
        feature = self._feature(options={CONF_REALTIME_ENABLED: False})
        assert src.effective_realtime_enabled(hass, feature) is False

    def test_schedule_failed_does_not_disable_realtime(self):
        # F1d: a failed refresh (SCHEDULE_FAILED readiness) pauses the lifecycle,
        # not the current data — realtime stays enabled when a credential exists.
        hass = _hass([_entry("hub1", _source_data("k"))])
        hass.data[DOMAIN]["hub1"] = {"readiness": READINESS_SCHEDULE_FAILED}
        assert src.effective_realtime_enabled(hass, self._feature()) is True

    def test_disabled_when_no_hub(self):
        assert src.effective_realtime_enabled(_hass([]), self._feature()) is False


class TestDependents:
    def test_find_dependents(self):
        hub = _entry("hub1", _source_data("k"))
        watch = _entry(
            "w1", {CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_SOURCE_ENTRY_ID: "hub1"}
        )
        orphan = _entry("w2", {CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH})
        hass = _hass([hub, watch, orphan])
        deps = src.find_dependents(hass, "hub1")
        assert [d.entry_id for d in deps] == ["w1"]


# ---------------------------------------------------------------------------
# Defensive reconciliation: strip a stowaway credential on feature setup
# ---------------------------------------------------------------------------


class TestReconcileFeatureData:
    def test_strips_stowaway_api_key_when_already_associated(self):
        """A feature with a copied api_key AND a correct source_entry_id still
        gets its credential stripped (the hub owns it)."""
        hub = _entry("hub1", _source_data("secret"))
        watch = _entry(
            "w1",
            {
                CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
                CONF_SOURCE_ENTRY_ID: "hub1",  # already correctly associated
                CONF_API_KEY: "secret",        # stowaway copy (defect)
            },
        )
        hass = _hass([hub, watch])

        result = tv._reconcile_feature_data(hass, watch, "hub1")

        assert result is not None
        assert CONF_API_KEY not in result
        assert result[CONF_SOURCE_ENTRY_ID] == "hub1"

    def test_preserves_key_when_hub_is_scheduled_only(self):
        """A scheduled-only hub never destroys a feature's only credential."""
        hub = _entry("hub1", _source_data(None))  # no credential
        watch = _entry(
            "w1",
            {
                CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
                CONF_SOURCE_ENTRY_ID: "hub1",
                CONF_API_KEY: "legacy-key",
            },
        )
        hass = _hass([hub, watch])

        result = tv._reconcile_feature_data(hass, watch, "hub1")

        assert result is None  # unchanged — the key is preserved

    def test_unchanged_when_no_key_and_associated(self):
        """A clean, associated feature reconciles to a no-op (None)."""
        hub = _entry("hub1", _source_data("secret"))
        watch = _entry(
            "w1",
            {CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_SOURCE_ENTRY_ID: "hub1"},
        )
        hass = _hass([hub, watch])

        assert tv._reconcile_feature_data(hass, watch, "hub1") is None


# ---------------------------------------------------------------------------
# Config-flow routing (single-hub rule + hub creation)
# ---------------------------------------------------------------------------


def _flow_with_entries(entries):
    flow = cf.PTVConfigFlow()
    flow.hass = _hass(entries)
    return flow


async def test_no_source_shows_hub_form():
    flow = _flow_with_entries([])
    shown = {}
    flow.async_show_form = lambda **kw: shown.update(kw) or {"type": "form"}
    result = await flow.async_step_user(None)
    assert result["type"] == "form"
    assert shown["step_id"] == "user"


async def test_source_exists_routes_to_feature_menu(monkeypatch):
    flow = _flow_with_entries([_entry("hub1", _source_data("k"))])
    called = {}

    async def fake_entry_type(user_input=None):
        called["ok"] = True
        return {"step_id": "entry_type"}

    monkeypatch.setattr(flow, "async_step_entry_type", fake_entry_type)
    await flow.async_step_user(None)
    assert called["ok"] is True


async def test_single_hub_aborts_second_source(monkeypatch):
    flow = _flow_with_entries([_entry("hub1", _source_data("k"))])
    aborted = {}
    monkeypatch.setattr(flow, "async_abort", lambda **kw: aborted.update(kw) or {"type": "abort"})
    await flow.async_step_user({CONF_DISPLAY_NAME: "x", CONF_ENABLE_REALTIME: SOURCE_CHOICE_REALTIME})
    assert aborted == {"reason": "already_configured"}


async def test_hub_realtime_choice_advances_to_key(monkeypatch):
    flow = _flow_with_entries([])
    called = {}

    async def fake_key(user_input=None):
        called["ok"] = True
        return {"step_id": "source_key"}

    monkeypatch.setattr(flow, "async_step_source_key", fake_key)
    await flow.async_step_user({CONF_DISPLAY_NAME: "TV", CONF_ENABLE_REALTIME: SOURCE_CHOICE_REALTIME})
    assert called["ok"] is True
    assert flow._source_mode is True
    assert flow._config[CONF_DISPLAY_NAME] == "TV"


async def test_hub_scheduled_choice_advances_to_download(monkeypatch):
    flow = _flow_with_entries([])
    called = {}

    async def fake_download(user_input=None):
        called["ok"] = True
        return {"step_id": "downloading"}

    monkeypatch.setattr(flow, "async_step_downloading", fake_download)
    await flow.async_step_user({CONF_DISPLAY_NAME: "TV", CONF_ENABLE_REALTIME: SOURCE_CHOICE_SCHEDULED})
    assert called["ok"] is True
    assert flow._config[CONF_API_KEY] == ""


async def test_source_key_valid_advances(monkeypatch):
    flow = _flow_with_entries([])

    async def fake_validate(hass, key):
        return {}

    monkeypatch.setattr(cf, "_validate_api_key", fake_validate)
    called = {}

    async def fake_download(user_input=None):
        called["ok"] = True
        return {"step_id": "downloading"}

    monkeypatch.setattr(flow, "async_step_downloading", fake_download)
    await flow.async_step_source_key({CONF_API_KEY: "valid"})
    assert called["ok"] is True
    assert flow._config[CONF_API_KEY] == "valid"


async def test_source_key_invalid_reshows(monkeypatch):
    flow = _flow_with_entries([])

    async def fake_validate(hass, key):
        return {"base": "invalid_auth"}

    monkeypatch.setattr(cf, "_validate_api_key", fake_validate)
    shown = {}
    flow.async_show_form = lambda **kw: shown.update(kw) or {"type": "form"}
    result = await flow.async_step_source_key({CONF_API_KEY: "bad"})
    assert result["type"] == "form"
    assert shown["errors"] == {"base": "invalid_auth"}


async def test_create_source_entry_data(monkeypatch):
    flow = _flow_with_entries([])
    flow._config = {CONF_DISPLAY_NAME: "My Hub", CONF_API_KEY: "secret"}
    created = {}
    monkeypatch.setattr(flow, "async_create_entry", lambda **kw: created.update(kw) or {"type": "create_entry"})

    async def _noop_set_unique_id(unique_id):
        return None

    monkeypatch.setattr(flow, "async_set_unique_id", _noop_set_unique_id)
    await flow.async_step_create_source()
    assert created["title"] == "My Hub"
    assert created["data"][CONF_ENTRY_TYPE] == ENTRY_TYPE_SOURCE
    assert created["data"][CONF_API_KEY] == "secret"


def test_flow_bundles_source_mode_is_all():
    flow = cf.PTVConfigFlow()
    flow._source_mode = True
    flow._config = {}
    assert flow._flow_bundles() == src.all_mode_bundles()
    flow._source_mode = False
    flow._config = {"mode": "metro"}
    assert flow._flow_bundles() == ["2"]


# ---------------------------------------------------------------------------
# Source options flow (reconfigure credential)
# ---------------------------------------------------------------------------


def _source_options_flow(key: str | None, monkeypatch):
    entry = _entry("hub1", _source_data(key))
    flow = cf.PTVSourceOptionsFlow()
    flow.config_entry = entry
    reloaded = []
    removed = []
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(config_dir="/unused"),
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [entry],
            async_update_entry=lambda e, **kw: e.data.update(kw.get("data", {})),
        ),
    )
    async def _reload(hass, hub_id):
        reloaded.append(hub_id)
    async def _remove(hass, hub_id):
        removed.append(hub_id)
        return []
    monkeypatch.setattr(cf, "async_reload_dependents", _reload)
    monkeypatch.setattr(cf, "async_remove_dependents", _remove)
    return flow, entry, reloaded


async def test_reconfigure_remove_key_scheduled_only(monkeypatch):
    flow, entry, reloaded = _source_options_flow("secret", monkeypatch)
    created = {}
    monkeypatch.setattr(flow, "async_create_entry", lambda **kw: created.update(kw) or {"type": "create_entry"})
    await flow.async_step_source_reconfigure(
        {CONF_ENABLE_REALTIME: SOURCE_CHOICE_SCHEDULED}
    )
    assert entry.data[CONF_API_KEY] == ""
    assert reloaded == ["hub1"]


async def test_reconfigure_add_key(monkeypatch):
    flow, entry, reloaded = _source_options_flow(None, monkeypatch)

    async def fake_validate(hass, key):
        return {}

    monkeypatch.setattr(cf, "_validate_api_key", fake_validate)
    created = {}
    monkeypatch.setattr(flow, "async_create_entry", lambda **kw: created.update(kw) or {"type": "create_entry"})
    await flow.async_step_source_reconfigure(
        {CONF_ENABLE_REALTIME: SOURCE_CHOICE_REALTIME, CONF_API_KEY: "new-key"}
    )
    assert entry.data[CONF_API_KEY] == "new-key"
    assert reloaded == ["hub1"]


async def test_reconfigure_realtime_requires_key(monkeypatch):
    flow, entry, reloaded = _source_options_flow(None, monkeypatch)
    shown = {}
    flow.async_show_form = lambda **kw: shown.update(kw) or {"type": "form"}
    result = await flow.async_step_source_reconfigure(
        {CONF_ENABLE_REALTIME: SOURCE_CHOICE_REALTIME, CONF_API_KEY: "  "}
    )
    assert result["type"] == "form"
    assert shown["errors"] == {CONF_API_KEY: "api_key_required"}
    assert entry.data.get(CONF_API_KEY, "") == ""  # unchanged (still scheduled-only)


# ---------------------------------------------------------------------------
# Scheduled-only gating: coordinators must not build a client without a key
# ---------------------------------------------------------------------------


class TestScheduledOnlyClientGating:
    def test_effective_realtime_off_for_keyless_watch(self):
        hass = _hass([_entry("hub1", _source_data(None))])
        entry = SimpleNamespace(
            entry_id="w1", options={}, data={CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH}
        )
        assert src.effective_realtime_enabled(hass, entry) is False
