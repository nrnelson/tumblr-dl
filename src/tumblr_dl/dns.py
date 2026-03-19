"""Async DNS cache to avoid hammering local resolvers.

Resolves hostnames once via ``asyncio.getaddrinfo()`` and caches results
with a TTL. Produces ``CURLOPT_RESOLVE`` entries so curl_cffi can skip
DNS entirely on each fresh ``AsyncSession``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Default cache TTL: 5 minutes.  CDN IPs rarely rotate faster than this,
# and a typical download session is well under an hour.
_DEFAULT_TTL = 300.0


@dataclass
class DNSCacheStats:
    """Counters for DNS cache activity."""

    hits: int = 0
    misses: int = 0
    expired: int = 0

    @property
    def total_lookups(self) -> int:
        """Total resolve() calls (hits + misses + expired re-resolves)."""
        return self.hits + self.misses + self.expired


class AsyncDNSCache:
    """TTL-based async DNS cache.

    Resolves hostnames via the OS resolver (``getaddrinfo`` in a thread
    pool) on first access, then serves cached results until the TTL
    expires.  Thread-safe for concurrent async callers.

    Args:
        ttl: Cache entry lifetime in seconds.
    """

    def __init__(self, ttl: float = _DEFAULT_TTL) -> None:
        self._ttl = ttl
        # hostname -> (ip_address, resolved_at)
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        self._stats = DNSCacheStats()

    @property
    def stats(self) -> DNSCacheStats:
        """Return current cache statistics."""
        return self._stats

    async def resolve(self, hostname: str) -> str:
        """Resolve *hostname* to an IPv4 address, using cache when fresh.

        Args:
            hostname: The hostname to resolve (e.g. ``64.media.tumblr.com``).

        Returns:
            An IPv4 address string.

        Raises:
            OSError: If DNS resolution fails.
        """
        now = time.monotonic()

        async with self._lock:
            entry = self._cache.get(hostname)
            if entry is not None:
                ip, resolved_at = entry
                age = now - resolved_at
                if age < self._ttl:
                    self._stats.hits += 1
                    logger.debug(
                        "DNS cache hit: %s -> %s (age: %.0fs, TTL: %.0fs)",
                        hostname,
                        ip,
                        age,
                        self._ttl,
                    )
                    return ip
                self._stats.expired += 1
                logger.debug(
                    "DNS cache expired: %s -> %s (age: %.0fs, TTL: %.0fs), "
                    "re-resolving",
                    hostname,
                    ip,
                    age,
                    self._ttl,
                )
            else:
                self._stats.misses += 1
                logger.debug(
                    "DNS cache miss: %s (no cached entry), resolving",
                    hostname,
                )

        # Resolve outside the lock so other lookups aren't blocked.
        ip = await self._do_resolve(hostname)

        async with self._lock:
            self._cache[hostname] = (ip, time.monotonic())

        return ip

    async def _do_resolve(self, hostname: str) -> str:
        """Perform the actual DNS lookup via the OS resolver."""
        loop = asyncio.get_running_loop()
        results = await loop.getaddrinfo(
            hostname,
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
        if not results:
            raise OSError(f"DNS resolution returned no results for {hostname}")

        ip = results[0][4][0]
        logger.debug("DNS resolved: %s -> %s", hostname, ip)
        return ip

    async def resolve_url(self, url: str) -> list[str]:
        """Return ``CURLOPT_RESOLVE`` entries for the host in *url*.

        Produces entries for both port 443 (HTTPS) and port 80 (HTTP)
        so redirects between schemes still hit the cache.

        Args:
            url: A full URL (e.g. ``https://64.media.tumblr.com/...``).

        Returns:
            A list of strings like ``["host:443:1.2.3.4", "host:80:1.2.3.4"]``.
        """
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return []

        ip = await self.resolve(hostname)
        return [
            f"{hostname}:443:{ip}",
            f"{hostname}:80:{ip}",
        ]

    async def warmup(self, hostnames: list[str]) -> None:
        """Pre-resolve a list of hostnames to prime the cache.

        Logs warnings for failures but does not raise, so a failed
        warmup doesn't block the download.
        """
        for hostname in hostnames:
            try:
                await self.resolve(hostname)
            except OSError as exc:
                logger.warning(
                    "DNS warmup failed for %s: %s (will retry on demand)",
                    hostname,
                    exc,
                )
