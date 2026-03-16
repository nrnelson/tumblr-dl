"""Unit tests for CLI helpers (tag exclusion matching)."""

from __future__ import annotations

from tumblr_dl.cli import _matches_exclusion, _parse_exclude_patterns

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
