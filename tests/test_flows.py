"""Focused tests for CLI flow adapter behaviors."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from comix_dl.application.cleanup_usecase import (
    CleanupCandidate,
    CleanupPlan,
    CleanupResult,
    DownloadedSeries,
)
from comix_dl.application.download_usecase import DownloadChapterEvent, DownloadIssue, DownloadSummary
from comix_dl.application.query_usecase import SeriesLookupResult
from comix_dl.application.session import RuntimeContext
from comix_dl.cli import flows
from comix_dl.comix_service import ChapterInfo, SearchResult, SeriesInfo
from comix_dl.config import AppConfig
from comix_dl.settings import Settings


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


@pytest.mark.asyncio
async def test_download_with_progress_runs_download_and_auto_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(output_dir=tmp_path, download=AsyncMock())
    summary = _make_summary(completed=1)

    async def fake_download(**kwargs: object) -> DownloadSummary:
        on_event = kwargs["on_event"]
        assert callable(on_event)
        on_event(DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="started"))
        return summary

    session.download.side_effect = fake_download

    render_summary = MagicMock()
    auto_cleanup = MagicMock()

    monkeypatch.setattr(flows, "Progress", _FakeProgress)
    monkeypatch.setattr(flows, "_render_download_summary", render_summary)
    monkeypatch.setattr(flows, "_auto_cleanup_prompt", auto_cleanup)

    await flows._download_with_progress(
        session,
        series_title="Series A",
        chapters=_make_chapters(1),
        fmt="pdf",
        optimize=False,
        auto_cleanup=True,
    )

    session.download.assert_awaited_once()
    call_kwargs = session.download.await_args.kwargs
    assert call_kwargs["series_title"] == "Series A"
    assert call_kwargs["fmt"] == "pdf"
    assert call_kwargs["optimize"] is False
    assert callable(call_kwargs["is_shutdown"])
    render_summary.assert_called_once_with(summary, tmp_path)
    auto_cleanup.assert_called_once_with(tmp_path, "Series A", auto_confirm=True)


@pytest.mark.asyncio
async def test_flow_search_returns_zero_when_no_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path)
    session.search.return_value = []

    monkeypatch.setattr(flows, "open_application_session", lambda: _SessionContext(session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())

    assert await flows.flow_search("naruto") == 0


@pytest.mark.asyncio
async def test_flow_search_reloads_after_info_preview_and_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path, default_format="pdf")
    results = [SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")]
    info = _make_series(chapters=_make_chapters(2))
    session.search.return_value = results
    session.load_series.return_value = info

    print_search_table = MagicMock()
    download_with_progress = AsyncMock()

    monkeypatch.setattr(flows, "open_application_session", lambda: _SessionContext(session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(flows, "print_search_table", print_search_table)
    monkeypatch.setattr(flows, "print_series_header", MagicMock())
    monkeypatch.setattr(flows, "print_dedup_report", MagicMock())
    monkeypatch.setattr(flows, "print_chapters_table", MagicMock())
    monkeypatch.setattr(flows, "_prompt_chapter_selection", MagicMock(return_value=info.chapters[:1]))
    monkeypatch.setattr(flows, "_download_with_progress", download_with_progress)
    monkeypatch.setattr(flows, "_render_series_info_panel", MagicMock())
    monkeypatch.setattr(flows.Prompt, "ask", MagicMock(side_effect=["1i", "n", "1", "pdf"]))

    result = await flows.flow_search("series", quiet=True)

    assert result == 0
    assert print_search_table.call_count == 2
    assert session.load_series.await_count == 2
    download_with_progress.assert_awaited_once_with(
        session,
        series_title="Series A",
        chapters=info.chapters[:1],
        fmt="pdf",
        optimize=True,
        auto_cleanup=True,
    )


@pytest.mark.asyncio
async def test_flow_search_returns_one_when_no_chapters_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path)
    session.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    session.load_series.return_value = _make_series(chapters=_make_chapters(2))

    monkeypatch.setattr(flows, "open_application_session", lambda: _SessionContext(session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(flows, "print_search_table", MagicMock())
    monkeypatch.setattr(flows, "print_series_header", MagicMock())
    monkeypatch.setattr(flows, "print_dedup_report", MagicMock())
    monkeypatch.setattr(flows, "print_chapters_table", MagicMock())
    monkeypatch.setattr(flows, "_prompt_chapter_selection", MagicMock(return_value=[]))
    monkeypatch.setattr(flows.Prompt, "ask", MagicMock(side_effect=["1"]))

    assert await flows.flow_search("series") == 1


@pytest.mark.asyncio
async def test_flow_url_download_uses_suggestions_then_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path, default_format="both", optimize_images=False)
    suggestions = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    info = _make_series(chapters=_make_chapters(2))
    session.resolve_series.return_value = SeriesLookupResult(slug="series-a", series=None, suggestions=suggestions)
    session.load_series.return_value = info

    download_with_progress = AsyncMock()

    monkeypatch.setattr(flows, "open_application_session", lambda: _SessionContext(session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(flows, "print_search_table", MagicMock())
    monkeypatch.setattr(flows, "print_series_header", MagicMock())
    monkeypatch.setattr(flows, "print_dedup_report", MagicMock())
    monkeypatch.setattr(flows, "print_chapters_table", MagicMock())
    monkeypatch.setattr(flows, "_prompt_chapter_selection", MagicMock(return_value=info.chapters))
    monkeypatch.setattr(flows, "_download_with_progress", download_with_progress)
    monkeypatch.setattr(flows.Prompt, "ask", MagicMock(side_effect=["1", "cbz"]))

    result = await flows.flow_url_download("series-a", quiet=True)

    assert result == 0
    session.load_series.assert_awaited_once_with("a")
    download_with_progress.assert_awaited_once_with(
        session,
        series_title="Series A",
        chapters=info.chapters,
        fmt="cbz",
        optimize=False,
        auto_cleanup=True,
    )


@pytest.mark.asyncio
async def test_flow_url_download_returns_one_when_series_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path)
    session.resolve_series.return_value = SeriesLookupResult(slug="missing", series=None, suggestions=[])

    monkeypatch.setattr(flows, "open_application_session", lambda: _SessionContext(session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())

    assert await flows.flow_url_download("missing") == 1


@pytest.mark.asyncio
async def test_flow_noninteractive_download_returns_one_when_series_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path)
    session.resolve_series.return_value = SeriesLookupResult(slug="missing", series=None, suggestions=[])

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(session))

    assert await flows.flow_noninteractive_download("missing", "all") == 1


@pytest.mark.asyncio
async def test_flow_noninteractive_download_uses_resolved_settings_and_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path, default_format="both", optimize_images=True)
    info = _make_series(chapters=_make_chapters(3))
    session.resolve_series.return_value = SeriesLookupResult(slug="series-a", series=info, suggestions=[])
    captured: dict[str, object] = {}
    download_with_progress = AsyncMock()

    def open_session(**kwargs: object) -> _SessionContext:
        captured.update(kwargs)
        return _SessionContext(session)

    monkeypatch.setattr(flows, "open_application_session", open_session)
    monkeypatch.setattr(flows, "parse_chapter_selection", MagicMock(return_value=info.chapters[:2]))
    monkeypatch.setattr(flows, "_download_with_progress", download_with_progress)

    settings = Settings(output_dir=str(tmp_path), default_format="both", optimize_images=True)
    config = AppConfig()

    result = await flows.flow_noninteractive_download(
        "series-a",
        "1-2",
        output=str(tmp_path / "custom"),
        optimize=False,
        settings=settings,
        config=config,
        quiet=True,
    )

    assert result == 0
    assert captured["settings"] is settings
    assert captured["config"] is config
    assert captured["output"] == str(tmp_path / "custom")
    download_with_progress.assert_awaited_once_with(
        session,
        series_title="Series A",
        chapters=info.chapters[:2],
        fmt="both",
        optimize=False,
        auto_cleanup=True,
    )


@pytest.mark.asyncio
async def test_flow_info_handles_missing_and_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_session = _make_session(tmp_path)
    missing_session.resolve_series.return_value = SeriesLookupResult(slug="missing", series=None, suggestions=[])

    monkeypatch.setattr(flows, "open_application_session", lambda: _SessionContext(missing_session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())

    assert await flows.flow_info("missing") == 1

    success_session = _make_session(tmp_path)
    success_session.resolve_series.return_value = SeriesLookupResult(
        slug="series-a",
        series=_make_series(),
        suggestions=[],
    )
    render_panel = MagicMock()
    monkeypatch.setattr(flows, "open_application_session", lambda: _SessionContext(success_session))
    monkeypatch.setattr(flows, "_render_series_info_panel", render_panel)

    assert await flows.flow_info("series-a") == 0
    render_panel.assert_called_once()


def test_flow_list_handles_missing_output_dir(tmp_path: Path) -> None:
    missing_dir = tmp_path / "missing"

    with patch.object(
        flows,
        "load_runtime",
        return_value=RuntimeContext(
            settings=Settings(output_dir=str(missing_dir)),
            config=AppConfig(),
            output_dir=missing_dir,
        ),
    ):
        assert flows.flow_list() == 0


def test_flow_list_renders_downloaded_series_table(tmp_path: Path) -> None:
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()

    with (
        patch.object(
            flows,
            "load_runtime",
            return_value=RuntimeContext(
                settings=Settings(output_dir=str(output_dir)),
                config=AppConfig(),
                output_dir=output_dir,
            ),
        ),
        patch.object(
            flows,
            "list_downloaded_series",
            return_value=[
                DownloadedSeries(
                    name="Series A",
                    path=output_dir / "Series A",
                    completed_chapters=2,
                    total_size_bytes=2048,
                )
            ],
        ),
        flows.console.capture() as capture,
    ):
        result = flows.flow_list()

    assert result == 0
    output = capture.get()
    assert "Downloaded Manga" in output
    assert "Series A" in output
    assert "2.0 KB" in output


def test_flow_clean_auto_confirm_skips_prompt_and_removes_candidates(tmp_path: Path) -> None:
    chapter_dir = tmp_path / "Series A" / "Chapter 1"
    chapter_dir.mkdir(parents=True)
    (chapter_dir / ".complete").touch()
    (chapter_dir.parent / "Chapter 1.pdf").write_bytes(b"pdf")
    (chapter_dir / "001.jpg").write_bytes(b"image")

    with (
        patch.object(
            flows,
            "load_runtime",
            return_value=RuntimeContext(
                settings=Settings(output_dir=str(tmp_path)),
                config=AppConfig(),
                output_dir=tmp_path,
            ),
        ),
        patch.object(flows.Prompt, "ask", side_effect=AssertionError("prompt should not be used")),
    ):
        result = flows.flow_clean(auto_confirm=True)

    assert result == 0
    assert not chapter_dir.exists()


def test_flow_clean_handles_cancel_and_failure_reporting(tmp_path: Path) -> None:
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    candidate = CleanupCandidate(
        path=output_dir / "Series A" / "Chapter 1",
        relative_path=Path("Series A/Chapter 1"),
        size_bytes=1024,
    )
    plan = CleanupPlan(output_dir=output_dir, candidates=[candidate], total_size_bytes=1024)

    with (
        patch.object(
            flows,
            "load_runtime",
            return_value=RuntimeContext(
                settings=Settings(output_dir=str(output_dir)),
                config=AppConfig(),
                output_dir=output_dir,
            ),
        ),
        patch.object(flows, "build_cleanup_plan", return_value=plan),
        patch.object(flows.Prompt, "ask", return_value="n"),
        patch.object(flows, "apply_cleanup_plan", side_effect=AssertionError("cleanup should not run")),
    ):
        assert flows.flow_clean() == 0

    with (
        patch.object(
            flows,
            "load_runtime",
            return_value=RuntimeContext(
                settings=Settings(output_dir=str(output_dir)),
                config=AppConfig(),
                output_dir=output_dir,
            ),
        ),
        patch.object(flows, "build_cleanup_plan", return_value=plan),
        patch.object(
            flows,
            "apply_cleanup_plan",
            return_value=CleanupResult(
                removed_count=0,
                failed=[(candidate.path, "permission denied")],
            ),
        ),
        flows.console.capture() as capture,
    ):
        result = flows.flow_clean(force=True)

    assert result == 0
    output = capture.get()
    assert "Failed to remove Chapter 1: permission denied" in output
    assert "freed 0.0 B" in output


def test_auto_cleanup_prompt_respects_decline_and_auto_confirm(tmp_path: Path) -> None:
    output_dir = tmp_path / "downloads"
    candidate = CleanupCandidate(
        path=output_dir / "Series A" / "Chapter 1",
        relative_path=Path("Series A/Chapter 1"),
        size_bytes=2048,
    )
    plan = CleanupPlan(output_dir=output_dir, candidates=[candidate], total_size_bytes=2048)

    with (
        patch.object(flows, "build_cleanup_plan", return_value=plan),
        patch.object(flows.Prompt, "ask", return_value="n"),
        patch.object(flows, "apply_cleanup_plan", side_effect=AssertionError("cleanup should not run")),
    ):
        flows._auto_cleanup_prompt(output_dir, "Series A", auto_confirm=False)

    with (
        patch.object(flows, "build_cleanup_plan", return_value=plan),
        patch.object(
            flows,
            "apply_cleanup_plan",
            return_value=CleanupResult(removed_count=1, failed=[]),
        ),
        flows.console.capture() as capture,
    ):
        flows._auto_cleanup_prompt(output_dir, "Series A", auto_confirm=True)

    assert "Cleaned 1 dir(s), freed 2.0 KB" in capture.get()
