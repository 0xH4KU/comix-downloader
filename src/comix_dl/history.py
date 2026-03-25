"""Download history with JSON storage.

Records each download session to ``~/.config/comix-dl/history.json``.
Entries are auto-trimmed when the list exceeds ``max_entries``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from comix_dl.fileio import atomic_write_text

logger = logging.getLogger(__name__)

_HISTORY_DIR = Path.home() / ".config" / "comix-dl"
_HISTORY_FILE = _HISTORY_DIR / "history.json"

MAX_ENTRIES = 500


@dataclass
class HistoryEntry:
    """A single download history record."""

    timestamp: str
    title: str
    chapters_count: int
    format: str
    total_size_bytes: int = 0
    completed: int = 0
    partial: int = 0
    failed: int = 0
    skipped: int = 0


def record_download(
    title: str,
    chapters_count: int,
    fmt: str,
    total_size_bytes: int = 0,
    completed: int = 0,
    partial: int = 0,
    failed: int = 0,
    skipped: int = 0,
) -> None:
    """Append a download record to history, auto-trimming old entries."""
    entry = HistoryEntry(
        timestamp=datetime.now(UTC).isoformat(),
        title=title,
        chapters_count=chapters_count,
        format=fmt,
        total_size_bytes=total_size_bytes,
        completed=completed,
        partial=partial,
        failed=failed,
        skipped=skipped,
    )

    entries = _load_entries()
    entries.append(asdict(entry))

    # Auto-trim oldest entries
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]

    _save_entries(entries)
    logger.debug("Recorded download: %s (%d chapters)", title, chapters_count)


def list_history() -> list[HistoryEntry]:
    """Return all history entries, newest first."""
    entries = _load_entries()
    result = []
    for data in reversed(entries):
        try:
            result.append(HistoryEntry(**{  # type: ignore[arg-type]
                k: v for k, v in data.items() if k in HistoryEntry.__dataclass_fields__
            }))
        except (TypeError, KeyError):
            continue
    return result


def clear_history() -> None:
    """Delete all history entries."""
    if _HISTORY_FILE.exists():
        _HISTORY_FILE.unlink()
        logger.info("History cleared")


def _load_entries() -> list[dict[str, object]]:
    """Load raw entries from disk."""
    if not _HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception as exc:
        logger.warning("Failed to load history: %s", exc)
    return []


def _save_entries(entries: list[dict[str, object]]) -> None:
    """Write entries to disk."""
    atomic_write_text(
        _HISTORY_FILE,
        json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
    )
