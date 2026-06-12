"""Search pane for the TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from textual import events, on
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from comix_dl.core.models import SearchResult, SeriesInfo
    from comix_dl.core.tui.state import SeriesNavigationState


class SearchController(Protocol):
    """Controller surface used by the search pane."""

    async def search(self, query: str) -> list[SearchResult]:
        """Search for matching series."""

    async def load_series(self, identifier: str) -> SeriesInfo:
        """Load a selected series."""


class SearchApp(Protocol):
    """App shell surface used by the search pane."""

    def set_active_view(self, active: str) -> None:
        """Set the active shell navigation destination."""

    def set_status(self, message: str) -> None:
        """Set the shared shell status text."""

    def set_loaded_series(self, series: SeriesInfo) -> SeriesNavigationState:
        """Store the current loaded series for navigation."""


class SearchInput(Input):
    """Search input that can release focus for global app shortcuts."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"escape", "tab"}:
            self.blur()
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class SearchScreen(Widget):
    """Search and result selection pane."""

    def __init__(self, controller: object) -> None:
        super().__init__()
        self.controller = cast("SearchController", controller)
        self.results: list[SearchResult] = []

    @property
    def shell(self) -> SearchApp:
        return cast("SearchApp", self.app)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Search", classes="pane-title")
            yield Static(
                "Type a manga name to begin. Results will appear below.",
                id="search-help",
                classes="muted helper-text",
            )
            yield SearchInput(placeholder="Manga title", id="search-input")
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
            self.shell.set_status("Type a manga name, then press Enter to search.")
            return
        self.run_worker(self._search(query), name="search", group="search", exclusive=True, exit_on_error=False)

    async def _search(self, query: str) -> None:
        table = self.query_one("#results", DataTable)
        self.shell.set_status(f"Searching for '{query}'")
        table.clear()
        self.results = []
        try:
            self.results = await self.controller.search(query)
        except Exception as exc:
            self.shell.set_status(f"Search failed: {exc}")
            return
        if not self.results:
            self.shell.set_status("No results found. Try a different title.")
            return
        for index, result in enumerate(self.results, 1):
            table.add_row(str(index), result.title, result.slug, key=str(index - 1))
        self.shell.set_status(f"{len(self.results)} results found")
        table.focus()

    @on(DataTable.RowSelected, "#results")
    async def _open_selected_result(self, event: DataTable.RowSelected) -> None:
        try:
            index = int(str(event.row_key.value))
            result = self.results[index]
        except (ValueError, IndexError):
            self.shell.set_status("Invalid result selection.")
            return
        self.app.run_worker(
            self._load_series(result),
            name="load-series",
            group="load-series",
            exclusive=True,
            exit_on_error=False,
        )

    async def _load_series(self, result: SearchResult) -> None:
        self.shell.set_status(f"Loading {result.title}")
        try:
            info = await self.controller.load_series(result.hash_id)
        except Exception as exc:
            self.shell.set_status(f"Could not load series: {exc}")
            return

        from comix_dl.core.tui.screens.series import SeriesPane

        host = self.app.query_one("#screen-host")
        if self not in host.children:
            return
        series_state = self.shell.set_loaded_series(info)
        self.shell.set_active_view("Chapters")
        self.shell.set_status(f"Series loaded: {info.title}")
        await host.remove_children()
        await host.mount(SeriesPane(self.controller, series_state))
