"""Async rate limiter using a token bucket algorithm."""

from __future__ import annotations

import asyncio
import time


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
            await asyncio.sleep(wait)

    def _refill(self) -> None:
        """Add tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            float(self._max_calls),
            self._tokens + elapsed * (self._max_calls / self._period),
        )
        self._last_refill = now
