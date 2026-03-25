"""Tests for comix_dl.settings — load, save, and apply user settings."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from comix_dl.config import AppConfig
from comix_dl.settings import (
    Settings,
    SettingsRepository,
    apply_settings_to_config,
    build_runtime_config,
    load_settings,
    save_settings,
)


class TestSettingsDefaults:
    def test_default_format(self):
        s = Settings()
        assert s.default_format == "pdf"

    def test_default_concurrency(self):
        s = Settings()
        assert s.concurrent_chapters == 2
        assert s.concurrent_images == 8

    def test_default_retries(self):
        s = Settings()
        assert s.max_retries == 3

    def test_default_delay(self):
        s = Settings()
        assert s.download_delay is True


class TestLoadSettings:
    def test_returns_defaults_when_file_missing(self, tmp_path: Path):
        fake_file = tmp_path / "settings.json"
        with patch("comix_dl.settings._SETTINGS_FILE", fake_file):
            s = load_settings()
        assert s.default_format == "pdf"
        assert s.concurrent_images == 8

    def test_loads_from_json(self, tmp_path: Path):
        fake_file = tmp_path / "settings.json"
        fake_file.write_text(json.dumps({
            "version": 1,
            "default_format": "cbz",
            "concurrent_images": 4,
            "max_retries": 5,
        }))
        with patch("comix_dl.settings._SETTINGS_FILE", fake_file):
            s = load_settings()
        assert s.default_format == "cbz"
        assert s.concurrent_images == 4
        assert s.max_retries == 5

    def test_ignores_unknown_fields(self, tmp_path: Path):
        fake_file = tmp_path / "settings.json"
        fake_file.write_text(json.dumps({
            "version": 1,
            "default_format": "cbz",
            "unknown_field": "should_be_ignored",
        }))
        with patch("comix_dl.settings._SETTINGS_FILE", fake_file):
            s = load_settings()
        assert s.default_format == "cbz"
        assert not hasattr(s, "unknown_field")

    def test_graceful_fallback_on_corrupt_json(self, tmp_path: Path):
        fake_file = tmp_path / "settings.json"
        fake_file.write_text("{broken json!!")
        with patch("comix_dl.settings._SETTINGS_FILE", fake_file):
            s = load_settings()
        # Should return defaults without crashing
        assert s.default_format == "pdf"

    def test_graceful_fallback_on_wrong_type(self, tmp_path: Path):
        fake_file = tmp_path / "settings.json"
        fake_file.write_text('"just a string"')
        with patch("comix_dl.settings._SETTINGS_FILE", fake_file):
            s = load_settings()
        assert s.default_format == "pdf"

    def test_migrates_legacy_settings_without_version(self, tmp_path: Path):
        fake_file = tmp_path / "settings.json"
        fake_file.write_text(json.dumps({
            "default_format": "cbz",
            "concurrent_images": 6,
        }))
        with patch("comix_dl.settings._SETTINGS_FILE", fake_file):
            s = load_settings()
        assert s.default_format == "cbz"
        assert s.concurrent_images == 6

    def test_future_version_falls_back_to_defaults(self, tmp_path: Path):
        fake_file = tmp_path / "settings.json"
        fake_file.write_text(json.dumps({
            "version": 999,
            "default_format": "cbz",
        }))
        with patch("comix_dl.settings._SETTINGS_FILE", fake_file):
            s = load_settings()
        assert s.default_format == "pdf"

    def test_invalid_values_are_normalized(self, tmp_path: Path):
        fake_file = tmp_path / "settings.json"
        fake_file.write_text(json.dumps({
            "version": 1,
            "default_format": "zip",
            "concurrent_chapters": 99,
            "concurrent_images": 0,
            "max_retries": -3,
            "download_delay": "yes",
            "optimize_images": "no",
        }))
        with patch("comix_dl.settings._SETTINGS_FILE", fake_file):
            s = load_settings()
        assert s.default_format == "pdf"
        assert s.concurrent_chapters == 5
        assert s.concurrent_images == 1
        assert s.max_retries == 0
        assert s.download_delay is True
        assert s.optimize_images is True


class TestSaveSettings:
    def test_save_creates_file(self, tmp_path: Path):
        fake_dir = tmp_path / "config"
        fake_file = fake_dir / "settings.json"
        with (
            patch("comix_dl.settings._SETTINGS_DIR", fake_dir),
            patch("comix_dl.settings._SETTINGS_FILE", fake_file),
        ):
            s = Settings(default_format="cbz", max_retries=7)
            save_settings(s)

        assert fake_file.exists()
        data = json.loads(fake_file.read_text())
        assert data["version"] == 1
        assert data["default_format"] == "cbz"
        assert data["max_retries"] == 7

    def test_round_trip(self, tmp_path: Path):
        fake_dir = tmp_path / "config"
        fake_file = fake_dir / "settings.json"
        with (
            patch("comix_dl.settings._SETTINGS_DIR", fake_dir),
            patch("comix_dl.settings._SETTINGS_FILE", fake_file),
        ):
            original = Settings(
                output_dir="/tmp/test-output",
                default_format="cbz",
                concurrent_chapters=3,
                concurrent_images=12,
                max_retries=5,
                download_delay=False,
            )
            save_settings(original)
            loaded = load_settings()

        assert loaded.output_dir == original.output_dir
        assert loaded.default_format == original.default_format
        assert loaded.concurrent_chapters == original.concurrent_chapters
        assert loaded.concurrent_images == original.concurrent_images
        assert loaded.max_retries == original.max_retries
        assert loaded.download_delay == original.download_delay

    def test_repository_round_trip(self, tmp_path: Path):
        repository = SettingsRepository(tmp_path / "settings.json")
        original = Settings(default_format="both", concurrent_images=4, optimize_images=False)

        repository.save(original)
        loaded = repository.load()

        assert loaded.default_format == "both"
        assert loaded.concurrent_images == 4
        assert loaded.optimize_images is False


class TestBuildRuntimeConfig:
    def test_applies_output_dir(self):
        s = Settings(output_dir="/custom/path")
        config = build_runtime_config(s)
        assert config.download.default_output_dir == Path("/custom/path")

    def test_applies_concurrency(self):
        s = Settings(concurrent_chapters=4, concurrent_images=16)
        config = build_runtime_config(s)
        assert config.download.max_concurrent_chapters == 4
        assert config.download.max_concurrent_images == 16

    def test_applies_format(self):
        s = Settings(default_format="cbz")
        config = build_runtime_config(s)
        assert config.convert.default_format == "cbz"

    def test_delay_enabled(self):
        s = Settings(download_delay=True)
        config = build_runtime_config(s)
        assert config.download.image_delay == 0.15
        assert config.download.chapter_delay == 0.8

    def test_delay_disabled(self):
        s = Settings(download_delay=False)
        config = build_runtime_config(s)
        assert config.download.image_delay == 0.0
        assert config.download.chapter_delay == 0.0

    def test_does_not_mutate_base_config(self):
        base = AppConfig()
        config = build_runtime_config(Settings(default_format="cbz", concurrent_images=4), base)

        assert base.convert.default_format == "pdf"
        assert base.download.max_concurrent_images == 8
        assert config.convert.default_format == "cbz"
        assert config.download.max_concurrent_images == 4

    def test_compatibility_wrapper_returns_runtime_config(self):
        config = apply_settings_to_config(Settings(default_format="both"))
        assert config.convert.default_format == "both"
