"""Async file download logic with pluggable deduplication."""

from __future__ import annotations

import abc
import asyncio
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
from curl_cffi.requests import AsyncSession

from tumblr_dl.exceptions import DownloadError
from tumblr_dl.models import DownloadStatus, MediaItem
from tumblr_dl.tracker import DownloadTracker
from tumblr_dl.utils import sanitize_filename

logger = logging.getLogger(__name__)

# Media download retry: short backoff for transient CDN failures (HTTP 0,
# connection resets). Shorter delays than API retry since these are CDN
# fetches, not rate-limited API calls.
_DL_RETRY_ATTEMPTS = 3
_DL_RETRY_BASE_DELAY = 5.0
_DL_RETRY_MAX_DELAY = 30.0

# Manual User-Agent string. curl_cffi's impersonate feature triggers Tumblr's
# CDN to serve HTML error pages instead of media — the browser TLS fingerprints
# are actively blocked. curl_cffi's *default* TLS stack (non-impersonated) is
# accepted, so we pair it with a conventional UA string.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
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
        return await asyncio.to_thread(dest.exists)

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
        return await asyncio.to_thread(dest.exists)

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
        if status is DownloadStatus.SUCCESS and await asyncio.to_thread(dest.exists):
            stat_result = await asyncio.to_thread(dest.stat)
            file_size = stat_result.st_size

        content_labels_str: str | None = None
        if item.content_labels:
            content_labels_str = ",".join(item.content_labels)

        await tracker.record_download(
            blog_name=item.blog_name,
            post_id=item.post_id,
            url=item.url,
            file_path=dest.name,
            media_type=item.media_type.value,
            status=status.value,
            file_size=file_size,
            post_url=item.post_url or None,  # "" → NULL in SQLite
            post_timestamp=item.post_timestamp or None,  # 0 → NULL in SQLite
            original_post_timestamp=item.original_post_timestamp,
            content_labels=content_labels_str,
        )


def _resolve_path(item: MediaItem, output_dir: Path) -> Path:
    """Determine the local file path for a media item.

    On Windows, truncates the filename further if the full path would
    exceed 260 characters (MAX_PATH), leaving room for the ``.part``
    suffix used during downloads.
    """
    raw_name = Path(urlparse(item.url).path).name
    safe_name = sanitize_filename(raw_name)
    dest = output_dir / safe_name

    # Windows MAX_PATH is 260 (including NUL terminator → 259 usable).
    # Reserve 5 extra chars for the ".part" temp suffix.
    if sys.platform == "win32":
        max_path = 259
        part_suffix_len = 5  # len(".part")
        full_len = len(str(dest.resolve())) + part_suffix_len
        if full_len > max_path:
            overflow = full_len - max_path
            stem = dest.stem
            ext = dest.suffix
            truncated_stem = stem[: len(stem) - overflow]
            if not truncated_stem:
                truncated_stem = sanitize_filename(raw_name, max_length=8)
                truncated_stem = Path(truncated_stem).stem
            dest = output_dir / (truncated_stem + ext)

    return dest


