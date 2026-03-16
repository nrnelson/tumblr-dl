"""Media URL extraction and post metadata parsing from Tumblr API data."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from bs4 import BeautifulSoup, Tag

from tumblr_dl.models import MediaItem, MediaType, PostMetadata, TrailEntry

logger = logging.getLogger(__name__)

# Return type for internal extractors: (url, media_type) pairs.
_RawMedia = tuple[str, MediaType]


def extract_media(
    post: dict[str, Any],
    blog_name: str,
    metadata: PostMetadata | None = None,
) -> list[MediaItem]:
    """Extract all media URLs from a single Tumblr post.

    Dispatches to a type-specific extractor based on the post's
    ``type`` field. Unknown post types are logged and skipped.

    Args:
        post: A post dict as returned by the Tumblr API.
        blog_name: The blog name (attached to each MediaItem).
        metadata: Optional pre-extracted post metadata to enrich items.

    Returns:
        List of MediaItem objects found in the post.
    """
    post_type = post.get("type", "unknown")
    post_id: int = post["id"]
    post_timestamp: int = post.get("timestamp", 0)

    extractors = {
        "photo": _extract_photo,
        "video": _extract_video,
        "audio": _extract_audio,
        "text": _extract_embedded_media,
        "answer": _extract_embedded_media,
    }

    extractor = extractors.get(post_type)
    if extractor is None:
        logger.debug(
            "Skipping post %d: unhandled type '%s'",
            post_id,
            post_type,
        )
        return []

    raw = extractor(post)
    if not raw:
        body = post.get("body", "")
        logger.debug(
            "No media found in %s post %d (body: %s)",
            post_type,
            post_id,
            body[:500] if body else "<empty>",
        )
        return []

    # Enrich with metadata if available.
    post_url = metadata.post_url if metadata else post.get("post_url", "")
    original_ts = metadata.original_post_timestamp if metadata else None
    content_labels = metadata.content_labels if metadata else []

    return [
        MediaItem(
            url=url,
            media_type=media_type,
            post_id=post_id,
            blog_name=blog_name,
            post_timestamp=post_timestamp,
            post_url=post_url,
            original_post_timestamp=original_ts,
            content_labels=content_labels,
        )
        for url, media_type in raw
    ]


def extract_post_metadata(
    post: dict[str, Any],
    blog_name: str,
) -> PostMetadata:
    """Extract per-post metadata (tags, trail, content labels).

    Args:
        post: A post dict as returned by the Tumblr API.
        blog_name: The blog name we fetched from.

    Returns:
        PostMetadata with tags, reblog trail, and content labels.
    """
    post_id: int = post["id"]
    post_timestamp: int = post.get("timestamp", 0)
    post_url: str = post.get("post_url", "")

    tags = [t.lower() for t in post.get("tags", [])]
    trail = _extract_trail(post)
    content_labels = _extract_content_labels(post)
    original_ts = trail[0].timestamp if trail else None

    return PostMetadata(
        blog_name=blog_name,
        post_id=post_id,
        post_url=post_url,
        post_timestamp=post_timestamp,
        tags=tags,
        trail=trail,
        content_labels=content_labels,
        original_post_timestamp=original_ts,
    )


def _extract_trail(post: dict[str, Any]) -> list[TrailEntry]:
    """Parse the reblog trail from a post response.

    The trail array is ordered oldest-first: index 0 is the original
    poster, and subsequent entries are rebloggers in chronological order.

    Args:
        post: A post dict that may contain a ``trail`` field.

    Returns:
        List of TrailEntry objects, empty if no trail.
    """
    trail_data = post.get("trail", [])
    if not isinstance(trail_data, list):
        return []

    entries: list[TrailEntry] = []
    for i, item in enumerate(trail_data):
        if not isinstance(item, dict):
            continue

        # Blog name can be in item["blog"]["name"] or item["broken_blog_name"].
        blog_info = item.get("blog", {})
        trail_blog = None
        if isinstance(blog_info, dict):
            trail_blog = blog_info.get("name")
        if trail_blog is None:
            trail_blog = item.get("broken_blog_name")

        # Post ID — may be nested under item["post"]["id"].
        trail_post_id = None
        post_info = item.get("post", {})
        if isinstance(post_info, dict):
            raw_id = post_info.get("id")
            if raw_id is not None:
                with contextlib.suppress(ValueError, TypeError):
                    trail_post_id = int(raw_id)

        # Timestamp — may be in item["post"]["timestamp"] or item["timestamp"].
        trail_ts = None
        if isinstance(post_info, dict):
            raw_ts = post_info.get("timestamp")
            if raw_ts is not None:
                with contextlib.suppress(ValueError, TypeError):
                    trail_ts = int(raw_ts)
        if trail_ts is None:
            raw_ts = item.get("timestamp")
            if raw_ts is not None:
                with contextlib.suppress(ValueError, TypeError):
                    trail_ts = int(raw_ts)

        entries.append(
            TrailEntry(
                position=i,
                blog_name=trail_blog,
                post_id=trail_post_id,
                timestamp=trail_ts,
                is_root=(i == 0),
            )
        )

    return entries


def _extract_content_labels(post: dict[str, Any]) -> list[str]:
    """Extract content classification labels from a post.

    Tumblr's Community Labels may appear under different field names
    depending on the API version. This checks known locations and
    logs unrecognized structures at DEBUG level.

    Args:
        post: A post dict from the API.

    Returns:
        List of lowercase label strings (e.g. ["mature", "sexual_themes"]).
    """
    labels: list[str] = []

    # Check "classification" field (object with category keys).
    classification = post.get("classification")
    if isinstance(classification, dict):
        for key, value in classification.items():
            if value:
                labels.append(key.lower().replace(" ", "_"))
    elif isinstance(classification, str) and classification:
        labels.append(classification.lower().replace(" ", "_"))

    # Check "content_rating" field.
    content_rating = post.get("content_rating")
    if isinstance(content_rating, str) and content_rating:
        rating = content_rating.lower().replace(" ", "_")
        if rating not in labels:
            labels.append(rating)

    # Check "community_labels" field (may be a list or object).
    community = post.get("community_labels")
    if isinstance(community, list):
        for item in community:
            if isinstance(item, str) and item:
                label = item.lower().replace(" ", "_")
                if label not in labels:
                    labels.append(label)
    elif isinstance(community, dict):
        for key, value in community.items():
            if value:
                label = key.lower().replace(" ", "_")
                if label not in labels:
                    labels.append(label)

    if not labels:
        # Log if we see unrecognized label-like fields for future discovery.
        for key in ("content_warning", "rating", "nsfw"):
            val = post.get(key)
            if val is not None:
                logger.debug(
                    "Post %d has potential label field '%s': %r",
                    post.get("id", 0),
                    key,
                    val,
                )

    return labels


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


def _extract_embedded_media(
    post: dict[str, Any],
) -> list[_RawMedia]:
    """Extract embedded media from text/answer post bodies.

    Handles:
    - ``<img src="...">`` tags (images)
    - ``<figure data-npf='{"type":"video","url":"..."}'>`` tags
      (reblogged videos stored as NPF JSON attributes)
    """
    body = post.get("body", "")
    if not body:
        return []

    soup = BeautifulSoup(body, "html.parser")
    items: list[_RawMedia] = []

    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            items.append((src, MediaType.IMAGE))

    for figure in soup.find_all("figure", attrs={"data-npf": True}):
        npf_raw = figure.get("data-npf", "")
        if not isinstance(npf_raw, str):
            continue
        try:
            npf = json.loads(npf_raw)
        except json.JSONDecodeError:
            continue
        if npf.get("type") == "video" and isinstance(npf.get("url"), str):
            items.append((npf["url"], MediaType.VIDEO))

    return items
