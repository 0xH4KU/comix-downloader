"""Tests for comix_dl.config — default configuration values."""

from __future__ import annotations

from pathlib import Path

import comix_dl.config as config_module
from comix_dl.config import AppConfig, BrowserConfig, ConvertConfig, DownloadConfig, ServiceConfig


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
    def test_no_global_config_singleton(self):
        assert not hasattr(config_module, "CONFIG")

    def test_sub_configs_are_instances(self):
        cfg = AppConfig()
        assert isinstance(cfg.browser, BrowserConfig)
        assert isinstance(cfg.download, DownloadConfig)
        assert isinstance(cfg.service, ServiceConfig)
        assert isinstance(cfg.convert, ConvertConfig)

    def test_new_instances_do_not_share_nested_state(self):
        first = AppConfig()
        second = AppConfig()

        first.download.max_retries = 99

        assert second.download.max_retries == 3
