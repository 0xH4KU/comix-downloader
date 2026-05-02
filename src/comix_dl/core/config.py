"""Application configuration models."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


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
class ServiceConfig:
    """comix.to API settings."""

    base_url: str = "https://comix.to"

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https":
            raise ValueError(
                f"base_url must use https (got {parsed.scheme!r}): {self.base_url}"
            )
        host = parsed.hostname
        if not host:
            raise ValueError(
                f"base_url must include a hostname: {self.base_url}"
            )
        if host == "localhost":
            raise ValueError(
                f"base_url must not point to a loopback or private address: {self.base_url}"
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
                f"base_url must not point to a loopback or private address: {self.base_url}"
            )


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
    """Root configuration passed explicitly into runtime components."""

    browser: BrowserConfig = field(default_factory=BrowserConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
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
