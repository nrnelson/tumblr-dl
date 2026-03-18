"""Async SQLite-based download tracker for incremental sync."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

import aiosqlite

from tumblr_dl.models import BlogState, PostMetadata

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 2

# Full schema for fresh databases (version 0 → 2).
_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS blog_state (
    blog_name        TEXT PRIMARY KEY,
    highest_post_id  INTEGER NOT NULL DEFAULT 0,
    newest_timestamp INTEGER NOT NULL DEFAULT 0,
    total_posts_seen INTEGER NOT NULL DEFAULT 0,
    last_run_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS downloads (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    blog_name               TEXT NOT NULL,
    post_id                 INTEGER NOT NULL,
    url                     TEXT NOT NULL,
    file_path               TEXT NOT NULL,
    media_type              TEXT NOT NULL,
    status                  TEXT NOT NULL,
    file_size               INTEGER,
    post_url                TEXT,
    post_timestamp          INTEGER,
    original_post_timestamp INTEGER,
    content_labels          TEXT,
    downloaded_at           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(blog_name, url)
);

CREATE INDEX IF NOT EXISTS idx_downloads_blog_post
    ON downloads(blog_name, post_id);

CREATE TABLE IF NOT EXISTS post_tags (
    blog_name  TEXT NOT NULL,
    post_id    INTEGER NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY (blog_name, post_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag);

CREATE TABLE IF NOT EXISTS reblog_trail (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    blog_name        TEXT NOT NULL,
    post_id          INTEGER NOT NULL,
    position         INTEGER NOT NULL,
    trail_blog_name  TEXT,
    trail_post_id    INTEGER,
    trail_timestamp  INTEGER,
    is_root          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(blog_name, post_id, position)
);

CREATE INDEX IF NOT EXISTS idx_trail_blog ON reblog_trail(trail_blog_name);

CREATE TABLE IF NOT EXISTS skipped_posts (
    blog_name    TEXT NOT NULL,
    post_id      INTEGER NOT NULL,
    skip_reason  TEXT NOT NULL,
    matched_tag  TEXT,
    skipped_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (blog_name, post_id)
);
"""

# Migration from v1 → v2: add new columns and tables.
_MIGRATE_V1_TO_V2_SQL = """\
ALTER TABLE downloads ADD COLUMN post_url TEXT;
ALTER TABLE downloads ADD COLUMN post_timestamp INTEGER;
ALTER TABLE downloads ADD COLUMN original_post_timestamp INTEGER;
ALTER TABLE downloads ADD COLUMN content_labels TEXT;

CREATE TABLE IF NOT EXISTS post_tags (
    blog_name  TEXT NOT NULL,
    post_id    INTEGER NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY (blog_name, post_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag);

CREATE TABLE IF NOT EXISTS reblog_trail (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    blog_name        TEXT NOT NULL,
    post_id          INTEGER NOT NULL,
    position         INTEGER NOT NULL,
    trail_blog_name  TEXT,
    trail_post_id    INTEGER,
    trail_timestamp  INTEGER,
    is_root          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(blog_name, post_id, position)
);

CREATE INDEX IF NOT EXISTS idx_trail_blog ON reblog_trail(trail_blog_name);

CREATE TABLE IF NOT EXISTS skipped_posts (
    blog_name    TEXT NOT NULL,
    post_id      INTEGER NOT NULL,
    skip_reason  TEXT NOT NULL,
    matched_tag  TEXT,
    skipped_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (blog_name, post_id)
);
"""


