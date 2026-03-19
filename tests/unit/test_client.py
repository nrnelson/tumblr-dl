"""Unit tests for the Tumblr API client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tumblr_dl.client import TumblrClient, _normalize_blog_name
from tumblr_dl.config import AuthCredentials
from tumblr_dl.exceptions import ApiError

_TEST_AUTH = AuthCredentials(
    consumer_key="ck",
    consumer_secret="cs",
    oauth_token="ot",
    oauth_token_secret="os",
)


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


def _make_mock_response(json_data: dict[str, Any], status_code: int = 200) -> MagicMock:
    """Create a mock curl_cffi response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


def _stub_client() -> TumblrClient:
    """Create a TumblrClient with mocked internals (no real config).

    Uses a mock limiter so tests don't interact with real token-bucket
    timing (which breaks when asyncio.sleep is patched).
    """
    with patch.object(TumblrClient, "__init__", lambda self, *a, **kw: None):
        client = TumblrClient.__new__(TumblrClient)
        client._oauth = MagicMock()
        client._oauth.sign.return_value = ("https://signed.url", {}, "")
        client._session = AsyncMock()
        limiter = AsyncMock()
        limiter.acquire = AsyncMock()
        limiter.drain = AsyncMock()
        client._limiter = limiter
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
    """Network-level failure retries then raises ApiError with blog context."""
    client = _stub_client()
    client._session.get = AsyncMock(side_effect=ConnectionError("timeout"))

    with (
        patch("tumblr_dl.client.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(ApiError, match="Network error") as exc_info,
    ):
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
async def test_client_context_manager() -> None:
    """Async context manager calls close on exit."""
    with patch("tumblr_dl.client.AsyncSession") as mock_session_cls:
        mock_session = MagicMock()
        mock_session.close = AsyncMock()
        mock_session_cls.return_value = mock_session

        async with TumblrClient(_TEST_AUTH) as client:
            assert client is not None

        mock_session.close.assert_awaited_once()


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
async def test_retry_429_uses_reset_header() -> None:
    """429 retries use the x-ratelimit-perhour-reset header for delay."""
    resp_429 = _make_mock_response({}, status_code=429)
    resp_429.headers = {
        "x-ratelimit-perhour-remaining": "0",
        "x-ratelimit-perhour-reset": "1800",
        "x-ratelimit-perday-remaining": "4000",
        "x-ratelimit-perday-reset": "80000",
    }
    posts = [{"id": 1}]
    resp_200 = _make_mock_response({"response": {"posts": posts}})

    client = _stub_client()
    client._session.get = AsyncMock(side_effect=[resp_429, resp_200])

    with patch("tumblr_dl.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await client.get_posts("testblog")

    assert result == posts
    # Should wait reset time (1800) + 5s buffer = 1805s.
    assert mock_sleep.call_args_list[0][0][0] == 1805.0


@pytest.mark.asyncio
async def test_retry_429_falls_back_to_base_delay() -> None:
    """429 without reset headers falls back to base delay."""
    resp_429 = _make_mock_response({}, status_code=429)
    posts = [{"id": 1}]
    resp_200 = _make_mock_response({"response": {"posts": posts}})

    client = _stub_client()
    client._session.get = AsyncMock(side_effect=[resp_429, resp_200])

    with patch("tumblr_dl.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await client.get_posts("testblog")

    assert result == posts
    assert mock_sleep.call_args_list[0][0][0] == 30.0


@pytest.mark.asyncio
async def test_retry_5xx_uses_exponential_backoff() -> None:
    """5xx retries still use exponential backoff."""
    resp_500 = _make_mock_response({}, status_code=500)
    posts = [{"id": 1}]
    resp_200 = _make_mock_response({"response": {"posts": posts}})

    client = _stub_client()
    client._session.get = AsyncMock(
        side_effect=[resp_500, resp_500, resp_500, resp_200]
    )

    with patch("tumblr_dl.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await client.get_posts("testblog")

    assert result == posts
    delays = [call[0][0] for call in mock_sleep.call_args_list]
    assert delays == [30.0, 60.0, 120.0]


# --- Proactive rate-limit pause tests ---


@pytest.mark.asyncio
async def test_proactive_pause_on_low_remaining() -> None:
    """Proactively pauses when remaining calls are near zero."""
    posts = [{"id": 1}]
    resp = _make_mock_response({"response": {"posts": posts}})
    resp.headers = {
        "x-ratelimit-perhour-remaining": "3",
        "x-ratelimit-perhour-reset": "900",
        "x-ratelimit-perday-remaining": "4500",
        "x-ratelimit-perday-reset": "80000",
    }

    client = _stub_client()
    client._session.get = AsyncMock(return_value=resp)

    with patch("tumblr_dl.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await client.get_posts("testblog")

    assert result == posts
    # Should pause for reset (900) + 5s buffer.
    mock_sleep.assert_awaited_once_with(905.0)
    # Should also drain the limiter.
    client._limiter.drain.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_pause_when_remaining_is_healthy() -> None:
    """No proactive pause when remaining calls are above threshold."""
    posts = [{"id": 1}]
    resp = _make_mock_response({"response": {"posts": posts}})
    resp.headers = {
        "x-ratelimit-perhour-remaining": "500",
        "x-ratelimit-perhour-reset": "1800",
        "x-ratelimit-perday-remaining": "4000",
        "x-ratelimit-perday-reset": "80000",
    }

    client = _stub_client()
    client._session.get = AsyncMock(return_value=resp)

    with patch("tumblr_dl.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await client.get_posts("testblog")

    assert result == posts
    mock_sleep.assert_not_awaited()
