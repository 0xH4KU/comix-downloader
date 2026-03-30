"""Download history with repository-backed JSON storage."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from comix_dl.fileio import atomic_write_text

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    summary_text: str = ""
    issues: list[str] = field(default_factory=list)


class HistoryRepository:
    """Repository for reading and writing persisted download history."""

    def __init__(self, history_file: Path | None = None, *, max_entries: int = MAX_ENTRIES) -> None:
        self._history_file = history_file or _HISTORY_FILE
        self._lock_file = self._history_file.with_suffix(".lock")
        self._max_entries = max_entries

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Acquire an exclusive file lock around history read-modify-write."""
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(self._lock_file), os.O_CREAT | os.O_RDWR)
        try:
            self._lock_fd(lock_fd)
            yield
        finally:
            self._unlock_fd(lock_fd)
            os.close(lock_fd)

    @staticmethod
    def _lock_fd(lock_fd: int) -> None:
        """Acquire a blocking exclusive lock on a file descriptor."""
        if os.name == "nt":
            import msvcrt

            if os.fstat(lock_fd).st_size == 0:
                os.write(lock_fd, b"\0")
                os.fsync(lock_fd)
            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
            return

        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_EX)

    @staticmethod
    def _unlock_fd(lock_fd: int) -> None:
        """Release a previously acquired file lock."""
        if os.name == "nt":
            import msvcrt

            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            return

        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def record_download(
        self,
        title: str,
        chapters_count: int,
        fmt: str,
        total_size_bytes: int = 0,
        completed: int = 0,
        partial: int = 0,
        failed: int = 0,
        skipped: int = 0,
        summary_text: str = "",
        issues: list[str] | None = None,
    ) -> None:
        """Append a download record and trim old entries."""
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
            summary_text=summary_text,
            issues=list(issues or []),
        )

        with self._file_lock():
            entries = self._load_entries()
            entries.append(asdict(entry))
            if len(entries) > self._max_entries:
                entries = entries[-self._max_entries :]
            self._save_entries(entries)

        logger.debug("Recorded download: %s (%d chapters)", title, chapters_count)

    def list_entries(self) -> list[HistoryEntry]:
        """Return all history entries, newest first."""
        result: list[HistoryEntry] = []
        for data in reversed(self._load_entries()):
            try:
                result.append(HistoryEntry(**{  # type: ignore[arg-type]
                    key: value for key, value in data.items() if key in HistoryEntry.__dataclass_fields__
                }))
            except (TypeError, KeyError):
                continue
        return result

    def clear(self) -> None:
        """Delete all history entries."""
        if self._history_file.exists():
            self._history_file.unlink()
            logger.info("History cleared")

    def _load_entries(self) -> list[dict[str, object]]:
        """Load raw entries from disk."""
        if not self._history_file.exists():
            return []
        try:
            data = json.loads(self._history_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as exc:
            logger.warning("Failed to load history: %s", exc)
        return []

    def _save_entries(self, entries: list[dict[str, object]]) -> None:
        """Write entries to disk."""
        atomic_write_text(
            self._history_file,
            json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
        )


def record_download(
    title: str,
    chapters_count: int,
    fmt: str,
    total_size_bytes: int = 0,
    completed: int = 0,
    partial: int = 0,
    failed: int = 0,
    skipped: int = 0,
    summary_text: str = "",
    issues: list[str] | None = None,
) -> None:
    """Compatibility wrapper around the default history repository."""
    HistoryRepository().record_download(
        title=title,
        chapters_count=chapters_count,
        fmt=fmt,
        total_size_bytes=total_size_bytes,
        completed=completed,
        partial=partial,
        failed=failed,
        skipped=skipped,
        summary_text=summary_text,
        issues=issues,
    )


def list_history() -> list[HistoryEntry]:
    """Compatibility wrapper around the default history repository."""
    return HistoryRepository().list_entries()


def clear_history() -> None:
    """Compatibility wrapper around the default history repository."""
    HistoryRepository().clear()
