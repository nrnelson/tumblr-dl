"""File download logic with pluggable deduplication."""

from __future__ import annotations

import abc
import logging
from pathlib import Path
from urllib.parse import urlparse

import requests

from tumblr_dl.exceptions import DownloadError
from tumblr_dl.models import DownloadStatus, MediaItem
from tumblr_dl.utils import sanitize_filename

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.124 Safari/537.36"
)


class DedupStrategy(abc.ABC):
    """Interface for duplicate detection.

    Subclass this to implement filesystem-based or SQLite-based
    dedup.
    """

    @abc.abstractmethod
    def is_duplicate(self, item: MediaItem, dest: Path) -> bool:
        """Return True if the item should be skipped."""

    @abc.abstractmethod
    def record(
        self,
        item: MediaItem,
        dest: Path,
        status: DownloadStatus,
    ) -> None:
        """Record the result of a download attempt."""


class FilesystemDedup(DedupStrategy):
    """Skip files that already exist on disk."""

    def is_duplicate(self, item: MediaItem, dest: Path) -> bool:
        """Check if the destination file already exists."""
        return dest.exists()

    def record(
        self,
        item: MediaItem,
        dest: Path,
        status: DownloadStatus,
    ) -> None:
        """No-op for filesystem-based dedup."""


def _resolve_path(item: MediaItem, output_dir: Path) -> Path:
    """Determine the local file path for a media item."""
    raw_name = Path(urlparse(item.url).path).name
    safe_name = sanitize_filename(raw_name)
    return output_dir / safe_name


def download_item(
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

    if dedup.is_duplicate(item, dest):
        logger.debug("Skipping (exists): %s", item.url)
        return DownloadStatus.SKIPPED

    logger.info("Downloading: %s -> %s", item.url, dest.name)

    try:
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": (f"https://{item.blog_name}.tumblr.com/"),
        }
        response = requests.get(
            item.url,
            stream=True,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()

        with dest.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    fh.write(chunk)

        dedup.record(item, dest, DownloadStatus.SUCCESS)
        return DownloadStatus.SUCCESS

    except requests.HTTPError as exc:
        dedup.record(item, dest, DownloadStatus.FAILED)
        status_code = exc.response.status_code if exc.response is not None else None
        raise DownloadError(
            f"Download failed ({status_code}): {item.url}",
            context={
                "url": item.url,
                "post_id": item.post_id,
                "blog": item.blog_name,
                "status_code": status_code,
            },
        ) from exc

    except requests.RequestException as exc:
        dedup.record(item, dest, DownloadStatus.FAILED)
        raise DownloadError(
            f"Download failed: {item.url}",
            context={
                "url": item.url,
                "post_id": item.post_id,
                "blog": item.blog_name,
            },
        ) from exc
