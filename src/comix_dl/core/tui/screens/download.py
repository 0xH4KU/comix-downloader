"""Download progress pane for the TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from comix_dl.core.tui.state import DownloadRowsState

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from comix_dl.core.tui.screens.series import SeriesController
    from comix_dl.core.tui.state import DownloadRequest


class DownloadTitle(Static):
    """Static title with a stable renderable test surface."""

    @property
    def renderable(self) -> object:
        return self.content


class DownloadPane(Widget):
    """Live download progress pane."""

    def __init__(self, controller: SeriesController, request: DownloadRequest) -> None:
        super().__init__()
        self.controller = controller
        self.request = request
        self.rows = DownloadRowsState.from_chapters(list(request.chapters))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield DownloadTitle(
                f"Downloading {self.request.series_title}",
                id="download-title",
                classes="pane-title",
            )
            yield Static("Preparing download...", id="download-status", classes="muted")
            table: DataTable[object] = DataTable(id="download-table")
            table.cursor_type = "row"
            yield table

    def on_mount(self) -> None:
        table = self.query_one("#download-table", DataTable)
        table.add_columns("Chapter", "Status", "Progress", "Detail")
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#download-table", DataTable)
        table.clear()
        for row in self.rows.rows.values():
            table.add_row(row.title, row.status, row.progress_text, row.detail, key=str(row.chapter_id))
