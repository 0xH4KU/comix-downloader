"""Pure state helpers for the Textual TUI."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from comix_dl.core.cli.display import format_bytes

if TYPE_CHECKING:
    from comix_dl.core.application.download_usecase import DownloadChapterEvent, DownloadSummary
    from comix_dl.core.models import ChapterInfo


OutputFormat = Literal["pdf", "cbz", "both"]
DownloadStatus = Literal[
    "queued",
    "skipped",
    "started",
    "planned",
    "progress",
    "missing_images",
    "failed",
    "partial",
    "converted",
    "conversion_failed",
]


@dataclass(frozen=True)
class DownloadRequest:
    """Immutable input for one TUI download batch."""

    series_title: str
    chapters: tuple[ChapterInfo, ...]
    fmt: OutputFormat
    optimize: bool


@dataclass
class ChapterSelectionState:
    """Filter and selection state for a chapter list."""

    chapters: list[ChapterInfo]
    visible_chapters: list[ChapterInfo]
    selected_ids: set[int]
    status: str = ""

    @classmethod
    def from_chapters(cls, chapters: list[ChapterInfo]) -> ChapterSelectionState:
        """Create a selection state with every chapter initially visible."""
        return cls(chapters=list(chapters), visible_chapters=list(chapters), selected_ids=set())

    @property
    def selected_count(self) -> int:
        """Number of selected chapters."""
        return len(self.selected_ids)

    @property
    def selected_chapters(self) -> list[ChapterInfo]:
        """Selected chapters in the original chapter ordering."""
        return [chapter for chapter in self.chapters if chapter.chapter_id in self.selected_ids]

    def apply_filter(self, raw_filter: str) -> None:
        """Apply a simple token filter to the visible chapter list."""
        raw = raw_filter.strip()
        if not raw:
            self.visible_chapters = list(self.chapters)
            self.status = f"Showing all {len(self.visible_chapters)} chapter(s)."
            return

        keep_words: list[str] = []
        remove_words: list[str] = []
        for token in re.findall(r"[+\-]?\S+", raw):
            if token.startswith("+"):
                word = token[1:].strip().lower()
                if word:
                    keep_words.append(word)
                continue
            if token.startswith("-"):
                word = token[1:].strip().lower()
                if word:
                    remove_words.append(word)
                continue
            keep_words.append(token.lower())

        filtered = list(self.chapters)
        if keep_words:
            filtered = [
                chapter
                for chapter in filtered
                if any(word in chapter.title.lower() for word in keep_words)
            ]
        if remove_words:
            filtered = [
                chapter
                for chapter in filtered
                if not any(word in chapter.title.lower() for word in remove_words)
            ]

        if not filtered:
            self.status = f"No chapters matched '{raw}'; keeping previous filter."
            return

        self.visible_chapters = filtered
        self.status = f"{len(filtered)} chapter(s) visible."

    def toggle(self, chapter_id: int) -> None:
        """Toggle one chapter's selected state."""
        if chapter_id in self.selected_ids:
            self.selected_ids.remove(chapter_id)
            return
        self.selected_ids.add(chapter_id)

    def select_visible(self) -> None:
        """Select every currently visible chapter."""
        self.selected_ids.update(chapter.chapter_id for chapter in self.visible_chapters)
        self.status = f"Selected {len(self.visible_chapters)} visible chapter(s)."

    def clear_visible(self) -> None:
        """Clear selection for every currently visible chapter."""
        for chapter in self.visible_chapters:
            self.selected_ids.discard(chapter.chapter_id)
        self.status = f"Cleared {len(self.visible_chapters)} visible chapter(s)."

    def clear_all(self) -> None:
        """Clear all selected chapters."""
        self.selected_ids.clear()
        self.status = "Selection cleared."


@dataclass(frozen=True)
class DownloadRow:
    """One row in the TUI download table."""

    chapter_id: int
    title: str
    status: DownloadStatus = "queued"
    completed: int = 0
    total: int | None = None
    detail: str = ""

    @property
    def progress_text(self) -> str:
        """Compact completed/total display for a download row."""
        total = "?" if self.total is None else str(self.total)
        return f"{self.completed}/{total}"


@dataclass
class DownloadRowsState:
    """Reducer state for TUI download progress rows."""

    rows: dict[int, DownloadRow]

    @classmethod
    def from_chapters(cls, chapters: list[ChapterInfo]) -> DownloadRowsState:
        """Create queued download rows for the provided chapters."""
        return cls(
            rows={
                chapter.chapter_id: DownloadRow(
                    chapter_id=chapter.chapter_id,
                    title=chapter.title,
                )
                for chapter in chapters
            }
        )

    def apply(self, event: DownloadChapterEvent) -> None:
        """Reduce one application download event into row state."""
        row = self.rows.get(event.chapter_id)
        if row is None:
            row = DownloadRow(chapter_id=event.chapter_id, title=event.chapter_title)

        if event.kind == "skipped":
            self.rows[event.chapter_id] = replace(
                row,
                status="skipped",
                completed=1,
                total=1,
                detail=event.message or "Already downloaded.",
            )
            return

        if event.kind == "started":
            self.rows[event.chapter_id] = replace(row, status="started", detail="")
            return

        if event.kind == "planned":
            self.rows[event.chapter_id] = replace(
                row,
                status="planned",
                completed=0,
                total=event.total,
                detail="",
            )
            return

        if event.kind == "progress":
            self.rows[event.chapter_id] = replace(
                row,
                status="progress",
                completed=event.completed,
                total=event.total if event.total is not None else row.total,
            )
            return

        if event.kind == "missing_images":
            self.rows[event.chapter_id] = replace(
                row,
                status="missing_images",
                detail=event.message or "No images found.",
            )
            return

        if event.kind == "failed":
            self.rows[event.chapter_id] = replace(
                row,
                status="failed",
                detail=event.message or "Download failed.",
            )
            return

        if event.kind == "partial":
            self.rows[event.chapter_id] = replace(
                row,
                status="partial",
                detail=event.message or "Download incomplete.",
            )
            return

        if event.kind == "converted":
            total = row.total if row.total is not None else event.total
            self.rows[event.chapter_id] = replace(
                row,
                status="converted",
                completed=total or row.completed,
                total=total,
                detail=event.output_name or event.chapter_title,
            )
            return

        if event.kind == "conversion_failed":
            self.rows[event.chapter_id] = replace(
                row,
                status="conversion_failed",
                detail=event.message or "Conversion failed.",
            )


def format_summary_line(summary: DownloadSummary) -> str:
    """Format a compact one-line download summary."""
    parts: list[str] = []
    if summary.completed:
        parts.append(f"{summary.completed} completed")
    if summary.skipped:
        parts.append(f"{summary.skipped} skipped")
    if summary.partial:
        parts.append(f"{summary.partial} partial")
    if summary.failed:
        parts.append(f"{summary.failed} failed")
    if not parts:
        parts.append("0 completed")

    speed = summary.total_bytes / summary.elapsed_seconds if summary.elapsed_seconds > 0 else 0
    parts.extend([
        format_bytes(summary.total_bytes),
        f"{format_bytes(int(speed))}/s",
        f"{summary.elapsed_seconds:.1f}s",
    ])
    return " · ".join(parts)
