"""Unit tests for rate_limiter.py."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.transport_victoria.rate_limiter import (
    AsyncRateLimiter,
    PerFeedRateLimiter,
)


@pytest.mark.asyncio
class TestAsyncRateLimiter:
    async def test_acquire_when_full(self):
        """Acquiring from a full bucket should return immediately."""
        limiter = AsyncRateLimiter(capacity=5.0, refill_rate=1.0)
        # Should not raise or hang
        await asyncio.wait_for(limiter.acquire(), timeout=0.5)

    async def test_consumes_tokens(self):
        """Each acquire() should consume one token."""
        limiter = AsyncRateLimiter(capacity=3.0, refill_rate=0.1)
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()
        # Bucket now empty — next acquire should block
        assert limiter._tokens < 1.0

    async def test_blocks_when_empty(self):
        """acquire() should block when no tokens remain."""
        limiter = AsyncRateLimiter(capacity=1.0, refill_rate=10.0)
        await limiter.acquire()  # drain
        # Next call must wait; with refill_rate=10.0 it should finish in ~0.1s
        await asyncio.wait_for(limiter.acquire(), timeout=1.0)

    async def test_does_not_exceed_capacity(self):
        """Token count must never exceed capacity."""
        limiter = AsyncRateLimiter(capacity=5.0, refill_rate=100.0)
        # Let it refill for a bit
        await asyncio.sleep(0.1)
        await limiter.acquire()
        assert limiter._tokens <= 5.0

    async def test_concurrent_acquires(self):
        """Multiple concurrent acquires should all succeed without deadlock."""
        limiter = AsyncRateLimiter(capacity=10.0, refill_rate=100.0)
        results = await asyncio.gather(*[limiter.acquire() for _ in range(5)])
        assert results == [None] * 5


def _per_feed(
    per_key_capacity=5.0,
    per_key_refill=100.0,
    global_capacity=20.0,
    global_refill=100.0,
) -> PerFeedRateLimiter:
    return PerFeedRateLimiter(
        per_key_capacity=per_key_capacity,
        per_key_refill=per_key_refill,
        global_capacity=global_capacity,
        global_refill=global_refill,
    )


@pytest.mark.asyncio
class TestPerFeedRateLimiter:
    async def test_per_key_buckets_are_independent(self):
        """Draining one key's bucket does not block a different key."""
        limiter = _per_feed(per_key_capacity=1.0, per_key_refill=100.0)
        await limiter.acquire("a")  # drains key "a"
        # Key "b" still has a full bucket and acquires immediately.
        await asyncio.wait_for(limiter.acquire("b"), timeout=0.5)

    async def test_same_key_shares_a_bucket(self):
        """The same key shares one bucket (acquire drains it)."""
        limiter = _per_feed(per_key_capacity=1.0, per_key_refill=0.01)
        await limiter.acquire("a")
        # Same key now empty → blocks (refill is near-zero).
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(limiter.acquire("a"), timeout=0.05)

    async def test_global_ceiling_is_shared_across_keys(self):
        """The global ceiling spans keys: exhausting it blocks any key."""
        limiter = _per_feed(per_key_capacity=100.0, global_capacity=2.0, global_refill=0.01)
        await limiter.acquire("a")
        await limiter.acquire("b")
        # Global ceiling (2) is now exhausted; a third acquire (any key) blocks.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(limiter.acquire("c"), timeout=0.05)
