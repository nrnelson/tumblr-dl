"""Utility functions for tumblr-dl."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


def sanitize_filename(filename: str, max_length: int = 190) -> str:
    """Sanitize a filename by removing invalid characters and truncating.

    Falls back to a short hash if the filename stem is empty after
    sanitization (e.g. URL paths ending in ``/``).

    Args:
        filename: The raw filename to sanitize.
        max_length: Maximum length for the name portion (excluding extension).

    Returns:
        A sanitized filename safe for most filesystems.
    """
    path = Path(filename)
    name = path.stem
    ext = path.suffix

    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:max_length]

    if not name:
        name = hashlib.sha256(filename.encode()).hexdigest()[:16]

    return name + ext
