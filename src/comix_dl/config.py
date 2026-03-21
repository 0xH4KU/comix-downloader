"""Application configuration.

Provides sensible defaults that can be overridden by user settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BrowserConfig:
    """Chrome / CDP settings."""

    headless: bool = True
    timeout_ms: int = 30_000
    cf_wait_seconds: int = 60
    cookie_dir: Path = field(default_factory=lambda: Path.home() / ".config" / "comix-dl")
    cookie_file: str = "cookies.json"
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
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
    graphql_path: str = "/apo/"
    rate_limit_delay: float = 0.5
    max_search_pages: int = 3


@dataclass
class ConvertConfig:
    """Converter settings."""

    pdf_dpi: float = 100.0
    default_format: str = "pdf"
    supported_image_formats: tuple[str, ...] = ("png", "jpg", "jpeg", "gif", "bmp", "webp", "avif")


@dataclass
class AppConfig:
    """Root configuration — mutable so user settings can override defaults."""

    browser: BrowserConfig = field(default_factory=BrowserConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    convert: ConvertConfig = field(default_factory=ConvertConfig)


CONFIG = AppConfig()
