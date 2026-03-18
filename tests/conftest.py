"""Shared test fixtures for tumblr-dl."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tumblr_dl.tracker import DownloadTracker

_TUMBLR_MEDIA = "https://64.media.tumblr.com"


@pytest.fixture
async def tracker(tmp_path: Path) -> DownloadTracker:
    """Provide an open tracker with an in-memory-like temp DB."""
    db_path = tmp_path / ".tumblr-dl.db"
    t = DownloadTracker(db_path)
    await t.open()
    yield t  # type: ignore[misc]
    await t.close()


@pytest.fixture
def sample_photo_post() -> dict[str, Any]:
    """A minimal Tumblr photo post."""
    return {
        "id": 12345,
        "type": "photo",
        "photos": [
            {
                "original_size": {
                    "url": f"{_TUMBLR_MEDIA}/photo1.jpg",
                    "width": 1280,
                    "height": 720,
                }
            }
        ],
    }


@pytest.fixture
def sample_video_post() -> dict[str, Any]:
    """A minimal Tumblr video post with direct URL."""
    return {
        "id": 12346,
        "type": "video",
        "video_url": "https://va.media.tumblr.com/video1.mp4",
    }


@pytest.fixture
def sample_audio_post() -> dict[str, Any]:
    """A minimal Tumblr audio post."""
    return {
        "id": 12347,
        "type": "audio",
        "audio_url": "https://a.tumblr.com/audio1.mp3",
    }


@pytest.fixture
def sample_text_post_with_images() -> dict[str, Any]:
    """A Tumblr text post with embedded images."""
    return {
        "id": 12348,
        "type": "text",
        "body": (f'<p>Hello</p><img src="{_TUMBLR_MEDIA}/embedded.png"/>'),
    }


@pytest.fixture
def sample_answer_post_with_images() -> dict[str, Any]:
    """A Tumblr answer post with embedded images."""
    return {
        "id": 12349,
        "type": "answer",
        "body": (f'<img src="{_TUMBLR_MEDIA}/answer_img.jpg"/>'),
    }
