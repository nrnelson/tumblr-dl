"""Unit tests for the async rate limiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from tumblr_dl.ratelimit import AsyncRateLimiter, CompoundRateLimiter


@pytest.mark.asyncio
async def test_acquire_within_limit_does_not_block() -> None:
    """Acquiring tokens within the limit returns immediately."""
    limiter = AsyncRateLimiter(max_calls=10, period=60.0)
    start = time.monotonic()

    for _ in range(10):
        await limiter.acquire()

    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"Should not block, took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_acquire_over_limit_blocks() -> None:
    """Exceeding the limit causes acquire to wait for token refill."""
    # 5 calls per second — 6th call should block ~0.2s.
    limiter = AsyncRateLimiter(max_calls=5, period=1.0)

    for _ in range(5):
        await limiter.acquire()

    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.1, f"Should have blocked, only waited {elapsed:.3f}s"
    assert elapsed < 1.0, f"Blocked too long: {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_tokens_refill_over_time() -> None:
    """Tokens regenerate after time passes."""
    limiter = AsyncRateLimiter(max_calls=10, period=1.0)

    # Consume all tokens.
    for _ in range(10):
        await limiter.acquire()

    # Wait for partial refill (~5 tokens in 0.5s at 10/s rate).
    await asyncio.sleep(0.5)

    start = time.monotonic()
    for _ in range(4):
        await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed < 0.1, f"Refilled tokens should be available, took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_tokens_cap_at_max() -> None:
    """Tokens don't accumulate beyond max_calls."""
    limiter = AsyncRateLimiter(max_calls=5, period=1.0)

    # Wait to let tokens "over-accumulate".
    await asyncio.sleep(0.3)

    # Should still only have 5 tokens max.
    for _ in range(5):
        await limiter.acquire()

    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.1, "Should block after exhausting capped tokens"


@pytest.mark.asyncio
async def test_concurrent_acquires_are_serialized() -> None:
    """Multiple concurrent acquires don't over-consume tokens."""
    limiter = AsyncRateLimiter(max_calls=3, period=1.0)

    results: list[float] = []

    async def timed_acquire() -> None:
        await limiter.acquire()
        results.append(time.monotonic())

    # Launch 5 concurrent acquires with only 3 tokens available.
    await asyncio.gather(*[timed_acquire() for _ in range(5)])

    assert len(results) == 5
    # First 3 should complete nearly simultaneously.
    spread_first_3 = results[2] - results[0]
    assert spread_first_3 < 0.1
    # Last 2 should be delayed.
    assert results[4] - results[0] >= 0.1


# --- drain tests ---


@pytest.mark.asyncio
async def test_drain_empties_tokens() -> None:
    """Drain forces the next acquire to wait for refill."""
    limiter = AsyncRateLimiter(max_calls=10, period=1.0)

    await limiter.drain()

    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.05, f"Should block after drain, only waited {elapsed:.3f}s"


# --- CompoundRateLimiter tests ---


@pytest.mark.asyncio
async def test_compound_respects_strictest_limiter() -> None:
    """Compound limiter blocks when the stricter limiter is exhausted."""
    # Fast limiter: 3/s, slow limiter: 100/s (effectively unlimited).
    compound = CompoundRateLimiter([
        AsyncRateLimiter(max_calls=3, period=1.0),
        AsyncRateLimiter(max_calls=100, period=1.0),
    ])

    for _ in range(3):
        await compound.acquire()

    start = time.monotonic()
    await compound.acquire()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.1, "Should be limited by the stricter (3/s) limiter"


@pytest.mark.asyncio
async def test_compound_drain_empties_all() -> None:
    """Drain empties all sub-limiters."""
    compound = CompoundRateLimiter([
        AsyncRateLimiter(max_calls=10, period=1.0),
        AsyncRateLimiter(max_calls=10, period=1.0),
    ])

    await compound.drain()

    start = time.monotonic()
    await compound.acquire()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.05, "Both limiters should be drained"


@pytest.mark.asyncio
async def test_tumblr_default_factory() -> None:
    """Factory creates a working compound limiter."""
    limiter = CompoundRateLimiter.tumblr_default(per_minute=5, per_hour=100)

    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed < 0.1, "First 5 calls should not block"
