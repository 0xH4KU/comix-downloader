"""Domain error types used across the application."""

from __future__ import annotations


class ComixError(RuntimeError):
    """Base class for user-meaningful application errors."""


class ConfigurationError(ComixError):
    """Raised when runtime configuration is invalid or inconsistent."""


class CloudflareChallengeError(ComixError):
    """Raised when Cloudflare clearance cannot be recovered automatically."""


class RemoteApiError(ComixError):
    """Raised when remote site API access fails in a user-meaningful way."""


class SchemaMismatchError(RemoteApiError):
    """Raised when remote API response shape does not match adapter expectations.

    Reserved primarily for site adapters performing JSON schema validation
    after the framework hands off raw payloads.
    """


class PartialDownloadError(ComixError):
    """Raised when a chapter download completed only partially."""


class ConversionError(ComixError):
    """Raised when archive or PDF conversion cannot produce a valid output."""


class Http403Error(ComixError):
    """Raised when an HTTP request is rejected with 403 / Forbidden.

    Typically indicates that Cloudflare clearance has expired or that the
    current session has lost authorization. Callers above the browser
    boundary should treat this as a recoverable signal that warrants a
    clearance refresh rather than a generic transport failure.
    """


class BrowserTimeoutError(ComixError):
    """Raised when a bounded browser operation exceeds its timeout.

    Distinct from the standard library ``TimeoutError`` so that callers can
    differentiate browser-side timeouts from event-loop / asyncio timeouts.
    """


class PagePoolUnavailableError(ComixError):
    """Raised when a pooled browser page cannot be acquired.

    Common causes: the browser session is shutting down, all pooled pages
    became unhealthy, or no page could be created within the configured
    capacity. Callers should not retry blindly; investigate engine state.
    """
