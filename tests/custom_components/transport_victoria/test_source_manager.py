"""T18.1 F1 — ScheduleManager owns the source refresh/retry/readiness (light env).

Verifies the manager (not an untracked driver) drives the hub's import-all
refresh, writes readiness ON the guarded refresh completion, schedules a retry
on failure, and emits the DOWNLOADING/IMPORTING phases at the real work
boundaries.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.transport_victoria as tv
from custom_components.transport_victoria import ScheduleManager
from custom_components.transport_victoria.const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_SOURCE_ENTRY_ID,
    CONF_STOP_ID,
    DOMAIN,
    ENTRY_TYPE_SOURCE,
    ENTRY_TYPE_WATCH,
    READINESS_CONFIGURED,
    READINESS_DOWNLOADING,
    READINESS_IMPORTING,
    READINESS_READY,
    READINESS_SCHEDULE_FAILED,
)
from custom_components.transport_victoria.source import all_mode_bundles


class _ManagerHass:
    """A hass double sufficient for the manager's refresh/readiness paths."""

    def __init__(self) -> None:
        self.data: dict = {DOMAIN: {}}
        self.is_stopping = False

    def async_create_task(self, coro):
        return asyncio.create_task(coro)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _manager(hass: _ManagerHass) -> ScheduleManager:
    return ScheduleManager(hass, Path("gtfs.db"), Path("gtfs.zip"), retry_seconds=0.01)


async def test_refresh_source_failure_schedules_retry_and_recovers(monkeypatch):
    """Failure → SCHEDULE_FAILED + manager retry → retry succeeds → READY (F1)."""
    hass = _ManagerHass()
    hass.data[DOMAIN]["hub1"] = {"readiness": READINESS_CONFIGURED}
    manager = _manager(hass)
    manager.register_source("hub1")

    results = iter([False, True])

    async def fake_refresh(hass, bundles, *, skip_download_if_exists=False, on_phase=None):
        return next(results)

    monkeypatch.setattr(tv, "async_trigger_gtfs_refresh", fake_refresh)

    assert await manager.refresh_source() is False
    assert hass.data[DOMAIN]["hub1"]["readiness"] == READINESS_SCHEDULE_FAILED
    # A manager retry was scheduled (constant backoff, deduplicated).
    assert any(not t.done() for t in manager._retry_tasks.values())

    # The retry succeeds and clears SCHEDULE_FAILED → READY (no reload needed).
    await asyncio.sleep(0.05)
    assert hass.data[DOMAIN]["hub1"]["readiness"] == READINESS_READY


async def test_refresh_source_success_writes_ready(monkeypatch):
    hass = _ManagerHass()
    hass.data[DOMAIN]["hub1"] = {"readiness": READINESS_CONFIGURED}
    manager = _manager(hass)
    manager.register_source("hub1")

    async def fake_refresh(hass, bundles, *, skip_download_if_exists=False, on_phase=None):
        return True

    monkeypatch.setattr(tv, "async_trigger_gtfs_refresh", fake_refresh)
    assert await manager.refresh_source() is True
    assert hass.data[DOMAIN]["hub1"]["readiness"] == READINESS_READY
    assert manager._retry_tasks == {}


async def test_refresh_source_no_source_is_noop():
    hass = _ManagerHass()
    manager = _manager(hass)
    assert await manager.refresh_source() is True


async def test_refresh_impl_emits_phases_in_order(monkeypatch, tmp_path):
    """DOWNLOADING fires during download; IMPORTING only when import begins (F1c)."""
    phases: list[str] = []
    download_ran = asyncio.Event()
    import_ran = asyncio.Event()

    monkeypatch.setattr(tv, "refresh_due", lambda *a, **k: True)
    monkeypatch.setattr(tv, "get_stored_zip_meta", lambda *a, **k: {})
    monkeypatch.setattr(tv, "resolve_gtfs_schedule_url", _noop_resolve)
    monkeypatch.setattr(tv, "check_for_remote_changes", _noop_changed)

    async def fake_download(hass, url, zip_path, *, skip_if_exists=False):
        download_ran.set()
        return (b"", "sha256", "etag", 100)

    monkeypatch.setattr(tv, "download_zip", fake_download)

    def fake_import(*a, **k):
        import_ran.set()

    monkeypatch.setattr(tv, "import_bundles_to_generation", fake_import)
    monkeypatch.setattr(tv, "schedule_state", lambda *a, **k: "ready")

    def on_phase(phase: str) -> None:
        phases.append(phase)

    ok = await tv._refresh_impl(
        _ManagerHass(), Path("gtfs.db"), Path("gtfs.zip"), ["2"], False, on_phase=on_phase
    )
    assert ok is True
    # DOWNLOADING was emitted before the download, IMPORTING before the import.
    assert phases == [READINESS_DOWNLOADING, READINESS_IMPORTING]
    assert download_ran.is_set()
    assert import_ran.is_set()


