"""Tests for CLI flow download and lookup paths."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from comix_dl.core.application.download_usecase import DownloadChapterEvent, DownloadSummary
from comix_dl.core.application.query_usecase import SeriesLookupResult
from comix_dl.core.cli import flows
from comix_dl.core.config import AppConfig
from comix_dl.core.errors import RemoteApiError
from comix_dl.core.models import SearchResult
from comix_dl.core.settings import Settings
from tests.flow_helpers import (
    _FakeProgress,
    _make_chapters,
    _make_series,
    _make_session,
    _make_summary,
    _SessionContext,
)

if TYPE_CHECKING:
    from pathlib import Path


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

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())

    assert await flows.flow_search("naruto") == 0


@pytest.mark.asyncio
async def test_flow_search_surfaces_remote_api_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path)
    session.search.side_effect = RemoteApiError("Search for 'omori' failed")

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())

    with flows.console.capture() as capture:
        result = await flows.flow_search("omori")

    assert result == 1
    assert "Search for 'omori' failed" in capture.get()


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

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(session))
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
async def test_confirm_and_download_series_reuses_interactive_download_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path, default_format="cbz", optimize_images=False)
    info = _make_series(chapters=_make_chapters(2))
    selected = info.chapters[:1]
    download_with_progress = AsyncMock()

    monkeypatch.setattr(flows, "print_series_header", MagicMock())
    monkeypatch.setattr(flows, "print_dedup_report", MagicMock())
    monkeypatch.setattr(flows, "print_chapters_table", MagicMock())
    monkeypatch.setattr(flows, "_prompt_chapter_selection", MagicMock(return_value=selected))
    monkeypatch.setattr(flows, "_download_with_progress", download_with_progress)
    monkeypatch.setattr(flows.Prompt, "ask", MagicMock(return_value="pdf"))

    result = await flows._confirm_and_download_series(session, info, quiet=True)

    assert result == 0
    download_with_progress.assert_awaited_once_with(
        session,
        series_title="Series A",
        chapters=selected,
        fmt="pdf",
        optimize=False,
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

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(session))
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

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(session))
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

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())

    assert await flows.flow_url_download("missing") == 1


@pytest.mark.asyncio
async def test_flow_url_download_surfaces_remote_api_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path)
    session.resolve_series.side_effect = RemoteApiError("API blocked")

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())

    with flows.console.capture() as capture:
        result = await flows.flow_url_download("series-a")

    assert result == 1
    assert "API blocked" in capture.get()


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
async def test_flow_noninteractive_download_surfaces_remote_api_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path)
    session.resolve_series.side_effect = RemoteApiError("lookup failed")

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(session))

    with flows.console.capture() as capture:
        result = await flows.flow_noninteractive_download("missing", "all")

    assert result == 1
    assert "lookup failed" in capture.get()


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
