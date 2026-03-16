"""Tumblr API client wrapper with config loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytumblr  # type: ignore[import-untyped]
import yaml

from tumblr_dl.exceptions import ApiError, ConfigError

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


class TumblrClient:
    """Thin wrapper around pytumblr for fetching blog posts.

    Args:
        config_path: Path to YAML OAuth credentials file.
    """

    def __init__(self, config_path: str | Path) -> None:
        creds = load_config(config_path)
        self._client = pytumblr.TumblrRestClient(
            creds["consumer_key"],
            creds["consumer_secret"],
            creds["oauth_token"],
            creds["oauth_token_secret"],
        )

    def get_posts(
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
            ApiError: If the API response is missing expected data.
        """
        response = self._client.posts(blog_name, offset=offset, limit=limit)

        if not isinstance(response, dict):
            raise ApiError(
                "Unexpected API response type",
                context={
                    "blog": blog_name,
                    "offset": offset,
                    "type": str(type(response)),
                },
            )

        if "posts" not in response:
            raise ApiError(
                "API response missing 'posts' key",
                context={
                    "blog": blog_name,
                    "offset": offset,
                    "keys": list(response.keys()),
                },
            )

        return response["posts"]  # type: ignore[no-any-return]