async def _async_download(url: str, dest: Path, blog_name: str) -> int:
    """Download a file using native async HTTP with streaming.

    Uses a fresh session per request to avoid curl_cffi connection-pool
    issues (stale keep-alive connections cause IncompleteRead / ConnectionError
    on Tumblr's CDN).

    Retries on transient connection failures (HTTP 0, connection resets,
    timeouts) with exponential backoff. Content-level errors (HTML
    responses, empty files, HTTP 4xx) are not retried.

    Streams the response to a temporary file, then renames on success.

    Returns:
        Total bytes written to disk.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": f"https://{blog_name}.tumblr.com/",
    }
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_exc: Exception | None = None

    for attempt in range(_DL_RETRY_ATTEMPTS):
        try:
            # Fresh session per download — curl_cffi's shared session reuses
            # keep-alive connections that Tumblr's CDN resets mid-transfer.
            async with (
                AsyncSession() as session,
                session.stream(
                    "GET", url, headers=headers, timeout=(30, 300)
                ) as response,
            ):
                # Check for HTTP errors. Non-retryable client errors (4xx)
                # are raised immediately; server errors (5xx) are retried.
                status_code = response.status_code
                if status_code >= 400:
                    if status_code < 500:
                        raise DownloadError(
                            f"HTTP {status_code}: {url}",
                            context={
                                "url": url,
                                "status_code": status_code,
                            },
                        )
                    raise _TransientError(
                        f"HTTP {status_code}", status_code=status_code
                    )

                # Reject HTML error pages served with 200 status.
                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type:
                    raise DownloadError(
                        f"Server returned HTML instead of media: {url}",
                        context={"url": url, "content_type": content_type},
                    )

                total_bytes = 0
                async with aiofiles.open(tmp, "wb") as f:
                    async for chunk in response.aiter_content():
                        await f.write(chunk)
                        total_bytes += len(chunk)

            # Validate against Content-Length to detect truncated downloads.
            expected = response.headers.get("content-length")
            if expected is not None and total_bytes != int(expected):
                raise _TransientError(
                    f"Incomplete: {total_bytes}/{expected} bytes",
                    status_code=0,
                )

            logger.debug(
                "Downloaded %d bytes (content-type: %s) for %s: %s",
                total_bytes,
                content_type,
                blog_name,
                url,
            )
            if total_bytes == 0:
                raise DownloadError(
                    f"Downloaded file is empty (0 bytes): {url}",
                    context={"url": url},
                )

            await asyncio.to_thread(tmp.rename, dest)
            return total_bytes

        except (DownloadError, KeyboardInterrupt, SystemExit):
            # Non-retryable: content errors, user interrupt.
            tmp.unlink(missing_ok=True)
            raise

        except (_TransientError, ConnectionError, OSError) as exc:
            # Retryable: connection resets, timeouts, HTTP 5xx, truncated.
            tmp.unlink(missing_ok=True)
            delay = min(
                _DL_RETRY_BASE_DELAY * (2**attempt), _DL_RETRY_MAX_DELAY
            )
            last_exc = exc
            if attempt + 1 < _DL_RETRY_ATTEMPTS:
                logger.debug(
                    "Download failed (%s), retrying in %.0fs "
                    "(attempt %d/%d): %s",
                    exc,
                    delay,
                    attempt + 1,
                    _DL_RETRY_ATTEMPTS,
                    url,
                )
                await asyncio.sleep(delay)
            # else: fall through to raise after loop

        except BaseException:
            # Unexpected errors: clean up .part file and propagate.
            tmp.unlink(missing_ok=True)
            raise

    # All retries exhausted.
    tmp.unlink(missing_ok=True)
    raise DownloadError(
        f"Download failed after {_DL_RETRY_ATTEMPTS} attempts: {url}",
        context={"url": url, "last_error": str(last_exc)},
    )


class _TransientError(Exception):
    """Internal marker for retryable download failures."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


async def download_item(
    item: MediaItem,
    output_dir: Path,
    dedup: DedupStrategy,
) -> tuple[DownloadStatus, int]:
    """Download a single media item to disk.

    Args:
        item: The media item to download.
        output_dir: Directory to save files into.
        dedup: Strategy for duplicate detection.

    Returns:
        Tuple of (status, bytes_downloaded). Bytes is 0 for skipped/failed.

    Raises:
        DownloadError: If the HTTP request fails.
    """
    dest = _resolve_path(item, output_dir)

    if await dedup.is_duplicate(item, dest):
        logger.debug("Skipping (exists): %s", item.url)
        return DownloadStatus.SKIPPED, 0

    logger.info("Downloading: %s -> %s", item.url, dest.name)

    try:
        byte_count = await _async_download(item.url, dest, item.blog_name)

        await dedup.record(item, dest, DownloadStatus.SUCCESS)
        return DownloadStatus.SUCCESS, byte_count

    except DownloadError:
        await dedup.record(item, dest, DownloadStatus.FAILED)
        raise  # byte_count is irrelevant; caller catches and records FAILED

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
