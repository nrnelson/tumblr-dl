"""Unit tests for tracker schema v2 migration and new metadata methods."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from tumblr_dl.models import PostMetadata, TrailEntry
from tumblr_dl.tracker import DownloadTracker


@pytest.fixture
async def tracker(tmp_path: Path) -> DownloadTracker:
    """Provide an open v2 tracker."""
    db_path = tmp_path / ".tumblr-dl.db"
    t = DownloadTracker(db_path)
    await t.open()
    yield t  # type: ignore[misc]
    await t.close()


# --- Schema v2 fresh creation ---


async def test_fresh_db_creates_all_tables(tmp_path: Path) -> None:
    """A fresh database has all v2 tables."""
    db_path = tmp_path / "fresh.db"
    async with DownloadTracker(db_path):
        pass

    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]

    assert "blog_state" in tables
    assert "downloads" in tables
    assert "post_tags" in tables
    assert "reblog_trail" in tables
    assert "skipped_posts" in tables


async def test_fresh_db_schema_version_is_2(tmp_path: Path) -> None:
    """Fresh database has schema version 2."""
    db_path = tmp_path / "fresh.db"
    async with DownloadTracker(db_path):
        pass

    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 2


# --- Schema v1 → v2 migration ---


async def test_migration_creates_backup(tmp_path: Path) -> None:
    """Migrating from v1 creates a .v1.bak backup file."""
    db_path = tmp_path / "migrate.db"

    # Create a v1 database manually.
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE blog_state (
                blog_name TEXT PRIMARY KEY,
                highest_post_id INTEGER NOT NULL DEFAULT 0,
                newest_timestamp INTEGER NOT NULL DEFAULT 0,
                total_posts_seen INTEGER NOT NULL DEFAULT 0,
                last_run_at TEXT NOT NULL DEFAULT (datetime('now')),
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blog_name TEXT NOT NULL,
                post_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                file_path TEXT NOT NULL,
                media_type TEXT NOT NULL,
                status TEXT NOT NULL,
                file_size INTEGER,
                downloaded_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(blog_name, url)
            );
            """
        )
        await conn.execute("PRAGMA user_version=1")
        await conn.commit()

    # Open with new tracker — should trigger migration.
    async with DownloadTracker(db_path):
        pass

    backup_path = db_path.with_suffix(".db.v1.bak")
    assert backup_path.exists(), "Backup file should exist after migration"


async def test_migration_preserves_existing_data(tmp_path: Path) -> None:
    """Migration from v1 preserves existing download records."""
    db_path = tmp_path / "migrate.db"

    # Create v1 DB with a download record.
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE blog_state (
                blog_name TEXT PRIMARY KEY,
                highest_post_id INTEGER NOT NULL DEFAULT 0,
                newest_timestamp INTEGER NOT NULL DEFAULT 0,
                total_posts_seen INTEGER NOT NULL DEFAULT 0,
                last_run_at TEXT NOT NULL DEFAULT (datetime('now')),
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blog_name TEXT NOT NULL,
                post_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                file_path TEXT NOT NULL,
                media_type TEXT NOT NULL,
                status TEXT NOT NULL,
                file_size INTEGER,
                downloaded_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(blog_name, url)
            );
            """
        )
        await conn.execute("PRAGMA user_version=1")
        await conn.execute(
            "INSERT INTO downloads "
            "(blog_name, post_id, url, file_path, media_type, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "myblog",
                100,
                "https://example.com/a.jpg",
                "/out/a.jpg",
                "image",
                "success",
            ),
        )
        await conn.commit()

    # Open with v2 tracker.
    async with DownloadTracker(db_path) as tracker:
        # Old data should survive.
        assert (
            await tracker.is_downloaded("myblog", "https://example.com/a.jpg") is True
        )

        # New columns should accept data.
        await tracker.record_download(
            blog_name="myblog",
            post_id=200,
            url="https://example.com/b.jpg",
            file_path="/out/b.jpg",
            media_type="image",
            status="success",
            post_url="https://myblog.tumblr.com/post/200",
            post_timestamp=1700000000,
            original_post_timestamp=1600000000,
            content_labels="mature,sexual_themes",
        )
        assert (
            await tracker.is_downloaded("myblog", "https://example.com/b.jpg") is True
        )


async def test_migration_adds_new_tables(tmp_path: Path) -> None:
    """Migration from v1 creates the new v2 tables."""
    db_path = tmp_path / "migrate.db"

    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE blog_state (
                blog_name TEXT PRIMARY KEY,
                highest_post_id INTEGER NOT NULL DEFAULT 0,
                newest_timestamp INTEGER NOT NULL DEFAULT 0,
                total_posts_seen INTEGER NOT NULL DEFAULT 0,
                last_run_at TEXT NOT NULL DEFAULT (datetime('now')),
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blog_name TEXT NOT NULL,
                post_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                file_path TEXT NOT NULL,
                media_type TEXT NOT NULL,
                status TEXT NOT NULL,
                file_size INTEGER,
                downloaded_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(blog_name, url)
            );
            """
        )
        await conn.execute("PRAGMA user_version=1")
        await conn.commit()

    async with DownloadTracker(db_path):
        pass

    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]

    assert "post_tags" in tables
    assert "reblog_trail" in tables
    assert "skipped_posts" in tables


