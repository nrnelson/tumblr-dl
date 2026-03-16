"""Media URL extraction from Tumblr post data."""

from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup, Tag

from tumblr_dl.models import MediaItem, MediaType

logger = logging.getLogger(__name__)

# Return type for internal extractors: (url, media_type) pairs.
_RawMedia = tuple[str, MediaType]


def extract_media(post: dict[str, Any], blog_name: str) -> list[MediaItem]:
    """Extract all media URLs from a single Tumblr post.

    Dispatches to a type-specific extractor based on the post's
    ``type`` field. Unknown post types are logged and skipped.

    Args:
        post: A post dict as returned by the Tumblr API.
        blog_name: The blog name (attached to each MediaItem).

    Returns:
        List of MediaItem objects found in the post.
    """
    post_type = post.get("type", "unknown")
    post_id: int = post["id"]

    extractors = {
        "photo": _extract_photo,
        "video": _extract_video,
        "audio": _extract_audio,
        "text": _extract_embedded_images,
        "answer": _extract_embedded_images,
    }

    extractor = extractors.get(post_type)
    if extractor is None:
        logger.info(
            "Unhandled post type: %s (post %d)",
            post_type,
            post_id,
        )
        return []

    return [
        MediaItem(
            url=url,
            media_type=media_type,
            post_id=post_id,
            blog_name=blog_name,
        )
        for url, media_type in extractor(post)
    ]


def _extract_photo(
    post: dict[str, Any],
) -> list[_RawMedia]:
    """Extract image URLs from a photo post."""
    if "photos" in post:
        return [
            (photo["original_size"]["url"], MediaType.IMAGE) for photo in post["photos"]
        ]

    if "photo_url" in post:
        return [(post["photo_url"], MediaType.IMAGE)]

    logger.warning("No photo URL in photo post %d", post["id"])
    return []


def _extract_video(
    post: dict[str, Any],
) -> list[_RawMedia]:
    """Extract video URLs from a video post."""
    if "video_url" in post:
        return [(post["video_url"], MediaType.VIDEO)]

    if "player" in post and post["player"]:
        embed_code = post["player"][0].get("embed_code", "")
        if embed_code:
            soup = BeautifulSoup(embed_code, "html.parser")
            iframe = soup.find("iframe")
            if isinstance(iframe, Tag):
                src = iframe.get("src")
                if isinstance(src, str):
                    return [(src, MediaType.VIDEO)]

    logger.warning("No video URL in video post %d", post["id"])
    return []


def _extract_audio(
    post: dict[str, Any],
) -> list[_RawMedia]:
    """Extract audio URLs from an audio post."""
    if "audio_url" in post:
        return [(post["audio_url"], MediaType.AUDIO)]

    logger.warning("No audio URL in audio post %d", post["id"])
    return []


def _extract_embedded_images(
    post: dict[str, Any],
) -> list[_RawMedia]:
    """Extract embedded image URLs from text/answer post bodies."""
    body = post.get("body", "")
    if not body:
        return []

    soup = BeautifulSoup(body, "html.parser")
    return [
        (img["src"], MediaType.IMAGE) for img in soup.find_all("img") if img.get("src")
    ]