async def test_set_readiness_guards_absent_hub():
    """A late write for a removed/unloaded hub must not recreate its state (F1b)."""
    hass = _ManagerHass()
    # No runtime dict for "hub1" → the write is a no-op.
    tv._set_readiness(hass, "hub1", READINESS_READY)
    assert "hub1" not in hass.data[DOMAIN]


async def _noop_resolve(hass):
    return "http://example/gtfs.zip"


async def _noop_changed(hass, url, etag, size):
    return True


async def test_periodic_refresh_failure_writes_schedule_failed_then_recovers(monkeypatch):
    """F2 — the daily all-bundle refresh (not the hub driver) updates readiness."""
    hass = _ManagerHass()
    hass.data[DOMAIN]["hub1"] = {"readiness": READINESS_READY}
    manager = ScheduleManager(hass, Path("gtfs.db"), Path("gtfs.zip"), retry_seconds=3600)
    manager.register_source("hub1")

    outcome = {"ok": False}

    async def fake_refresh(hass, bundles, *, skip_download_if_exists=False, on_phase=None):
        return outcome["ok"]

    monkeypatch.setattr(tv, "async_trigger_gtfs_refresh", fake_refresh)

    assert await manager.refresh(all_mode_bundles()) is False
    assert hass.data[DOMAIN]["hub1"]["readiness"] == READINESS_SCHEDULE_FAILED

    outcome["ok"] = True
    assert await manager.refresh(all_mode_bundles()) is True
    assert hass.data[DOMAIN]["hub1"]["readiness"] == READINESS_READY


