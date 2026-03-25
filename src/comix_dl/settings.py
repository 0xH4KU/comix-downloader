"""Persistent user settings with repository-backed JSON storage."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

from comix_dl.config import AppConfig
from comix_dl.fileio import atomic_write_text

logger = logging.getLogger(__name__)

_SETTINGS_DIR = Path.home() / ".config" / "comix-dl"
_SETTINGS_FILE = _SETTINGS_DIR / "settings.json"
_CURRENT_SETTINGS_VERSION = 1


@dataclass
class Settings:
    """User-configurable settings (persisted to disk)."""

    output_dir: str = str(Path.home() / "Downloads" / "comix-dl")
    default_format: str = "pdf"
    concurrent_chapters: int = 2
    concurrent_images: int = 8
    max_retries: int = 3
    download_delay: bool = True
    optimize_images: bool = True


class SettingsRepository:
    """Repository for reading and writing persisted user settings."""

    _ALLOWED_FORMATS: ClassVar[set[str]] = {"pdf", "cbz", "both"}

    def __init__(self, settings_file: Path | None = None) -> None:
        self._settings_file = settings_file or _SETTINGS_FILE

    def load(self) -> Settings:
        """Load settings from disk and return normalized values."""
        if not self._settings_file.exists():
            return Settings()

        try:
            data = json.loads(self._settings_file.read_text(encoding="utf-8"))
            return self._deserialize(data)
        except Exception as exc:
            logger.warning("Failed to load settings: %s", exc)
            return Settings()

    def save(self, settings: Settings) -> None:
        """Save normalized settings to disk."""
        normalized = self._normalize_settings(asdict(settings))
        atomic_write_text(
            self._settings_file,
            json.dumps(
                {"version": _CURRENT_SETTINGS_VERSION, **asdict(normalized)},
                indent=2,
                ensure_ascii=False,
            ) + "\n",
        )
        logger.debug("Settings saved to %s", self._settings_file)

    @staticmethod
    def build_runtime_config(settings: Settings, base_config: AppConfig | None = None) -> AppConfig:
        """Build a per-run AppConfig from persisted user settings."""
        base = base_config if base_config is not None else AppConfig()
        image_delay = 0.15 if settings.download_delay else 0.0
        chapter_delay = 0.8 if settings.download_delay else 0.0
        return AppConfig(
            browser=replace(base.browser),
            service=replace(base.service),
            download=replace(
                base.download,
                default_output_dir=Path(settings.output_dir),
                max_concurrent_chapters=settings.concurrent_chapters,
                max_concurrent_images=settings.concurrent_images,
                max_retries=settings.max_retries,
                image_delay=image_delay,
                chapter_delay=chapter_delay,
            ),
            convert=replace(
                base.convert,
                default_format=settings.default_format,
                optimize_images=settings.optimize_images,
            ),
        )

    def _deserialize(self, data: object) -> Settings:
        """Deserialize JSON data, including legacy settings formats."""
        if not isinstance(data, dict):
            logger.warning("Settings file did not contain an object; using defaults.")
            return Settings()

        version = data.get("version")
        if version is None:
            logger.info("Loading legacy settings without version metadata.")
            return self._normalize_settings(data)
        if not isinstance(version, int):
            logger.warning("Settings version %r is invalid; using defaults.", version)
            return Settings()
        if version > _CURRENT_SETTINGS_VERSION:
            logger.warning(
                "Settings version %d is newer than supported version %d; using defaults.",
                version,
                _CURRENT_SETTINGS_VERSION,
            )
            return Settings()
        if version < _CURRENT_SETTINGS_VERSION:
            logger.info("Migrating settings from version %d to %d.", version, _CURRENT_SETTINGS_VERSION)
        return self._normalize_settings(data)

    def _normalize_settings(self, data: dict[str, Any]) -> Settings:
        """Validate and normalize persisted settings values."""
        defaults = Settings()
        return Settings(
            output_dir=self._normalize_output_dir(data.get("output_dir"), defaults.output_dir),
            default_format=self._normalize_format(data.get("default_format"), defaults.default_format),
            concurrent_chapters=self._normalize_int(
                data.get("concurrent_chapters"),
                default=defaults.concurrent_chapters,
                minimum=1,
                maximum=5,
                field_name="concurrent_chapters",
            ),
            concurrent_images=self._normalize_int(
                data.get("concurrent_images"),
                default=defaults.concurrent_images,
                minimum=1,
                maximum=16,
                field_name="concurrent_images",
            ),
            max_retries=self._normalize_int(
                data.get("max_retries"),
                default=defaults.max_retries,
                minimum=0,
                maximum=10,
                field_name="max_retries",
            ),
            download_delay=self._normalize_bool(
                data.get("download_delay"),
                default=defaults.download_delay,
                field_name="download_delay",
            ),
            optimize_images=self._normalize_bool(
                data.get("optimize_images"),
                default=defaults.optimize_images,
                field_name="optimize_images",
            ),
        )

    @staticmethod
    def _normalize_output_dir(value: object, default: str) -> str:
        if isinstance(value, str) and value.strip():
            return value
        return default

    def _normalize_format(self, value: object, default: str) -> str:
        if isinstance(value, str) and value in self._ALLOWED_FORMATS:
            return value
        if value is not None:
            logger.warning("Settings field default_format=%r is invalid; using %r.", value, default)
        return default

    @staticmethod
    def _normalize_bool(value: object, *, default: bool, field_name: str) -> bool:
        if isinstance(value, bool):
            return value
        if value is not None:
            logger.warning("Settings field %s=%r is invalid; using %r.", field_name, value, default)
        return default

    @staticmethod
    def _normalize_int(
        value: object,
        *,
        default: int,
        minimum: int,
        maximum: int,
        field_name: str,
    ) -> int:
        if value is None:
            normalized = default
        elif isinstance(value, (int, float, str)):
            normalized = int(value)
        else:
            logger.warning("Settings field %s=%r is invalid; using %d.", field_name, value, default)
            return default
        try:
            normalized = int(normalized)
        except (TypeError, ValueError):
            logger.warning("Settings field %s=%r is invalid; using %d.", field_name, value, default)
            return default
        if normalized < minimum or normalized > maximum:
            clamped = max(minimum, min(maximum, normalized))
            logger.warning(
                "Settings field %s=%r is out of range; clamping to %d.",
                field_name,
                value,
                clamped,
            )
            return clamped
        return normalized


def load_settings() -> Settings:
    """Compatibility wrapper around the default settings repository."""
    return SettingsRepository().load()


def save_settings(settings: Settings) -> None:
    """Compatibility wrapper around the default settings repository."""
    SettingsRepository().save(settings)


def build_runtime_config(settings: Settings, base_config: AppConfig | None = None) -> AppConfig:
    """Compatibility wrapper for building an injected runtime config."""
    return SettingsRepository.build_runtime_config(settings, base_config=base_config)


def apply_settings_to_config(settings: Settings, base_config: AppConfig | None = None) -> AppConfig:
    """Backward-compatible alias for runtime config construction."""
    return build_runtime_config(settings, base_config=base_config)
