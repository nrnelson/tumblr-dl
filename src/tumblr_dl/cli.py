"""CLI entry point for tumblr-dl."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import logging
import sys
import time
from pathlib import Path

from tumblr_dl.client import TumblrClient
from tumblr_dl.downloader import (
    DedupStrategy,
    FilesystemDedup,
    SqliteDedup,
    download_item,
)
from tumblr_dl.exceptions import (
    ConfigError,
    DownloadError,
    TumblrDlError,
)
from tumblr_dl.extractors import extract_media, extract_post_metadata
from tumblr_dl.models import DownloadStats, DownloadStatus, MediaItem, MediaType
from tumblr_dl.tracker import DownloadTracker

logger = logging.getLogger(__name__)

_BATCH_SIZE = 20

# Exit codes
_EXIT_OK = 0
_EXIT_CONFIG = 2
_EXIT_RUNTIME = 3


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="tumblr-dl",
        description="Download media from a Tumblr blog or tag search.",
    )
    parser.add_argument(
        "blog_name",
        nargs="?",
        default=None,
        help="The Tumblr blog name (e.g. 'example'). Optional with --tag.",
    )
    parser.add_argument(
        "output_dir",
        help="Directory to save downloaded media",
    )
    parser.add_argument(
        "--config",
        default="~/.tumblr",
        help="Path to YAML OAuth config file (default: ~/.tumblr)",
    )
    parser.add_argument(
        "--start-post",
        type=int,
        default=0,
        help="Post offset to start from (default: 0)",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=None,
        help="Maximum number of posts to process",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite database path (default: <output_dir>/.tumblr-dl.db)",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Disable SQLite tracking; use filesystem-only dedup",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Ignore stored cursor; scan the entire blog",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-download previously failed items before main scan",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Search Tumblr by tag instead of downloading a specific blog",
    )
    parser.add_argument(
        "--exclude-tags",
        default=None,
        help="Comma-separated glob patterns to exclude (e.g. 'nsfw,explicit*')",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def _configure_logging(debug: bool) -> None:
    """Configure logging level and format.

    Only our package gets DEBUG output. Third-party loggers
    (oauthlib, curl_cffi) stay at WARNING to prevent leaking
    credentials or auth headers.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    pkg_logger = logging.getLogger("tumblr_dl")
    pkg_logger.setLevel(logging.DEBUG if debug else logging.INFO)


def _parse_exclude_patterns(raw: str | None) -> list[str]:
    """Parse comma-separated exclude tag patterns to lowercase list."""
    if not raw:
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _matches_exclusion(tags: list[str], patterns: list[str]) -> str | None:
    """Check if any tag matches any exclusion pattern.

    Uses case-insensitive fnmatch glob matching.

    Args:
        tags: Lowercase tag list from the post.
        patterns: Lowercase glob patterns from --exclude-tags.

    Returns:
        The first matching tag string, or None if no match.
    """
    for tag in tags:
        for pattern in patterns:
            if fnmatch.fnmatch(tag, pattern):
                return tag
    return None


async def _retry_failed_downloads(
    tracker: DownloadTracker,
    blog_name: str,
    output_dir: Path,
    dedup: DedupStrategy,
    stats: DownloadStats,
) -> None:
    """Re-attempt previously failed downloads.

    Args:
        tracker: The download tracker with failed records.
        blog_name: Blog to retry failures for.
        output_dir: Directory to save files into.
        dedup: Duplicate detection strategy.
        stats: Stats object to record results into.
    """
    failed = await tracker.get_failed_downloads(blog_name)
    if not failed:
        return

    logger.info("Retrying %d previously failed download(s)...", len(failed))
    for record in failed:
        media_type = MediaType(record["media_type"])
        item = MediaItem(
            url=record["url"],
            post_id=record["post_id"],
            media_type=media_type,
            blog_name=blog_name,
        )
        try:
            status = await download_item(item, output_dir, dedup)
        except DownloadError as exc:
            logger.warning("Retry failed: %s", exc)
            status = DownloadStatus.FAILED
        stats.record(media_type, status)


