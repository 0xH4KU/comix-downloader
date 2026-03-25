"""Persistent user settings with repository-backed JSON storage."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from comix_dl.config import CONFIG
from comix_dl.fileio import atomic_write_text

logger = logging.getLogger(__name__)

_SETTINGS_DIR = Path.home() / ".config" / "comix-dl"
_SETTINGS_FILE = _SETTINGS_DIR / "settings.json"


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

    def __init__(self, settings_file: Path | None = None) -> None:
        self._settings_file = settings_file or _SETTINGS_FILE

    def load(self) -> Settings:
        """Load settings from disk, apply them to CONFIG, and return them."""
        if not self._settings_file.exists():
            settings = Settings()
        else:
            try:
                data = json.loads(self._settings_file.read_text(encoding="utf-8"))
                settings = Settings(**{
                    key: value for key, value in data.items() if key in Settings.__dataclass_fields__
                })
            except Exception as exc:
                logger.warning("Failed to load settings: %s", exc)
                settings = Settings()

        self.apply_to_config(settings)
        return settings

    def save(self, settings: Settings) -> None:
        """Save settings to disk and update CONFIG."""
        atomic_write_text(
            self._settings_file,
            json.dumps(asdict(settings), indent=2, ensure_ascii=False) + "\n",
        )
        self.apply_to_config(settings)
        logger.debug("Settings saved to %s", self._settings_file)

    @staticmethod
    def apply_to_config(settings: Settings) -> None:
        """Apply user settings to the current global CONFIG."""
        CONFIG.download.default_output_dir = Path(settings.output_dir)
        CONFIG.download.max_concurrent_chapters = settings.concurrent_chapters
        CONFIG.download.max_concurrent_images = settings.concurrent_images
        CONFIG.download.max_retries = settings.max_retries
        CONFIG.convert.default_format = settings.default_format
        CONFIG.convert.optimize_images = settings.optimize_images
        if settings.download_delay:
            CONFIG.download.image_delay = 0.15
            CONFIG.download.chapter_delay = 0.8
        else:
            CONFIG.download.image_delay = 0.0
            CONFIG.download.chapter_delay = 0.0


def load_settings() -> Settings:
    """Compatibility wrapper around the default settings repository."""
    return SettingsRepository().load()


def save_settings(settings: Settings) -> None:
    """Compatibility wrapper around the default settings repository."""
    SettingsRepository().save(settings)


def apply_settings_to_config(settings: Settings) -> None:
    """Compatibility wrapper around repository-owned config application."""
    SettingsRepository.apply_to_config(settings)
