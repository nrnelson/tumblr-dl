"""Async Tumblr API client with OAuth1 authentication."""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any

import requests
import yaml
from requests_oauthlib import OAuth1  # type: ignore[import-untyped]

from tumblr_dl.exceptions import ApiError, ConfigError

_API_BASE = "https://api.tumblr.com/v2"

_REQUIRED_KEYS = (
    "consumer_key",
    "consumer_secret",
    "oauth_token",
    "oauth_token_secret",
)


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

    Uses requests + requests-oauthlib under the hood (run in a
    thread pool) for TLS compatibility with Tumblr's CDN.

    Args:
        config_path: Path to YAML OAuth credentials file.

    Usage::

        async with TumblrClient("~/.tumblr") as client:
            posts = await client.get_posts("blogname")
    """

    def __init__(self, config_path: str | Path) -> None:
        creds = load_config(config_path)
        self._auth = OAuth1(
            creds["consumer_key"],
            client_secret=creds["consumer_secret"],
            resource_owner_key=creds["oauth_token"],
            resource_owner_secret=creds["oauth_token_secret"],
        )
        self._session = requests.Session()
        self._session.auth = self._auth

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
        self._session.close()

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
        url = f"{_API_BASE}/blog/{hostname}/posts"

        try:
            response = await asyncio.to_thread(
                functools.partial(
                    self._session.get,
                    url,
                    params={"offset": offset, "limit": limit},
                    timeout=30,
                )
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            raise ApiError(
                f"API returned {status}",
                context={
                    "blog": blog_name,
                    "offset": offset,
                    "status_code": status,
                },
            ) from exc
        except requests.RequestException as exc:
            raise ApiError(
                f"API request failed: {exc}",
                context={"blog": blog_name, "offset": offset},
            ) from exc

        data = response.json()
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
