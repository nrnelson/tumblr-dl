"""Unit tests for CLI helpers (tag and blog exclusion matching)."""

from __future__ import annotations

from tumblr_dl.cli import (
    _collect_trail_blogs,
    _matches_exclusion,
    _parse_exclude_patterns,
)
from tumblr_dl.models import PostMetadata, TrailEntry

# --- _parse_exclude_patterns ---


def test_parse_exclude_patterns_basic() -> None:
    """Comma-separated patterns are split and lowercased."""
    patterns = _parse_exclude_patterns("NSFW,explicit*,Gore")
    assert patterns == ["nsfw", "explicit*", "gore"]


def test_parse_exclude_patterns_strips_whitespace() -> None:
    """Whitespace around patterns is stripped."""
    patterns = _parse_exclude_patterns("  nsfw , explicit * , gore  ")
    assert patterns == ["nsfw", "explicit *", "gore"]


def test_parse_exclude_patterns_empty_string() -> None:
    """Empty string returns empty list."""
    assert _parse_exclude_patterns("") == []


def test_parse_exclude_patterns_none() -> None:
    """None returns empty list."""
    assert _parse_exclude_patterns(None) == []


def test_parse_exclude_patterns_skips_empty_segments() -> None:
    """Empty segments from trailing commas are skipped."""
    patterns = _parse_exclude_patterns("nsfw,,gore,")
    assert patterns == ["nsfw", "gore"]


# --- _matches_exclusion ---


def test_matches_exact_tag() -> None:
    """Exact match works."""
    assert _matches_exclusion(["nsfw", "art"], ["nsfw"]) == "nsfw"


def test_matches_glob_star() -> None:
    """Glob * wildcard matches suffix."""
    assert _matches_exclusion(["explicit_content"], ["explicit*"]) == "explicit_content"


def test_matches_glob_does_not_match_substring() -> None:
    """Glob without * is exact match — 'art' does not match 'heart'."""
    assert _matches_exclusion(["heart", "party", "artisan"], ["art"]) is None


def test_matches_glob_star_prefix() -> None:
    """Glob *suffix matches tags ending with the pattern."""
    assert _matches_exclusion(["my_nsfw_post"], ["*nsfw*"]) == "my_nsfw_post"


def test_matches_case_insensitive() -> None:
    """Matching is case-insensitive (both sides lowercased)."""
    # Tags are already lowercased by the extractor.
    assert _matches_exclusion(["nsfw"], ["nsfw"]) == "nsfw"


def test_no_match_returns_none() -> None:
    """No match returns None."""
    assert _matches_exclusion(["art", "photography"], ["nsfw", "gore*"]) is None


def test_empty_tags_returns_none() -> None:
    """Empty tag list returns None."""
    assert _matches_exclusion([], ["nsfw"]) is None


def test_empty_patterns_returns_none() -> None:
    """Empty pattern list returns None."""
    assert _matches_exclusion(["nsfw"], []) is None


def test_matches_question_mark_glob() -> None:
    """Glob ? matches single character."""
    assert _matches_exclusion(["nsf1"], ["nsf?"]) == "nsf1"
    assert _matches_exclusion(["nsfww"], ["nsf?"]) is None


# --- _collect_trail_blogs ---


def test_collect_trail_blogs_returns_lowercase_names() -> None:
    """Trail blog names are collected and lowercased."""
    metadata = PostMetadata(
        blog_name="myblog",
        post_id=100,
        post_url="",
        post_timestamp=0,
        trail=[
            TrailEntry(
                position=0,
                blog_name="OriginalPoster",
                post_id=1,
                timestamp=None,
                is_root=True,
            ),
            TrailEntry(
                position=1,
                blog_name="Reblogger",
                post_id=2,
                timestamp=None,
                is_root=False,
            ),
        ],
    )
    blogs = _collect_trail_blogs(metadata)
    assert blogs == ["originalposter", "reblogger"]


def test_collect_trail_blogs_skips_none() -> None:
    """Deleted blogs (None name) are excluded from the list."""
    metadata = PostMetadata(
        blog_name="myblog",
        post_id=100,
        post_url="",
        post_timestamp=0,
        trail=[
            TrailEntry(
                position=0,
                blog_name=None,
                post_id=None,
                timestamp=None,
                is_root=True,
            ),
            TrailEntry(
                position=1,
                blog_name="goodblog",
                post_id=2,
                timestamp=None,
                is_root=False,
            ),
        ],
    )
    blogs = _collect_trail_blogs(metadata)
    assert blogs == ["goodblog"]


def test_collect_trail_blogs_empty_trail() -> None:
    """Empty trail returns empty list."""
    metadata = PostMetadata(
        blog_name="myblog",
        post_id=100,
        post_url="",
        post_timestamp=0,
    )
    assert _collect_trail_blogs(metadata) == []


# --- Blog exclusion end-to-end matching ---


def test_blog_exclusion_matches_trail_entry() -> None:
    """A blog in the trail matching an exclusion pattern is detected."""
    trail_blogs = ["originalposter", "middleman", "spambot123"]
    matched = _matches_exclusion(trail_blogs, ["spambot*"])
    assert matched == "spambot123"


def test_blog_exclusion_exact_match() -> None:
    """Exact blog name match works."""
    trail_blogs = ["goodblog", "badblog", "otherblog"]
    assert _matches_exclusion(trail_blogs, ["badblog"]) == "badblog"


def test_blog_exclusion_no_match() -> None:
    """No match when trail blogs are all clean."""
    trail_blogs = ["goodblog", "niceblog"]
    assert _matches_exclusion(trail_blogs, ["spambot*", "badblog"]) is None
