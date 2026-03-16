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
class MediaItem:
    """A single media URL extracted from a post."""

    url: str
    media_type: MediaType
    post_id: int
    blog_name: str


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
