"""Persistent user settings with JSON storage.

Settings are stored at ``~/.config/comix-dl/settings.json``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

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


def load_settings() -> Settings:
    """Load settings from disk, or return defaults."""
    if not _SETTINGS_FILE.exists():
        return Settings()

    try:
        data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        return Settings(**{
            k: v for k, v in data.items() if k in Settings.__dataclass_fields__
        })
    except Exception as exc:
        logger.warning("Failed to load settings: %s", exc)
        return Settings()


def save_settings(settings: Settings) -> None:
    """Save settings to disk."""
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(
        json.dumps(asdict(settings), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.debug("Settings saved to %s", _SETTINGS_FILE)
