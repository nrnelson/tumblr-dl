"""Unit tests for media extraction and post metadata parsing."""

from __future__ import annotations

from typing import Any

from tumblr_dl.extractors import extract_media, extract_post_metadata
from tumblr_dl.models import MediaType

_TUMBLR_MEDIA = "https://64.media.tumblr.com"


# --- extract_post_metadata: tags ---


def test_extract_tags_normalized_to_lowercase() -> None:
    """Tags are normalized to lowercase."""
    post: dict[str, Any] = {
        "id": 100,
        "type": "photo",
        "timestamp": 1700000000,
        "post_url": "https://example.tumblr.com/post/100",
        "tags": ["Art", "PHOTOGRAPHY", "landscape"],
    }
    metadata = extract_post_metadata(post, "example")
    assert metadata.tags == ["art", "photography", "landscape"]


def test_extract_tags_empty_when_missing() -> None:
    """Missing tags field results in empty list."""
    post: dict[str, Any] = {"id": 101, "type": "text", "timestamp": 0}
    metadata = extract_post_metadata(post, "example")
    assert metadata.tags == []


# --- extract_post_metadata: post_url ---


def test_extract_post_url() -> None:
    """post_url is extracted from the post."""
    post: dict[str, Any] = {
        "id": 200,
        "type": "photo",
        "timestamp": 1700000000,
        "post_url": "https://example.tumblr.com/post/200",
    }
    metadata = extract_post_metadata(post, "example")
    assert metadata.post_url == "https://example.tumblr.com/post/200"


def test_extract_post_url_missing() -> None:
    """Missing post_url defaults to empty string."""
    post: dict[str, Any] = {"id": 201, "type": "text", "timestamp": 0}
    metadata = extract_post_metadata(post, "example")
    assert metadata.post_url == ""


# --- extract_post_metadata: trail ---


def test_extract_trail_basic() -> None:
    """Reblog trail is parsed correctly with blog name and position."""
    post: dict[str, Any] = {
        "id": 300,
        "type": "photo",
        "timestamp": 1700000000,
        "trail": [
            {
                "blog": {"name": "originalblog"},
                "post": {"id": "99999", "timestamp": 1600000000},
            },
            {
                "blog": {"name": "reblogger1"},
                "post": {"id": "100001", "timestamp": 1650000000},
            },
        ],
    }
    metadata = extract_post_metadata(post, "currentblog")
    assert len(metadata.trail) == 2

    root = metadata.trail[0]
    assert root.position == 0
    assert root.blog_name == "originalblog"
    assert root.post_id == 99999
    assert root.timestamp == 1600000000
    assert root.is_root is True

    reblog = metadata.trail[1]
    assert reblog.position == 1
    assert reblog.blog_name == "reblogger1"
    assert reblog.post_id == 100001
    assert reblog.timestamp == 1650000000
    assert reblog.is_root is False


def test_extract_trail_sets_original_post_timestamp() -> None:
    """original_post_timestamp comes from the first trail entry."""
    post: dict[str, Any] = {
        "id": 301,
        "type": "photo",
        "timestamp": 1700000000,
        "trail": [
            {
                "blog": {"name": "originalblog"},
                "post": {"id": "88888", "timestamp": 1500000000},
            },
        ],
    }
    metadata = extract_post_metadata(post, "currentblog")
    assert metadata.original_post_timestamp == 1500000000


def test_extract_trail_empty_when_missing() -> None:
    """No trail field results in empty list and None original timestamp."""
    post: dict[str, Any] = {"id": 302, "type": "text", "timestamp": 0}
    metadata = extract_post_metadata(post, "example")
    assert metadata.trail == []
    assert metadata.original_post_timestamp is None


def test_extract_trail_broken_blog() -> None:
    """Trail entries with deleted blogs use broken_blog_name."""
    post: dict[str, Any] = {
        "id": 303,
        "type": "photo",
        "timestamp": 1700000000,
        "trail": [
            {
                "broken_blog_name": "deletedblog",
                "post": {"id": "77777"},
            },
        ],
    }
    metadata = extract_post_metadata(post, "currentblog")
    assert len(metadata.trail) == 1
    assert metadata.trail[0].blog_name == "deletedblog"
    assert metadata.trail[0].post_id == 77777


def test_extract_trail_missing_nested_fields() -> None:
    """Trail entries with missing nested fields are handled gracefully."""
    post: dict[str, Any] = {
        "id": 304,
        "type": "photo",
        "timestamp": 1700000000,
        "trail": [
            {},  # empty trail entry
            {"blog": {}, "post": {}},  # empty nested dicts
        ],
    }
    metadata = extract_post_metadata(post, "currentblog")
    assert len(metadata.trail) == 2
    assert metadata.trail[0].blog_name is None
    assert metadata.trail[0].post_id is None
    assert metadata.trail[0].timestamp is None


