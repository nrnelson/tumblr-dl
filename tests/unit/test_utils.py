"""Unit tests for utility functions."""

from __future__ import annotations

import pytest

from tumblr_dl.utils import sanitize_filename


@pytest.mark.parametrize(
    ("input_name", "expected"),
    [
        ("photo.jpg", "photo.jpg"),
        ('file<name>"bad.png', "file_name__bad.png"),
        ("hello world.jpg", "hello world.jpg"),
    ],
)
def test_sanitize_basic(input_name: str, expected: str) -> None:
    """Basic sanitization removes invalid characters."""
    assert sanitize_filename(input_name) == expected


def test_sanitize_empty_stem_uses_hash() -> None:
    """Empty stem after sanitization falls back to a hash."""
    result = sanitize_filename("?.jpg")
    assert result.endswith(".jpg")
    assert len(result) > 4  # hash prefix + .jpg


def test_sanitize_truncates_long_names() -> None:
    """Names longer than max_length are truncated."""
    long_name = "a" * 300 + ".jpg"
    result = sanitize_filename(long_name, max_length=50)
    stem = result.removesuffix(".jpg")
    assert len(stem) == 50


def test_sanitize_strips_trailing_dots() -> None:
    """Trailing dots are stripped (Windows silently removes them)."""
    # "photo...jpg" → stem="photo..", ext=".jpg" → rstrip(".") → "photo.jpg"
    assert sanitize_filename("photo...jpg") == "photo.jpg"
    # Dots embedded in the middle are fine
    assert sanitize_filename("photo.2024.01.jpg") == "photo.2024.01.jpg"
    # Pure trailing dots with no extension
    result = sanitize_filename("photo...")
    assert not result.endswith(".")


@pytest.mark.parametrize(
    "reserved",
    ["CON", "PRN", "AUX", "NUL", "COM0", "COM1", "COM9", "LPT0", "LPT1"],
)
def test_sanitize_windows_reserved_names(reserved: str) -> None:
    """Windows reserved device names get prefixed with underscore."""
    result = sanitize_filename(f"{reserved}.jpg")
    assert result == f"_{reserved}.jpg"


def test_sanitize_windows_reserved_case_insensitive() -> None:
    """Reserved name check is case-insensitive."""
    assert sanitize_filename("con.txt").startswith("_")
    assert sanitize_filename("Con.txt").startswith("_")
    assert sanitize_filename("CON.txt").startswith("_")


def test_sanitize_windows_reserved_prefix_not_triggered() -> None:
    """Names that start with a reserved name but are longer are fine."""
    # "CONSOLE.jpg" should NOT be prefixed — only exact stem matches matter
    result = sanitize_filename("CONSOLE.jpg")
    assert result == "CONSOLE.jpg"