async def test_refresh_source_unexpected_exception_schedules_retry(monkeypatch):
    """F2 — an unexpected exception → SCHEDULE_FAILED + retry, no task crash."""
    hass = _ManagerHass()
    hass.data[DOMAIN]["hub1"] = {"readiness": READINESS_CONFIGURED}
    manager = _manager(hass)
    manager.register_source("hub1")

    async def boom(hass, bundles, *, skip_download_if_exists=False, on_phase=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(tv, "async_trigger_gtfs_refresh", boom)

    assert await manager.refresh_source() is False
    assert hass.data[DOMAIN]["hub1"]["readiness"] == READINESS_SCHEDULE_FAILED
    assert any(not t.done() for t in manager._retry_tasks.values())


def test_set_readiness_guards_replaced_hub():
    """F1(b) — a late write for a removed hub must not touch the replacement."""
    hass = _ManagerHass()
    hass.data[DOMAIN]["new_hub"] = {"readiness": READINESS_READY}
    tv._set_readiness(hass, "old_hub", READINESS_SCHEDULE_FAILED)
    assert "old_hub" not in hass.data[DOMAIN]
    assert hass.data[DOMAIN]["new_hub"]["readiness"] == READINESS_READY


async def test_retry_chain_survives_repeated_exception_and_recovers(monkeypatch):
    """F1 — initial + first retry both raise → a third retry is scheduled → READY.

    ``refresh()`` normalizes exceptions into failure outcomes, so ``_retry_after``
    (which calls ``refresh()``) survives a repeated crash and keeps scheduling the
    next retry instead of killing the task chain.
    """
    hass = _ManagerHass()
    hass.data[DOMAIN]["hub1"] = {"readiness": READINESS_CONFIGURED}
    manager = ScheduleManager(hass, Path("gtfs.db"), Path("gtfs.zip"), retry_seconds=0.01)
    manager.register_source("hub1")

    calls = {"n": 0}

    async def flaky(hass, bundles, *, skip_download_if_exists=False, on_phase=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(tv, "async_trigger_gtfs_refresh", flaky)

    def _pending():
        return [t for t in manager._retry_tasks.values() if not t.done()]

    # Initial attempt raises → SCHEDULE_FAILED + one retry scheduled.
    assert await manager.refresh_source() is False
    assert calls["n"] == 1
    assert hass.data[DOMAIN]["hub1"]["readiness"] == READINESS_SCHEDULE_FAILED
    assert len(_pending()) == 1

    # First retry raises again → a further retry is scheduled (chain survives).
    await _pending()[0]
    assert calls["n"] == 2
    assert hass.data[DOMAIN]["hub1"]["readiness"] == READINESS_SCHEDULE_FAILED
    assert len(_pending()) == 1  # third attempt queued

    # Next retry succeeds → READY recovery.
    await _pending()[0]
    assert calls["n"] == 3
    assert hass.data[DOMAIN]["hub1"]["readiness"] == READINESS_READY


async def test_refresh_preserves_cancellation(monkeypatch):
    """F1 — asyncio.CancelledError must propagate, not be converted to failure."""
    hass = _ManagerHass()
    manager = _manager(hass)

    async def cancelled(hass, bundles, *, skip_download_if_exists=False, on_phase=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(tv, "async_trigger_gtfs_refresh", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await manager.refresh(["2"])


class _AssocConfigEntries:
    def __init__(self, entries):
        self._entries = entries

    def async_entries(self, domain):
        return self._entries

    def async_get_entry(self, sid):
        return next((e for e in self._entries if e.entry_id == sid), None)

    def async_update_entry(self, entry, **kw):
        if "data" in kw:
            entry.data = dict(kw["data"])


class _AssocHass:
    def __init__(self, entries):
        self.data = {DOMAIN: {}}
        self.config_entries = _AssocConfigEntries(entries)


def _assoc_entry(entry_id, data):
    return SimpleNamespace(entry_id=entry_id, data=dict(data), options={}, title=entry_id)


async def test_ensure_feature_associated_skips_without_hub():
    """Defensive stamp must not strip a feature's key before a hub carries one."""
    feature = _assoc_entry("w1", {CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_API_KEY: "k"})
    hass = _AssocHass([feature])
    result = await tv._ensure_feature_associated(hass, feature)
    assert result is None
    assert CONF_API_KEY in feature.data  # key preserved — no hub to carry it


async def test_ensure_feature_associated_stamps_and_strips():
    hub = _assoc_entry("hub1", {CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE, CONF_API_KEY: "k"})
    feature = _assoc_entry(
        "w1",
        {CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_MODE: "metro", CONF_STOP_ID: "14308", CONF_API_KEY: "k"},
    )
    hass = _AssocHass([hub, feature])
    result = await tv._ensure_feature_associated(hass, feature)
    assert result is not None
    assert feature.data[CONF_SOURCE_ENTRY_ID] == "hub1"
    assert CONF_API_KEY not in feature.data


async def test_migration_task_swallows_exceptions(monkeypatch):
    """The fire-and-forget migration wrapper never raises out of the task."""
    hass = _ManagerHass()

    async def boom(hass):
        raise RuntimeError("boom")

    monkeypatch.setattr(tv, "async_migrate_entries_to_source", boom)
    await tv._migration_task(hass)  # must not raise


async def test_scheduled_only_hub_preserves_feature_key_until_it_gains_one():
    """A scheduled-only hub must never strip a feature's only credential."""
    hub = _assoc_entry("hub1", {CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE})  # keyless
    feature = _assoc_entry(
        "w1",
        {CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_MODE: "metro", CONF_STOP_ID: "14308", CONF_API_KEY: "legacy-key"},
    )
    hass = _AssocHass([hub, feature])

    # Association is stamped, but the feature's key is PRESERVED (no recovery source yet).
    result = await tv._ensure_feature_associated(hass, feature)
    assert result is not None
    assert feature.data[CONF_SOURCE_ENTRY_ID] == "hub1"
    assert feature.data.get(CONF_API_KEY) == "legacy-key"

    # Hub reconfigure adds a credential → the next reconcile strips the copy.
    hub.data[CONF_API_KEY] = "new-key"
    await tv._ensure_feature_associated(hass, feature)
    assert CONF_API_KEY not in feature.data
    assert feature.data[CONF_SOURCE_ENTRY_ID] == "hub1"
