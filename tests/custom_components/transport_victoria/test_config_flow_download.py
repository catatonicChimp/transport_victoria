"""Config-flow progress tests for the GTFS schedule download/import steps.

Before this hotfix the flow advanced from the download spinner to the import
spinner unconditionally and then polled forever when the download had failed
(the dead-URL 404 in v0.2.0 never surfaced an error). These tests pin the
honest behaviour:

* while the refresh task runs → ``async_show_progress`` is returned;
* when it finishes successfully → ``async_show_progress_done("importing")``;
* when it fails → ``schedule_failed`` (retry/cancel menu) instead of a
  spinner that never ends;
* the post-download poll stops as soon as the refresh task reports failure.

The light env stubs HA's ConfigFlow, so the flow's result helpers are captured
by monkeypatching the instance methods — the same style as the board-flow tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.transport_victoria import config_flow as cf
from custom_components.transport_victoria.const import CONF_MODE, MODE_METRO


class FakeHass:
    """Minimal hass double backed by the real event loop."""

    def __init__(self, config_dir: Path) -> None:
        self.config = SimpleNamespace(config_dir=str(config_dir))
        self.is_stopping = False

    def async_create_task(self, coro):
        return asyncio.create_task(coro)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _flow(tmp_path: Path) -> cf.PTVConfigFlow:
    flow = cf.PTVConfigFlow()
    flow.hass = FakeHass(tmp_path)
    flow._config = {CONF_MODE: MODE_METRO}
    return flow


def _capture(flow: cf.PTVConfigFlow) -> dict:
    """Capture the flow's result helpers instead of returning HA result types."""
    captured: dict = {}

    def show_progress(**kwargs):
        captured["progress"] = kwargs
        return {"type": "progress", **kwargs}

    def show_progress_done(**kwargs):
        captured["done"] = kwargs
        return {"type": "progress_done", **kwargs}

    flow.async_show_progress = show_progress
    flow.async_show_progress_done = show_progress_done
    return captured


@pytest.fixture(autouse=True)
def _not_imported(monkeypatch):
    """Never touch a real DB from the progress steps."""
    monkeypatch.setattr(cf, "is_mode_ready", lambda *a, **k: False)


async def test_downloading_shows_progress_then_moves_to_importing(tmp_path, monkeypatch):
    gate = asyncio.Event()

    async def refresh(hass, bundles, **kwargs):
        await gate.wait()
        return True

    monkeypatch.setattr(cf, "async_trigger_gtfs_refresh", refresh)
    flow = _flow(tmp_path)
    captured = _capture(flow)

    result = await flow.async_step_downloading()

    assert result["type"] == "progress"
    assert captured["progress"]["step_id"] == "downloading"
    assert captured["progress"]["progress_action"] == "downloading_gtfs"
    assert isinstance(captured["progress"]["progress_task"], asyncio.Task)

    gate.set()
    await flow._zip_wait_task

    result = await flow.async_step_downloading()
    assert result["type"] == "progress_done"
    assert captured["done"]["next_step_id"] == "importing"


async def test_downloading_failure_offers_retry_instead_of_spinning(tmp_path, monkeypatch):
    async def refresh(hass, bundles, **kwargs):
        return False  # e.g. the 404 the v0.2.0 flow silently ignored

    monkeypatch.setattr(cf, "async_trigger_gtfs_refresh", refresh)
    flow = _flow(tmp_path)
    captured = _capture(flow)

    result = await flow.async_step_downloading()
    assert result["type"] == "progress"

    await flow._zip_wait_task

    result = await flow.async_step_downloading()
    assert result["type"] == "progress_done"
    assert captured["done"]["next_step_id"] == "schedule_failed"


async def test_downloading_exception_offers_retry(tmp_path, monkeypatch):
    async def refresh(hass, bundles, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cf, "async_trigger_gtfs_refresh", refresh)
    flow = _flow(tmp_path)
    captured = _capture(flow)

    await flow.async_step_downloading()
    # Retrieve the exception so asyncio doesn't warn about it being unhandled.
    with pytest.raises(RuntimeError):
        await flow._zip_wait_task

    result = await flow.async_step_downloading()
    assert result["type"] == "progress_done"
    assert captured["done"]["next_step_id"] == "schedule_failed"


async def test_retry_download_resets_wait_tasks(tmp_path, monkeypatch):
    flow = _flow(tmp_path)

    async def fake_step():
        return {"type": "progress"}

    monkeypatch.setattr(flow, "async_step_downloading", fake_step)
    flow._zip_wait_task = object()
    flow._db_wait_task = object()

    result = await flow.async_step_retry_download()

    assert result == {"type": "progress"}
    assert flow._zip_wait_task is None
    assert flow._db_wait_task is None


async def test_schedule_failed_menu_offers_retry_and_cancel(tmp_path):
    flow = _flow(tmp_path)
    captured: dict = {}
    flow.async_show_menu = lambda **kwargs: captured.update(kwargs) or {"type": "menu"}

    result = await flow.async_step_schedule_failed()

    assert result == {"type": "menu"}
    assert captured["step_id"] == "schedule_failed"
    assert captured["menu_options"] == ["retry_download", "abort_setup"]


async def test_abort_setup_aborts_with_reason(tmp_path):
    flow = _flow(tmp_path)
    captured: dict = {}
    flow.async_abort = lambda **kwargs: captured.update(kwargs) or {"type": "abort"}

    result = await flow.async_step_abort_setup()

    assert result == {"type": "abort"}
    assert captured == {"reason": "schedule_unavailable"}


async def test_poll_stops_when_refresh_task_failed(tmp_path, monkeypatch):
    flow = _flow(tmp_path)

    async def failed():
        return False

    task = asyncio.create_task(failed())
    await task

    assert await flow._poll_until_mode_ready(tmp_path / "gtfs.db", ["2"], task) is False


async def test_poll_returns_true_when_mode_ready(tmp_path, monkeypatch):
    flow = _flow(tmp_path)
    monkeypatch.setattr(cf, "is_mode_ready", lambda *a, **k: True)

    async def ok():
        return True

    task = asyncio.create_task(ok())
    await task

    assert await flow._poll_until_mode_ready(tmp_path / "gtfs.db", ["2"], task) is True


def test_task_succeeded_handles_cancelled_task():
    async def never():
        return True

    async def _run():
        task = asyncio.create_task(never())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return cf._task_succeeded(task)

    assert asyncio.run(_run()) is False
