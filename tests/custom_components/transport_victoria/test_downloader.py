"""Tests for the GTFS schedule downloader (CKAN URL resolution + fallback).

The portal re-publishes the GTFS Schedule dataset under a new resource UUID, so
a hardcoded URL eventually 404s (the v0.2.0 first-run regression). These tests
pin the behaviour that keeps fresh installs working:

* ``resolve_gtfs_schedule_url`` asks CKAN ``package_show`` and picks the ZIP
  resource;
* every lookup failure path (HTTP error, malformed payload, no ZIP resource)
  falls back to the last known-good constant instead of raising;
* the downloaded stream logs periodic ``MiB received`` progress lines.

All HTTP is faked — no network access in the light env.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.transport_victoria.const import (
    GTFS_SCHEDULE_CKAN_URL,
    GTFS_SCHEDULE_DATASET_ID,
    GTFS_SCHEDULE_URL,
)
from custom_components.transport_victoria.gtfs import downloader

_LIVE_RESOURCE_URL = (
    "https://opendata.transport.vic.gov.au/dataset/"
    "3f4e292e-7f8a-4ffe-831f-1953be0fe448/resource/"
    "8af7737c-5c9e-4022-aa98-911aa04370a7/download/gtfs.zip"
)


class FakeHass:
    """Minimal hass double — runs executor jobs synchronously."""

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class FakeResponse:
    """Async context manager mimicking an aiohttp ClientResponse."""

    def __init__(self, *, status=200, payload=None, chunks=(), headers=None):
        self.status = status
        self._payload = payload
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.content = SimpleNamespace(iter_chunked=self._iter_chunked)

    async def _iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    """Fake aiohttp.ClientSession returning one canned response or raising."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._error is not None:
            raise self._error
        return self._response


def _use_session(monkeypatch, session):
    monkeypatch.setattr(
        downloader, "async_get_clientsession", lambda hass: session
    )


def _package_payload(*resources):
    return {"success": True, "result": {"resources": list(resources)}}


# ---------------------------------------------------------------------------
# Fallback constant
# ---------------------------------------------------------------------------


def test_fallback_url_points_at_live_verified_resource():
    """Guard against re-introducing the dead resource id from v0.2.0."""
    assert "fb152201-859f-4882-9206-b768060b50ad" not in GTFS_SCHEDULE_URL
    assert "8af7737c-5c9e-4022-aa98-911aa04370a7" in GTFS_SCHEDULE_URL
    assert GTFS_SCHEDULE_URL.endswith("/download/gtfs.zip")


# ---------------------------------------------------------------------------
# CKAN dynamic resolution
# ---------------------------------------------------------------------------


async def test_resolve_picks_zip_resource(monkeypatch):
    session = FakeSession(
        FakeResponse(
            payload=_package_payload(
                {"id": "csv-1", "format": "CSV", "url": "https://x/other.csv"},
                {"id": "zip-1", "format": "ZIP", "url": _LIVE_RESOURCE_URL},
            )
        )
    )
    _use_session(monkeypatch, session)

    url = await downloader.resolve_gtfs_schedule_url(FakeHass())

    assert url == _LIVE_RESOURCE_URL
    assert session.calls == [
        (
            GTFS_SCHEDULE_CKAN_URL,
            {"params": {"id": GTFS_SCHEDULE_DATASET_ID}, "timeout": 10},
        )
    ]


async def test_resolve_picks_zip_by_url_when_format_missing(monkeypatch):
    url_by_path = "https://x/resource/abc/download/gtfs"
    session = FakeSession(
        FakeResponse(
            payload=_package_payload(
                {"id": "csv-1", "format": "CSV", "url": "https://x/other.csv"},
                {"id": "zip-1", "format": None, "url": url_by_path},
            )
        )
    )
    _use_session(monkeypatch, session)

    assert await downloader.resolve_gtfs_schedule_url(FakeHass()) == url_by_path


