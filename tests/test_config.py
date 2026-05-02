"""Tests for comix_dl.core.config — default configuration values."""

from __future__ import annotations

from pathlib import Path

import pytest

import comix_dl.core.config as config_module
from comix_dl.core.config import (
    AppConfig,
    BrowserConfig,
    ConvertConfig,
    DownloadConfig,
    validate_public_https_url,
)


class TestBrowserConfig:
    def test_defaults(self) -> None:
        cfg = BrowserConfig()
        assert cfg.timeout_ms == 30_000
        assert cfg.cf_wait_seconds == 60
        assert isinstance(cfg.cookie_dir, Path)


class TestDownloadConfig:
    def test_defaults(self) -> None:
        cfg = DownloadConfig()
        assert cfg.max_concurrent_chapters == 2
        assert cfg.max_concurrent_images == 8
        assert cfg.max_retries == 3
        assert cfg.retry_delay == 1.0
        assert cfg.image_delay == 0.15
        assert cfg.chapter_delay == 0.8
        assert isinstance(cfg.default_output_dir, Path)


class TestValidatePublicHttpsUrl:
    def test_accepts_public_https_url(self) -> None:
        # Should not raise.
        validate_public_https_url("https://example.com")

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "file:///tmp/comix",
            "https://localhost",
            "https://127.0.0.1",
            "https://10.0.0.1",
            "https://192.168.1.10",
            "https://172.16.0.5",
            "https://[::1]",
            "https://[fc00::1]",
        ],
    )
    def test_rejects_non_public_or_non_https(self, url: str) -> None:
        with pytest.raises(ValueError):
            validate_public_https_url(url)

    def test_rejects_missing_hostname(self) -> None:
        with pytest.raises(ValueError):
            validate_public_https_url("https:///missing-host")

    def test_includes_label_in_error_message(self) -> None:
        with pytest.raises(ValueError, match="mirror"):
            validate_public_https_url("http://example.com", label="mirror")


class TestConvertConfig:
    def test_defaults(self) -> None:
        cfg = ConvertConfig()
        assert cfg.default_format == "pdf"
        assert cfg.pdf_dpi == 100.0
        assert cfg.pdf_batch_size == 20
        assert "png" in cfg.supported_image_formats
        assert "webp" in cfg.supported_image_formats
        assert "avif" in cfg.supported_image_formats


class TestAppConfig:
    def test_no_global_config_singleton(self) -> None:
        assert not hasattr(config_module, "CONFIG")

    def test_sub_configs_are_instances(self) -> None:
        cfg = AppConfig()
        assert isinstance(cfg.browser, BrowserConfig)
        assert isinstance(cfg.download, DownloadConfig)
        assert isinstance(cfg.convert, ConvertConfig)

    def test_no_service_field_on_app_config(self) -> None:
        # Site-specific URL config now lives on SiteAdapter, not AppConfig.
        cfg = AppConfig()
        assert not hasattr(cfg, "service")

    def test_new_instances_do_not_share_nested_state(self) -> None:
        first = AppConfig()
        second = AppConfig()

        first.download.max_retries = 99

        assert second.download.max_retries == 3
