"""CLI entry point for tumblr-dl."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from tumblr_dl import __version__
from tumblr_dl.client import TumblrClient
from tumblr_dl.config import (
    AppConfig,
    AppSettings,
    BlogConfig,
    load_auth,
    load_toml_config,
    resolve_blog_config,
    resolve_config_path,
)
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
from tumblr_dl.models import (
    DownloadStats,
    DownloadStatus,
    MediaItem,
    MediaType,
    PostMetadata,
)
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
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "blog_names",
        nargs="*",
        help="One or more Tumblr blog names. Optional with --tag or --sync.",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=None,
        help="Directory to save downloaded media (default: tumblr_downloads/)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to TOML config file (default: auto-discovered via XDG)",
    )
    parser.add_argument(
        "--start-post",
        type=int,
        default=None,
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
        default=None,
        help="Disable SQLite tracking; use filesystem-only dedup",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        default=None,
        help="Ignore stored cursor; scan the entire blog",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        default=None,
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
        "--exclude-blogs",
        default=None,
        help="Comma-separated glob patterns of blog names to exclude",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Download all blogs defined in the TOML config file",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging (also writes a log file)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Write logs to this file (implies debug-level file logging)",
    )
    return parser


def _resolve_log_dir() -> Path:
    """Return the XDG-compliant log directory.

    Uses ``$XDG_STATE_HOME/tumblr-dl/logs/`` (defaults to
    ``~/.local/state/tumblr-dl/logs/``).
    """
    xdg = os.environ.get("XDG_STATE_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "tumblr-dl" / "logs"


def _configure_logging(debug: bool, log_file: str | None = None) -> Path | None:
    """Configure logging level, format, and optional file handler.

    Console always logs at INFO (or DEBUG with ``--debug``).
    When a log file is active, it always captures DEBUG-level output.

    Third-party loggers (oauthlib, curl_cffi) stay at WARNING in all
    handlers to prevent leaking credentials or auth headers.

    Args:
        debug: If True, set console to DEBUG and auto-create a log file.
        log_file: Explicit log file path. Overrides the auto-generated path.

    Returns:
        Path to the log file if one was created, None otherwise.
    """
    # Console handler — stderr.
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    # Root logger: WARNING floor keeps third-party libs quiet.
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(console)

    # Our package logger.
    pkg_logger = logging.getLogger("tumblr_dl")
    pkg_logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Determine log file path.
    log_path: Path | None = None
    if log_file:
        log_path = Path(log_file)
    elif debug:
        log_dir = _resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        log_path = log_dir / f"tumblr-dl-{stamp}.log"

    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        pkg_logger.addHandler(file_handler)
        pkg_logger.info("Log file: %s", log_path)

    return log_path


def _parse_exclude_patterns(raw: str | list[str] | None) -> list[str]:
    """Parse exclude patterns from CLI string or config list to lowercase list."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [p.strip().lower() for p in raw if p.strip()]
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _matches_exclusion(values: list[str], patterns: list[str]) -> str | None:
    """Check if any value matches any exclusion pattern.

    Uses case-insensitive fnmatch glob matching.

    Args:
        values: Lowercase strings to check (tags, blog names, etc.).
        patterns: Lowercase glob patterns.

    Returns:
        The first matching string, or None if no match.
    """
    for value in values:
        for pattern in patterns:
            if fnmatch.fnmatch(value, pattern):
                return value
    return None


def _collect_trail_blogs(metadata: PostMetadata) -> list[str]:
    """Collect all blog names from a post's reblog trail.

    Returns lowercase blog names from the trail entries,
    excluding None (deleted blogs).
    """
    blogs: list[str] = []
    for entry in metadata.trail:
        if entry.blog_name:
            blogs.append(entry.blog_name.lower())
    return blogs


