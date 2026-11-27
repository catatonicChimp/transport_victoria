"""Async token-bucket rate limiters for PTV Open Data API calls.

The PTV Open Data API publishes "20–27 calls per minute (depending on data
size)" PER realtime endpoint (not one global budget).  Two limiter layers cover
this (constants in ``const``):

* :class:`AsyncRateLimiter` — a single token bucket (one budget).
* :class:`PerFeedRateLimiter` — a bucket per ``(mode, feed_type)`` PLUS a global
  safety ceiling bucket, so bursts across the 10 endpoints never hammer the API.

Usage:
    limiter = AsyncRateLimiter(capacity=24, refill_rate=0.4)
    await limiter.acquire()   # blocks until a token is available
    # make API call

    per_feed = PerFeedRateLimiter(
        per_key_capacity=20, per_key_refill=20 / 60,
        global_capacity=40, global_refill=40 / 60,
    )
    await per_feed.acquire(("metro", "trip-updates"))
"""

from __future__ import annotations

import asyncio
import logging
from typing import Hashable

_LOGGER = logging.getLogger(__name__)


class AsyncRateLimiter:
    """Token-bucket rate limiter, asyncio-native.

    Parameters
    ----------
    capacity:
        Maximum burst size (tokens). The bucket starts full.
    refill_rate:
        Tokens added per second. 24 req / 60 s = 0.4 tokens/s.
    """

    def __init__(self, capacity: float = 24.0, refill_rate: float = 0.4) -> None:
        self._capacity = capacity
        self._tokens = capacity          # Start full — no burst penalty on startup
        self._refill_rate = refill_rate
        self._last_refill: float = 0.0  # asyncio monotonic time
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume one.

        Refills the bucket based on elapsed wall time before checking.
        Uses asyncio.sleep (never time.sleep) so the event loop is not blocked.
        """
        async with self._lock:
            # Lazily initialise _last_refill on first call so the loop is running
            if self._last_refill == 0.0:
                self._last_refill = asyncio.get_event_loop().time()

            while True:
                now = asyncio.get_event_loop().time()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._capacity,
                    self._tokens + elapsed * self._refill_rate,
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                # Calculate how long until we have a token again
                wait_seconds = (1.0 - self._tokens) / self._refill_rate
                _LOGGER.debug(
                    "Rate limit reached — waiting %.2fs for next token", wait_seconds
                )
                # Release the lock while sleeping so other coroutines can check
                self._lock.release()
                try:
                    await asyncio.sleep(wait_seconds)
                finally:
                    await self._lock.acquire()


class PerFeedRateLimiter:
    """Per-key token buckets with a shared global safety ceiling.

    ``acquire(key)`` waits for BOTH the per-key bucket AND the global ceiling
    bucket before returning, so each endpoint stays within its own budget while
    the aggregate across all endpoints stays within the ceiling.

    Parameters
    ----------
    per_key_capacity / per_key_refill:
        Token bucket shape for each distinct key (e.g. 20 calls / 60 s).
    global_capacity / global_refill:
        Global ceiling bucket shape (e.g. 40 calls / 60 s across all keys).
    """

    def __init__(
        self,
        *,
        per_key_capacity: float,
        per_key_refill: float,
        global_capacity: float,
        global_refill: float,
    ) -> None:
        self._per_key_capacity = per_key_capacity
        self._per_key_refill = per_key_refill
        self._global = AsyncRateLimiter(
            capacity=global_capacity, refill_rate=global_refill
        )
        self._buckets: dict[Hashable, AsyncRateLimiter] = {}
        self._buckets_lock = asyncio.Lock()

    def _bucket(self, key: Hashable) -> AsyncRateLimiter:
        """Return (creating on first use) the per-key bucket for ``key``."""
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = AsyncRateLimiter(
                capacity=self._per_key_capacity, refill_rate=self._per_key_refill
            )
            self._buckets[key] = bucket
        return bucket

    async def acquire(self, key: Hashable) -> None:
        """Wait for a token from the per-key bucket AND the global ceiling."""
        bucket = self._bucket(key)
        # Per-key first, then global, so a slow per-key wait never holds global
        # capacity that other keys could use.
        await bucket.acquire()
        await self._global.acquire()
