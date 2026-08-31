"""Coordinator gating of service-alerts fetches per mode.

The open data portal only publishes a GTFS Realtime Service Alerts feed for
Metro Train and Yarra Trams (https://opendata.transport.vic.gov.au/dataset/gtfs-realtime).
Fetching it for other modes fails on every poll cycle (403 for bus, 404 for
regional-bus), so the coordinator must skip the request entirely for modes
without a published feed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from custom_components.transport_victoria.const import (
    CONF_API_KEY,
    CONF_MODE,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_WATCH_NAME,
    DOMAIN,
    FEED_SERVICE_ALERTS,
    FEED_TRIP_UPDATES,
    MODE_BUS,
    MODE_METRO,
    MODE_REGIONAL_BUS,
    MODE_TRAM,
    MODE_VLINE,
    MODES,
    SERVICE_ALERTS_MODES,
)
from custom_components.transport_victoria.coordinator import PTVDataUpdateCoordinator


class _EmptyFeed:
    """Stands in for a decoded FeedMessage with no entities."""

    entity: list = []


class _RecordingClient:
    """Records every fetch_feed call instead of hitting the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def fetch_feed(self, mode: str, feed_type: str) -> object:
        self.calls.append((mode, feed_type))
        return _EmptyFeed()


class _NoopRateLimiter:
    async def acquire(self) -> None:
        return None


class _FakeHass:
    def __init__(self) -> None:
        self.data = {DOMAIN: {"rate_limiter": _NoopRateLimiter()}}

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _make_coordinator(
    mode: str, gtfs_db: Path, client: _RecordingClient
) -> PTVDataUpdateCoordinator:
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={
            CONF_API_KEY: "key",
            CONF_MODE: mode,
            CONF_STOP_ID: "PLAT1",
            CONF_STOP_NAME: "Coburg Station",
            CONF_WATCH_NAME: "Test watch",
        },
        options={},
    )
    hass = _FakeHass()
    coord = PTVDataUpdateCoordinator(hass, entry, 0, gtfs_db)
    # The stubbed DataUpdateCoordinator base does not store hass
    coord.hass = hass
    coord._client = client
    return coord


async def test_alerts_not_fetched_for_bus(gtfs_db: Path) -> None:
    client = _RecordingClient()
    coord = _make_coordinator(MODE_BUS, gtfs_db, client)

    data = await coord._async_update_data()

    assert (MODE_BUS, FEED_TRIP_UPDATES) in client.calls
    assert (MODE_BUS, FEED_SERVICE_ALERTS) not in client.calls
    assert data.alerts == []


async def test_alerts_fetched_for_metro(gtfs_db: Path) -> None:
    client = _RecordingClient()
    coord = _make_coordinator(MODE_METRO, gtfs_db, client)

    await coord._async_update_data()

    assert (MODE_METRO, FEED_TRIP_UPDATES) in client.calls
    assert (MODE_METRO, FEED_SERVICE_ALERTS) in client.calls


def test_service_alerts_modes_membership() -> None:
    assert SERVICE_ALERTS_MODES <= set(MODES)
    assert MODE_METRO in SERVICE_ALERTS_MODES
    assert MODE_TRAM in SERVICE_ALERTS_MODES
    assert MODE_BUS not in SERVICE_ALERTS_MODES
    assert MODE_REGIONAL_BUS not in SERVICE_ALERTS_MODES
    assert MODE_VLINE not in SERVICE_ALERTS_MODES
