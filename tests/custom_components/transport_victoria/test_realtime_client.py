"""Unit tests for the realtime client's fetch/classify/cache behaviour (T13).

Covers per-feed fetch dedupe (TTL), 429 cooldown honouring Retry-After, 401/403
diagnosis, 404-on-known-missing "unavailable" suppression, cancellation safety
(no partial cache poison), and last-success/last-failure tracking — all against a
fake aiohttp session (no network), so they run in the light env.

The client's rate limiter is injected as a no-op here; per-feed/global token
buckets are unit-tested separately in ``test_rate_limiter.py``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest
from google.transit import gtfs_realtime_pb2 as rt

from custom_components.transport_victoria.realtime import client as client_module
from custom_components.transport_victoria.realtime.client import (
    FeedError,
    PTVRealtimeClient,
    feed_last_failure,
    feed_last_success,
    invalidate_cache,
    rt_url_mode,
)

from tests.realtime_fixtures import load_bytes


class NoopLimiter:
    async def acquire(self, key):
        return None


class FakeResponse:
    """Async context manager mimicking an aiohttp ClientResponse."""

    def __init__(self, status: int = 200, content: bytes = b"", headers=None):
        self.status = status
        self._content = content
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        return self._content


class FakeSession:
    """A fake aiohttp.ClientSession that records get() calls and returns canned
    responses (or raises)."""

    def __init__(self, responses=None, error=None):
        self.calls: list[tuple[str, dict]] = []
        self._responses = responses or {}
        self._error = error

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._error is not None:
            raise self._error
        return self._responses[url]

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _client(responses=None, error=None, **kw):
    return PTVRealtimeClient(
        "test-key",
        FakeSession(responses=responses, error=error),
        rate_limiter=NoopLimiter(),
        **kw,
    )


def _feed_bytes() -> bytes:
    return load_bytes("header_tick_0")


@pytest.fixture(autouse=True)
def _reset_client_state():
    invalidate_cache()
    yield
    invalidate_cache()


# ---------------------------------------------------------------------------
# Shared cache dedupe (entries sharing a feed → ONE fetch per cache interval)
# ---------------------------------------------------------------------------


class TestCacheDedupe:
    async def test_two_fetches_same_feed_share_one_http_request(self):
        session = FakeSession(
            responses={"https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1/metro/trip-updates": FakeResponse(200, _feed_bytes())}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        first = await client.fetch_feed("metro", "trip-updates")
        second = await client.fetch_feed("metro", "trip-updates")

        assert isinstance(first, rt.FeedMessage)
        assert second is first  # exact same decoded object (cache hit)
        assert session.call_count == 1  # ONE fetch per cache interval

    async def test_two_client_instances_share_the_module_cache(self):
        # A second client instance still hits the shared module cache.
        session = FakeSession(
            responses={"https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1/metro/trip-updates": FakeResponse(200, _feed_bytes())}
        )
        a = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())
        b = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        await a.fetch_feed("metro", "trip-updates")
        await b.fetch_feed("metro", "trip-updates")

        assert session.call_count == 1


# ---------------------------------------------------------------------------
# Failure classification (429 / 401 / 403 / 404-known-missing)
# ---------------------------------------------------------------------------


def _url(mode: str, feed_type: str) -> str:
    return (
        "https://api.opendata.transport.vic.gov.au/opendata/public-transport"
        f"/gtfs/realtime/v1/{mode}/{feed_type}"
    )


class TestRateLimit429:
    async def test_429_raises_and_sets_cooldown(self):
        session = FakeSession(
            responses={_url("metro", "trip-updates"): FakeResponse(429, headers={"Retry-After": "12"})}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        with pytest.raises(FeedError) as exc:
            await client.fetch_feed("metro", "trip-updates")
        assert exc.value.status == 429
        assert session.call_count == 1

    async def test_cooldown_suppresses_next_fetch_without_http(self):
        session = FakeSession(
            responses={_url("metro", "trip-updates"): FakeResponse(429, headers={"Retry-After": "60"})}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        with pytest.raises(FeedError):
            await client.fetch_feed("metro", "trip-updates")

        # Within the Retry-After clamp, the next call must NOT hit the network.
        with pytest.raises(FeedError) as exc:
            await client.fetch_feed("metro", "trip-updates")
        assert exc.value.status == 429
        assert session.call_count == 1  # still one HTTP request

    async def test_retry_after_is_clamped_to_max(self):
        # A server asking for a huge delay is clamped to the configured max.
        from custom_components.transport_victoria.const import (
            RATE_LIMIT_RETRY_AFTER_MAX_SECONDS,
        )

        session = FakeSession(
            responses={_url("metro", "trip-updates"): FakeResponse(429, headers={"Retry-After": "999999"})}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        before = datetime.now(tz=timezone.utc)
        with pytest.raises(FeedError):
            await client.fetch_feed("metro", "trip-updates")

        state = client_module._FEED_STATE[("metro", "trip-updates")]
        assert state.cooldown_until is not None
        remaining = (state.cooldown_until - before).total_seconds()
        assert 0 < remaining <= RATE_LIMIT_RETRY_AFTER_MAX_SECONDS + 1


class TestAuthRejected:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_rejected_raises_and_logs_hint(self, status, caplog):
        session = FakeSession(
            responses={_url("metro", "trip-updates"): FakeResponse(status)}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        with caplog.at_level(logging.WARNING):
            with pytest.raises(FeedError) as exc:
                await client.fetch_feed("metro", "trip-updates")
        assert exc.value.status == status
        assert any("auth rejected" in r.message for r in caplog.records)
        # A clear diagnosis hint about the key.
        assert any("subscription" in r.message for r in caplog.records)


class TestKnownMissing404:
    async def test_vline_service_alerts_is_unavailable_without_http(self):
        # V/Line has no service-alerts resource → the client short-circuits
        # (no HTTP) and reports "unavailable".
        session = FakeSession(responses={})
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        with pytest.raises(FeedError) as exc:
            await client.fetch_feed("vline", "service-alerts")
        assert exc.value.status == 404
        assert exc.value.unavailable is True
        assert session.call_count == 0  # never hit the network

    async def test_tram_vehicle_positions_is_unavailable(self):
        session = FakeSession(responses={})
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        with pytest.raises(FeedError) as exc:
            await client.fetch_feed("tram", "vehicle-positions")
        assert exc.value.unavailable is True
        assert session.call_count == 0


# ---------------------------------------------------------------------------
# Cancellation safety (cache stores only complete decoded feeds)
# ---------------------------------------------------------------------------


class TestCancellationSafety:
    async def test_cancelled_fetch_does_not_poison_cache(self):
        entered = asyncio.Event()

        class BlockingResponse:
            status = 200
            headers = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def read(self):
                entered.set()
                await asyncio.Event().wait()  # block until cancelled

        class BlockingSession:
            def get(self, url, **kw):
                return BlockingResponse()

        client = PTVRealtimeClient("k", BlockingSession(), rate_limiter=NoopLimiter())

        task = asyncio.ensure_future(client.fetch_feed("metro", "trip-updates"))
        await entered.wait()  # the fetch reached read() and is now blocked
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # No partial cache entry was committed.
        assert client_module._FEED_CACHE.get(("metro", "trip-updates")) is None

        # The next call (with a working session) succeeds.
        working = FakeSession(
            responses={_url("metro", "trip-updates"): FakeResponse(200, _feed_bytes())}
        )
        client._session = working
        feed = await client.fetch_feed("metro", "trip-updates")
        assert isinstance(feed, rt.FeedMessage)


class TestDecodeFailureNoPoison:
    async def test_decode_error_leaves_cache_empty(self, monkeypatch):
        session = FakeSession(
            responses={_url("metro", "trip-updates"): FakeResponse(200, _feed_bytes())}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        def boom(content):
            raise RuntimeError("decode boom")

        monkeypatch.setattr(client_module, "_decode_feed", boom)
        with pytest.raises(RuntimeError):
            await client.fetch_feed("metro", "trip-updates")

        assert client_module._FEED_CACHE.get(("metro", "trip-updates")) is None


# ---------------------------------------------------------------------------
# Last-success / last-failure tracking
# ---------------------------------------------------------------------------


class TestFeedStateTracking:
    async def test_last_success_recorded_on_success(self):
        session = FakeSession(
            responses={_url("metro", "trip-updates"): FakeResponse(200, _feed_bytes())}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        await client.fetch_feed("metro", "trip-updates")
        assert feed_last_success("metro", "trip-updates") is not None
        assert feed_last_failure("metro", "trip-updates") is None

    async def test_last_failure_recorded_on_failure(self):
        session = FakeSession(
            responses={_url("metro", "trip-updates"): FakeResponse(500)}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        with pytest.raises(FeedError):
            await client.fetch_feed("metro", "trip-updates")
        assert feed_last_failure("metro", "trip-updates") is not None
        assert feed_last_success("metro", "trip-updates") is None

    async def test_feed_error_carries_timestamps_for_board(self):
        # After a success then a failure, the FeedError carries last_success so
        # the board can surface data age after a failure.
        session = FakeSession(
            responses={_url("metro", "trip-updates"): FakeResponse(200, _feed_bytes())}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())
        await client.fetch_feed("metro", "trip-updates")

        session._responses[_url("metro", "trip-updates")] = FakeResponse(429, headers={"Retry-After": "1"})
        # Expire only the cache (not the state) so the failure path re-fetches
        # while the last-success timestamp survives.
        client_module._FEED_CACHE.pop(("metro", "trip-updates"), None)

        with pytest.raises(FeedError) as exc:
            await client.fetch_feed("metro", "trip-updates")
        assert exc.value.last_success is not None
        assert exc.value.last_failure is not None


# ---------------------------------------------------------------------------
# Regional-bus → bus URL-mode resolution
# ---------------------------------------------------------------------------


class TestRegionalBusResolution:
    def test_rt_url_mode_maps_regional_bus_to_bus(self):
        assert rt_url_mode("regional-bus") == "bus"
        assert rt_url_mode("bus") == "bus"
        assert rt_url_mode("metro") == "metro"
        assert rt_url_mode("vline") == "vline"

    async def test_regional_bus_fetch_hits_bus_url(self):
        session = FakeSession(
            responses={_url("bus", "trip-updates"): FakeResponse(200, _feed_bytes())}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        await client.fetch_feed("regional-bus", "trip-updates")

        # The HTTP request went to the "bus" URL, not "regional-bus".
        assert session.call_count == 1
        assert session.calls[0][0].endswith("/bus/trip-updates")

    async def test_bus_and_regional_bus_share_one_fetch(self):
        session = FakeSession(
            responses={_url("bus", "trip-updates"): FakeResponse(200, _feed_bytes())}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        await client.fetch_feed("bus", "trip-updates")
        await client.fetch_feed("regional-bus", "trip-updates")

        # ONE HTTP request despite two distinct config modes.
        assert session.call_count == 1
        # The cache key is the resolved ("bus", "trip-updates").
        assert ("bus", "trip-updates") in client_module._FEED_CACHE
        assert ("regional-bus", "trip-updates") not in client_module._FEED_CACHE


# ---------------------------------------------------------------------------
# 403 scope-unavailable (portal product not enabled for the key)
# ---------------------------------------------------------------------------


class TestScopeUnavailable:
    async def test_403_scope_unavailable_is_not_a_retryable_failure(self):
        session = FakeSession(
            responses={_url("bus", "service-alerts"): FakeResponse(403)}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        with pytest.raises(FeedError) as exc:
            await client.fetch_feed("bus", "service-alerts")
        assert exc.value.status == 403
        assert exc.value.unavailable is True

    async def test_403_hint_logged_once(self, caplog):
        session = FakeSession(
            responses={_url("bus", "service-alerts"): FakeResponse(403)}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        with caplog.at_level(logging.WARNING):
            with pytest.raises(FeedError):
                await client.fetch_feed("bus", "service-alerts")
            with pytest.raises(FeedError):
                await client.fetch_feed("bus", "service-alerts")

        hints = [r for r in caplog.records if "Products" in r.message]
        assert len(hints) == 1  # one-time diagnostic, not per-poll spam

    async def test_200_overrides_scope_unavailable(self):
        # A 403 first, then the product is "enabled" → the next 200 self-heals.
        session = FakeSession(
            responses={_url("bus", "service-alerts"): FakeResponse(403)}
        )
        client = PTVRealtimeClient("k", session, rate_limiter=NoopLimiter())

        with pytest.raises(FeedError):
            await client.fetch_feed("bus", "service-alerts")

        # Product enabled → 200 now.
        session._responses[_url("bus", "service-alerts")] = FakeResponse(
            200, _feed_bytes()
        )
        feed = await client.fetch_feed("bus", "service-alerts")
        assert feed is not None
        assert feed_last_success("bus", "service-alerts") is not None
        assert client_module._FEED_STATE[("bus", "service-alerts")].unavailable is False


async def test_empty_api_key_raises_without_http_call():
    """F3 — an empty key must never issue a request (preflight guard)."""
    session = FakeSession(responses={})
    client = PTVRealtimeClient("", session, rate_limiter=NoopLimiter())

    with pytest.raises(FeedError) as excinfo:
        await client.fetch_feed("metro", "trip-updates")

    assert excinfo.value.status == 0
    assert excinfo.value.unavailable is True
    assert session.calls == []  # no HTTP request was made
