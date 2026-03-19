"""Unit tests for CLI helpers (tag and blog exclusion matching)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tumblr_dl.cli import (
    _collect_trail_blogs,
    _download_items_concurrent,
    _matches_exclusion,
    _parse_exclude_patterns,
    _resolve_log_dir,
)
from tumblr_dl.models import (
    DownloadStats,
    DownloadStatus,
    MediaItem,
    MediaType,
    PostMetadata,
    TrailEntry,
)

# --- _resolve_log_dir ---


def test_resolve_log_dir_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uses XDG_STATE_HOME when set."""
    monkeypatch.setenv("XDG_STATE_HOME", "/custom/state")
    result = _resolve_log_dir()
    assert result == Path("/custom/state/tumblr-dl/logs")


def test_resolve_log_dir_unix_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falls back to ~/.local/state on non-Windows."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("tumblr_dl.cli.sys.platform", "linux")
    result = _resolve_log_dir()
    assert result == Path.home() / ".local" / "state" / "tumblr-dl" / "logs"


def test_resolve_log_dir_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows without XDG, uses %LOCALAPPDATA%."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("tumblr_dl.cli.sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\test\\AppData\\Local")
    result = _resolve_log_dir()
    assert result == Path("C:\\Users\\test\\AppData\\Local") / "tumblr-dl" / "logs"


# --- _parse_exclude_patterns ---


def test_parse_exclude_patterns_basic() -> None:
    """Comma-separated patterns are split and lowercased."""
    patterns = _parse_exclude_patterns("NSFW,explicit*,Gore")
    assert patterns == ["nsfw", "explicit*", "gore"]


def test_parse_exclude_patterns_strips_whitespace() -> None:
    """Whitespace around patterns is stripped."""
    patterns = _parse_exclude_patterns("  nsfw , explicit * , gore  ")
    assert patterns == ["nsfw", "explicit *", "gore"]


def test_parse_exclude_patterns_empty_string() -> None:
    """Empty string returns empty list."""
    assert _parse_exclude_patterns("") == []


def test_parse_exclude_patterns_none() -> None:
    """None returns empty list."""
    assert _parse_exclude_patterns(None) == []


def test_parse_exclude_patterns_skips_empty_segments() -> None:
    """Empty segments from trailing commas are skipped."""
    patterns = _parse_exclude_patterns("nsfw,,gore,")
    assert patterns == ["nsfw", "gore"]


# --- _matches_exclusion ---


def test_matches_exact_tag() -> None:
    """Exact match works."""
    assert _matches_exclusion(["nsfw", "art"], ["nsfw"]) == "nsfw"


def test_matches_glob_star() -> None:
    """Glob * wildcard matches suffix."""
    assert _matches_exclusion(["explicit_content"], ["explicit*"]) == "explicit_content"


def test_matches_glob_does_not_match_substring() -> None:
    """Glob without * is exact match — 'art' does not match 'heart'."""
    assert _matches_exclusion(["heart", "party", "artisan"], ["art"]) is None


def test_matches_glob_star_prefix() -> None:
    """Glob *suffix matches tags ending with the pattern."""
    assert _matches_exclusion(["my_nsfw_post"], ["*nsfw*"]) == "my_nsfw_post"


def test_matches_case_insensitive() -> None:
    """Matching is case-insensitive (both sides lowercased)."""
    # Tags are already lowercased by the extractor.
    assert _matches_exclusion(["nsfw"], ["nsfw"]) == "nsfw"


def test_no_match_returns_none() -> None:
    """No match returns None."""
    assert _matches_exclusion(["art", "photography"], ["nsfw", "gore*"]) is None


def test_empty_tags_returns_none() -> None:
    """Empty tag list returns None."""
    assert _matches_exclusion([], ["nsfw"]) is None


def test_empty_patterns_returns_none() -> None:
    """Empty pattern list returns None."""
    assert _matches_exclusion(["nsfw"], []) is None


def test_matches_question_mark_glob() -> None:
    """Glob ? matches single character."""
    assert _matches_exclusion(["nsf1"], ["nsf?"]) == "nsf1"
    assert _matches_exclusion(["nsfww"], ["nsf?"]) is None


# --- _collect_trail_blogs ---


def test_collect_trail_blogs_returns_lowercase_names() -> None:
    """Trail blog names are collected and lowercased."""
    metadata = PostMetadata(
        blog_name="myblog",
        post_id=100,
        post_url="",
        post_timestamp=0,
        trail=[
            TrailEntry(
                position=0,
                blog_name="OriginalPoster",
                post_id=1,
                timestamp=None,
                is_root=True,
            ),
            TrailEntry(
                position=1,
                blog_name="Reblogger",
                post_id=2,
                timestamp=None,
                is_root=False,
            ),
        ],
    )
    blogs = _collect_trail_blogs(metadata)
    assert blogs == ["originalposter", "reblogger"]