# --- extract_post_metadata: content labels ---


def test_extract_content_labels_classification_dict() -> None:
    """Content labels from classification dict."""
    post: dict[str, Any] = {
        "id": 400,
        "type": "photo",
        "timestamp": 0,
        "classification": {"mature": True, "sexual_themes": True, "violence": False},
    }
    metadata = extract_post_metadata(post, "example")
    assert "mature" in metadata.content_labels
    assert "sexual_themes" in metadata.content_labels
    assert "violence" not in metadata.content_labels


def test_extract_content_labels_classification_string() -> None:
    """Content labels from classification string."""
    post: dict[str, Any] = {
        "id": 401,
        "type": "photo",
        "timestamp": 0,
        "classification": "Mature",
    }
    metadata = extract_post_metadata(post, "example")
    assert "mature" in metadata.content_labels


def test_extract_content_labels_content_rating() -> None:
    """Content labels from content_rating field."""
    post: dict[str, Any] = {
        "id": 402,
        "type": "photo",
        "timestamp": 0,
        "content_rating": "adult",
    }
    metadata = extract_post_metadata(post, "example")
    assert "adult" in metadata.content_labels


def test_extract_content_labels_community_labels_list() -> None:
    """Content labels from community_labels list."""
    post: dict[str, Any] = {
        "id": 403,
        "type": "photo",
        "timestamp": 0,
        "community_labels": ["Sexual Themes", "Drug Use"],
    }
    metadata = extract_post_metadata(post, "example")
    assert "sexual_themes" in metadata.content_labels
    assert "drug_use" in metadata.content_labels


def test_extract_content_labels_empty_when_none() -> None:
    """No content labels when fields are missing."""
    post: dict[str, Any] = {"id": 404, "type": "text", "timestamp": 0}
    metadata = extract_post_metadata(post, "example")
    assert metadata.content_labels == []


# --- extract_media with metadata enrichment ---


def test_extract_media_enriched_with_metadata() -> None:
    """MediaItem gets post_url and original_post_timestamp from metadata."""
    post: dict[str, Any] = {
        "id": 500,
        "type": "photo",
        "timestamp": 1700000000,
        "post_url": "https://example.tumblr.com/post/500",
        "tags": ["test"],
        "trail": [
            {
                "blog": {"name": "original"},
                "post": {"id": "400", "timestamp": 1600000000},
            }
        ],
        "photos": [
            {
                "original_size": {
                    "url": f"{_TUMBLR_MEDIA}/pic.jpg",
                    "width": 800,
                    "height": 600,
                }
            }
        ],
    }
    metadata = extract_post_metadata(post, "example")
    items = extract_media(post, "example", metadata=metadata)
    assert len(items) == 1
    assert items[0].post_url == "https://example.tumblr.com/post/500"
    assert items[0].original_post_timestamp == 1600000000


def test_extract_media_without_metadata_uses_post_url() -> None:
    """Without metadata, extract_media falls back to post dict."""
    post: dict[str, Any] = {
        "id": 501,
        "type": "photo",
        "timestamp": 1700000000,
        "post_url": "https://example.tumblr.com/post/501",
        "photos": [
            {
                "original_size": {
                    "url": f"{_TUMBLR_MEDIA}/pic2.jpg",
                    "width": 800,
                    "height": 600,
                }
            }
        ],
    }
    items = extract_media(post, "example")
    assert len(items) == 1
    assert items[0].post_url == "https://example.tumblr.com/post/501"
    assert items[0].original_post_timestamp is None


# --- Existing extraction behavior preserved ---


def test_extract_photo_post(sample_photo_post: dict[str, Any]) -> None:
    """Photo post extraction still works."""
    items = extract_media(sample_photo_post, "testblog")
    assert len(items) == 1
    assert items[0].media_type is MediaType.IMAGE
    assert items[0].post_id == 12345


def test_extract_video_post(sample_video_post: dict[str, Any]) -> None:
    """Video post extraction still works."""
    items = extract_media(sample_video_post, "testblog")
    assert len(items) == 1
    assert items[0].media_type is MediaType.VIDEO


def test_extract_audio_post(sample_audio_post: dict[str, Any]) -> None:
    """Audio post extraction still works."""
    items = extract_media(sample_audio_post, "testblog")
    assert len(items) == 1
    assert items[0].media_type is MediaType.AUDIO


def test_extract_text_with_embedded_images(
    sample_text_post_with_images: dict[str, Any],
) -> None:
    """Text post embedded image extraction still works."""
    items = extract_media(sample_text_post_with_images, "testblog")
    assert len(items) == 1
    assert items[0].media_type is MediaType.IMAGE
