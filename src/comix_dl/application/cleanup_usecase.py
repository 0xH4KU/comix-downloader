"""Application use cases for download listing and cleanup planning."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from comix_dl.downloader import sanitize_dirname

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class DownloadedSeries:
    """Summary of a downloaded series directory."""

    name: str
    path: Path
    completed_chapters: int
    total_size_bytes: int


@dataclass
class CleanupCandidate:
    """A removable raw-image directory with converted output."""

    path: Path
    relative_path: Path
    size_bytes: int


@dataclass
class CleanupPlan:
    """Directories eligible for cleanup under an output directory."""

    output_dir: Path
    candidates: list[CleanupCandidate]
    total_size_bytes: int


@dataclass
class CleanupResult:
    """Result of executing a cleanup plan."""

    removed_count: int
    failed: list[tuple[Path, str]]


def list_downloaded_series(output_dir: Path) -> list[DownloadedSeries]:
    """Summarize downloaded series under an output directory."""
    if not output_dir.exists():
        return []

    result: list[DownloadedSeries] = []
    for manga_dir in sorted(output_dir.iterdir()):
        if not manga_dir.is_dir():
            continue

        complete_count = 0
        total_size = 0
        for item in manga_dir.iterdir():
            if item.is_dir() and (item / ".complete").exists():
                complete_count += 1
            if item.is_file():
                total_size += item.stat().st_size

        if complete_count == 0 and total_size == 0:
            continue

        result.append(
            DownloadedSeries(
                name=manga_dir.name,
                path=manga_dir,
                completed_chapters=complete_count,
                total_size_bytes=total_size,
            )
        )

    return result


def build_cleanup_plan(output_dir: Path, *, series_title: str | None = None) -> CleanupPlan:
    """Find raw image directories that are safe to remove after conversion."""
    if not output_dir.exists():
        return CleanupPlan(output_dir=output_dir, candidates=[], total_size_bytes=0)

    roots: list[Path]
    if series_title is None:
        roots = [path for path in sorted(output_dir.iterdir()) if path.is_dir()]
    else:
        manga_dir = output_dir / sanitize_dirname(series_title)
        roots = [manga_dir] if manga_dir.exists() and manga_dir.is_dir() else []

    candidates: list[CleanupCandidate] = []
    total_size = 0

    for manga_dir in roots:
        for chapter_dir in sorted(manga_dir.iterdir()):
            if not chapter_dir.is_dir():
                continue

            has_output = (
                (chapter_dir.parent / f"{chapter_dir.name}.pdf").exists()
                or (chapter_dir.parent / f"{chapter_dir.name}.cbz").exists()
            )
            if not has_output or not (chapter_dir / ".complete").exists():
                continue

            chapter_size = sum(item.stat().st_size for item in chapter_dir.rglob("*") if item.is_file())

            candidates.append(
                CleanupCandidate(
                    path=chapter_dir,
                    relative_path=chapter_dir.relative_to(output_dir),
                    size_bytes=chapter_size,
                )
            )
            total_size += chapter_size

    return CleanupPlan(output_dir=output_dir, candidates=candidates, total_size_bytes=total_size)


def apply_cleanup_plan(plan: CleanupPlan) -> CleanupResult:
    """Delete all directories in a cleanup plan."""
    removed = 0
    failed: list[tuple[Path, str]] = []

    for candidate in plan.candidates:
        try:
            shutil.rmtree(candidate.path)
            removed += 1
        except OSError as exc:
            failed.append((candidate.path, str(exc)))

    return CleanupResult(removed_count=removed, failed=failed)