async def _download_blog(
    client: TumblrClient,
    blog_name: str,
    output_dir: Path,
    dedup: DedupStrategy,
    tracker: DownloadTracker | None = None,
    start_offset: int = 0,
    max_posts: int | None = None,
    full_scan: bool = False,
    exclude_patterns: list[str] | None = None,
) -> DownloadStats:
    """Paginate through a blog and download all media.

    Args:
        client: Authenticated Tumblr API client.
        blog_name: Blog to download from.
        output_dir: Directory to save files into.
        dedup: Duplicate detection strategy.
        tracker: Optional SQLite tracker for incremental sync.
        start_offset: Post offset to begin at.
        max_posts: Stop after processing this many posts.
        full_scan: If True, ignore stored cursor.
        exclude_patterns: Tag glob patterns to skip.

    Returns:
        Accumulated download statistics.

    Raises:
        TumblrDlError: On unrecoverable API errors.
    """
    stats = DownloadStats()
    offset = start_offset

    # Load the blog cursor for early termination.
    highest_known_id = 0
    last_known_ts = 0
    if tracker and not full_scan:
        blog_state = await tracker.get_blog_state(blog_name)
        if blog_state:
            highest_known_id = blog_state.highest_post_id
            last_known_ts = blog_state.newest_timestamp
            logger.info(
                "Incremental sync: will stop at post ID %d (ts %d).",
                highest_known_id,
                last_known_ts,
            )

    run_highest_id = 0
    run_newest_ts = 0

    while True:
        posts = await client.get_posts(blog_name, offset=offset, limit=_BATCH_SIZE)
        if not posts:
            logger.info("No more posts found.")
            break

        for post in posts:
            post_id: int = post["id"]
            post_ts: int = post.get("timestamp", 0)

            # Early termination: post already seen on a previous run.
            id_seen = highest_known_id and post_id <= highest_known_id
            ts_seen = last_known_ts == 0 or post_ts <= last_known_ts
            if id_seen and ts_seen:
                logger.info(
                    "Reached previously seen post %d (cursor: %d, ts: %d). Stopping.",
                    post_id,
                    highest_known_id,
                    last_known_ts,
                )
                stats.early_stopped = True
                stats.early_stop_post_id = post_id
                break

            run_highest_id = max(run_highest_id, post_id)
            run_newest_ts = max(run_newest_ts, post_ts)

            stats.posts_processed += 1
            logger.info(
                "Processing post %d (ID: %s, type: %s)...",
                stats.posts_processed,
                post_id,
                post.get("type", "unknown"),
            )

            # Extract post metadata (tags, trail, content labels).
            metadata = extract_post_metadata(post, blog_name)

            # Record metadata to DB.
            if tracker:
                await tracker.record_post_metadata(metadata)

            # Tag exclusion check.
            if exclude_patterns and metadata.tags:
                matched = _matches_exclusion(metadata.tags, exclude_patterns)
                if matched:
                    logger.info(
                        "Skipping post %d: tag '%s' matches exclusion pattern.",
                        post_id,
                        matched,
                    )
                    if tracker:
                        await tracker.record_skipped_post(
                            blog_name, post_id, "tag_exclusion", matched
                        )
                    continue

            for item in extract_media(post, blog_name, metadata=metadata):
                try:
                    status = await download_item(item, output_dir, dedup)
                except DownloadError as exc:
                    logger.warning("%s", exc)
                    status = DownloadStatus.FAILED
                stats.record(item.media_type, status)

            if max_posts and stats.posts_processed >= max_posts:
                logger.info(
                    "Reached maximum posts (%d). Stopping.",
                    max_posts,
                )
                break

        if stats.early_stopped:
            break
        if max_posts and stats.posts_processed >= max_posts:
            break

        offset += _BATCH_SIZE

    # Update the cursor with the high-water mark from this run.
    if tracker and run_highest_id > 0:
        new_highest = max(run_highest_id, highest_known_id)
        await tracker.update_blog_state(
            blog_name, new_highest, run_newest_ts, stats.posts_processed
        )
        logger.debug("Updated blog cursor: highest_post_id=%d", new_highest)

    return stats


