"""Async Tumblr API client with OAuth1 authentication."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from curl_cffi.requests import AsyncSession
from oauthlib.oauth1 import Client as OAuth1Client

from tumblr_dl.exceptions import ApiError, ConfigError
from tumblr_dl.ratelimit import AsyncRateLimiter

logger = logging.getLogger(__name__)

_API_BASE = "https://api.tumblr.com/v2"

_REQUIRED_KEYS = (
    "consumer_key",
    "consumer_secret",
    "oauth_token",
    "oauth_token_secret",
)

# 429 retry: exponential backoff starting at 30s, max 5 min, 4 attempts.
_RETRY_MAX_ATTEMPTS = 4
_RETRY_BASE_DELAY = 30.0
_RETRY_MAX_DELAY = 300.0


def load_config(config_path: str | Path) -> dict[str, str]:
    """Load OAuth credentials from a YAML config file.

    Args:
        config_path: Path to the YAML file (supports ~ expansion).

    Returns:
        Dict with the four required OAuth credential keys.

    Raises:
        ConfigError: If the file is missing or keys are absent.
    """
    path = Path(config_path).expanduser()
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Config file not found: {path}",
            context={"path": str(path)},
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"Invalid YAML in config: {path}",
            context={"path": str(path)},
        ) from exc

    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise ConfigError(
            f"Missing keys in config: {', '.join(missing)}",
            context={"path": str(path), "missing_keys": missing},
        )

    return {k: data[k] for k in _REQUIRED_KEYS}


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
        config_path: Path to YAML OAuth credentials file.
        rate_limit: Maximum API calls per minute (default: 300).

    Usage::

        async with TumblrClient("~/.tumblr") as client:
            posts = await client.get_posts("blogname")
    """

    def __init__(
        self,
        config_path: str | Path,
        rate_limit: int = 300,
    ) -> None:
        creds = load_config(config_path)
        self._oauth = OAuth1Client(
            creds["consumer_key"],
            client_secret=creds["consumer_secret"],
            resource_owner_key=creds["oauth_token"],
            resource_owner_secret=creds["oauth_token_secret"],
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
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()  # type: ignore[unused-coroutine]

    def _sign_url(self, url: str) -> tuple[str, dict[str, str]]:
        """Sign a URL with OAuth1 and return (signed_url, headers)."""
        uri, headers, _ = self._oauth.sign(url, http_method="GET")
        return uri, dict(headers)

    async def _request_with_retry(
        self,
        url: str,
        blog_name: str,
        offset: int,
    ) -> Any:
        """Make a rate-limited API request with 429 backoff/retry.

        Args:
            url: The full API URL (with query params).
            blog_name: Blog name for error context.
            offset: Pagination offset for error context.

        Returns:
            The parsed JSON response.

        Raises:
            ApiError: After all retries are exhausted or on non-429 errors.
        """
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
                    context={
                        "blog": blog_name,
                        "offset": offset,
                        "status_code": 429,
                    },
                )
                await asyncio.sleep(delay)
                continue

            try:
                response.raise_for_status()
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                raise ApiError(
                    f"API returned {status or response.status_code}",
                    context={
                        "blog": blog_name,
                        "offset": offset,
                        "status_code": status or response.status_code,
                    },
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
        url = f"{_API_BASE}/blog/{hostname}/posts?offset={offset}&limit={limit}"

        try:
            data = await self._request_with_retry(url, blog_name, offset)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(
                f"API request failed: {exc}",
                context={"blog": blog_name, "offset": offset},
            ) from exc

        posts = data.get("response", {}).get("posts")
        if posts is None:
            raise ApiError(
                "API response missing 'posts' key",
                context={
                    "blog": blog_name,
                    "offset": offset,
                    "keys": list(data.get("response", {}).keys()),
                },
            )

        return posts  # type: ignore[no-any-return]
