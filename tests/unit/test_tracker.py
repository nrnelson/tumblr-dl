"""Unit tests for the SQLite download tracker."""

from __future__ import annotations

from pathlib import Path

from tumblr_dl.tracker import DownloadTracker

# --- Schema ---


async def test_creates_database_file(tmp_path: Path) -> None:
    """Opening a tracker creates the SQLite file."""
    db_path = tmp_path / "test.db"
    async with DownloadTracker(db_path):
        assert db_path.exists()


async def test_open_is_idempotent(tmp_path: Path) -> None:
    """Opening twice with same schema doesn't raise."""
    db_path = tmp_path / "test.db"
    async with DownloadTracker(db_path):
        pass
    async with DownloadTracker(db_path):
        pass


# --- Blog state ---


async def test_get_blog_state_returns_none_for_new_blog(
    tracker: DownloadTracker,
) -> None:
    """First lookup for a blog returns None."""
    state = await tracker.get_blog_state("newblog")
    assert state is None


async def test_update_and_get_blog_state(tracker: DownloadTracker) -> None:
    """Blog state is persisted and retrievable."""
    await tracker.update_blog_state("myblog", 500, 1700000000, 25)
    state = await tracker.get_blog_state("myblog")

    assert state is not None
    assert state.blog_name == "myblog"
    assert state.highest_post_id == 500
    assert state.newest_timestamp == 1700000000
    assert state.total_posts_seen == 25


async def test_update_blog_state_keeps_highest_id(
    tracker: DownloadTracker,
) -> None:
    """Subsequent updates keep the MAX of highest_post_id."""
    await tracker.update_blog_state("myblog", 500, 1700000000, 10)
    await tracker.update_blog_state("myblog", 300, 1690000000, 5)

    state = await tracker.get_blog_state("myblog")
    assert state is not None
    assert state.highest_post_id == 500  # kept the higher value
    assert state.newest_timestamp == 1700000000
    assert state.total_posts_seen == 15  # 10 + 5


async def test_update_blog_state_advances_cursor(
    tracker: DownloadTracker,
) -> None:
    """A higher post ID advances the cursor."""
    await tracker.update_blog_state("myblog", 500, 1700000000, 10)
    await tracker.update_blog_state("myblog", 700, 1710000000, 3)

    state = await tracker.get_blog_state("myblog")
    assert state is not None
    assert state.highest_post_id == 700
    assert state.newest_timestamp == 1710000000
    assert state.total_posts_seen == 13


# --- Download records ---


async def test_is_downloaded_false_for_unknown_url(
    tracker: DownloadTracker,
) -> None:
    """Unknown URL is not considered downloaded."""
    assert await tracker.is_downloaded("myblog", "https://example.com/a.jpg") is False


async def test_record_and_check_successful_download(
    tracker: DownloadTracker,
) -> None:
    """A successful download is recognized as downloaded."""
    await tracker.record_download(
        blog_name="myblog",
        post_id=100,
        url="https://example.com/a.jpg",
        file_path="/out/a.jpg",
        media_type="image",
        status="success",
        file_size=1024,
    )
    assert await tracker.is_downloaded("myblog", "https://example.com/a.jpg") is True


async def test_failed_download_not_counted_as_downloaded(
    tracker: DownloadTracker,
) -> None:
    """A failed download is NOT counted as downloaded."""
    await tracker.record_download(
        blog_name="myblog",
        post_id=100,
        url="https://example.com/b.jpg",
        file_path="/out/b.jpg",
        media_type="image",
        status="failed",
    )
    assert await tracker.is_downloaded("myblog", "https://example.com/b.jpg") is False


