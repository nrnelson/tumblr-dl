"""Custom exception hierarchy for tumblr-dl."""

from __future__ import annotations

from typing import Any


class TumblrDlError(Exception):
    """Base exception for all tumblr-dl errors."""

    def __init__(
        self,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.context = context or {}


class ConfigError(TumblrDlError):
    """Raised when configuration is missing or invalid."""


class ApiError(TumblrDlError):
    """Raised when the Tumblr API returns an error."""


class DownloadError(TumblrDlError):
    """Raised when a media download fails."""
