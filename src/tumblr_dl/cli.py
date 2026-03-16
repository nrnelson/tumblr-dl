"""CLI entry point for tumblr-dl."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from tumblr_dl.client import TumblrClient
from tumblr_dl.downloader import (
    DedupStrategy,
    FilesystemDedup,
    download_item,
)
from tumblr_dl.exceptions import (
    ConfigError,
    DownloadError,
    TumblrDlError,
)
from tumblr_dl.extractors import extract_media
from tumblr_dl.models import DownloadStats, DownloadStatus

logger = logging.getLogger(__name__)

_BATCH_SIZE = 20
_BATCH_DELAY_SECONDS = 1

# Exit codes
_EXIT_OK = 0
_EXIT_CONFIG = 2
_EXIT_RUNTIME = 3


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="tumblr-dl",
        description="Download media from a Tumblr blog.",
    )
    parser.add_argument(
        "blog_name",
        help="The Tumblr blog name (e.g. 'example')",
    )
    parser.add_argument(
        "output_dir",
        help="Directory to save downloaded media",
    )
    parser.add_argument(
        "--config",
        default="~/.tumblr",
        help=("Path to YAML OAuth config file (default: ~/.tumblr)"),
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
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def _configure_logging(debug: bool) -> None:
    """Configure logging level and format."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
    )


def _download_blog(
    client: TumblrClient,
    blog_name: str,
    output_dir: Path,
    dedup: DedupStrategy,
    start_offset: int = 0,
    max_posts: int | None = None,
) -> DownloadStats:
    """Paginate through a blog and download all media.

    Args:
        client: Authenticated Tumblr API client.
        blog_name: Blog to download from.
        output_dir: Directory to save files into.
        dedup: Duplicate detection strategy.
        start_offset: Post offset to begin at.
        max_posts: Stop after processing this many posts.

    Returns:
        Accumulated download statistics.

    Raises:
        TumblrDlError: On unrecoverable API errors.
    """
    stats = DownloadStats()
    offset = start_offset

    while True:
        posts = client.get_posts(blog_name, offset=offset, limit=_BATCH_SIZE)
        if not posts:
            logger.info("No more posts found.")
            break

        for post in posts:
            stats.posts_processed += 1
            logger.info(
                "Processing post %d (ID: %s)...",
                stats.posts_processed,
                post.get("id"),
            )

            for item in extract_media(post, blog_name):
                try:
                    status = download_item(item, output_dir, dedup)
                except DownloadError as exc:
                    logger.warning("%s", exc)
                    status = DownloadStatus.FAILED
                stats.record(item.media_type, status)

            if max_posts and stats.posts_processed >= max_posts:
                logger.info(
                    "Reached maximum posts (%d). Stopping.",
                    max_posts,
                )
                return stats

        offset += _BATCH_SIZE
        time.sleep(_BATCH_DELAY_SECONDS)

    return stats


def _run(args: argparse.Namespace) -> int:
    """Execute the download workflow. Returns exit code."""
    _configure_logging(args.debug)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = TumblrClient(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return _EXIT_CONFIG

    logger.info(
        "Starting download from %s to %s",
        args.blog_name,
        output_dir,
    )

    try:
        stats = _download_blog(
            client=client,
            blog_name=args.blog_name,
            output_dir=output_dir,
            dedup=FilesystemDedup(),
            start_offset=args.start_post,
            max_posts=args.max_posts,
        )
    except TumblrDlError as exc:
        logger.error("API error: %s", exc)
        return _EXIT_RUNTIME

    logger.info("\n%s", stats.summary())
    return _EXIT_OK


def main() -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(_run(args))


if __name__ == "__main__":
    main()
