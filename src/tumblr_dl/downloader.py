"""Async file download logic with pluggable deduplication."""

from __future__ import annotations

import abc
import logging
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

from tumblr_dl.exceptions import DownloadError
from tumblr_dl.models import DownloadStatus, MediaItem
from tumblr_dl.utils import sanitize_filename

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.124 Safari/537.36"
)

# Shared session for all downloads within a process.
_session: AsyncSession | None = None  # type: ignore[type-arg]


def _get_session() -> AsyncSession:  # type: ignore[type-arg]
    """Return the shared download session, creating it if needed."""
    global _session  # noqa: PLW0603
    if _session is None:
        _session = AsyncSession()
    return _session


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

    def __init__(self, tracker: object) -> None:
        # Import here to avoid circular dependency; runtime type is DownloadTracker.
        self._tracker = tracker

    async def is_duplicate(self, item: MediaItem, dest: Path) -> bool:
        """Check DB first, then filesystem as fallback."""
        from tumblr_dl.tracker import DownloadTracker

        tracker: DownloadTracker = self._tracker  # type: ignore[assignment]
        if await tracker.is_downloaded(item.blog_name, item.url):
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
        from tumblr_dl.tracker import DownloadTracker

        tracker: DownloadTracker = self._tracker  # type: ignore[assignment]
        file_size: int | None = None
        if status is DownloadStatus.SUCCESS and dest.exists():
            file_size = dest.stat().st_size
        await tracker.record_download(
            blog_name=item.blog_name,
            post_id=item.post_id,
            url=item.url,
            file_path=str(dest),
            media_type=item.media_type.value,
            status=status.value,
            file_size=file_size,
        )


def _resolve_path(item: MediaItem, output_dir: Path) -> Path:
    """Determine the local file path for a media item."""
    raw_name = Path(urlparse(item.url).path).name
    safe_name = sanitize_filename(raw_name)
    return output_dir / safe_name


async def _async_download(url: str, dest: Path, blog_name: str) -> None:
    """Download a file using native async HTTP.

    Writes to a temporary file first, then renames on success.
    This prevents 0-byte files from being left behind on failure.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": f"https://{blog_name}.tumblr.com/",
    }
    session = _get_session()
    response = await session.get(url, headers=headers, stream=True, timeout=30)
    response.raise_for_status()

    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with tmp.open("wb") as fh:
            async for chunk in response.aiter_content():
                if chunk:
                    fh.write(chunk)
        tmp.rename(dest)
    except BaseException:
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

    except Exception as exc:
        await dedup.record(item, dest, DownloadStatus.FAILED)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code is not None:
            raise DownloadError(
                f"Download failed ({status_code}): {item.url}",
                context={
                    "url": item.url,
                    "post_id": item.post_id,
                    "blog": item.blog_name,
                    "status_code": status_code,
                },
            ) from exc
        raise DownloadError(
            f"Download failed: {item.url}",
            context={
                "url": item.url,
                "post_id": item.post_id,
                "blog": item.blog_name,
            },
        ) from exc
