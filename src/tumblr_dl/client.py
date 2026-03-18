"""Async Tumblr API client with OAuth1 authentication."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from curl_cffi.requests import AsyncSession
from oauthlib.oauth1 import Client as OAuth1Client

from tumblr_dl.config import AuthCredentials
from tumblr_dl.exceptions import ApiError
from tumblr_dl.ratelimit import AsyncRateLimiter

logger = logging.getLogger(__name__)

_API_BASE = "https://api.tumblr.com/v2"

# 429 retry: exponential backoff starting at 30s, max 5 min, 4 attempts.
_RETRY_MAX_ATTEMPTS = 4
_RETRY_BASE_DELAY = 30.0
_RETRY_MAX_DELAY = 300.0


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
    rate limiter (300 calls/min) and 429 backoff/retry.

    Args:
        auth: Pre-resolved OAuth credentials.
        rate_limit: Maximum API calls per minute (default: 300).

    Usage::

        async with TumblrClient(auth) as client:
            posts = await client.get_posts("blogname")
    """

    def __init__(
        self,
        auth: AuthCredentials,
        rate_limit: int = 300,
    ) -> None:
        self._oauth = OAuth1Client(
            auth.consumer_key,
            client_secret=auth.consumer_secret,
            resource_owner_key=auth.oauth_token,
            resource_owner_secret=auth.oauth_token_secret,
        )
        self._session: AsyncSession = AsyncSession()  # type: ignore[type-arg]
        self._limiter = AsyncRateLimiter(max_calls=rate_limit, period=60.0)
        self._rate_limit = rate_limit
        self.api_calls: int = 0

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
        """Make a rate-limited API request with 429 backoff/retry.

        Args:
            url: The full API URL (with query params).
            error_context: Extra context dict for ApiError messages.

        Returns:
            The parsed JSON response.

        Raises:
            ApiError: After all retries are exhausted or on non-429 errors.
        """
        ctx = error_context or {}
        last_exc: Exception | None = None

        for attempt in range(_RETRY_MAX_ATTEMPTS):
            await self._limiter.acquire()

            signed_url, headers = self._sign_url(url)
            response = await self._session.get(
                signed_url,
                headers=headers,
                timeout=30,
            )
            self.api_calls += 1

            if response.status_code == 429:
                delay = min(
                    _RETRY_BASE_DELAY * (2**attempt),
                    _RETRY_MAX_DELAY,
                )
                logger.warning(
                    "Rate limited (429). Retrying in %.0fs (attempt %d/%d).",
                    delay,
                    attempt + 1,
                    _RETRY_MAX_ATTEMPTS,
                )
                last_exc = ApiError(
                    "API returned 429",
                    context={**ctx, "status_code": 429},
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

            return response.json()

        # All retries exhausted on 429.
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
            raise
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
        url = f"{_API_BASE}/tagged?tag={tag}&limit={limit}&npf=true"
        if before is not None:
            url += f"&before={before}"
        ctx: dict[str, Any] = {"tag": tag, "before": before}

        try:
            data = await self._request_with_retry(url, error_context=ctx)
        except ApiError:
            raise
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
