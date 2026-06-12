"""Textual smoke tests for TUI screens."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable, Input, Static

from comix_dl.core.application.cleanup_usecase import DownloadedSeries
from comix_dl.core.application.download_usecase import DownloadChapterEvent
from comix_dl.core.history import HistoryEntry
from comix_dl.core.models import ChapterInfo, SearchResult, SeriesInfo
from comix_dl.core.settings import Settings
from comix_dl.core.tui.app import ComixTuiApp, NavigationRail, StatusLog
from comix_dl.core.tui.screens.download import DownloadStatus, DownloadTitle
from comix_dl.core.tui.screens.manage import DownloadsPane, SettingsOutput
from comix_dl.core.tui.screens.series import SeriesTitle
from comix_dl.core.tui.state import SeriesNavigationState
from tests.flow_helpers import _make_summary

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class FakeController:
    def __init__(self, tmp_path: Path) -> None:
        self.opened = False
        self.closed = False
        self.settings = Settings(output_dir=str(tmp_path), default_format="pdf", optimize_images=True)
        self.output_dir = tmp_path
        self.search = AsyncMock(return_value=[])
        self.load_series = AsyncMock()
        self.download = AsyncMock()
        self.request_shutdown_called = False
        self.cleanup_plan_result: Any = SimpleNamespace(candidates=[], total_size_bytes=0)
        self.cleanup_result: Any = SimpleNamespace(removed_count=0, failed=[])
        self.applied_cleanup_plan: object | None = None

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    def list_downloads(self) -> Sequence[object]:
        return []

    def history_entries(self) -> Sequence[object]:
        return []

    def load_settings(self) -> Settings:
        return self.settings

    def request_shutdown(self) -> None:
        self.request_shutdown_called = True

    def cleanup_plan(self, series_title: str | None = None) -> Any:
        return self.cleanup_plan_result

    def apply_cleanup(self, plan: object) -> Any:
        self.applied_cleanup_plan = plan
        return self.cleanup_result


def _series() -> SeriesInfo:
    return SeriesInfo(
        title="Series A",
        authors=["Author A"],
        genres=["Action"],
        description="A short description",
        chapters=[
            ChapterInfo(title="Chapter 1", chapter_id=1, number="1", image_count=10),
            ChapterInfo(title="Chapter 2 Extra", chapter_id=2, number="2", image_count=12),
        ],
        url="https://comix.to/manga/series-a",
        hash_id="a",
    )


@pytest.mark.asyncio
async def test_app_mounts_search_screen_and_opens_controller(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        assert controller.opened is True
        assert app.query_one("#status-log", StatusLog).renderable == "Ready to search"

    assert controller.closed is True


@pytest.mark.asyncio
async def test_shell_starts_with_clickable_state_aware_navigation(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        rail = app.query_one("#sidebar", NavigationRail)

        assert "WORKFLOW" in rail.rendered_text
        assert "Search" in rail.rendered_text
        assert "Chapters" not in rail.rendered_text
        assert "Download" not in rail.rendered_text
        assert "TOOLS" in rail.rendered_text
        assert "Library" in rail.rendered_text
        assert "History" in rail.rendered_text
        assert "Settings" in rail.rendered_text
        assert "1 Search" not in rail.rendered_text
        assert app.query_one("#status-log", StatusLog).renderable == "Ready to search"


@pytest.mark.asyncio
async def test_sidebar_clicks_open_management_panes(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.click("#nav-library")
        await pilot.pause()
        assert app.query_one("#downloads-table", DataTable).row_count == 0
        assert app.query_one("#status-log", StatusLog).renderable == "Viewing library"

        await pilot.click("#nav-history")
        await pilot.pause()
        assert app.query_one("#history-table", DataTable).row_count == 0
        assert app.query_one("#status-log", StatusLog).renderable == "Viewing history"

        await pilot.click("#nav-settings")
        await pilot.pause()
        assert str(app.query_one("#settings-output", SettingsOutput).renderable) == f"Output folder: {tmp_path}"
        assert app.query_one("#status-log", StatusLog).renderable == "Viewing settings"


@pytest.mark.asyncio
async def test_keyboard_navigation_continues_after_shell_screen_switch(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("escape")
        await pilot.press("d")
        await pilot.pause()
        assert app.query_one("#downloads-table", DataTable).row_count == 0

        await pilot.press("h")
        await pilot.pause()

        assert app.query_one("#history-table", DataTable).row_count == 0


@pytest.mark.asyncio
async def test_search_screen_submits_query_and_renders_results(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()
        table = app.query_one("#results", DataTable)
        assert table.row_count == 1

    controller.search.assert_awaited_once_with("series")


@pytest.mark.asyncio
async def test_search_screen_guides_empty_query(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("enter")
        await pilot.pause()

        assert "Type a manga name to begin" in str(app.query_one("#search-help", Static).content)
        assert app.query_one("#status-log", StatusLog).renderable == (
            "Type a manga name, then press Enter to search."
        )


@pytest.mark.asyncio
async def test_search_results_update_global_status(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a"),
        SearchResult(title="Series B", url="https://comix.to/manga/series-b", slug="series-b", hash_id="b"),
    ]
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()

        assert app.query_one("#status-log", StatusLog).renderable == "2 results found"


@pytest.mark.asyncio
async def test_result_enter_opens_series_pane(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    controller.load_series.return_value = _series()
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#series-title", SeriesTitle).renderable == "Series A"

    controller.load_series.assert_awaited_once_with("a")


@pytest.mark.asyncio
async def test_chapters_navigation_appears_after_series_load(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    controller.load_series.return_value = _series()
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()

        assert "Chapters" not in app.query_one("#sidebar", NavigationRail).rendered_text

        await pilot.press("enter")
        await pilot.pause()

        assert "Chapters" in app.query_one("#sidebar", NavigationRail).rendered_text
        assert "1 Search" not in app.query_one("#sidebar", NavigationRail).rendered_text


@pytest.mark.asyncio
async def test_chapters_sidebar_click_restores_filter_selection_and_format(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    controller.load_series.return_value = _series()
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("/")
        await pilot.press(*"+extra")
        await pilot.press("enter")
        await pilot.press("space")
        await pilot.press("f")
        await pilot.click("#nav-library")
        await pilot.pause()
        await pilot.click("#nav-chapters")
        await pilot.pause()

        assert str(app.query_one("#selection-summary", Static).content) == "1 selected from 1 visible chapters."
        assert "Format: CBZ." in str(app.query_one("#series-status", Static).content)


@pytest.mark.asyncio
async def test_pending_series_load_does_not_replace_navigation_target(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    load_started = asyncio.Event()
    load_finished = asyncio.Event()

    async def _load_series(_identifier: str) -> SeriesInfo:
        load_started.set()
        await load_finished.wait()
        return _series()

    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    controller.load_series.side_effect = _load_series
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await asyncio.wait_for(load_started.wait(), timeout=1)
        await app.action_show_downloads()
        await pilot.pause()

        load_finished.set()
        await pilot.pause()

        assert app.query_one(DownloadsPane)
        with pytest.raises(NoMatches):
            app.query_one("#series-title", SeriesTitle)

    controller.load_series.assert_awaited_once_with("a")


@pytest.mark.asyncio
async def test_series_pane_filters_selects_and_starts_download(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    controller.load_series.return_value = _series()
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("/")
        await pilot.press(*"+extra")
        await pilot.press("enter")
        await pilot.press("space")
        await pilot.press("f")
        await pilot.press("d")
        await pilot.pause()
        assert app.query_one("#download-title", DownloadTitle).renderable == "Download"


@pytest.mark.asyncio
async def test_series_screen_shows_selection_guidance(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    controller.load_series.return_value = _series()
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert "Use +extra to keep matches or -extra to exclude them" in str(
            app.query_one("#filter-help", Static).content
        )
        assert str(app.query_one("#selection-summary", Static).content) == "0 selected from 2 visible chapters."
        assert app.query_one("#status-log", StatusLog).renderable == "Series loaded: Series A"


@pytest.mark.asyncio
async def test_series_download_without_selection_is_actionable(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.load_series.return_value = _series()
    from comix_dl.core.tui.screens.series import SeriesPane

    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        host = app.query_one("#screen-host")
        await host.remove_children()
        series_state = SeriesNavigationState.from_series(_series(), default_format="pdf")
        await host.mount(SeriesPane(controller, series_state))
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        assert str(app.query_one("#series-status", Static).content) == (
            "Select at least one chapter with Space, then press D to download."
        )
        assert app.query_one("#status-log", StatusLog).renderable == "Select chapters before downloading"


@pytest.mark.asyncio
async def test_download_pane_runs_download_and_renders_summary(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    controller.load_series.return_value = _series()

    async def fake_download(request: object, *, on_event: Any = None) -> object:
        assert on_event is not None
        on_event(DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2 Extra", kind="started"))
        on_event(DownloadChapterEvent(chapter_id=2, chapter_title="Chapter 2 Extra", kind="planned", total=12))
        on_event(
            DownloadChapterEvent(
                chapter_id=2,
                chapter_title="Chapter 2 Extra",
                kind="progress",
                completed=12,
                total=12,
            )
        )
        on_event(
            DownloadChapterEvent(
                chapter_id=2,
                chapter_title="Chapter 2 Extra",
                kind="converted",
                output_name="Chapter 2 Extra.cbz",
            )
        )
        return _make_summary(completed=1, total_bytes=4096, elapsed_seconds=4.0)

    controller.download = AsyncMock(side_effect=fake_download)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("/")
        await pilot.press(*"+extra")
        await pilot.press("enter")
        await pilot.press("space")
        await pilot.press("d")
        await pilot.pause()
        assert "completed" in str(app.query_one("#download-status", DownloadStatus).renderable)

    controller.download.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_screen_shows_batch_summary(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.download.return_value = _make_summary(completed=1, total_bytes=2048, elapsed_seconds=2.0)
    from comix_dl.core.tui.screens.download import DownloadPane
    from comix_dl.core.tui.state import DownloadRequest

    series = _series()
    request = DownloadRequest(series_title=series.title, chapters=tuple(series.chapters[:1]), fmt="pdf", optimize=True)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        host = app.query_one("#screen-host")
        await host.remove_children()
        await host.mount(DownloadPane(controller, request))
        await pilot.pause()

        assert str(app.query_one("#download-title", DownloadTitle).renderable) == "Download"
        assert "Series A · 1 chapter(s) · PDF" in str(app.query_one("#download-summary", Static).content)
        assert "Next: cleanup raw folders, return to Search, or inspect Library." in str(
            app.query_one("#download-status", DownloadStatus).renderable
        )
        assert app.query_one("#status-log", StatusLog).renderable == "Download complete"


@pytest.mark.asyncio
async def test_download_cancel_requests_shutdown(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    from comix_dl.core.tui.screens.download import DownloadPane
    from comix_dl.core.tui.state import DownloadRequest

    series = _series()
    request = DownloadRequest(series_title=series.title, chapters=tuple(series.chapters[:1]), fmt="pdf", optimize=True)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(100, 32)) as pilot:
        host = app.query_one("#screen-host")
        await host.remove_children()
        await host.mount(DownloadPane(controller, request))
        await pilot.pause()
        await pilot.press("c")
        assert controller.request_shutdown_called is True


@pytest.mark.asyncio
async def test_download_cleanup_button_applies_plan_and_renders_result(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.download.return_value = _make_summary(completed=1)
    cleanup_plan = SimpleNamespace(candidates=[SimpleNamespace()], total_size_bytes=2048)
    controller.cleanup_plan_result = cleanup_plan
    controller.cleanup_result = SimpleNamespace(removed_count=1, failed=[])
    from comix_dl.core.tui.screens.download import DownloadPane
    from comix_dl.core.tui.state import DownloadRequest

    series = _series()
    request = DownloadRequest(series_title=series.title, chapters=tuple(series.chapters[:1]), fmt="pdf", optimize=True)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(100, 32)) as pilot:
        host = app.query_one("#screen-host")
        await host.remove_children()
        await host.mount(DownloadPane(controller, request))
        await pilot.pause()

        assert app.query_one("#cleanup-button", Button).disabled is False
        controller.cleanup_plan_result = SimpleNamespace(
            candidates=[SimpleNamespace(), SimpleNamespace()],
            total_size_bytes=4096,
        )

        await pilot.click("#cleanup-button")
        await pilot.pause()

        assert controller.applied_cleanup_plan is cleanup_plan
        assert "Cleanup removed 1 raw folder(s)." in str(
            app.query_one("#download-status", DownloadStatus).renderable
        )


@pytest.mark.asyncio
async def test_downloads_history_and_settings_panes_render_controller_data(tmp_path: Path) -> None:
    class ManagementController(FakeController):
        def list_downloads(self) -> list[DownloadedSeries]:
            return [
                DownloadedSeries(
                    name="Series A",
                    path=tmp_path / "Series A",
                    completed_chapters=2,
                    total_size_bytes=2048,
                )
            ]

        def history_entries(self) -> list[HistoryEntry]:
            return [
                HistoryEntry(
                    timestamp="2026-06-11T00:00:00+00:00",
                    title="Series A",
                    chapters_count=2,
                    format="cbz",
                    total_size_bytes=2048,
                    completed=2,
                    summary_text="2 completed",
                )
            ]

    controller = ManagementController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("escape")
        await pilot.press("d")
        await pilot.pause()
        assert app.query_one("#downloads-table", DataTable).row_count == 1
        await pilot.press("h")
        await pilot.pause()
        assert app.query_one("#history-table", DataTable).row_count == 1
        await pilot.press("g")
        await pilot.pause()
        assert str(app.query_one("#settings-output", SettingsOutput).renderable) == f"Output folder: {tmp_path}"


@pytest.mark.asyncio
async def test_management_panes_show_empty_states(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("escape")
        await pilot.press("d")
        await pilot.pause()
        assert "No downloads yet. Completed manga will appear here." in str(
            app.query_one("#library-empty", Static).content
        )

        await pilot.press("h")
        await pilot.pause()
        assert "No history yet. Finished downloads will be listed here." in str(
            app.query_one("#history-empty", Static).content
        )


@pytest.mark.asyncio
async def test_settings_pane_uses_labeled_rows(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("escape")
        await pilot.press("g")
        await pilot.pause()

        assert str(app.query_one("#settings-output", SettingsOutput).renderable) == f"Output folder: {tmp_path}"
        assert "Default format: pdf" in str(app.query_one("#settings-format", Static).content)
        assert "Changes apply to the next app session." in str(app.query_one("#settings-note", Static).content)


@pytest.mark.asyncio
async def test_search_input_accepts_query_starting_with_navigation_letter(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press(*"dragon")
        await pilot.pause()

        assert app.query_one("#search-input", Input).value == "dragon"
        with pytest.raises(NoMatches):
            app.query_one("#downloads-table", DataTable)


@pytest.mark.asyncio
async def test_search_input_escape_allows_global_navigation_shortcut(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press(*"dragon")
        await pilot.press("escape")
        await pilot.press("d")
        await pilot.pause()

        assert app.query_one("#downloads-table", DataTable).row_count == 0


@pytest.mark.asyncio
async def test_search_input_keeps_normal_text_entry_after_first_character(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("n")
        await pilot.press("d")
        await pilot.press("g")
        await pilot.pause()

        assert app.query_one("#search-input", Input).value == "ndg"
