"""Unit tests for the Tumblr API client."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from tumblr_dl.client import TumblrClient, _normalize_blog_name, load_config
from tumblr_dl.exceptions import ApiError, ConfigError
from tumblr_dl.ratelimit import AsyncRateLimiter

# --- load_config tests ---


def test_load_config_success(tmp_path: Path) -> None:
    """Load valid config returns the four OAuth keys."""
    config = {
        "consumer_key": "ck",
        "consumer_secret": "cs",
        "oauth_token": "ot",
        "oauth_token_secret": "os",
    }
    config_file = tmp_path / ".tumblr"
    config_file.write_text(yaml.dump(config))

    result = load_config(config_file)

    assert result == config


def test_load_config_file_not_found(tmp_path: Path) -> None:
    """Missing config file raises ConfigError with path context."""
    missing = tmp_path / "nope.yaml"

    with pytest.raises(ConfigError, match="Config file not found") as exc_info:
        load_config(missing)

    assert str(missing) in exc_info.value.context["path"]


def test_load_config_invalid_yaml(tmp_path: Path) -> None:
    """Malformed YAML raises ConfigError."""
    bad_file = tmp_path / ".tumblr"
    bad_file.write_text("{{invalid: yaml: [")

    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_config(bad_file)


def test_load_config_missing_keys(tmp_path: Path) -> None:
    """Config missing required keys raises ConfigError listing them."""
    config_file = tmp_path / ".tumblr"
    config_file.write_text(yaml.dump({"consumer_key": "ck"}))

    with pytest.raises(ConfigError, match="Missing keys") as exc_info:
        load_config(config_file)

    missing = exc_info.value.context["missing_keys"]
    assert "consumer_secret" in missing
    assert "oauth_token" in missing
    assert "oauth_token_secret" in missing


# --- _normalize_blog_name tests ---


def test_normalize_plain_name() -> None:
    """Plain blog name gets .tumblr.com appended."""
    assert _normalize_blog_name("example") == "example.tumblr.com"


def test_normalize_full_hostname() -> None:
    """Full hostname is returned unchanged."""
    assert _normalize_blog_name("example.tumblr.com") == "example.tumblr.com"


def test_normalize_custom_domain() -> None:
    """Custom domain with dots is returned unchanged."""
    assert _normalize_blog_name("blog.example.com") == "blog.example.com"


# --- TumblrClient helpers ---


def _make_config_file(tmp_path: Path) -> Path:
    """Create a valid config file and return its path."""
    config = {
        "consumer_key": "ck",
        "consumer_secret": "cs",
        "oauth_token": "ot",
        "oauth_token_secret": "os",
    }
    config_file = tmp_path / ".tumblr"
    config_file.write_text(yaml.dump(config))
    return config_file


def _make_mock_response(json_data: dict[str, Any], status_code: int = 200) -> MagicMock:
    """Create a mock curl_cffi response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


def _stub_client() -> TumblrClient:
    """Create a TumblrClient with mocked internals (no real config)."""
    with patch.object(TumblrClient, "__init__", lambda self, *a, **kw: None):
        client = TumblrClient.__new__(TumblrClient)
        client._oauth = MagicMock()
        client._oauth.sign.return_value = ("https://signed.url", {}, "")
        client._session = AsyncMock()
        client._limiter = AsyncRateLimiter(max_calls=1000, period=60.0)
        client._rate_limit = 300
        client.api_calls = 0
        return client


# --- TumblrClient.get_posts tests ---


@pytest.mark.asyncio
async def test_get_posts_returns_post_list() -> None:
    """Successful API call returns the posts list."""
    posts = [{"id": 1, "type": "photo"}, {"id": 2, "type": "text"}]
    mock_resp = _make_mock_response({"response": {"posts": posts}})

    client = _stub_client()
    client._session.get = AsyncMock(return_value=mock_resp)

    result = await client.get_posts("testblog", offset=0, limit=20)

    assert result == posts
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_posts_api_error_with_status() -> None:
    """HTTP error from API raises ApiError with status code context."""
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    error = Exception("HTTP 401")
    error.response = mock_resp  # type: ignore[attr-defined]
    mock_resp.raise_for_status.side_effect = error

    client = _stub_client()
    client._session.get = AsyncMock(return_value=mock_resp)

    with pytest.raises(ApiError, match="API returned 401") as exc_info:
        await client.get_posts("testblog")

    assert exc_info.value.context["status_code"] == 401
    assert exc_info.value.context["blog"] == "testblog"


@pytest.mark.asyncio
async def test_get_posts_network_error() -> None:
    """Network-level failure raises ApiError with blog context."""
    client = _stub_client()
    client._session.get = AsyncMock(side_effect=ConnectionError("timeout"))

    with pytest.raises(ApiError, match="API request failed") as exc_info:
        await client.get_posts("testblog")

    assert exc_info.value.context["blog"] == "testblog"


@pytest.mark.asyncio
async def test_get_posts_missing_posts_key() -> None:
    """Response without 'posts' key raises ApiError."""
    mock_resp = _make_mock_response({"response": {"blog": {}}})

    client = _stub_client()
    client._session.get = AsyncMock(return_value=mock_resp)

    with pytest.raises(ApiError, match="missing 'posts' key"):
        await client.get_posts("testblog")


@pytest.mark.asyncio
async def test_client_context_manager(tmp_path: Path) -> None:
    """Async context manager calls close on exit."""
    config_file = _make_config_file(tmp_path)

    with patch("tumblr_dl.client.AsyncSession") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        async with TumblrClient(config_file) as client:
            assert client is not None

        mock_session.close.assert_called_once()


# --- 429 retry tests ---


@pytest.mark.asyncio
async def test_retry_on_429_then_success() -> None:
    """429 response triggers retry; succeeds on next attempt."""
    posts = [{"id": 1, "type": "photo"}]
    resp_429 = _make_mock_response({}, status_code=429)
    resp_200 = _make_mock_response({"response": {"posts": posts}})

    client = _stub_client()
    client._session.get = AsyncMock(side_effect=[resp_429, resp_200])

    with patch("tumblr_dl.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await client.get_posts("testblog")

    assert result == posts
    mock_sleep.assert_awaited_once()
    # First retry delay should be 30s (base delay).
    assert mock_sleep.call_args[0][0] == 30.0


@pytest.mark.asyncio
async def test_retry_exhausted_raises_api_error() -> None:
    """All 429 retries exhausted raises ApiError."""
    resp_429 = _make_mock_response({}, status_code=429)

    client = _stub_client()
    client._session.get = AsyncMock(return_value=resp_429)

    with (
        patch("tumblr_dl.client.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(ApiError, match="429") as exc_info,
    ):
        await client.get_posts("testblog")

    assert exc_info.value.context["status_code"] == 429


@pytest.mark.asyncio
async def test_retry_backoff_is_exponential() -> None:
    """429 retries use exponential backoff delays."""
    resp_429 = _make_mock_response({}, status_code=429)
    posts = [{"id": 1}]
    resp_200 = _make_mock_response({"response": {"posts": posts}})

    # Fail 3 times, succeed on 4th.
    client = _stub_client()
    client._session.get = AsyncMock(
        side_effect=[resp_429, resp_429, resp_429, resp_200]
    )

    with patch("tumblr_dl.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await client.get_posts("testblog")

    assert result == posts
    delays = [call[0][0] for call in mock_sleep.call_args_list]
    assert delays == [30.0, 60.0, 120.0]
