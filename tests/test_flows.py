"""Tests for CLI flow display and prompt helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from comix_dl.core.application.download_usecase import DownloadChapterEvent, DownloadIssue
from comix_dl.core.cli import flows
from comix_dl.core.errors import RemoteApiError
from tests.flow_helpers import _FakeProgress, _make_chapters, _make_series, _make_summary

if TYPE_CHECKING:
    import pytest


def test_render_series_info_panel_includes_truncated_metadata() -> None:
    info = _make_series(description="x" * 320)

    with flows.console.capture() as capture:
        flows._render_series_info_panel(info)

    output = capture.get()
    assert "Manga Info" in output
    assert "Series A" in output
    assert "Authors:" in output
    assert "Genres:" in output
    assert "https://comix.to/manga/series-a" in output
    assert "…" in output


def test_render_remote_api_error_outputs_message() -> None:
    with flows.console.capture() as capture:
        flows._render_remote_api_error(RemoteApiError("Cloudflare challenge still active"))

    assert "Cloudflare challenge still active" in capture.get()


def test_prompt_chapter_selection_returns_none_for_quit(monkeypatch: pytest.MonkeyPatch) -> None:
    chapters = _make_chapters()

    monkeypatch.setattr(flows, "filter_chapters_interactive", lambda items: items)
    monkeypatch.setattr(flows.Prompt, "ask", lambda *_args, **_kwargs: "q")

    assert flows._prompt_chapter_selection(chapters) is None


def test_prompt_chapter_selection_returns_empty_for_invalid_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    chapters = _make_chapters()

    monkeypatch.setattr(flows, "filter_chapters_interactive", lambda items: items)
    monkeypatch.setattr(flows.Prompt, "ask", lambda *_args, **_kwargs: "7")
    monkeypatch.setattr(flows, "parse_chapter_selection", lambda *_args, **_kwargs: [])

    with flows.console.capture() as capture:
        result = flows._prompt_chapter_selection(chapters)

    assert result == []
    assert "No valid chapters selected." in capture.get()


def test_prompt_chapter_selection_lists_selection_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    chapters = _make_chapters(12)

    monkeypatch.setattr(flows, "filter_chapters_interactive", lambda items: items)
    monkeypatch.setattr(flows.Prompt, "ask", lambda *_args, **_kwargs: "all")
    monkeypatch.setattr(flows, "parse_chapter_selection", lambda *_args, **_kwargs: chapters)

    with flows.console.capture() as capture:
        result = flows._prompt_chapter_selection(chapters)

    assert result == chapters
    output = capture.get()
    assert "Selected 12 chapter(s)" in output
    assert "Chapter 1" in output
    assert "… and 2 more" in output


def test_render_download_event_covers_all_supported_kinds() -> None:
    progress = _FakeProgress()
    task_ids: dict[int, int] = {}

    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="skipped"),
    )
    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="skipped"),
    )
    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2", kind="started"),
    )
    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2", kind="planned", total=5),
    )
    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2", kind="progress", completed=2),
    )
    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2", kind="progress", completed=3, total=5),
    )
    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2", kind="missing_images"),
    )
    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2", kind="failed"),
    )
    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2", kind="partial"),
    )
    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2", kind="converted"),
    )
    flows._render_download_event(
        progress,
        task_ids,
        DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2", kind="conversion_failed"),
    )

    assert progress.added[0]["description"] == "  [dim]↳ Chapter 1 (skipped)[/dim]"
    assert progress.added[1]["description"] == "  Chapter 2"
    assert progress.updated[0][1]["description"] == "  [dim]↳ Chapter 1 (skipped)[/dim]"
    assert progress.updated[1][1]["description"] == "  Chapter 2"
    assert progress.updated[2][1] == {"total": 5, "completed": 0}
    assert progress.updated[3][1] == {"completed": 2}
    assert progress.updated[4][1] == {"completed": 3, "total": 5}
    assert progress.updated[5][1]["description"] == "  [red]✗ Chapter 2 (no images)[/red]"
    assert progress.updated[6][1]["description"] == "  [red]✗ Chapter 2[/red]"
    assert progress.updated[7][1]["description"] == "  [yellow]⚠ Chapter 2 is incomplete[/yellow]"
    assert progress.updated[8][1]["description"] == "  [green]✓ Chapter 2[/green]"
    assert progress.updated[9][1]["description"] == "  [yellow]⚠ Chapter 2 (convert failed)[/yellow]"


def test_render_download_summary_shows_issue_preview() -> None:
    issues = tuple(
        DownloadIssue(chapter_title=f"Chapter {idx}", kind="failed", message="boom")
        for idx in range(1, 7)
    )
    summary = _make_summary(completed=1, failed=1, total_bytes=4096, elapsed_seconds=4.0, issues=issues)

    with flows.console.capture() as capture:
        flows._render_download_summary(summary, Path("/tmp/output"))

    output = capture.get()
    assert "Download Summary" in output
    assert "Issues" in output
    assert "Chapter 1: boom" in output
    assert "... and 1 more issue(s)" in output
    assert "/tmp/output" in output
