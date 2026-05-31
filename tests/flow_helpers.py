"""Shared helpers for CLI flow tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from comix_dl.core.application.download_usecase import DownloadIssue, DownloadSummary
from comix_dl.core.models import ChapterInfo, SeriesInfo
from comix_dl.core.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


class _SessionContext:
    def __init__(self, session: object) -> None:
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeProgress:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.added: list[dict[str, object]] = []
        self.updated: list[tuple[int, dict[str, object]]] = []
        self._next_id = 1

    def __enter__(self) -> _FakeProgress:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def add_task(
        self,
        description: str,
        *,
        total: int | None = None,
        completed: int | None = None,
    ) -> int:
        task_id = self._next_id
        self._next_id += 1
        self.added.append(
            {
                "task_id": task_id,
                "description": description,
                "total": total,
                "completed": completed,
            }
        )
        return task_id

    def update(self, task_id: int, **kwargs: object) -> None:
        self.updated.append((task_id, kwargs))


def _make_chapters(count: int = 3) -> list[ChapterInfo]:
    return [
        ChapterInfo(
            title=f"Chapter {idx}",
            chapter_id=idx,
            number=str(idx),
            image_count=10 + idx,
        )
        for idx in range(1, count + 1)
    ]


def _write_valid_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")


def _make_series(
    *,
    title: str = "Series A",
    chapters: list[ChapterInfo] | None = None,
    description: str = "A short description",
) -> SeriesInfo:
    return SeriesInfo(
        title=title,
        authors=["Author A"],
        genres=["Action"],
        description=description,
        chapters=chapters or _make_chapters(),
        url="https://comix.to/manga/series-a",
        hash_id="series-a",
    )


def _make_session(tmp_path: Path, *, default_format: str = "cbz", optimize_images: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        settings=Settings(
            output_dir=str(tmp_path),
            default_format=default_format,
            optimize_images=optimize_images,
        ),
        output_dir=tmp_path,
        search=AsyncMock(),
        load_series=AsyncMock(),
        resolve_series=AsyncMock(),
        download=AsyncMock(),
    )


def _make_summary(
    *,
    completed: int = 1,
    skipped: int = 0,
    partial: int = 0,
    failed: int = 0,
    total_bytes: int = 1024,
    elapsed_seconds: float = 2.0,
    issues: tuple[DownloadIssue, ...] = (),
) -> DownloadSummary:
    return DownloadSummary(
        total_chapters=completed + skipped + partial + failed,
        completed=completed,
        skipped=skipped,
        partial=partial,
        failed=failed,
        total_bytes=total_bytes,
        elapsed_seconds=elapsed_seconds,
        issues=issues,
    )