async def test_retry_overwrites_failed_with_success(
    tracker: DownloadTracker,
) -> None:
    """A successful retry overwrites the failed record."""
    url = "https://example.com/c.jpg"
    await tracker.record_download(
        blog_name="myblog",
        post_id=100,
        url=url,
        file_path="/out/c.jpg",
        media_type="image",
        status="failed",
    )
    assert await tracker.is_downloaded("myblog", url) is False

    await tracker.record_download(
        blog_name="myblog",
        post_id=100,
        url=url,
        file_path="/out/c.jpg",
        media_type="image",
        status="success",
        file_size=2048,
    )
    assert await tracker.is_downloaded("myblog", url) is True


# --- Failed downloads ---


async def test_get_failed_downloads_returns_failures(
    tracker: DownloadTracker,
) -> None:
    """get_failed_downloads returns only failed records."""
    await tracker.record_download(
        blog_name="myblog",
        post_id=100,
        url="https://example.com/ok.jpg",
        file_path="/out/ok.jpg",
        media_type="image",
        status="success",
    )
    await tracker.record_download(
        blog_name="myblog",
        post_id=101,
        url="https://example.com/bad.jpg",
        file_path="/out/bad.jpg",
        media_type="image",
        status="failed",
    )

    failed = await tracker.get_failed_downloads("myblog")
    assert len(failed) == 1
    assert failed[0]["url"] == "https://example.com/bad.jpg"
    assert failed[0]["post_id"] == 101


async def test_get_failed_downloads_empty_when_none(
    tracker: DownloadTracker,
) -> None:
    """Returns empty list when no failures exist."""
    failed = await tracker.get_failed_downloads("myblog")
    assert failed == []


# --- Full-scan progress ---


async def test_full_scan_offset_none_by_default(
    tracker: DownloadTracker,
) -> None:
    """No full-scan offset for a new blog."""
    assert await tracker.get_full_scan_offset("newblog") is None


async def test_full_scan_offset_none_after_blog_state_created(
    tracker: DownloadTracker,
) -> None:
    """Blog with state but no active scan returns None."""
    await tracker.update_blog_state("myblog", 100, 170000, 5)
    assert await tracker.get_full_scan_offset("myblog") is None


async def test_update_and_get_full_scan_offset(
    tracker: DownloadTracker,
) -> None:
    """Full-scan offset is persisted and retrievable."""
    await tracker.update_full_scan_offset("myblog", 500)
    assert await tracker.get_full_scan_offset("myblog") == 500


async def test_update_full_scan_offset_overwrites(
    tracker: DownloadTracker,
) -> None:
    """Subsequent updates overwrite the offset."""
    await tracker.update_full_scan_offset("myblog", 100)
    await tracker.update_full_scan_offset("myblog", 300)
    assert await tracker.get_full_scan_offset("myblog") == 300


async def test_clear_full_scan_offset(
    tracker: DownloadTracker,
) -> None:
    """Clearing resets offset to None."""
    await tracker.update_full_scan_offset("myblog", 500)
    await tracker.clear_full_scan_offset("myblog")
    assert await tracker.get_full_scan_offset("myblog") is None


async def test_full_scan_offset_independent_of_blog_state(
    tracker: DownloadTracker,
) -> None:
    """Full-scan offset doesn't interfere with normal cursor."""
    await tracker.update_blog_state("myblog", 100, 170000, 5)
    await tracker.update_full_scan_offset("myblog", 500)

    state = await tracker.get_blog_state("myblog")
    assert state is not None
    assert state.highest_post_id == 100
    assert await tracker.get_full_scan_offset("myblog") == 500


async def test_clear_all_full_scan_offsets(
    tracker: DownloadTracker,
) -> None:
    """Clearing all offsets resets every blog."""
    await tracker.update_full_scan_offset("blog_a", 100)
    await tracker.update_full_scan_offset("blog_b", 200)
    await tracker.update_full_scan_offset("blog_c", 300)

    await tracker.clear_all_full_scan_offsets()

    assert await tracker.get_full_scan_offset("blog_a") is None
    assert await tracker.get_full_scan_offset("blog_b") is None
    assert await tracker.get_full_scan_offset("blog_c") is None
