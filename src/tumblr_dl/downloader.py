"""Async file download logic with pluggable deduplication."""

from __future__ import annotations

import abc
import logging
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

from tumblr_dl.exceptions import DownloadError
from tumblr_dl.models import DownloadStatus, MediaItem
from tumblr_dl.tracker import DownloadTracker
from tumblr_dl.utils import sanitize_filename

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


class DedupStrategy(abc.ABC):
    """Interface for duplicate detection.

    Subclass this to implement filesystem-based or SQLite-based
    dedup.
    """

    @abc.abstractmethod
    async def is_duplicate(self, item: MediaItem, dest: Path) -> bool:
        """Return True if the item should be skipped."""

    @abc.abstractmethod
    async def record(
        self,
        item: MediaItem,
        dest: Path,
        status: DownloadStatus,
    ) -> None:
        """Record the result of a download attempt."""


class FilesystemDedup(DedupStrategy):
    """Skip files that already exist on disk."""

    async def is_duplicate(self, item: MediaItem, dest: Path) -> bool:
        """Check if the destination file already exists."""
        return dest.exists()

    async def record(
        self,
        item: MediaItem,
        dest: Path,
        status: DownloadStatus,
    ) -> None:
        """No-op for filesystem-based dedup."""


class SqliteDedup(DedupStrategy):
    """Dedup using a SQLite download tracker, with filesystem fallback.

    Args:
        tracker: An open DownloadTracker instance.
    """

    def __init__(self, tracker: DownloadTracker) -> None:
        self._tracker = tracker

    async def is_duplicate(self, item: MediaItem, dest: Path) -> bool:
        """Check DB first, then filesystem as fallback."""
        if await self._tracker.is_downloaded(item.blog_name, item.url):
            return True
        return dest.exists()

    async def record(
        self,
        item: MediaItem,
        dest: Path,
        status: DownloadStatus,
    ) -> None:
        """Record download result in the database."""
        if status is DownloadStatus.SKIPPED:
            return
        tracker = self._tracker
        file_size: int | None = None
        if status is DownloadStatus.SUCCESS and dest.exists():
            file_size = dest.stat().st_size

        content_labels_str: str | None = None
        if item.content_labels:
            content_labels_str = ",".join(item.content_labels)

        await tracker.record_download(
            blog_name=item.blog_name,
            post_id=item.post_id,
            url=item.url,
            file_path=str(dest),
            media_type=item.media_type.value,
            status=status.value,
            file_size=file_size,
            post_url=item.post_url or None,
            post_timestamp=item.post_timestamp or None,
            original_post_timestamp=item.original_post_timestamp,
            content_labels=content_labels_str,
        )


def _resolve_path(item: MediaItem, output_dir: Path) -> Path:
    """Determine the local file path for a media item.

    Prefixes with post_id to prevent filename collisions across posts
    (Tumblr frequently reuses generic basenames like ``tumblr_abc123.jpg``).
    """
    raw_name = Path(urlparse(item.url).path).name
    safe_name = sanitize_filename(raw_name)
    return output_dir / f"{item.post_id}_{safe_name}"


async def _async_download(url: str, dest: Path, blog_name: str) -> None:
    """Download a file using native async HTTP.

    Uses a fresh session per request to avoid curl_cffi connection-pool
    issues (stale keep-alive connections cause IncompleteRead / ConnectionError
    on Tumblr's CDN).

    Writes to a temporary file first, then renames on success.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": f"https://{blog_name}.tumblr.com/",
    }
    # Fresh session per download — curl_cffi's shared session reuses
    # keep-alive connections that Tumblr's CDN resets mid-transfer.
    async with AsyncSession() as session:
        response = await session.get(url, headers=headers, timeout=(30, 300))
    response.raise_for_status()

    # Reject HTML error pages served with 200 status
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        raise DownloadError(
            f"Server returned HTML instead of media: {url}",
            context={"url": url, "content_type": content_type},
        )

    # Loads full response into memory; curl_cffi async doesn't support streaming.
    data = response.content
    logger.debug(
        "Downloaded %d bytes (content-type: %s): %s", len(data), content_type, url
    )
    if not data:
        raise DownloadError(
            f"Downloaded file is empty (0 bytes): {url}",
            context={"url": url},
        )

    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        tmp.write_bytes(data)
        tmp.rename(dest)
    except BaseException:
        # Catch BaseException (not just Exception) to ensure the .part
        # file is cleaned up on KeyboardInterrupt and SystemExit too.
        tmp.unlink(missing_ok=True)
        raise


async def download_item(
    item: MediaItem,
    output_dir: Path,
    dedup: DedupStrategy,
) -> DownloadStatus:
    """Download a single media item to disk.

    Args:
        item: The media item to download.
        output_dir: Directory to save files into.
        dedup: Strategy for duplicate detection.

    Returns:
        The resulting DownloadStatus.

    Raises:
        DownloadError: If the HTTP request fails.
    """
    dest = _resolve_path(item, output_dir)

    if await dedup.is_duplicate(item, dest):
        logger.debug("Skipping (exists): %s", item.url)
        return DownloadStatus.SKIPPED

    logger.info("Downloading: %s -> %s", item.url, dest.name)

    try:
        await _async_download(item.url, dest, item.blog_name)

        await dedup.record(item, dest, DownloadStatus.SUCCESS)
        return DownloadStatus.SUCCESS

    except DownloadError:
        await dedup.record(item, dest, DownloadStatus.FAILED)
        raise

    except Exception as exc:
        await dedup.record(item, dest, DownloadStatus.FAILED)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        exc_type = type(exc).__name__
        if status_code is not None:
            raise DownloadError(
                f"Download failed ({exc_type}, HTTP {status_code}): {item.url}",
                context={
                    "url": item.url,
                    "post_id": item.post_id,
                    "blog": item.blog_name,
                    "status_code": status_code,
                    "error_type": exc_type,
                },
            ) from exc
        raise DownloadError(
            f"Download failed ({exc_type}): {item.url}",
            context={
                "url": item.url,
                "post_id": item.post_id,
                "blog": item.blog_name,
                "error_type": exc_type,
            },
        ) from exc