async def _download_tagged(
    client: TumblrClient,
    tag: str,
    output_dir: Path,
    dedup: DedupStrategy,
    tracker: DownloadTracker | None = None,
    max_posts: int | None = None,
    exclude_patterns: list[str] | None = None,
) -> DownloadStats:
    """Search by tag across Tumblr and download matching media.

    Uses the /tagged endpoint with timestamp-based cursor pagination.

    Args:
        client: Authenticated Tumblr API client.
        tag: The tag to search for.
        output_dir: Directory to save files into.
        dedup: Duplicate detection strategy.
        tracker: Optional SQLite tracker for metadata storage.
        max_posts: Stop after processing this many posts.
        exclude_patterns: Tag glob patterns to skip.

    Returns:
        Accumulated download statistics.
    """
    stats = DownloadStats()
    before: int | None = None

    while True:
        posts = await client.get_tagged_posts(tag, before=before, limit=_BATCH_SIZE)
        if not posts:
            logger.info("No more tagged posts found.")
            break

        for post in posts:
            post_id: int = post["id"]

            # The blog name comes from each individual post.
            post_blog = post.get("blog_name") or post.get("blog", {}).get(
                "name", "unknown"
            )

            stats.posts_processed += 1
            logger.info(
                "Processing tagged post %d from %s (ID: %s, type: %s)...",
                stats.posts_processed,
                post_blog,
                post_id,
                post.get("type", "unknown"),
            )

            # Extract post metadata.
            metadata = extract_post_metadata(post, post_blog)

            if tracker:
                await tracker.record_post_metadata(metadata)

            # Tag exclusion check.
            if exclude_patterns and metadata.tags:
                matched = _matches_exclusion(metadata.tags, exclude_patterns)
                if matched:
                    logger.info(
                        "Skipping post %d: tag '%s' matches exclusion pattern.",
                        post_id,
                        matched,
                    )
                    if tracker:
                        await tracker.record_skipped_post(
                            post_blog, post_id, "tag_exclusion", matched
                        )
                    continue

            for item in extract_media(post, post_blog, metadata=metadata):
                try:
                    status = await download_item(item, output_dir, dedup)
                except DownloadError as exc:
                    logger.warning("%s", exc)
                    status = DownloadStatus.FAILED
                stats.record(item.media_type, status)

            if max_posts and stats.posts_processed >= max_posts:
                logger.info(
                    "Reached maximum posts (%d). Stopping.",
                    max_posts,
                )
                break

        if max_posts and stats.posts_processed >= max_posts:
            break

        # Advance cursor: use the timestamp of the last post in the batch.
        last_ts = posts[-1].get("timestamp", 0)
        if last_ts and last_ts != before:
            before = last_ts
        else:
            # No progress — avoid infinite loop.
            logger.info("Pagination cursor did not advance. Stopping.")
            break

    return stats


async def _run(args: argparse.Namespace) -> int:
    """Execute the download workflow. Returns exit code."""
    _configure_logging(args.debug)

    # Validate arguments.
    if not args.tag and not args.blog_name:
        logger.error("blog_name is required unless --tag is specified.")
        return _EXIT_CONFIG

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exclude_patterns = _parse_exclude_patterns(args.exclude_tags)

    start_time = time.monotonic()

    try:
        async with TumblrClient(args.config) as client:
            if args.tag:
                logger.info(
                    "Starting tag search for '%s' to %s",
                    args.tag,
                    output_dir,
                )
            else:
                logger.info(
                    "Starting download from %s to %s",
                    args.blog_name,
                    output_dir,
                )

            # Set up tracker and dedup strategy.
            if args.no_db:
                tracker = None
                dedup: DedupStrategy = FilesystemDedup()
            else:
                db_path = Path(
                    args.db_path if args.db_path else output_dir / ".tumblr-dl.db"
                )
                tracker = DownloadTracker(db_path)
                await tracker.open()
                dedup = SqliteDedup(tracker)

            try:
                stats = DownloadStats()

                if args.tag:
                    # Tag search mode.
                    stats = await _download_tagged(
                        client=client,
                        tag=args.tag,
                        output_dir=output_dir,
                        dedup=dedup,
                        tracker=tracker,
                        max_posts=args.max_posts,
                        exclude_patterns=exclude_patterns,
                    )
                else:
                    # Blog download mode.
                    # Retry previously failed downloads if requested.
                    if args.retry_failed and tracker:
                        await _retry_failed_downloads(
                            tracker, args.blog_name, output_dir, dedup, stats
                        )

                    # Main download loop.
                    main_stats = await _download_blog(
                        client=client,
                        blog_name=args.blog_name,
                        output_dir=output_dir,
                        dedup=dedup,
                        tracker=tracker,
                        start_offset=args.start_post,
                        max_posts=args.max_posts,
                        full_scan=args.full_scan,
                        exclude_patterns=exclude_patterns,
                    )

                    # Merge stats from retry pass into main stats.
                    for mt in MediaType:
                        main_stats.found[mt] += stats.found[mt]
                        main_stats.downloaded[mt] += stats.downloaded[mt]
                        main_stats.skipped[mt] += stats.skipped[mt]
                        main_stats.failed[mt] += stats.failed[mt]

                    stats = main_stats
            finally:
                if tracker:
                    await tracker.close()

            stats.api_calls = client.api_calls
            stats.rate_limit = client._rate_limit

    except ConfigError as exc:
        logger.error("%s", exc)
        return _EXIT_CONFIG
    except TumblrDlError as exc:
        logger.error("API error: %s", exc)
        return _EXIT_RUNTIME

    stats.elapsed_seconds = time.monotonic() - start_time
    logger.info("\n%s", stats.summary())
    return _EXIT_OK


def main() -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
