"""Data models and enums for tumblr-dl."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class DownloadStatus(enum.Enum):
    """Result of a download attempt."""

    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


class MediaType(enum.Enum):
    """Type of media extracted from a post."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


def _zero_counts() -> dict[MediaType, int]:
    return dict.fromkeys(MediaType, 0)


@dataclass
class TrailEntry:
    """A single entry in a reblog trail."""

    position: int
    blog_name: str | None
    post_id: int | None
    timestamp: int | None
    is_root: bool


@dataclass
class PostMetadata:
    """Per-post metadata extracted from the API response.

    Captures tags, reblog trail, content labels, and timestamps
    that apply to the post as a whole (not per-media-item).
    """

    blog_name: str
    post_id: int
    post_url: str
    post_timestamp: int
    tags: list[str] = field(default_factory=list)
    trail: list[TrailEntry] = field(default_factory=list)
    content_labels: list[str] = field(default_factory=list)
    original_post_timestamp: int | None = None


@dataclass
class MediaItem:
    """A single media URL extracted from a post."""

    url: str
    media_type: MediaType
    post_id: int
    blog_name: str
    post_timestamp: int = 0
    post_url: str = ""
    original_post_timestamp: int | None = None
    content_labels: list[str] = field(default_factory=list)


@dataclass
class BlogState:
    """Persisted state for a blog's download cursor."""

    blog_name: str
    highest_post_id: int
    newest_timestamp: int
    total_posts_seen: int
    last_run_at: str


@dataclass
class DownloadStats:
    """Aggregated download statistics."""

    found: dict[MediaType, int] = field(
        default_factory=_zero_counts,
    )
    downloaded: dict[MediaType, int] = field(
        default_factory=_zero_counts,
    )
    skipped: dict[MediaType, int] = field(
        default_factory=_zero_counts,
    )
    failed: dict[MediaType, int] = field(
        default_factory=_zero_counts,
    )
    posts_processed: int = 0
    api_calls: int = 0
    elapsed_seconds: float = 0.0
    rate_limit: int = 300
    early_stopped: bool = False
    early_stop_post_id: int = 0

    def record(
        self,
        media_type: MediaType,
        status: DownloadStatus,
    ) -> None:
        """Record the result of a download attempt."""
        self.found[media_type] += 1
        if status is DownloadStatus.SUCCESS:
            self.downloaded[media_type] += 1
        elif status is DownloadStatus.SKIPPED:
            self.skipped[media_type] += 1
        elif status is DownloadStatus.FAILED:
            self.failed[media_type] += 1

    def summary(self) -> str:
        """Return a human-readable summary of download stats."""
        lines = ["--- File Type Summary ---"]
        for media_type in MediaType:
            found = self.found[media_type]
            got = self.downloaded[media_type]
            skip = self.skipped[media_type]
            fail = self.failed[media_type]
            label = media_type.value.capitalize()
            lines.append(
                f"  {label}: {found} found, "
                f"{got} downloaded, {skip} skipped, "
                f"{fail} failed"
            )

        total = sum(self.downloaded.values())
        lines.append(
            f"Total: {self.posts_processed} posts processed, {total} files downloaded"
        )
        if self.early_stopped:
            lines.append(f"  (stopped early at known post {self.early_stop_post_id})")

        # API and timing stats.
        lines.append("")
        lines.append("--- API Stats ---")

        minutes = self.elapsed_seconds / 60.0
        if minutes >= 1.0:
            elapsed_str = f"{minutes:.1f}m"
        else:
            elapsed_str = f"{self.elapsed_seconds:.1f}s"
        lines.append(f"  Run time: {elapsed_str}")
        lines.append(f"  API calls: {self.api_calls}")

        if self.elapsed_seconds > 0:
            calls_per_min = self.api_calls / (self.elapsed_seconds / 60.0)
            utilization = (calls_per_min / self.rate_limit) * 100
            lines.append(f"  API calls/min: {calls_per_min:.1f}")
            lines.append(
                f"  Rate limit utilization: {utilization:.1f}% "
                f"(limit: {self.rate_limit}/min)"
            )

        return "\n".join(lines)
