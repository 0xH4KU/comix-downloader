"""Management panes for the TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from comix_dl.core.cli.display import format_bytes

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from textual.app import ComposeResult

    from comix_dl.core.tui.app import TuiControllerLike


class DownloadedSeriesLike(Protocol):
    """Downloaded series fields rendered by the management pane."""

    name: str
    path: Path
    completed_chapters: int
    total_size_bytes: int


class HistoryEntryLike(Protocol):
    """History entry fields rendered by the management pane."""

    timestamp: str
    title: str
    chapters_count: int
    format: str
    total_size_bytes: int
    completed: int
    summary_text: str


class SettingsOutput(Static):
    """Static settings value with a stable renderable test surface."""

    @property
    def renderable(self) -> object:
        return self.content


class DownloadsPane(Widget):
    """Downloaded manga management pane."""

    def __init__(self, controller: TuiControllerLike) -> None:
        super().__init__()
        self.controller = controller

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Downloads", classes="pane-title")
            yield DataTable(id="downloads-table")

    def on_mount(self) -> None:
        table = self.query_one("#downloads-table", DataTable)
        table.add_columns("Series", "Chapters", "Size", "Path")
        downloads = cast("Sequence[DownloadedSeriesLike]", self.controller.list_downloads())
        for series in downloads:
            table.add_row(
                str(series.name),
                str(series.completed_chapters),
                format_bytes(series.total_size_bytes),
                str(series.path),
            )


class HistoryPane(Widget):
    """Download history pane."""

    def __init__(self, controller: TuiControllerLike) -> None:
        super().__init__()
        self.controller = controller

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("History", classes="pane-title")
            yield DataTable(id="history-table")

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.add_columns("Date", "Series", "Chapters", "Format", "Status", "Size")
        entries = cast("Sequence[HistoryEntryLike]", self.controller.history_entries())
        for entry in entries[:50]:
            table.add_row(
                str(entry.timestamp),
                str(entry.title),
                str(entry.chapters_count),
                str(entry.format),
                entry.summary_text or f"{entry.completed} completed",
                format_bytes(entry.total_size_bytes),
            )


class SettingsPane(Widget):
    """Settings summary pane."""

    def __init__(self, controller: TuiControllerLike) -> None:
        super().__init__()
        self.controller = controller

    def compose(self) -> ComposeResult:
        settings = self.controller.load_settings()
        with Vertical():
            yield Static("Settings", classes="pane-title")
            yield SettingsOutput(str(settings.output_dir), id="settings-output")
            yield Static(f"Default format: {settings.default_format}")
            yield Static(f"Concurrency profile: {settings.concurrency_profile}")
            yield Static(f"Optimize images: {settings.optimize_images}")