def test_collect_trail_blogs_skips_none() -> None:
    """Deleted blogs (None name) are excluded from the list."""
    metadata = PostMetadata(
        blog_name="myblog",
        post_id=100,
        post_url="",
        post_timestamp=0,
        trail=[
            TrailEntry(
                position=0,
                blog_name=None,
                post_id=None,
                timestamp=None,
                is_root=True,
            ),
            TrailEntry(
                position=1,
                blog_name="goodblog",
                post_id=2,
                timestamp=None,
                is_root=False,
            ),
        ],
    )
    blogs = _collect_trail_blogs(metadata)
    assert blogs == ["goodblog"]


def test_collect_trail_blogs_empty_trail() -> None:
    """Empty trail returns empty list."""
    metadata = PostMetadata(
        blog_name="myblog",
        post_id=100,
        post_url="",
        post_timestamp=0,
    )
    assert _collect_trail_blogs(metadata) == []


# --- Blog exclusion end-to-end matching ---


def test_blog_exclusion_matches_trail_entry() -> None:
    """A blog in the trail matching an exclusion pattern is detected."""
    trail_blogs = ["originalposter", "middleman", "spambot123"]
    matched = _matches_exclusion(trail_blogs, ["spambot*"])
    assert matched == "spambot123"


def test_blog_exclusion_exact_match() -> None:
    """Exact blog name match works."""
    trail_blogs = ["goodblog", "badblog", "otherblog"]
    assert _matches_exclusion(trail_blogs, ["badblog"]) == "badblog"


def test_blog_exclusion_no_match() -> None:
    """No match when trail blogs are all clean."""
    trail_blogs = ["goodblog", "niceblog"]
    assert _matches_exclusion(trail_blogs, ["spambot*", "badblog"]) is None


# --- _download_items_concurrent ---


def _make_item(post_id: int = 1) -> MediaItem:
    """Create a minimal MediaItem for testing."""
    return MediaItem(
        url=f"https://example.com/{post_id}.jpg",
        post_id=post_id,
        media_type=MediaType.IMAGE,
        blog_name="testblog",
    )


async def test_concurrent_downloads_records_stats(tmp_path: object) -> None:
    """All items are downloaded and stats are recorded."""
    items = [_make_item(i) for i in range(5)]
    stats = DownloadStats()
    semaphore = asyncio.Semaphore(4)

    with patch(
        "tumblr_dl.cli.download_item",
        new_callable=AsyncMock,
        return_value=(DownloadStatus.SUCCESS, 2048),
    ):
        await _download_items_concurrent(
            items,
            tmp_path,
            AsyncMock(),
            stats,
            semaphore,  # type: ignore[arg-type]
        )

    assert sum(stats.downloaded.values()) == 5
    assert stats.downloaded[MediaType.IMAGE] == 5
    assert stats.bytes_downloaded[MediaType.IMAGE] == 2048 * 5


async def test_concurrent_downloads_handles_failures(tmp_path: object) -> None:
    """Failed downloads are recorded as FAILED, not raised."""
    from tumblr_dl.exceptions import DownloadError

    items = [_make_item(i) for i in range(3)]
    stats = DownloadStats()
    semaphore = asyncio.Semaphore(4)

    async def mock_download(
        item: MediaItem, output_dir: object, dedup: object
    ) -> tuple[DownloadStatus, int]:
        if item.post_id == 1:
            raise DownloadError("test failure", context={"url": item.url})
        return DownloadStatus.SUCCESS, 1024

    with patch("tumblr_dl.cli.download_item", side_effect=mock_download):
        await _download_items_concurrent(
            items,
            tmp_path,
            AsyncMock(),
            stats,
            semaphore,  # type: ignore[arg-type]
        )

    assert stats.downloaded[MediaType.IMAGE] == 2
    assert stats.failed[MediaType.IMAGE] == 1


async def test_concurrent_downloads_respects_semaphore(tmp_path: object) -> None:
    """No more than N downloads run simultaneously."""
    max_concurrent = 2
    active = 0
    peak = 0

    async def mock_download(
        item: MediaItem, output_dir: object, dedup: object
    ) -> tuple[DownloadStatus, int]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return DownloadStatus.SUCCESS, 512

    items = [_make_item(i) for i in range(10)]
    semaphore = asyncio.Semaphore(max_concurrent)
    stats = DownloadStats()

    with patch("tumblr_dl.cli.download_item", side_effect=mock_download):
        await _download_items_concurrent(
            items,
            tmp_path,
            AsyncMock(),
            stats,
            semaphore,  # type: ignore[arg-type]
        )

    assert peak <= max_concurrent
    assert sum(stats.downloaded.values()) == 10


async def test_concurrent_downloads_empty_list(tmp_path: object) -> None:
    """Empty item list completes without error."""
    stats = DownloadStats()
    semaphore = asyncio.Semaphore(4)
    await _download_items_concurrent(
        [],
        tmp_path,
        AsyncMock(),
        stats,
        semaphore,  # type: ignore[arg-type]
    )
    assert stats.posts_processed == 0
    assert sum(stats.downloaded.values()) == 0
