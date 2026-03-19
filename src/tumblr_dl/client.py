"""Async Tumblr API client with OAuth1 authentication."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

from curl_cffi.requests import AsyncSession
from oauthlib.oauth1 import Client as OAuth1Client

from tumblr_dl.config import AuthCredentials
from tumblr_dl.exceptions import ApiError
from tumblr_dl.ratelimit import CompoundRateLimiter

logger = logging.getLogger(__name__)

_API_BASE = "https://api.tumblr.com/v2"

# 429 retry: exponential backoff starting at 30s, max 5 min, 4 attempts.
_RETRY_MAX_ATTEMPTS = 4
_RETRY_BASE_DELAY = 30.0
_RETRY_MAX_DELAY = 300.0


def _log_ratelimit_headers(headers: Any) -> None:
    """Log any X-RateLimit-* headers from the API response.

    Tumblr sends headers like ``x-ratelimit-perhour-remaining`` and
    ``x-ratelimit-perhour-reset`` (seconds until window resets).
    """
    for key in headers:
        if str(key).lower().startswith("x-ratelimit"):
            logger.debug("Rate-limit header: %s: %s", key, headers[key])


def _extract_reset_delay(headers: Any) -> float:
    """Determine how long to wait from rate-limit response headers.

    Checks ``x-ratelimit-perhour-reset`` and ``x-ratelimit-perday-reset``
    headers (values are seconds until the window resets). Uses the
    shortest non-zero reset if the corresponding ``remaining`` is 0.

    Falls back to ``_RETRY_BASE_DELAY`` if no usable header is found.
    """
    best: float | None = None

    for window in ("perhour", "perday"):
        remaining_key = f"x-ratelimit-{window}-remaining"
        reset_key = f"x-ratelimit-{window}-reset"

        remaining_val = _header_int(headers, remaining_key)
        reset_val = _header_int(headers, reset_key)

        if remaining_val is not None and remaining_val <= 0 and reset_val:
            # This window is exhausted — use its reset time.
            # Add a small buffer to avoid hitting the boundary.
            reset_seconds = float(reset_val) + 5.0
            if best is None or reset_seconds < best:
                best = reset_seconds

    if best is not None:
        logger.info(
            "Rate limit exhausted. Waiting %.0fs (from reset header).",
            best,
        )
        return best

    return _RETRY_BASE_DELAY


def _header_int(headers: Any, key: str) -> int | None:
    """Safely extract an integer value from a response header."""
    val = headers.get(key)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# Pause proactively when remaining calls drop to this threshold.
_REMAINING_THRESHOLD = 5


def _check_remaining(headers: Any) -> float | None:
    """Check rate-limit remaining headers and return a delay if near zero.

    Called after every successful response. If any window's remaining
    count is at or below the threshold, returns the reset time so the
    caller can sleep before the next request.

    Returns:
        Seconds to wait, or None if no pause is needed.
    """
    for window in ("perhour", "perday"):
        remaining_key = f"x-ratelimit-{window}-remaining"
        reset_key = f"x-ratelimit-{window}-reset"

        remaining = _header_int(headers, remaining_key)
        reset = _header_int(headers, reset_key)

        if remaining is not None and remaining <= _REMAINING_THRESHOLD and reset:
            delay = float(reset) + 5.0
            logger.warning(
                "Rate limit nearly exhausted (%s: %d remaining). "
                "Pausing for %.0fs until window resets.",
                window,
                remaining,
                delay,
            )
            return delay

    return None


def _normalize_blog_name(blog_name: str) -> str:
    """Ensure blog name is a full hostname.

    Args:
        blog_name: Either 'example' or 'example.tumblr.com'.

    Returns:
        Full hostname like 'example.tumblr.com'.
    """
    if "." not in blog_name:
        return f"{blog_name}.tumblr.com"
    return blog_name


class TumblrClient:
    """Async Tumblr API v2 client with OAuth1 authentication.

    Uses curl_cffi for native async HTTP with TLS compatibility,
    and oauthlib for OAuth1 request signing. Includes a built-in
    compound rate limiter (300 calls/min + 1,000 calls/hour) and
    429 backoff/retry with bucket drain.

    Args:
        auth: Pre-resolved OAuth credentials.
        rate_limit: Maximum API calls per minute (default: 300).
        rate_limit_hourly: Maximum API calls per hour (default: 1000).

    Usage::

        async with TumblrClient(auth) as client:
            posts = await client.get_posts("blogname")
    """

    def __init__(
        self,
        auth: AuthCredentials,
        rate_limit: int = 300,
        rate_limit_hourly: int = 1000,
    ) -> None:
        self._oauth = OAuth1Client(
            auth.consumer_key,
            client_secret=auth.consumer_secret,
            resource_owner_key=auth.oauth_token,
            resource_owner_secret=auth.oauth_token_secret,
        )
        self._session: AsyncSession = AsyncSession()  # type: ignore[type-arg]
        self._limiter = CompoundRateLimiter.tumblr_default(
            per_minute=rate_limit, per_hour=rate_limit_hourly,
        )
        self._rate_limit = rate_limit
        self.api_calls: int = 0

    @property
    def rate_limit(self) -> int:
        """Maximum API calls per minute."""
        return self._rate_limit

    async def __aenter__(self) -> TumblrClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        await self._session.close()

    def _sign_url(self, url: str) -> tuple[str, dict[str, str]]:
        """Sign a URL with OAuth1 and return (signed_url, headers)."""
        uri, headers, _ = self._oauth.sign(url, http_method="GET")
        return uri, dict(headers)

    async def _request_with_retry(
        self,
        url: str,
        error_context: dict[str, Any] | None = None,
    ) -> Any:
        """Make a rate-limited API request with retry on transient errors.

        Retries on 429 (rate limit), 5xx (server errors), and network
        errors (``ConnectionError``, ``OSError``). Uses exponential
        backoff starting at 30s, capped at 5 minutes.

        Args:
            url: The full API URL (with query params).
            error_context: Extra context dict for ApiError messages.

        Returns:
            The parsed JSON response.

        Raises:
            ApiError: After all retries are exhausted or on non-retryable errors.
        """
        ctx = error_context or {}
        last_exc: Exception | None = None

        for attempt in range(_RETRY_MAX_ATTEMPTS):
            await self._limiter.acquire()

            logger.debug("API request: %s (attempt %d)", url, attempt + 1)
            try:
                signed_url, headers = self._sign_url(url)
                response = await self._session.get(
                    signed_url,
                    headers=headers,
                    timeout=30,
                )
            except (ConnectionError, OSError) as exc:
                delay = min(
                    _RETRY_BASE_DELAY * (2**attempt),
                    _RETRY_MAX_DELAY,
                )
                logger.warning(
                    "Network error: %s. Retrying in %.0fs (attempt %d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _RETRY_MAX_ATTEMPTS,
                )
                last_exc = ApiError(
                    f"Network error: {exc}",
                    context={**ctx, "error_type": type(exc).__name__},
                )
                await asyncio.sleep(delay)
                continue

            self.api_calls += 1
            logger.debug("API response: %d", response.status_code)

            # Log any rate-limit headers Tumblr sends (undocumented
            # but useful for debugging).
            _log_ratelimit_headers(response.headers)

            if response.status_code == 429 or response.status_code >= 500:
                if response.status_code == 429:
                    delay = _extract_reset_delay(response.headers)
                    # Drain token buckets so refill starts from zero
                    # during the backoff sleep — prevents a burst of
                    # requests immediately after waking.
                    await self._limiter.drain()
                else:
                    delay = min(
                        _RETRY_BASE_DELAY * (2**attempt),
                        _RETRY_MAX_DELAY,
                    )
                logger.warning(
                    "Server returned %d. Retrying in %.0fs (attempt %d/%d).",
                    response.status_code,
                    delay,
                    attempt + 1,
                    _RETRY_MAX_ATTEMPTS,
                )
                last_exc = ApiError(
                    f"API returned {response.status_code}",
                    context={**ctx, "status_code": response.status_code},
                )
                await asyncio.sleep(delay)
                continue

            try:
                response.raise_for_status()
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                raise ApiError(
                    f"API returned {status or response.status_code}",
                    context={**ctx, "status_code": status or response.status_code},
                ) from exc

            # Proactively pause if any rate-limit window is nearly
            # exhausted, rather than burning remaining calls and
            # getting 429'd.
            preemptive_delay = _check_remaining(response.headers)
            if preemptive_delay:
                await self._limiter.drain()
                await asyncio.sleep(preemptive_delay)

            return response.json()

        # All retries exhausted.
        raise last_exc  # type: ignore[misc]

    async def get_posts(
        self,
        blog_name: str,
        offset: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch a batch of posts from a blog.

        Args:
            blog_name: The Tumblr blog name.
            offset: Post offset for pagination.
            limit: Number of posts per request (max 20).

        Returns:
            List of post dicts from the API.

        Raises:
            ApiError: If the API request fails or response is malformed.
        """
        hostname = _normalize_blog_name(blog_name)
        url = (
            f"{_API_BASE}/blog/{hostname}/posts?offset={offset}&limit={limit}&npf=true"
        )
        ctx = {"blog": blog_name, "offset": offset}

        try:
            data = await self._request_with_retry(url, error_context=ctx)
        except ApiError:
            raise  # Don't re-wrap ApiError in another ApiError.
        except Exception as exc:
            raise ApiError(
                f"API request failed: {exc}",
                context=ctx,
            ) from exc

        posts = data.get("response", {}).get("posts")
        if posts is None:
            raise ApiError(
                "API response missing 'posts' key",
                context={
                    **ctx,
                    "keys": list(data.get("response", {}).keys()),
                },
            )

        return posts  # type: ignore[no-any-return]

    async def get_tagged_posts(
        self,
        tag: str,
        before: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch posts from the global /tagged endpoint.

        Args:
            tag: The tag to search for (e.g. 'landscape').
            before: Unix timestamp cursor — fetch posts before this time.
            limit: Number of posts per request (max 20).

        Returns:
            List of post dicts from the API.

        Raises:
            ApiError: If the API request fails or response is malformed.
        """
        url = f"{_API_BASE}/tagged?tag={quote(tag)}&limit={limit}&npf=true"
        if before is not None:
            url += f"&before={before}"
        ctx: dict[str, Any] = {"tag": tag, "before": before}

        try:
            data = await self._request_with_retry(url, error_context=ctx)
        except ApiError:
            raise  # Don't re-wrap ApiError in another ApiError.
        except Exception as exc:
            raise ApiError(
                f"API request failed: {exc}",
                context=ctx,
            ) from exc

        # The /tagged endpoint returns response as an array directly,
        # not nested under {"posts": [...]}.
        response = data.get("response")
        if isinstance(response, list):
            return response

        # Some API versions may still nest under "posts".
        if isinstance(response, dict):
            posts = response.get("posts")
            if isinstance(posts, list):
                return posts

        raise ApiError(
            "Unexpected /tagged response format",
            context={**ctx, "response_type": type(response).__name__},
        )