async def test_resolve_prefers_zip_format_over_heuristic(monkeypatch):
    heuristic = "https://x/resource/abc/download/gtfs"
    typed = "https://x/resource/def/download/gtfs.zip"
    session = FakeSession(
        FakeResponse(
            payload=_package_payload(
                {"id": "heuristic", "format": "GTFS", "url": heuristic},
                {"id": "typed", "format": "ZIP", "url": typed},
            )
        )
    )
    _use_session(monkeypatch, session)

    assert await downloader.resolve_gtfs_schedule_url(FakeHass()) == typed


async def test_resolve_falls_back_on_http_error(monkeypatch):
    _use_session(monkeypatch, FakeSession(FakeResponse(status=404)))

    assert await downloader.resolve_gtfs_schedule_url(FakeHass()) == GTFS_SCHEDULE_URL


async def test_resolve_falls_back_on_network_error(monkeypatch):
    _use_session(monkeypatch, FakeSession(error=RuntimeError("boom")))

    assert await downloader.resolve_gtfs_schedule_url(FakeHass()) == GTFS_SCHEDULE_URL


async def test_resolve_falls_back_on_malformed_payload(monkeypatch):
    _use_session(monkeypatch, FakeSession(FakeResponse(payload={"success": False})))

    assert await downloader.resolve_gtfs_schedule_url(FakeHass()) == GTFS_SCHEDULE_URL


async def test_resolve_falls_back_when_no_zip_resource(monkeypatch):
    session = FakeSession(
        FakeResponse(
            payload=_package_payload(
                {"id": "csv-1", "format": "CSV", "url": "https://x/other.csv"},
            )
        )
    )
    _use_session(monkeypatch, session)

    assert await downloader.resolve_gtfs_schedule_url(FakeHass()) == GTFS_SCHEDULE_URL


async def test_resolve_returns_custom_fallback(monkeypatch):
    _use_session(monkeypatch, FakeSession(error=RuntimeError("boom")))

    assert (
        await downloader.resolve_gtfs_schedule_url(
            FakeHass(), fallback="https://fallback.invalid/gtfs.zip"
        )
        == "https://fallback.invalid/gtfs.zip"
    )


# ---------------------------------------------------------------------------
# Streaming download: bytes, digest, progress logging
# ---------------------------------------------------------------------------


async def test_download_zip_streams_writes_and_logs_progress(
    tmp_path: Path, monkeypatch, caplog
):
    # Log every 2 MiB so the test needs only a few MiB of data.
    monkeypatch.setattr(downloader, "_PROGRESS_LOG_BYTES", 2 * 1024 * 1024)
    chunks = [b"a" * (1024 * 1024), b"b" * (1024 * 1024), b"c" * 1024]
    session = FakeSession(
        FakeResponse(chunks=chunks, headers={"Content-Length": str(3 * 1024 * 1024)})
    )
    _use_session(monkeypatch, session)

    dest = tmp_path / "data" / "gtfs.zip"
    with caplog.at_level(
        logging.INFO, logger="custom_components.transport_victoria.gtfs.downloader"
    ):
        changed, sha256, etag, content_length = await downloader.download_zip(
            FakeHass(), _LIVE_RESOURCE_URL, dest
        )

    assert changed is True
    assert dest.read_bytes() == b"".join(chunks)
    assert sha256 == hashlib.sha256(b"".join(chunks)).hexdigest()
    assert content_length == str(3 * 1024 * 1024)
    assert session.calls[0][0] == _LIVE_RESOURCE_URL
    assert "GTFS schedule download: 2 MiB received" in caplog.text


async def test_download_zip_raises_on_http_error(tmp_path: Path, monkeypatch):
    session = FakeSession(FakeResponse(status=404))
    _use_session(monkeypatch, session)

    with pytest.raises(RuntimeError):
        await downloader.download_zip(FakeHass(), _LIVE_RESOURCE_URL, tmp_path / "gtfs.zip")

    assert not (tmp_path / "gtfs.zip").exists()
