"""Persistent user settings with JSON storage.

Settings are stored at ``~/.config/comix-dl/settings.json`` and applied to
the global ``CONFIG`` at startup so that all modules use the same values.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from comix_dl.config import CONFIG

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
    download_delay: bool = True  # add random delays to avoid rate limits
    optimize_images: bool = True  # convert images to WebP before packaging


def load_settings() -> Settings:
    """Load settings from disk, or return defaults."""
    if not _SETTINGS_FILE.exists():
        settings = Settings()
    else:
        try:
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            settings = Settings(**{
                k: v for k, v in data.items() if k in Settings.__dataclass_fields__
            })
        except Exception as exc:
            logger.warning("Failed to load settings: %s", exc)
            settings = Settings()

    # Sync settings → CONFIG so all modules see the user's values
    apply_settings_to_config(settings)
    return settings


def save_settings(settings: Settings) -> None:
    """Save settings to disk and update CONFIG."""
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(
        json.dumps(asdict(settings), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    apply_settings_to_config(settings)
    logger.debug("Settings saved to %s", _SETTINGS_FILE)


def apply_settings_to_config(settings: Settings) -> None:
    """Apply user settings to the global CONFIG."""
    CONFIG.download.default_output_dir = Path(settings.output_dir)
    CONFIG.download.max_concurrent_chapters = settings.concurrent_chapters
    CONFIG.download.max_concurrent_images = settings.concurrent_images
    CONFIG.download.max_retries = settings.max_retries
    CONFIG.convert.default_format = settings.default_format
    CONFIG.convert.optimize_images = settings.optimize_images
    # Rate limiting: 0 means no delay
    if settings.download_delay:
        CONFIG.download.image_delay = 0.15
        CONFIG.download.chapter_delay = 0.8
    else:
        CONFIG.download.image_delay = 0.0
        CONFIG.download.chapter_delay = 0.0
