"""Tests for application cleanup use cases."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

from comix_dl.core.application.cleanup_usecase import apply_cleanup_plan, build_cleanup_plan, list_downloaded_series

if TYPE_CHECKING:
    from pathlib import Path


def _write_text(path: Path, content: str = "data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_valid_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")


def _write_valid_cbz(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.jpg", b"\xff\xd8\xff\xe0")


class TestListDownloadedSeries:
    def test_summarizes_completed_chapters_and_output_size(self, tmp_path: Path) -> None:
        manga_dir = tmp_path / "Series A"
        chapter_dir = manga_dir / "Chapter 1"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / ".complete").touch()
        pdf = manga_dir / "Chapter 1.pdf"
        pdf.write_bytes(b"x" * 2048)

        empty_dir = tmp_path / "Empty Series"
        empty_dir.mkdir()

        result = list_downloaded_series(tmp_path)

        assert [item.name for item in result] == ["Series A"]
        assert result[0].completed_chapters == 1
        assert result[0].total_size_bytes == 2048


class TestBuildCleanupPlan:
    def test_includes_only_complete_chapters_with_converted_output(self, tmp_path: Path) -> None:
        kept_dir = tmp_path / "Series A" / "Chapter 1"
        kept_dir.mkdir(parents=True)
        (kept_dir / ".complete").touch()
        _write_valid_pdf(kept_dir.parent / "Chapter 1.pdf")
        (kept_dir / "001.jpg").write_bytes(b"image-data")

        ignored_dir = tmp_path / "Series A" / "Chapter 2"
        ignored_dir.mkdir()
        (ignored_dir / "001.jpg").write_bytes(b"partial")

        plan = build_cleanup_plan(tmp_path)

        assert [candidate.relative_path.as_posix() for candidate in plan.candidates] == ["Series A/Chapter 1"]
        assert plan.candidates[0].size_bytes == len(b"image-data")
        assert plan.total_size_bytes == len(b"image-data")

    def test_ignores_complete_chapter_when_converted_output_is_invalid(self, tmp_path: Path) -> None:
        chapter_dir = tmp_path / "Series A" / "Chapter 1"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / ".complete").touch()
        (chapter_dir / "001.jpg").write_bytes(b"image-data")
        (chapter_dir.parent / "Chapter 1.pdf").write_bytes(b"not-a-pdf")

        plan = build_cleanup_plan(tmp_path)

        assert plan.candidates == []

    def test_ignores_complete_chapter_when_cbz_contains_no_images(self, tmp_path: Path) -> None:
        chapter_dir = tmp_path / "Series A" / "Chapter 1"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / ".complete").touch()
        (chapter_dir / "001.jpg").write_bytes(b"image-data")
        with zipfile.ZipFile(chapter_dir.parent / "Chapter 1.cbz", "w") as zf:
            zf.writestr("readme.txt", "not an image")

        plan = build_cleanup_plan(tmp_path)

        assert plan.candidates == []

    def test_can_scope_cleanup_to_one_series(self, tmp_path: Path) -> None:
        scoped_dir = tmp_path / "Series - Special" / "Chapter 1"
        scoped_dir.mkdir(parents=True)
        (scoped_dir / ".complete").touch()
        _write_valid_cbz(scoped_dir.parent / "Chapter 1.cbz")

        other_dir = tmp_path / "Series B" / "Chapter 2"
        other_dir.mkdir(parents=True)
        (other_dir / ".complete").touch()
        _write_valid_pdf(other_dir.parent / "Chapter 2.pdf")

        plan = build_cleanup_plan(tmp_path, series_title="Series: Special")

        assert [candidate.relative_path.as_posix() for candidate in plan.candidates] == ["Series - Special/Chapter 1"]


class TestApplyCleanupPlan:
    def test_removes_candidate_directories(self, tmp_path: Path) -> None:
        chapter_dir = tmp_path / "Series A" / "Chapter 1"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / ".complete").touch()
        _write_valid_pdf(chapter_dir.parent / "Chapter 1.pdf")
        _write_text(chapter_dir / "001.jpg", "image")

        plan = build_cleanup_plan(tmp_path)
        result = apply_cleanup_plan(plan)

        assert result.removed_count == 1
        assert result.failed == []
        assert not chapter_dir.exists()
