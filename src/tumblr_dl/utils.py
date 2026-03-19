"""Utility functions for tumblr-dl."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# Windows reserved device names (case-insensitive, with or without extension).
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }
)


def sanitize_filename(filename: str, max_length: int = 190) -> str:
    """Sanitize a filename by removing invalid characters and truncating.

    Falls back to a short hash if the filename stem is empty after
    sanitization (e.g. URL paths ending in ``/``).

    On Windows (or when the stem matches a Windows reserved device name),
    the stem is prefixed with ``_`` to avoid filesystem errors.

    Args:
        filename: The raw filename to sanitize.
        max_length: Maximum length for the name portion (excluding extension).

    Returns:
        A sanitized filename safe for all major platforms.
    """
    path = Path(filename)
    name = path.stem
    ext = path.suffix

    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    # Strip trailing dots/spaces — Windows silently strips these, causing
    # mismatched filenames and dedup failures.
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    name = name[:max_length]

    if not name:
        name = hashlib.sha256(filename.encode()).hexdigest()[:16]

    # Guard against Windows reserved device names (e.g. CON, NUL, COM1).
    # These are reserved regardless of extension, so CON.txt also fails.
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = f"_{name}"

    return name + ext
