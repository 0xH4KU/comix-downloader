"""Domain error types used across the application."""

from __future__ import annotations


class ComixError(RuntimeError):
    """Base class for user-meaningful application errors."""


class ConfigurationError(ComixError):
    """Raised when runtime configuration is invalid or inconsistent."""


class CloudflareChallengeError(ComixError):
    """Raised when Cloudflare clearance cannot be recovered automatically."""


class RemoteApiError(ComixError):
    """Raised when comix.to API access fails in a user-meaningful way."""


class PartialDownloadError(ComixError):
    """Raised when a chapter download completed only partially."""


class ConversionError(ComixError):
    """Raised when archive or PDF conversion cannot produce a valid output."""