def _cli_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Extract CLI flag values as a dict for config merging.

    Only includes values that were explicitly provided (not None).
    Converts --exclude-tags/--exclude-blogs from CSV to lists.
    """
    overrides: dict[str, object] = {}
    if args.output_dir is not None:
        overrides["output_dir"] = args.output_dir
    if args.max_posts is not None:
        overrides["max_posts"] = args.max_posts
    if args.start_post is not None:
        overrides["start_post"] = args.start_post
    if args.db_path is not None:
        overrides["db_path"] = args.db_path
    if args.no_db is not None:
        overrides["no_db"] = args.no_db
    if args.full_scan is not None:
        overrides["full_scan"] = args.full_scan
    if args.retry_failed is not None:
        overrides["retry_failed"] = args.retry_failed
    if args.tag is not None:
        overrides["tag"] = args.tag
    if args.exclude_tags is not None:
        overrides["exclude_tags"] = _parse_exclude_patterns(args.exclude_tags)
    if args.exclude_blogs is not None:
        overrides["exclude_blogs"] = _parse_exclude_patterns(args.exclude_blogs)
    return overrides


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
    exclude_blog_patterns: list[str] | None = None,
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

            # Blog exclusion check — skip if any blog in the reblog
            # trail matches an excluded pattern.
            if exclude_blog_patterns:
                trail_blogs = _collect_trail_blogs(metadata)
                matched_blog = _matches_exclusion(trail_blogs, exclude_blog_patterns)
                if matched_blog:
                    logger.info(
                        "Skipping post %d: reblogged from excluded blog '%s'.",
                        post_id,
                        matched_blog,
                    )
                    if tracker:
                        await tracker.record_skipped_post(
                            blog_name,
                            post_id,
                            "blog_exclusion",
                            matched_blog,
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

        # Flush tracker writes after each pagination batch.
        if tracker:
            await tracker.commit()

        if stats.early_stopped:
            break
        if max_posts and stats.posts_processed >= max_posts:
            break

        offset += _BATCH_SIZE

    # Update the cursor with the high-water mark from this run.
    # Always update last_run_at so we know when we last checked,
    # even if there was no new content.
    if tracker:
        new_highest = max(run_highest_id, highest_known_id)
        new_ts = max(run_newest_ts, last_known_ts)
        await tracker.update_blog_state(
            blog_name, new_highest, new_ts, stats.posts_processed
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
    exclude_blog_patterns: list[str] | None = None,
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

            # Blog exclusion check.
            if exclude_blog_patterns:
                trail_blogs = _collect_trail_blogs(metadata)
                matched_blog = _matches_exclusion(trail_blogs, exclude_blog_patterns)
                if matched_blog:
                    logger.info(
                        "Skipping post %d: reblogged from excluded blog '%s'.",
                        post_id,
                        matched_blog,
                    )
                    if tracker:
                        await tracker.record_skipped_post(
                            post_blog,
                            post_id,
                            "blog_exclusion",
                            matched_blog,
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

        # Flush tracker writes after each pagination batch.
        if tracker:
            await tracker.commit()

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


def _merge_stats(target: DownloadStats, source: DownloadStats) -> None:
    """Merge source stats into target in-place."""
    for mt in MediaType:
        target.found[mt] += source.found[mt]
        target.downloaded[mt] += source.downloaded[mt]
        target.skipped[mt] += source.skipped[mt]
        target.failed[mt] += source.failed[mt]
    target.posts_processed += source.posts_processed


async def _run_blog_download(
    client: TumblrClient,
    blog_name: str,
    blog_config: BlogConfig,
    tracker: DownloadTracker | None,
    dedup: DedupStrategy,
) -> DownloadStats:
    """Run a single blog download using resolved BlogConfig."""
    output_dir = Path(blog_config.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    exclude_patterns = _parse_exclude_patterns(blog_config.exclude_tags)
    exclude_blog_patterns = _parse_exclude_patterns(blog_config.exclude_blogs)

    retry_stats = DownloadStats()
    if blog_config.retry_failed and tracker:
        await _retry_failed_downloads(
            tracker, blog_name, output_dir, dedup, retry_stats
        )

    if blog_config.tag:
        blog_stats = await _download_tagged(
            client=client,
            tag=blog_config.tag,
            output_dir=output_dir,
            dedup=dedup,
            tracker=tracker,
            max_posts=blog_config.max_posts,
            exclude_patterns=exclude_patterns,
            exclude_blog_patterns=exclude_blog_patterns,
        )
    else:
        blog_stats = await _download_blog(
            client=client,
            blog_name=blog_name,
            output_dir=output_dir,
            dedup=dedup,
            tracker=tracker,
            start_offset=blog_config.start_post,
            max_posts=blog_config.max_posts,
            full_scan=blog_config.full_scan,
            exclude_patterns=exclude_patterns,
            exclude_blog_patterns=exclude_blog_patterns,
        )

    _merge_stats(blog_stats, retry_stats)
    return blog_stats


async def _setup_tracker_and_dedup(
    blog_config: BlogConfig,
) -> tuple[DownloadTracker | None, DedupStrategy]:
    """Create tracker and dedup strategy from config."""
    if blog_config.no_db:
        return None, FilesystemDedup()

    output_dir = Path(blog_config.output_dir).expanduser()
    db_path = Path(
        blog_config.db_path if blog_config.db_path else output_dir / ".tumblr-dl.db"
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tracker = DownloadTracker(db_path)
    await tracker.open()
    return tracker, SqliteDedup(tracker)


def _resolve_logging_settings(
    args: argparse.Namespace, settings: AppSettings
) -> tuple[bool, str | None]:
    """Merge logging settings from TOML config and CLI flags.

    CLI flags override config values.

    Returns:
        Tuple of (debug, log_file).
    """
    debug = settings.debug or args.debug
    log_file = args.log_file if args.log_file is not None else settings.log_file
    return debug, log_file


async def _run(args: argparse.Namespace) -> int:
    """Execute the download workflow. Returns exit code."""
    # Load .env file if present.
    load_dotenv()

    # Load TOML config first so [settings] can inform logging.
    config_path = Path(args.config) if args.config else resolve_config_path()
    app_config: AppConfig | None = None
    if config_path:
        app_config = load_toml_config(config_path)

    settings = app_config.settings if app_config else AppSettings()
    debug, log_file = _resolve_logging_settings(args, settings)
    _configure_logging(debug, log_file=log_file)

    overrides = _cli_overrides(args)

    # Handle --sync flag.
    if args.sync:
        if not app_config or not app_config.blogs:
            logger.error(
                "No blogs configured. Add [blog.*] sections to your config.toml."
            )
            return _EXIT_CONFIG

        auth = load_auth(app_config)
        start_time = time.monotonic()

        try:
            async with TumblrClient(auth) as client:
                total_stats = DownloadStats()
                blog_names = list(app_config.blogs.keys())

                for i, blog_name in enumerate(blog_names):
                    blog_config = resolve_blog_config(blog_name, app_config, overrides)

                    if len(blog_names) > 1:
                        logger.info(
                            "--- Blog %d/%d: %s ---",
                            i + 1,
                            len(blog_names),
                            blog_name,
                        )

                    logger.info(
                        "Starting download from %s to %s",
                        blog_name,
                        blog_config.output_dir,
                    )

                    tracker, dedup = await _setup_tracker_and_dedup(blog_config)
                    try:
                        blog_stats = await _run_blog_download(
                            client, blog_name, blog_config, tracker, dedup
                        )
                    finally:
                        if tracker:
                            await tracker.close()

                    if len(blog_names) > 1:
                        logger.info(
                            "  %s: %d posts, %d downloaded",
                            blog_name,
                            blog_stats.posts_processed,
                            sum(blog_stats.downloaded.values()),
                        )

                    _merge_stats(total_stats, blog_stats)

                total_stats.api_calls = client.api_calls
                total_stats.rate_limit = client._rate_limit

        except ConfigError as exc:
            logger.error("%s", exc)
            return _EXIT_CONFIG
        except TumblrDlError as exc:
            logger.error("API error: %s", exc)
            return _EXIT_RUNTIME

        total_stats.elapsed_seconds = time.monotonic() - start_time
        logger.info("\n%s", total_stats.summary())
        return _EXIT_OK

    # Ad-hoc mode: blog names from CLI.
    blog_names_cli: list[str] = args.blog_names or []
    tag = overrides.get("tag") or (app_config.defaults.tag if app_config else None)

    if not tag and not blog_names_cli:
        logger.error("At least one blog_name is required unless --tag is specified.")
        return _EXIT_CONFIG

    start_time = time.monotonic()

    try:
        auth = load_auth(app_config)
        async with TumblrClient(auth) as client:
            total_stats = DownloadStats()

            if tag:
                # Tag mode: single tracker for the tag search.
                base_config = resolve_blog_config(
                    blog_names_cli[0] if blog_names_cli else None,
                    app_config,
                    overrides,
                )
                tracker, dedup = await _setup_tracker_and_dedup(base_config)
                try:
                    output_dir = Path(base_config.output_dir).expanduser()
                    output_dir.mkdir(parents=True, exist_ok=True)
                    exclude_patterns = _parse_exclude_patterns(base_config.exclude_tags)
                    exclude_blog_patterns = _parse_exclude_patterns(
                        base_config.exclude_blogs
                    )

                    logger.info(
                        "Starting tag search for '%s' to %s",
                        tag,
                        output_dir,
                    )
                    total_stats = await _download_tagged(
                        client=client,
                        tag=str(tag),
                        output_dir=output_dir,
                        dedup=dedup,
                        tracker=tracker,
                        max_posts=base_config.max_posts,
                        exclude_patterns=exclude_patterns,
                        exclude_blog_patterns=exclude_blog_patterns,
                    )
                finally:
                    if tracker:
                        await tracker.close()
            else:
                # Ad-hoc blog mode: per-blog tracker/dedup to match --sync.
                for i, blog_name in enumerate(blog_names_cli):
                    blog_config = resolve_blog_config(blog_name, app_config, overrides)

                    if len(blog_names_cli) > 1:
                        logger.info(
                            "--- Blog %d/%d: %s ---",
                            i + 1,
                            len(blog_names_cli),
                            blog_name,
                        )
                    logger.info(
                        "Starting download from %s to %s",
                        blog_name,
                        blog_config.output_dir,
                    )

                    tracker, dedup = await _setup_tracker_and_dedup(blog_config)
                    try:
                        blog_stats = await _run_blog_download(
                            client, blog_name, blog_config, tracker, dedup
                        )
                    finally:
                        if tracker:
                            await tracker.close()

                    if len(blog_names_cli) > 1:
                        logger.info(
                            "  %s: %d posts, %d downloaded",
                            blog_name,
                            blog_stats.posts_processed,
                            sum(blog_stats.downloaded.values()),
                        )

                    _merge_stats(total_stats, blog_stats)

            total_stats.api_calls = client.api_calls
            total_stats.rate_limit = client._rate_limit

    except ConfigError as exc:
        logger.error("%s", exc)
        return _EXIT_CONFIG
    except TumblrDlError as exc:
        logger.error("API error: %s", exc)
        return _EXIT_RUNTIME

    total_stats.elapsed_seconds = time.monotonic() - start_time
    logger.info("\n%s", total_stats.summary())
    return _EXIT_OK


def main() -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        logger.info("\nInterrupted. Partial progress has been saved.")
        sys.exit(130)


if __name__ == "__main__":
    main()
