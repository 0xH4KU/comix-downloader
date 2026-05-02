"""Application configuration models.

Configuration here is intentionally site-agnostic. Anything specific to
a remote site (base URL, mirror list, signing details, deduplication
rules) belongs to a :class:`~comix_dl.sites.base.SiteAdapter`.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def validate_public_https_url(url: str, *, label: str = "url") -> None:
    """Reject URLs that are not safe public HTTPS origins.

    Used by site adapters to validate user-supplied or configured base
    URLs / mirror entries before they are handed to the engine. Raises
    :class:`ValueError` for non-HTTPS schemes, missing hostnames, or
    hosts that resolve to loopback / private / link-local / multicast
    / reserved / unspecified addresses (or the literal name
    ``localhost``).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"{label} must use https (got {parsed.scheme!r}): {url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"{label} must include a hostname: {url}")
    if host == "localhost":
        raise ValueError(
            f"{label} must not point to a loopback or private address: {url}",
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ValueError(
            f"{label} must not point to a loopback or private address: {url}",
        )


@dataclass
class BrowserConfig:
    """Chrome / CDP settings."""

    timeout_ms: int = 30_000
    cf_wait_seconds: int = 60
    cookie_dir: Path = field(default_factory=lambda: Path.home() / ".config" / "comix-dl")
    chrome_path: str | None = None  # User override; auto-detect if None
    cf_titles: tuple[str, ...] = ("Just a moment...", "Attention Required!", "Verify you are human")
    cf_selectors: tuple[str, ...] = (
        "#challenge-running",
        "#cf-challenge-running",
        "iframe[src*='challenges.cloudflare.com']",
    )


@dataclass
class DownloadConfig:
    """Download behaviour."""

    max_concurrent_chapters: int = 2
    max_concurrent_images: int = 8
    max_retries: int = 3
    retry_delay: float = 1.0
    image_delay: float = 0.15  # seconds between image requests (anti-rate-limit)
    chapter_delay: float = 0.8  # seconds between chapters
    connect_timeout_ms: int = 10_000
    read_timeout_ms: int = 30_000
    default_output_dir: Path = field(default_factory=lambda: Path.home() / "Downloads" / "comix-dl")


@dataclass
class ConvertConfig:
    """Converter settings."""

    pdf_dpi: float = 100.0
    pdf_batch_size: int = 20
    default_format: str = "pdf"
    supported_image_formats: tuple[str, ...] = ("png", "jpg", "jpeg", "gif", "bmp", "webp", "avif")
    optimize_images: bool = True


@dataclass
class AppConfig:
    """Root configuration passed explicitly into runtime components.

    Site-specific fields (base URL, mirrors, signing) are not stored
    here; they live on the active :class:`~comix_dl.sites.base.SiteAdapter`
    instance and are injected into engines / services at construction
    time.
    """

    browser: BrowserConfig = field(default_factory=BrowserConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    convert: ConvertConfig = field(default_factory=ConvertConfig)


# Shared default instance — avoids recreating defaults in every constructor.
_DEFAULT_CONFIG: AppConfig | None = None


def default_config() -> AppConfig:
    """Return a lazily-created shared default AppConfig."""
    global _DEFAULT_CONFIG
    if _DEFAULT_CONFIG is None:
        _DEFAULT_CONFIG = AppConfig()
    return _DEFAULT_CONFIG


def resolve_config(config: AppConfig | None) -> AppConfig:
    """Return *config* if given, otherwise the shared default."""
    return config if config is not None else default_config()
