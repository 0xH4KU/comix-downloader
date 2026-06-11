"""Textual smoke tests for TUI screens."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from textual.css.query import NoMatches
from textual.widgets import DataTable

from comix_dl.core.models import ChapterInfo, SearchResult, SeriesInfo
from comix_dl.core.settings import Settings
from comix_dl.core.tui.app import ComixTuiApp, StatusBar
from comix_dl.core.tui.screens.download import DownloadTitle
from comix_dl.core.tui.screens.manage import DownloadsPane
from comix_dl.core.tui.screens.series import SeriesTitle

if TYPE_CHECKING:
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

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    def list_downloads(self) -> list[object]:
        return []

    def history_entries(self) -> list[object]:
        return []

    def load_settings(self) -> Settings:
        return self.settings


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
        assert app.query_one("#status", StatusBar).renderable == "Ready"

    assert controller.closed is True


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
        assert app.query_one("#download-title", DownloadTitle).renderable == "Downloading Series A"
