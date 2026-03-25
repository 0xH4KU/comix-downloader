"""Tests for comix_dl.config — default configuration values."""

from __future__ import annotations

from pathlib import Path

from comix_dl.config import CONFIG, AppConfig, BrowserConfig, ConvertConfig, DownloadConfig, ServiceConfig


class TestBrowserConfig:
    def test_defaults(self):
        cfg = BrowserConfig()
        assert cfg.timeout_ms == 30_000
        assert cfg.cf_wait_seconds == 60
        assert isinstance(cfg.cookie_dir, Path)


class TestDownloadConfig:
    def test_defaults(self):
        cfg = DownloadConfig()
        assert cfg.max_concurrent_chapters == 2
        assert cfg.max_concurrent_images == 8
        assert cfg.max_retries == 3
        assert cfg.retry_delay == 1.0
        assert cfg.image_delay == 0.15
        assert cfg.chapter_delay == 0.8
        assert isinstance(cfg.default_output_dir, Path)


class TestServiceConfig:
    def test_base_url(self):
        cfg = ServiceConfig()
        assert cfg.base_url == "https://comix.to"


class TestConvertConfig:
    def test_defaults(self):
        cfg = ConvertConfig()
        assert cfg.default_format == "pdf"
        assert cfg.pdf_dpi == 100.0
        assert "png" in cfg.supported_image_formats
        assert "webp" in cfg.supported_image_formats
        assert "avif" in cfg.supported_image_formats


class TestAppConfig:
    def test_global_config_exists(self):
        assert isinstance(CONFIG, AppConfig)

    def test_sub_configs_are_instances(self):
        assert isinstance(CONFIG.browser, BrowserConfig)
        assert isinstance(CONFIG.download, DownloadConfig)
        assert isinstance(CONFIG.service, ServiceConfig)
        assert isinstance(CONFIG.convert, ConvertConfig)

    def test_config_is_mutable(self):
        """CONFIG must be mutable so user settings can override defaults."""
        original = CONFIG.download.max_retries
        CONFIG.download.max_retries = 99
        assert CONFIG.download.max_retries == 99
        CONFIG.download.max_retries = original