# --- Post metadata recording ---


async def test_record_post_tags(tracker: DownloadTracker) -> None:
    """Tags are stored in the post_tags table."""
    metadata = PostMetadata(
        blog_name="myblog",
        post_id=100,
        post_url="https://myblog.tumblr.com/post/100",
        post_timestamp=1700000000,
        tags=["art", "photography", "landscape"],
    )
    await tracker.record_post_metadata(metadata)

    conn = tracker._ensure_conn()
    cursor = await conn.execute(
        "SELECT tag FROM post_tags WHERE blog_name = ? AND post_id = ? ORDER BY tag",
        ("myblog", 100),
    )
    rows = await cursor.fetchall()
    tags = [row[0] for row in rows]
    assert tags == ["art", "photography", "landscape"]


async def test_record_post_tags_deduplicates(tracker: DownloadTracker) -> None:
    """Recording the same tags twice doesn't create duplicates."""
    metadata = PostMetadata(
        blog_name="myblog",
        post_id=100,
        post_url="",
        post_timestamp=0,
        tags=["art"],
    )
    await tracker.record_post_metadata(metadata)
    await tracker.record_post_metadata(metadata)

    conn = tracker._ensure_conn()
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM post_tags WHERE blog_name = ? AND post_id = ?",
        ("myblog", 100),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_record_reblog_trail(tracker: DownloadTracker) -> None:
    """Reblog trail entries are stored correctly."""
    metadata = PostMetadata(
        blog_name="myblog",
        post_id=200,
        post_url="",
        post_timestamp=1700000000,
        trail=[
            TrailEntry(
                position=0,
                blog_name="original",
                post_id=99,
                timestamp=1600000000,
                is_root=True,
            ),
            TrailEntry(
                position=1,
                blog_name="reblogger",
                post_id=150,
                timestamp=1650000000,
                is_root=False,
            ),
        ],
    )
    await tracker.record_post_metadata(metadata)

    conn = tracker._ensure_conn()
    cursor = await conn.execute(
        "SELECT position, trail_blog_name, trail_post_id, trail_timestamp, is_root "
        "FROM reblog_trail WHERE blog_name = ? AND post_id = ? ORDER BY position",
        ("myblog", 200),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 2
    assert rows[0] == (0, "original", 99, 1600000000, 1)
    assert rows[1] == (1, "reblogger", 150, 1650000000, 0)


async def test_record_reblog_trail_with_null_fields(tracker: DownloadTracker) -> None:
    """Trail entries with None fields are stored as NULL."""
    metadata = PostMetadata(
        blog_name="myblog",
        post_id=201,
        post_url="",
        post_timestamp=0,
        trail=[
            TrailEntry(
                position=0, blog_name=None, post_id=None, timestamp=None, is_root=True
            ),
        ],
    )
    await tracker.record_post_metadata(metadata)

    conn = tracker._ensure_conn()
    cursor = await conn.execute(
        "SELECT trail_blog_name, trail_post_id, trail_timestamp "
        "FROM reblog_trail WHERE blog_name = ? AND post_id = ?",
        ("myblog", 201),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row == (None, None, None)


# --- Skipped posts ---


async def test_record_skipped_post(tracker: DownloadTracker) -> None:
    """Skipped posts are recorded with reason and matched tag."""
    await tracker.record_skipped_post("myblog", 300, "tag_exclusion", "nsfw")

    conn = tracker._ensure_conn()
    cursor = await conn.execute(
        "SELECT skip_reason, matched_tag FROM skipped_posts "
        "WHERE blog_name = ? AND post_id = ?",
        ("myblog", 300),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "tag_exclusion"
    assert row[1] == "nsfw"


async def test_record_download_with_new_columns(tracker: DownloadTracker) -> None:
    """record_download stores the new v2 columns."""
    await tracker.record_download(
        blog_name="myblog",
        post_id=400,
        url="https://example.com/d.jpg",
        file_path="/out/d.jpg",
        media_type="image",
        status="success",
        file_size=4096,
        post_url="https://myblog.tumblr.com/post/400",
        post_timestamp=1700000000,
        original_post_timestamp=1600000000,
        content_labels="mature",
    )

    conn = tracker._ensure_conn()
    cursor = await conn.execute(
        "SELECT post_url, post_timestamp, original_post_timestamp, content_labels "
        "FROM downloads WHERE blog_name = ? AND url = ?",
        ("myblog", "https://example.com/d.jpg"),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "https://myblog.tumblr.com/post/400"
    assert row[1] == 1700000000
    assert row[2] == 1600000000
    assert row[3] == "mature"
