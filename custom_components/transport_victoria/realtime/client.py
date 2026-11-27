"""PTV GTFS Realtime client.

Fetches and decodes protobuf feeds from the PTV Open Data API.

Key design choices:
- Module-level TTL cache keyed by the RESOLVED (mode, feed_type) so multiple
  coordinator instances watching the same mode share one HTTP request per poll
  cycle — and so ``regional-bus`` and ``bus`` (which share one portal RT feed)
  also share one fetch.  See :func:`rt_url_mode`.
- Rate limiting lives here (T13): a :class:`PerFeedRateLimiter` enforces a
  per-(mode, feed_type) budget (20 calls/60s) AND a global safety ceiling
  (40 calls/60s), so the board/watch coordinators and device trackers all share
  one enforcement point regardless of which caller triggers a fetch.
- HTTP failure classification (T13): 429 → per-feed cooldown honouring
  ``Retry-After`` (clamped); 401/403 → logged with a diagnosis hint; 404 on a
  KNOWN-missing feed type → "unavailable" and suppressed retry; 403 on a
  scope-unavailable feed → "unavailable" + a one-time product-scope hint.
  Failures raise :class:`FeedError` (an ``aiohttp.ClientResponseError`` subclass)
  carrying per-feed ``last_success``/``last_failure``/``unavailable`` so the
  board aggregator can surface data age even after a failure.
- Protobuf decoding (_decode_feed) and the google.transit import are both run in
  a thread-pool executor because importing the C extension
  (google._upb._message) calls importlib.import_module, which blocks the event
  loop and triggers HA's blocking-call detector.  The module cache is written
  only AFTER a complete decode, so a cancelled fetch can never poison the cache
  with partial data.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp

from ..const import (
    REALTIME_AUTH_HEADER,
    REALTIME_BASE_URL,
    REALTIME_CACHE_TTL_SECONDS,
    REALTIME_KNOWN_MISSING_FEEDS,
    REALTIME_SCOPE_UNAVAILABLE_FEEDS,
    RATE_LIMIT_GLOBAL_CALLS,
    RATE_LIMIT_GLOBAL_PERIOD,
    RATE_LIMIT_PER_FEED_CALLS,
    RATE_LIMIT_PER_FEED_PERIOD,
    RATE_LIMIT_RETRY_AFTER_MAX_SECONDS,
    RT_URL_FEED_MODE,
)
from ..rate_limiter import PerFeedRateLimiter

if TYPE_CHECKING:
    from google.transit import gtfs_realtime_pb2 as gtfs_rt

_LOGGER = logging.getLogger(__name__)

# Module-level cache: {(mode, feed_type): (FeedMessage, fetched_at)}
_FEED_CACHE: dict[
    tuple[str, str],
    tuple[object, datetime],
] = {}

# Module-level per-feed state (shared across client instances, like the cache):
# last success/failure timestamps, 429 cooldown, and the "unavailable" flag for
# feeds the portal does not publish.
@dataclass
class _FeedState:
    last_success: datetime | None = None
    last_failure: datetime | None = None
    cooldown_until: datetime | None = None   # 429 Retry-After clamp
    unavailable: bool = False                # 404 known-missing / 403 scope-unavailable
    scope_hint_logged: bool = False          # one-time 403 product-scope diagnosis hint


_FEED_STATE: dict[tuple[str, str], _FeedState] = {}

_CACHE_TTL = timedelta(seconds=REALTIME_CACHE_TTL_SECONDS)


class FeedError(aiohttp.ClientResponseError):
    """A realtime feed fetch failed, carrying per-feed health metadata.

    Subclasses ``aiohttp.ClientResponseError`` so the watch coordinator's
    existing ``aiohttp`` handlers keep working (it reads ``exc.status``), while
    the board aggregator reads ``last_success`` / ``last_failure`` /
    ``unavailable`` off the same exception to build a richer ``FeedHealth``.
    """

    def __init__(
        self,
        status: int = 0,
        message: str = "",
        *,
        unavailable: bool = False,
        last_success: datetime | None = None,
        last_failure: datetime | None = None,
    ) -> None:
        self.status = status
        self.message = message
        self.unavailable = unavailable
        self.last_success = last_success
        self.last_failure = last_failure
        # Bypass aiohttp.ClientResponseError.__init__ (its signature is absent in
        # the light test env where aiohttp is stubbed to Exception) and set args
        # directly so str(exc) is meaningful in both environments.
        Exception.__init__(self, f"HTTP {status}: {message}")


# ---------------------------------------------------------------------------
# Shared rate limiter (per-feed buckets + global ceiling)
# ---------------------------------------------------------------------------

_RATE_LIMITER: PerFeedRateLimiter | None = None


def _default_rate_limiter() -> PerFeedRateLimiter:
    """Return (creating once) the shared per-feed rate limiter singleton."""
    global _RATE_LIMITER
    if _RATE_LIMITER is None:
        _RATE_LIMITER = PerFeedRateLimiter(
            per_key_capacity=RATE_LIMIT_PER_FEED_CALLS,
            per_key_refill=RATE_LIMIT_PER_FEED_CALLS / RATE_LIMIT_PER_FEED_PERIOD,
            global_capacity=RATE_LIMIT_GLOBAL_CALLS,
            global_refill=RATE_LIMIT_GLOBAL_CALLS / RATE_LIMIT_GLOBAL_PERIOD,
        )
    return _RATE_LIMITER


def _state_for(mode: str, feed_type: str) -> _FeedState:
    """Return (creating on first use) the per-feed state for ``(mode, feed_type)``."""
    key = (mode, feed_type)
    state = _FEED_STATE.get(key)
    if state is None:
        state = _FeedState()
        _FEED_STATE[key] = state
    return state


def rt_url_mode(mode: str) -> str:
    """Resolve a config mode to the realtime URL mode.

    Regional buses ride the combined "bus" RT feed (the portal has no
    ``regional-bus`` / ``rural-bus`` / ``coach`` RT resources), so
    ``"regional-bus"`` resolves to ``"bus"`` here — the URL segment, cache key,
    rate bucket, and classification keys all use this resolved mode.
    """
    return RT_URL_FEED_MODE.get(mode, mode)


def _cache_key(mode: str, feed_type: str) -> tuple[str, str]:
    """Return the resolved cache/state key for ``(mode, feed_type)``."""
    return (rt_url_mode(mode), feed_type)


def _parse_retry_after(value: str | None) -> int:
    """Parse a ``Retry-After`` header into a clamped cooldown (seconds).

    Accepts the seconds form (the portal's usual shape).  An HTTP-date or a
    missing/garbage value falls back to the max clamp; the result is always
    clamped to ``[0, RATE_LIMIT_RETRY_AFTER_MAX_SECONDS]``.
    """
    if value is None:
        return RATE_LIMIT_RETRY_AFTER_MAX_SECONDS
    try:
        seconds = int(str(value).strip())
    except (ValueError, TypeError):
        return RATE_LIMIT_RETRY_AFTER_MAX_SECONDS
    if seconds < 0:
        return 0
    return min(seconds, RATE_LIMIT_RETRY_AFTER_MAX_SECONDS)


class PTVRealtimeClient:
    """Client for the PTV GTFS Realtime API."""

    def __init__(
        self,
        api_key: str,
        session: aiohttp.ClientSession,
        *,
        rate_limiter: PerFeedRateLimiter | None = None,
    ) -> None:
        self._api_key = api_key
        self._session = session
        # Defaults to the shared module-level limiter; injectable for tests.
        self._rate_limiter = (
            rate_limiter if rate_limiter is not None else _default_rate_limiter()
        )

    # ------------------------------------------------------------------
    # Feed fetching
    # ------------------------------------------------------------------

    async def fetch_feed(self, mode: str, feed_type: str) -> object:
        """Fetch and decode a GTFS Realtime protobuf feed.

        Returns a cached FeedMessage if the TTL has not expired, avoiding
        redundant HTTP requests when multiple coordinators poll the same mode.

        Rate limiting is enforced here (per-feed budget + global ceiling) before
        any HTTP request.  HTTP failures raise :class:`FeedError` with per-feed
        health metadata (429 → cooldown, 401 → auth diagnosis, 403 on a
        scope-unavailable feed → unavailable, 404 on a known-missing feed →
        unavailable); network errors re-raise the original ``aiohttp.ClientError``
        after recording ``last_failure``.

        Protobuf decoding runs in a thread-pool executor, and the cache is
        committed only after a complete decode, so a cancelled fetch never leaves
        partial data in the module cache.

        Args:
            mode: One of the MODE_* constants (e.g. "metro").  ``regional-bus`` is
                resolved to the "bus" RT feed (see :func:`rt_url_mode`).
            feed_type: One of the FEED_* constants (e.g. "trip-updates").

        Returns:
            A decoded gtfs_realtime_pb2.FeedMessage instance.

        Raises:
            FeedError: On HTTP errors (including 401/403/404/429), carrying
                per-feed health metadata.
            aiohttp.ClientError: On network errors.
        """
        url_mode = rt_url_mode(mode)
        cache_key = (url_mode, feed_type)
        now = datetime.now(tz=timezone.utc)

        cached_feed, cached_at = _FEED_CACHE.get(cache_key, (None, None))
        if cached_feed is not None and cached_at is not None:
            if now - cached_at < _CACHE_TTL:
                _LOGGER.debug("Cache hit for %s/%s", url_mode, feed_type)
                return cached_feed

        state = _state_for(url_mode, feed_type)

        # Known-missing feed (the portal does not publish it) → "unavailable",
        # never issue the HTTP request (don't hammer endpoints that won't exist).
        if cache_key in REALTIME_KNOWN_MISSING_FEEDS:
            state.unavailable = True
            state.last_failure = now
            _LOGGER.debug("Feed %s/%s is not published — unavailable", url_mode, feed_type)
            raise FeedError(
                404,
                "feed not published for this mode",
                unavailable=True,
                last_success=state.last_success,
                last_failure=state.last_failure,
            )

        # 429 cooldown active → suppress the retry until Retry-After elapses.
        if state.cooldown_until is not None and now < state.cooldown_until:
            _LOGGER.debug(
                "Feed %s/%s in rate-limit cooldown; suppressing fetch", url_mode, feed_type
            )
            raise FeedError(
                429,
                "rate-limit cooldown active",
                last_success=state.last_success,
                last_failure=state.last_failure,
            )

        # Per-feed budget + global safety ceiling (keyed on the resolved mode so
        # bus and regional-bus share one bucket).
        await self._rate_limiter.acquire(cache_key)

        url = f"{REALTIME_BASE_URL}/{url_mode}/{feed_type}"
        _LOGGER.debug("Fetching realtime feed: %s", url)

        try:
            async with self._session.get(
                url,
                headers={REALTIME_AUTH_HEADER: self._api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                status = getattr(response, "status", 200)

                if status == 429:
                    retry_after = _parse_retry_after(
                        response.headers.get("Retry-After")
                    )
                    state.cooldown_until = now + timedelta(seconds=retry_after)
                    state.last_failure = now
                    _LOGGER.warning(
                        "Feed %s/%s rate-limited (429); cooldown %ss",
                        url_mode, feed_type, retry_after,
                    )
                    raise FeedError(
                        429,
                        "rate limited",
                        last_success=state.last_success,
                        last_failure=state.last_failure,
                    )

                # 403 on a scope-unavailable feed: the portal *serves* it but the
                # key isn't scoped to its product.  Classify unavailable + a
                # one-time diagnosis hint (not a retryable failure); a later 200
                # (product enabled) overrides it via the normal success path.
                if status == 403 and cache_key in REALTIME_SCOPE_UNAVAILABLE_FEEDS:
                    state.unavailable = True
                    state.last_failure = now
                    if not state.scope_hint_logged:
                        state.scope_hint_logged = True
                        _LOGGER.warning(
                            "Feed %s/%s unavailable (403) — the portal product is not "
                            "enabled for this key; enable it under My Account → "
                            "Products on opendata.transport.vic.gov.au",
                            url_mode, feed_type,
                        )
                    raise FeedError(
                        403,
                        "portal product not enabled for this key",
                        unavailable=True,
                        last_success=state.last_success,
                        last_failure=state.last_failure,
                    )

                if status in (401, 403):
                    state.last_failure = now
                    _LOGGER.warning(
                        "Feed %s/%s auth rejected (%s) — check the PTV subscription "
                        "key (a 401 means it is missing, invalid, or expired)",
                        url_mode, feed_type, status,
                    )
                    raise FeedError(
                        status,
                        "auth rejected",
                        last_success=state.last_success,
                        last_failure=state.last_failure,
                    )

                if status == 404 and cache_key in REALTIME_KNOWN_MISSING_FEEDS:
                    state.unavailable = True
                    state.last_failure = now
                    raise FeedError(
                        404,
                        "feed not published for this mode",
                        unavailable=True,
                        last_success=state.last_success,
                        last_failure=state.last_failure,
                    )

                if status >= 400:
                    state.last_failure = now
                    raise FeedError(
                        status,
                        f"HTTP {status}",
                        last_success=state.last_success,
                        last_failure=state.last_failure,
                    )

                content = await response.read()
        except FeedError:
            raise
        except aiohttp.ClientError:
            state.last_failure = now
            raise

        # Decode protobuf in executor — importing the C extension and parsing
        # binary data both block; running in a thread keeps the event loop free.
        loop = asyncio.get_running_loop()
        feed = await loop.run_in_executor(None, _decode_feed, content)

        # Commit only after a complete decode (cancellation-safe: a task cancelled
        # during the executor call above never reaches this line).
        _FEED_CACHE[cache_key] = (feed, now)
        state.last_success = now
        state.cooldown_until = None
        state.unavailable = False
        state.scope_hint_logged = False  # re-arm the hint if the 403 recurs later
        return feed

    async def fetch_feed_with_meta(
        self, mode: str, feed_type: str
    ) -> tuple[object, FeedMeta]:
        """Fetch a feed and return ``(feed, FeedMeta)`` for freshness.

        Thin wrapper over :meth:`fetch_feed` that also surfaces the feed's
        header timestamp and retrieval time, so the board aggregator (T9) can
        classify freshness from the fetched data itself rather than reading the
        module cache directly.
        """
        feed = await self.fetch_feed(mode, feed_type)
        fetched_at = feed_fetched_at(mode, feed_type)
        return feed, feed_meta(feed, fetched_at)

    # ------------------------------------------------------------------
    # API key validation
    # ------------------------------------------------------------------

    async def test_connection(self) -> None:
        """Validate the API key by fetching the metro trip-updates feed.

        Raises:
            aiohttp.ClientResponseError: On 401 (invalid key) or other HTTP errors.
            aiohttp.ClientError: On network errors.
        """
        url = f"{REALTIME_BASE_URL}/metro/trip-updates"
        async with self._session.get(
            url,
            headers={REALTIME_AUTH_HEADER: self._api_key},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            await response.read()


def _decode_feed(content: bytes) -> object:
    """Decode raw protobuf bytes into a FeedMessage.

    Designed to run in a thread-pool executor — importing google.transit
    triggers a blocking C-extension import the first time it is called.
    """
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)
    return feed


def feed_fetched_at(mode: str, feed_type: str) -> datetime | None:
    """Return when the cached feed for (mode, feed_type) was fetched, or None.

    The mode is resolved to its URL mode (``regional-bus`` → ``bus``) so the
    shared cache entry is found regardless of which config mode asks for it.

    The board aggregator (T9) uses this to classify a feed as ``stale`` when
    its data is older than ``REALTIME_CACHE_TTL_SECONDS``.  Returns an aware
    UTC datetime matching ``fetch_feed``'s cache timestamps.
    """
    entry = _FEED_CACHE.get(_cache_key(mode, feed_type))
    if entry is None:
        return None
    return entry[1]


def feed_last_success(mode: str, feed_type: str) -> datetime | None:
    """Return when ``(mode, feed_type)`` last fetched successfully, or None."""
    state = _FEED_STATE.get(_cache_key(mode, feed_type))
    return state.last_success if state is not None else None


def feed_last_failure(mode: str, feed_type: str) -> datetime | None:
    """Return when ``(mode, feed_type)`` last failed, or None."""
    state = _FEED_STATE.get(_cache_key(mode, feed_type))
    return state.last_failure if state is not None else None


@dataclass(slots=True, frozen=True)
class FeedMeta:
    """Freshness metadata for one fetched realtime feed.

    ``header_timestamp`` is the GTFS-RT feed's own publication time
    (``feed.header.timestamp``, epoch seconds → aware UTC) — the PRIMARY
    freshness signal.  ``fetched_at`` is when the data was retrieved (the TTL
    cache's timestamp), used only as a fallback when the header is absent.
    """

    header_timestamp: datetime | None
    fetched_at: datetime | None


def feed_meta(feed: object, fetched_at: datetime | None) -> FeedMeta:
    """Build :class:`FeedMeta` for a decoded feed (header timestamp + fetch time)."""
    header_ts: datetime | None = None
    try:
        ts = feed.header.timestamp  # type: ignore[attr-defined]
    except AttributeError:
        ts = 0
    if ts:
        header_ts = datetime.fromtimestamp(ts, tz=timezone.utc)
    return FeedMeta(header_timestamp=header_ts, fetched_at=fetched_at)


def invalidate_cache(mode: str | None = None, feed_type: str | None = None) -> None:
    """Invalidate one or all cache entries (and their per-feed state).

    Called when we know the cache is stale (e.g. after a config change).
    If mode and feed_type are both None, clears the entire cache and state.
    When both are given, the mode is resolved to its URL mode so ``regional-bus``
    invalidates the shared "bus" entry.
    """
    if mode is None and feed_type is None:
        _FEED_CACHE.clear()
        _FEED_STATE.clear()
    else:
        if mode is not None and feed_type is not None:
            key = _cache_key(mode, feed_type)
            _FEED_CACHE.pop(key, None)
            _FEED_STATE.pop(key, None)
