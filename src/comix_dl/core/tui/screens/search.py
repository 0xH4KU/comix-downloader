"""Search pane for the TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from textual import on
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from comix_dl.core.models import SearchResult, SeriesInfo


class SearchController(Protocol):
    """Controller surface used by the search pane."""

    async def search(self, query: str) -> list[SearchResult]:
        """Search for matching series."""

    async def load_series(self, identifier: str) -> SeriesInfo:
        """Load a selected series."""


class SearchScreen(Widget):
    """Search and result selection pane."""

    def __init__(self, controller: object) -> None:
        super().__init__()
        self.controller = cast("SearchController", controller)
        self.results: list[SearchResult] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Search manga", classes="pane-title")
            yield Input(placeholder="Type a manga name and press Enter", id="search-input")
            yield Static("Ready.", id="search-status", classes="muted")
            table: DataTable[object] = DataTable(id="results")
            table.cursor_type = "row"
            yield table

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_columns("#", "Title", "Slug")
        self.query_one("#search-input", Input).focus()

    @on(Input.Submitted, "#search-input")
    async def _submit_search(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            self.query_one("#search-status", Static).update("Enter a search query.")
            return
        self.run_worker(self._search(query), name="search", group="search", exclusive=True, exit_on_error=False)

    async def _search(self, query: str) -> None:
        status = self.query_one("#search-status", Static)
        table = self.query_one("#results", DataTable)
        status.update(f"Searching '{query}'...")
        table.clear()
        self.results = []
        try:
            self.results = await self.controller.search(query)
        except Exception as exc:
            status.update(f"Search failed: {exc}")
            return
        if not self.results:
            status.update("No results found.")
            return
        for index, result in enumerate(self.results, 1):
            table.add_row(str(index), result.title, result.slug, key=str(index - 1))
        status.update(f"{len(self.results)} result(s). Press Enter to open the selected row.")
        table.focus()

    @on(DataTable.RowSelected, "#results")
    async def _open_selected_result(self, event: DataTable.RowSelected) -> None:
        try:
            index = int(str(event.row_key.value))
            result = self.results[index]
        except (ValueError, IndexError):
            self.query_one("#search-status", Static).update("Invalid result selection.")
            return
        self.app.run_worker(
            self._load_series(result),
            name="load-series",
            group="load-series",
            exclusive=True,
            exit_on_error=False,
        )

    async def _load_series(self, result: SearchResult) -> None:
        status = self.query_one("#search-status", Static)
        status.update(f"Loading {result.title}...")
        try:
            info = await self.controller.load_series(result.hash_id)
        except Exception as exc:
            status.update(f"Could not load series: {exc}")
            return

        from comix_dl.core.tui.screens.series import SeriesPane

        host = self.app.query_one("#screen-host")
        await host.remove_children()
        await host.mount(SeriesPane(self.controller, info))