class DownloadTracker:
    """Async SQLite tracker for blog state and download records.

    Manages per-blog pagination cursors, per-file download history,
    post tags, reblog trails, and skipped-post records.

    Args:
        db_path: Path to the SQLite database file.

    Usage::

        async with DownloadTracker(Path("out/.tumblr-dl.db")) as tracker:
            state = await tracker.get_blog_state("myblog")
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Open the database and ensure the schema exists."""
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        cur_version = await self._conn.execute("PRAGMA user_version")
        row = await cur_version.fetchone()
        version = row[0] if row else 0

        if version == 0:
            # Fresh database — create full v2 schema.
            await self._conn.executescript(_SCHEMA_SQL)
            await self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            await self._conn.commit()
        elif version < _SCHEMA_VERSION:
            await self._migrate_v1_to_v2()

        logger.debug(
            "Opened tracker database: %s (schema v%d)", self._db_path, _SCHEMA_VERSION
        )

    async def _migrate_v1_to_v2(self) -> None:
        """Migrate from schema v1 to v2.

        Backs up the database file, then adds new columns and tables.
        Existing download records are preserved with NULL for new columns.
        If migration fails, the backup is automatically restored.
        """
        conn = self._ensure_conn()

        # Backup the old database before migrating.
        backup_path = self._db_path.with_suffix(".db.v1.bak")
        logger.info("Backing up database to %s before migration...", backup_path)
        # Close temporarily to ensure a clean backup.
        await conn.close()
        shutil.copy2(self._db_path, backup_path)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        conn = self._conn

        try:
            logger.info("Migrating database schema from v1 to v2...")
            await conn.executescript(_MIGRATE_V1_TO_V2_SQL)
            await conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            await conn.commit()
            logger.info("Migration complete. Backup at %s", backup_path)
        except Exception:
            logger.error("Migration failed. Restoring backup from %s...", backup_path)
            await conn.close()
            shutil.copy2(backup_path, self._db_path)
            self._conn = await aiosqlite.connect(self._db_path)
            await self._conn.execute("PRAGMA journal_mode=WAL")
            raise

    async def close(self) -> None:
        """Flush pending writes and close the database connection."""
        if self._conn:
            await self._conn.commit()
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> DownloadTracker:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.close()

    def _ensure_conn(self) -> aiosqlite.Connection:
        """Return the active connection or raise."""
        if self._conn is None:
            msg = "Tracker is not open. Use 'async with' or call open()."
            raise RuntimeError(msg)
        return self._conn

    async def commit(self) -> None:
        """Flush pending writes to disk.

        Call this after processing each batch of posts rather than
        after every individual insert for better performance.
        """
        conn = self._ensure_conn()
        await conn.commit()

    # --- Blog state ---

    async def get_blog_state(self, blog_name: str) -> BlogState | None:
        """Return the saved state for a blog, or None on first run.

        Args:
            blog_name: The Tumblr blog name.

        Returns:
            BlogState if a record exists, otherwise None.
        """
        conn = self._ensure_conn()
        cursor = await conn.execute(
            "SELECT blog_name, highest_post_id, newest_timestamp, "
            "total_posts_seen, last_run_at "
            "FROM blog_state WHERE blog_name = ?",
            (blog_name,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return BlogState(
            blog_name=row[0],
            highest_post_id=row[1],
            newest_timestamp=row[2],
            total_posts_seen=row[3],
            last_run_at=row[4],
        )

    async def update_blog_state(
        self,
        blog_name: str,
        highest_post_id: int,
        newest_timestamp: int,
        posts_delta: int,
    ) -> None:
        """Update the blog cursor after processing a batch.

        Args:
            blog_name: The Tumblr blog name.
            highest_post_id: The highest post ID seen.
            newest_timestamp: Unix timestamp of the newest post.
            posts_delta: Number of new posts processed this run.
        """
        conn = self._ensure_conn()
        await conn.execute(
            "INSERT INTO blog_state "
            "(blog_name, highest_post_id, newest_timestamp, "
            "total_posts_seen, last_run_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(blog_name) DO UPDATE SET "
            "highest_post_id = MAX(highest_post_id, excluded.highest_post_id), "
            "newest_timestamp = MAX(newest_timestamp, excluded.newest_timestamp), "
            "total_posts_seen = total_posts_seen + excluded.total_posts_seen, "
            "last_run_at = datetime('now')",
            (blog_name, highest_post_id, newest_timestamp, posts_delta),
        )
        await conn.commit()

    # --- Download records ---

    async def is_downloaded(self, blog_name: str, url: str) -> bool:
        """Return True if the URL was previously downloaded successfully.

        Args:
            blog_name: The Tumblr blog name.
            url: The media URL.

        Returns:
            True if a successful download record exists.
        """
        conn = self._ensure_conn()
        cursor = await conn.execute(
            "SELECT 1 FROM downloads "
            "WHERE blog_name = ? AND url = ? AND status = 'success' "
            "LIMIT 1",
            (blog_name, url),
        )
        return await cursor.fetchone() is not None

    async def record_download(
        self,
        blog_name: str,
        post_id: int,
        url: str,
        file_path: str,
        media_type: str,
        status: str,
        file_size: int | None = None,
        post_url: str | None = None,
        post_timestamp: int | None = None,
        original_post_timestamp: int | None = None,
        content_labels: str | None = None,
    ) -> None:
        """Record a download attempt.

        Uses INSERT OR REPLACE so a failed download can be overwritten
        by a successful retry.

        Args:
            blog_name: The Tumblr blog name.
            post_id: The post ID this media belongs to.
            url: The media URL.
            file_path: Local path where the file was saved.
            media_type: Media type (image, video, audio).
            status: Result status (success, failed).
            file_size: File size in bytes (if known).
            post_url: Canonical Tumblr post URL.
            post_timestamp: Unix timestamp of the post/reblog.
            original_post_timestamp: Unix timestamp of the original post.
            content_labels: Comma-separated content labels.
        """
        conn = self._ensure_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO downloads "
            "(blog_name, post_id, url, file_path, media_type, "
            "status, file_size, post_url, post_timestamp, "
            "original_post_timestamp, content_labels, downloaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                blog_name,
                post_id,
                url,
                file_path,
                media_type,
                status,
                file_size,
                post_url,
                post_timestamp,
                original_post_timestamp,
                content_labels,
            ),
        )

    async def get_failed_downloads(self, blog_name: str) -> list[dict[str, Any]]:
        """Return all failed download records for a blog.

        Args:
            blog_name: The Tumblr blog name.

        Returns:
            List of dicts with url, post_id, media_type, file_path.
        """
        conn = self._ensure_conn()
        cursor = await conn.execute(
            "SELECT url, post_id, media_type, file_path "
            "FROM downloads "
            "WHERE blog_name = ? AND status = 'failed'",
            (blog_name,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "url": row[0],
                "post_id": row[1],
                "media_type": row[2],
                "file_path": row[3],
            }
            for row in rows
        ]

    # --- Post metadata (tags + reblog trail) ---

    async def record_post_metadata(self, metadata: PostMetadata) -> None:
        """Store tags and reblog trail entries for a post.

        Args:
            metadata: Extracted post metadata to persist.
        """
        conn = self._ensure_conn()

        # Insert tags (normalized to lowercase by the extractor).
        for tag in metadata.tags:
            await conn.execute(
                "INSERT OR IGNORE INTO post_tags (blog_name, post_id, tag) "
                "VALUES (?, ?, ?)",
                (metadata.blog_name, metadata.post_id, tag),
            )

        # Insert reblog trail entries.
        for entry in metadata.trail:
            await conn.execute(
                "INSERT OR IGNORE INTO reblog_trail "
                "(blog_name, post_id, position, trail_blog_name, "
                "trail_post_id, trail_timestamp, is_root) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    metadata.blog_name,
                    metadata.post_id,
                    entry.position,
                    entry.blog_name,
                    entry.post_id,
                    entry.timestamp,
                    1 if entry.is_root else 0,
                ),
            )

    # --- Skipped posts ---

    async def record_skipped_post(
        self,
        blog_name: str,
        post_id: int,
        skip_reason: str,
        matched_tag: str | None = None,
    ) -> None:
        """Record that a post was skipped (e.g. tag exclusion).

        Args:
            blog_name: The Tumblr blog name.
            post_id: The skipped post ID.
            skip_reason: Why it was skipped (e.g. 'tag_exclusion').
            matched_tag: The tag that triggered the exclusion.
        """
        conn = self._ensure_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO skipped_posts "
            "(blog_name, post_id, skip_reason, matched_tag, skipped_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (blog_name, post_id, skip_reason, matched_tag),
        )
