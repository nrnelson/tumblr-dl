"""Async rate limiter using a token bucket algorithm."""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    """Token bucket rate limiter for async code.

    Allows up to ``max_calls`` calls per ``period`` seconds. Callers
    that exceed the limit are held with ``await`` until a token is
    available — no requests are dropped.

    Args:
        max_calls: Maximum number of calls allowed per period.
        period: Length of the rate-limit window in seconds.

    Usage::

        limiter = AsyncRateLimiter(max_calls=300, period=60.0)


        async def do_request():
            await limiter.acquire()
            ...
    """

    def __init__(self, max_calls: int = 300, period: float = 60.0) -> None:
        self._max_calls = max_calls
        self._period = period
        self._tokens = float(max_calls)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a rate-limit token is available, then consume it."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Calculate how long until the next token arrives.
                wait = (1.0 - self._tokens) / (self._max_calls / self._period)
            logger.debug("Rate limited, waiting %.2fs for token", wait)
            await asyncio.sleep(wait)

    async def drain(self) -> None:
        """Empty all tokens to force a cooldown before next acquire."""
        async with self._lock:
            self._tokens = 0.0

    def _refill(self) -> None:
        """Add tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            float(self._max_calls),
            self._tokens + elapsed * (self._max_calls / self._period),
        )
        self._last_refill = now


class CompoundRateLimiter:
    """Enforces multiple rate-limit windows simultaneously.

    Tumblr's API has overlapping limits per consumer key:
    - 300 calls/minute (per IP)
    - 1,000 calls/hour (per consumer key)

    This class wraps multiple ``AsyncRateLimiter`` instances so that a
    single ``acquire()`` call respects all windows.

    Args:
        limiters: Sequence of rate limiters to enforce.

    Usage::

        limiter = CompoundRateLimiter.tumblr_default()
        await limiter.acquire()   # respects both per-min and per-hour
    """

    def __init__(self, limiters: list[AsyncRateLimiter]) -> None:
        self._limiters = limiters

    @classmethod
    def tumblr_default(
        cls,
        per_minute: int = 300,
        per_hour: int = 1000,
    ) -> CompoundRateLimiter:
        """Create a limiter matching Tumblr's documented API limits.

        Args:
            per_minute: Max calls per minute (IP-level limit).
            per_hour: Max calls per hour (consumer-key limit).
        """
        return cls([
            AsyncRateLimiter(max_calls=per_minute, period=60.0),
            AsyncRateLimiter(max_calls=per_hour, period=3600.0),
        ])

    async def acquire(self) -> None:
        """Acquire a token from every limiter (sequential)."""
        for limiter in self._limiters:
            await limiter.acquire()

    async def drain(self) -> None:
        """Drain all limiters to force cooldown after a 429."""
        for limiter in self._limiters:
            await limiter.drain()
