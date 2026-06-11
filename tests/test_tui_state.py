"""Tests for pure TUI state helpers."""

from __future__ import annotations

import dataclasses

import pytest

from comix_dl.core.application.download_usecase import DownloadChapterEvent, DownloadSummary
from comix_dl.core.models import ChapterInfo
from comix_dl.core.tui.state import (
    ChapterSelectionState,
    DownloadRequest,
    DownloadRowsState,
    DownloadStatus,
    format_summary_line,
)


def _chapters() -> list[ChapterInfo]:
    return [
        ChapterInfo(title="Chapter 1 - Start", chapter_id=1, number="1", image_count=10),
        ChapterInfo(title="Chapter 2 - Extra", chapter_id=2, number="2", image_count=12),
        ChapterInfo(title="Chapter 3 - Stage", chapter_id=3, number="3", image_count=8),
    ]


def _status(rows: DownloadRowsState, chapter_id: int) -> DownloadStatus:
    return rows.rows[chapter_id].status


def _detail(rows: DownloadRowsState, chapter_id: int) -> str:
    return rows.rows[chapter_id].detail


def test_chapter_selection_starts_with_all_chapters_visible() -> None:
    state = ChapterSelectionState.from_chapters(_chapters())

    assert [chapter.chapter_id for chapter in state.chapters] == [1, 2, 3]
    assert [chapter.chapter_id for chapter in state.visible_chapters] == [1, 2, 3]
    assert state.selected_ids == set()
    assert state.status == ""


def test_filter_keeps_and_removes_terms() -> None:
    state = ChapterSelectionState.from_chapters(_chapters())

    state.apply_filter("+chapter -extra")

    assert [chapter.chapter_id for chapter in state.visible_chapters] == [1, 3]


def test_empty_filter_resets_to_all_chapters() -> None:
    state = ChapterSelectionState.from_chapters(_chapters())

    state.apply_filter("+stage")
    state.apply_filter(" ")

    assert [chapter.chapter_id for chapter in state.visible_chapters] == [1, 2, 3]


def test_filter_keeps_previous_visible_list_when_result_is_empty() -> None:
    state = ChapterSelectionState.from_chapters(_chapters())

    state.apply_filter("+stage")
    state.apply_filter("+missing")

    assert [chapter.chapter_id for chapter in state.visible_chapters] == [3]
    assert state.status == "No chapters matched '+missing'; keeping previous filter."


def test_selection_tracks_visible_rows_and_extracts_chapters() -> None:
    state = ChapterSelectionState.from_chapters(_chapters())

    state.toggle(2)
    state.select_visible()
    state.apply_filter("+stage")
    state.clear_visible()

    assert [chapter.chapter_id for chapter in state.selected_chapters] == [1, 2]
    assert state.selected_count == 2

    state.clear_all()

    assert state.selected_chapters == []
    assert state.selected_count == 0


def test_download_request_is_immutable_batch_input() -> None:
    chapters = _chapters()[:2]
    request = DownloadRequest(series_title="Series A", chapters=tuple(chapters), fmt="cbz", optimize=True)

    assert request.series_title == "Series A"
    assert request.chapters == tuple(chapters)
    assert request.fmt == "cbz"
    assert request.optimize is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.series_title = "Series B"  # type: ignore[misc]


def test_download_rows_start_queued_from_chapters() -> None:
    rows = DownloadRowsState.from_chapters(_chapters()[:2])

    assert list(rows.rows) == [1, 2]
    assert rows.rows[1].title == "Chapter 1 - Start"
    assert rows.rows[1].status == "queued"
    assert rows.rows[1].progress_text == "0/?"


def test_download_rows_reduce_all_event_kinds() -> None:
    rows = DownloadRowsState.from_chapters(_chapters()[:2])

    rows.apply(DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="skipped"))
    assert _status(rows, 1) == "skipped"
    assert rows.rows[1].completed == 1
    assert rows.rows[1].total == 1

    rows.apply(DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="started"))
    assert _status(rows, 1) == "started"

    rows.apply(DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="planned", total=10))
    assert _status(rows, 1) == "planned"
    assert rows.rows[1].completed == 0
    assert rows.rows[1].total == 10

    rows.apply(DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="progress", completed=4, total=10))
    assert _status(rows, 1) == "progress"
    assert rows.rows[1].completed == 4
    assert rows.rows[1].total == 10

    rows.apply(DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="missing_images"))
    assert _status(rows, 1) == "missing_images"
    assert _detail(rows, 1) == "No images found."

    rows.apply(DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="failed", message="network error"))
    assert _status(rows, 1) == "failed"
    assert _detail(rows, 1) == "network error"

    rows.apply(
        DownloadChapterEvent(
            chapter_id=1,
            chapter_title="Chapter 1",
            kind="converted",
            output_name="Chapter 1.cbz",
        )
    )
    assert _status(rows, 1) == "converted"
    assert rows.rows[1].completed == 10
    assert rows.rows[1].total == 10
    assert _detail(rows, 1) == "Chapter 1.cbz"

    rows.apply(
        DownloadChapterEvent(
            chapter_id=2,
            chapter_title="Chapter 2",
            kind="partial",
            message="2 image(s) failed",
        )
    )
    assert _status(rows, 2) == "partial"
    assert _detail(rows, 2) == "2 image(s) failed"

    rows.apply(DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2", kind="conversion_failed"))
    assert _status(rows, 2) == "conversion_failed"
    assert _detail(rows, 2) == "Conversion failed."


def test_download_row_progress_text() -> None:
    rows = DownloadRowsState.from_chapters(_chapters()[:1])

    rows.apply(DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="planned", total=10))
    assert rows.rows[1].progress_text == "0/10"

    rows.apply(DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="progress", completed=4))
    assert rows.rows[1].progress_text == "4/10"


def test_format_summary_line_is_compact() -> None:
    summary = DownloadSummary(
        total_chapters=3,
        completed=2,
        skipped=1,
        partial=0,
        failed=0,
        total_bytes=2048,
        elapsed_seconds=2.0,
    )

    assert format_summary_line(summary) == "2 completed · 1 skipped · 2.0 KB · 1.0 KB/s · 2.0s"
