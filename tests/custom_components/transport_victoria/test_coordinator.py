"""Unit tests for the coordinator's schedule-health guard (stale → unavailable).

Covers the failure→recovery flag behaviour (log once, reset only on recovery)
and the service-day boundary (a post-midnight poll must not treat a calendar
ending "yesterday" as expired).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.transport_victoria.const import CONF_API_KEY, CONF_MODE, CONF_STOP_ID
from custom_components.transport_victoria.coordinator import (
    PTVDataUpdateCoordinator,
    UpdateFailed,
)
import custom_components.transport_victoria.coordinator as coordinator_module


class FakeHass:
    def __init__(self) -> None:
        self.data: dict = {}

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)  # run inline (no executor needed for these tests)

    def async_create_task(self, coro):
        return asyncio.create_task(coro)

    @property
    def loop(self):
        return asyncio.get_running_loop()


def _entry(mode: str = "metro"):
    return SimpleNamespace(
        entry_id="test-entry",
        data={CONF_API_KEY: "k", CONF_MODE: mode, CONF_STOP_ID: "S1"},
        options={},
    )


def _coordinator(mode: str = "metro", db_path: Path | None = None) -> PTVDataUpdateCoordinator:
    hass = FakeHass()
    entry = _entry(mode)
    c = PTVDataUpdateCoordinator(hass, entry, entry_index=0, db_path=db_path or Path("x.db"))
    c.hass = hass  # the stub DataUpdateCoordinator base does not set .hass
    return c


# ---------------------------------------------------------------------------
# Failure → recovery flag behaviour
# ---------------------------------------------------------------------------

class TestApplyScheduleState:
    def test_raises_and_sets_flag_when_not_ready(self):
        c = _coordinator()
        with pytest.raises(UpdateFailed):
            c._apply_schedule_state("expired")
        assert c._expired_logged is True

    def test_reset_on_recovery_only(self):
        c = _coordinator()
        # First failure sets the flag.
        with pytest.raises(UpdateFailed):
            c._apply_schedule_state("expired")
        assert c._expired_logged is True
        # Recovery resets it.
        c._apply_schedule_state("ready")
        assert c._expired_logged is False
        # A subsequent failure logs again (flag toggles once more).
        with pytest.raises(UpdateFailed):
            c._apply_schedule_state("expired")
        assert c._expired_logged is True

    def test_stale_and_missing_also_unavailable(self):
        c = _coordinator()
        for state in ("stale", "missing"):
            with pytest.raises(UpdateFailed):
                c._apply_schedule_state(state)
        assert c._expired_logged is True

    def test_non_flap_warning_count(self, caplog):
        # Repeated "expired" polls log the warning once; recovery then re-expiry
        # logs it again (no flap, no spam).
        c = _coordinator()
        with caplog.at_level(logging.WARNING):
            with pytest.raises(UpdateFailed):
                c._apply_schedule_state("expired")
            with pytest.raises(UpdateFailed):
                c._apply_schedule_state("expired")  # no new warning
            c._apply_schedule_state("ready")  # recovery
            with pytest.raises(UpdateFailed):
                c._apply_schedule_state("expired")  # logs again
        warnings = [r for r in caplog.records if "unavailable" in r.message]
        assert len(warnings) == 2  # once per expiry episode, not per poll


# ---------------------------------------------------------------------------
# Service-day boundary
# ---------------------------------------------------------------------------

class TestServiceDayBoundary:
    async def test_fetch_state_uses_service_day(self, monkeypatch):
        # 00:30 on June 2 → the GTFS service day is June 1 (rollback).
        fixed_now = datetime(2026, 6, 2, 0, 30)
        captured: dict = {}

        def fake_schedule_state(db_path, bundle_folders, today):
            captured["today"] = today
            return "ready"

        monkeypatch.setattr(coordinator_module, "schedule_state", fake_schedule_state)
        monkeypatch.setattr(
            coordinator_module, "dt_util", SimpleNamespace(now=lambda: fixed_now)
        )

        c = _coordinator()
        state = await c._fetch_schedule_state()
        assert state == "ready"
        # The service-local date passed is June 1 (not June 2).
        assert captured["today"] == date(2026, 6, 1)

    async def test_utc_to_melbourne_cutover(self, monkeypatch):
        # An explicit UTC timestamp just after Melbourne midnight (14:30 UTC ==
        # 00:30 AEST) must resolve to the *previous* Melbourne service day.
        utc_now = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
        melbourne_now = utc_now.astimezone(ZoneInfo("Australia/Melbourne")).replace(tzinfo=None)

        captured: dict = {}

        def fake_schedule_state(db_path, bundle_folders, today):
            captured["today"] = today
            return "ready"

        monkeypatch.setattr(coordinator_module, "schedule_state", fake_schedule_state)
        monkeypatch.setattr(
            coordinator_module, "dt_util", SimpleNamespace(now=lambda: melbourne_now)
        )

        c = _coordinator()
        await c._fetch_schedule_state()
        assert captured["today"] == date(2026, 6, 1)  # service day rolls back
