"""Unit tests for the download logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tumblr_dl.downloader import (
    DedupStrategy,
    FilesystemDedup,
    _resolve_path,
    download_item,
)
from tumblr_dl.exceptions import DownloadError
from tumblr_dl.models import DownloadStatus, MediaItem, MediaType

# --- Fixtures ---


def _make_item(
    url: str = "https://64.media.tumblr.com/photo1.jpg",
    post_id: int = 123,
    blog_name: str = "testblog",
    media_type: MediaType = MediaType.IMAGE,
) -> MediaItem:
    return MediaItem(
        url=url, media_type=media_type, post_id=post_id, blog_name=blog_name
    )


# --- _resolve_path tests ---


def test_resolve_path_extracts_filename(tmp_path: Path) -> None:
    """Path is derived from URL basename, placed in output dir."""
    item = _make_item(url="https://cdn.example.com/abc123/photo.jpg")
    result = _resolve_path(item, tmp_path)

    assert result == tmp_path / "photo.jpg"


def test_resolve_path_sanitizes_filename(tmp_path: Path) -> None:
    """Invalid characters in filename are replaced."""
    item = _make_item(url="https://cdn.example.com/a/photo<bad>.jpg")
    result = _resolve_path(item, tmp_path)

    assert "<" not in result.name
    assert ">" not in result.name
    assert result.parent == tmp_path


# --- FilesystemDedup tests ---


async def test_filesystem_dedup_detects_existing(tmp_path: Path) -> None:
    """Existing file is detected as duplicate."""
    item = _make_item()
    dest = tmp_path / "photo1.jpg"
    dest.touch()

    dedup = FilesystemDedup()
    assert await dedup.is_duplicate(item, dest) is True


async def test_filesystem_dedup_allows_new(tmp_path: Path) -> None:
    """Non-existent file is not a duplicate."""
    item = _make_item()
    dest = tmp_path / "photo1.jpg"

    dedup = FilesystemDedup()
    assert await dedup.is_duplicate(item, dest) is False


async def test_filesystem_dedup_record_is_noop(tmp_path: Path) -> None:
    """Record is a no-op and doesn't raise."""
    dedup = FilesystemDedup()
    item = _make_item()
    dest = tmp_path / "photo1.jpg"
    await dedup.record(item, dest, DownloadStatus.SUCCESS)


# --- download_item tests ---


def _make_async_dedup(is_dup: bool = False) -> AsyncMock:
    """Create an AsyncMock dedup strategy."""
    dedup = AsyncMock(spec=DedupStrategy)
    dedup.is_duplicate.return_value = is_dup
    return dedup


async def test_download_item_skips_duplicate(tmp_path: Path) -> None:
    """Duplicate items return SKIPPED without downloading."""
    item = _make_item()
    dedup = _make_async_dedup(is_dup=True)

    status = await download_item(item, tmp_path, dedup)

    assert status is DownloadStatus.SKIPPED
    dedup.record.assert_not_called()


async def test_download_item_success(tmp_path: Path) -> None:
    """Successful download returns SUCCESS and records it."""
    item = _make_item()
    dedup = _make_async_dedup()

    with patch(
        "tumblr_dl.downloader._async_download", new_callable=AsyncMock
    ) as mock_dl:
        status = await download_item(item, tmp_path, dedup)

    assert status is DownloadStatus.SUCCESS
    mock_dl.assert_awaited_once()
    dedup.record.assert_called_once()
    _, _, recorded_status = dedup.record.call_args[0]
    assert recorded_status is DownloadStatus.SUCCESS


async def test_download_item_http_error(tmp_path: Path) -> None:
    """HTTP error raises DownloadError with status code context."""
    item = _make_item()
    dedup = _make_async_dedup()

    error = Exception("HTTP 404")
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    error.response = mock_resp  # type: ignore[attr-defined]

    with (
        patch(
            "tumblr_dl.downloader._async_download",
            new_callable=AsyncMock,
            side_effect=error,
        ),
        pytest.raises(
            DownloadError, match=r"Download failed \(Exception, HTTP 404\)"
        ) as exc_info,
    ):
        await download_item(item, tmp_path, dedup)

    assert exc_info.value.context["status_code"] == 404
    assert exc_info.value.context["url"] == item.url
    assert exc_info.value.context["post_id"] == 123
    dedup.record.assert_called_once()
    _, _, recorded_status = dedup.record.call_args[0]
    assert recorded_status is DownloadStatus.FAILED


async def test_download_item_network_error(tmp_path: Path) -> None:
    """Network error raises DownloadError without status code."""
    item = _make_item()
    dedup = _make_async_dedup()

    with (
        patch(
            "tumblr_dl.downloader._async_download",
            new_callable=AsyncMock,
            side_effect=ConnectionError("timeout"),
        ),
        pytest.raises(
            DownloadError, match=r"Download failed \(ConnectionError\)"
        ) as exc_info,
    ):
        await download_item(item, tmp_path, dedup)

    assert "status_code" not in exc_info.value.context
    assert exc_info.value.context["blog"] == "testblog"
