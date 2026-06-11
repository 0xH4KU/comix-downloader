"""Textual smoke tests for TUI screens."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from comix_dl.core.settings import Settings
from comix_dl.core.tui.app import ComixTuiApp, StatusBar

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


@pytest.mark.asyncio
async def test_app_mounts_search_screen_and_opens_controller(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        assert controller.opened is True
        assert app.query_one("#status", StatusBar).renderable == "Ready"

    assert controller.closed is True
