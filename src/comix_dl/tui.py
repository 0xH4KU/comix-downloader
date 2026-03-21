"""Interactive TUI for comix-downloader built with Textual."""

from __future__ import annotations

import logging
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    ProgressBar,
    RichLog,
    Static,
)

from comix_dl.cdp_browser import CdpBrowser
from comix_dl.comix_service import ComixService, SearchResult, SeriesInfo
from comix_dl.config import CONFIG
from comix_dl.converters import convert
from comix_dl.downloader import Downloader, DownloadProgress

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------
APP_CSS = """
Screen {
    background: $surface;
}

#search-input {
    dock: top;
    margin: 1 2;
}

#status-bar {
    dock: bottom;
    height: 1;
    padding: 0 2;
    background: $primary-background;
    color: $text;
}

#results-table, #chapters-table {
    height: 1fr;
    margin: 0 2;
}

#detail-panel {
    margin: 1 2;
    height: auto;
    max-height: 6;
}

#progress-container {
    margin: 1 2;
    height: auto;
}

#log-panel {
    margin: 1 2;
    height: 1fr;
    border: solid $primary;
}

.action-bar {
    dock: bottom;
    height: 3;
    margin: 0 2;
    align: center middle;
}

.action-bar Button {
    margin: 0 1;
}
"""


# ---------------------------------------------------------------------------
# Main TUI App
# ---------------------------------------------------------------------------


class ComixTUI(App[None]):
    """Interactive manga downloader TUI."""

    CSS = APP_CSS
    TITLE = "comix-downloader"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("s", "focus_search", "Search", show=True),
        Binding("d", "download_selected", "Download", show=True),
        Binding("a", "select_all", "Select All", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._client: CdpBrowser | None = None
        self._search_results: list[SearchResult] = []
        self._series_info: SeriesInfo | None = None
        self._selected_chapters: set[int] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search manga… (Enter to search)", id="search-input")
        yield DataTable(id="results-table")
        yield Static("", id="detail-panel")
        yield DataTable(id="chapters-table")
        with Horizontal(classes="action-bar"):
            yield Button("Download Selected", id="btn-download", variant="primary")
            yield Button("Select All", id="btn-select-all", variant="default")
        yield ProgressBar(id="progress-bar", total=100, show_eta=True)
        yield RichLog(id="log-panel", highlight=True, markup=True)
        yield Static("Ready", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        # Setup results table
        results = self.query_one("#results-table", DataTable)
        results.add_columns("#", "Title", "URL")
        results.cursor_type = "row"

        # Setup chapters table
        chapters = self.query_one("#chapters-table", DataTable)
        chapters.add_columns("✓", "#", "Chapter")
        chapters.cursor_type = "row"

        # Hide progress initially
        self.query_one("#progress-bar", ProgressBar).display = False
        self.query_one("#chapters-table", DataTable).display = False
        self.query_one(".action-bar", Horizontal).display = False
        self.query_one("#detail-panel", Static).display = False

        self._log("Welcome to comix-downloader! Press [bold]s[/bold] to search.")

    # -- actions ---------------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_select_all(self) -> None:
        self._toggle_all_chapters()

    def action_download_selected(self) -> None:
        self._start_download()

    # -- event handlers -------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            query = event.value.strip()
            if query:
                self._do_search(query)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table_id = event.data_table.id

        if table_id == "results-table":
            row_index = event.cursor_row
            if 0 <= row_index < len(self._search_results):
                result = self._search_results[row_index]
                self._load_series(result.url)

        elif table_id == "chapters-table":
            row_index = event.cursor_row
            self._toggle_chapter(row_index)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-download":
            self._start_download()
        elif event.button.id == "btn-select-all":
            self._toggle_all_chapters()

    # -- workers --------------------------------------------------------------

    @work(exclusive=True, thread=False)
    async def _do_search(self, query: str) -> None:
        self._set_status(f"Searching '{query}'…")
        session = await self._ensure_client()
        service = ComixService(session)

        try:
            results = await service.search(query)
        except Exception as exc:
            self._log(f"[red]Search failed: {exc}[/red]")
            self._set_status("Search failed")
            return

        self._search_results = results
        table = self.query_one("#results-table", DataTable)
        table.clear()

        if not results:
            self._log("[yellow]No results found.[/yellow]")
            self._set_status("No results")
            return

        for i, r in enumerate(results, 1):
            table.add_row(str(i), r.title, r.url)

        # Hide chapters when showing new search results
        self.query_one("#chapters-table", DataTable).display = False
        self.query_one(".action-bar", Horizontal).display = False
        self.query_one("#detail-panel", Static).display = False

        self._set_status(f"Found {len(results)} results")
        self._log(f"Found [bold]{len(results)}[/bold] results for '{query}'")

    @work(exclusive=True, thread=False)
    async def _load_series(self, url: str) -> None:
        self._set_status("Loading series info…")
        session = await self._ensure_client()
        service = ComixService(session)

        try:
            info = await service.get_series(url)
        except Exception as exc:
            self._log(f"[red]Failed to load series: {exc}[/red]")
            self._set_status("Failed to load series")
            return

        self._series_info = info
        self._selected_chapters.clear()

        # Update detail panel
        detail = self.query_one("#detail-panel", Static)
        meta_parts: list[str] = [f"[bold]{info.title}[/bold]"]
        if info.authors:
            meta_parts.append(f"Authors: {', '.join(info.authors)}")
        if info.genres:
            meta_parts.append(f"Genres: {', '.join(info.genres)}")
        detail.update("\n".join(meta_parts))
        detail.display = True

        # Populate chapter table
        table = self.query_one("#chapters-table", DataTable)
        table.clear()
        table.display = True

        for i, ch in enumerate(info.chapters, 1):
            table.add_row("☐", str(i), ch.title)

        self.query_one(".action-bar", Horizontal).display = True
        self._set_status(f"{info.title} — {len(info.chapters)} chapters")
        self._log(f"Loaded [bold]{info.title}[/bold] ({len(info.chapters)} chapters)")

    @work(exclusive=True, thread=False)
    async def _start_download(self) -> None:
        if not self._series_info:
            self._log("[yellow]No series loaded.[/yellow]")
            return

        chapters = self._series_info.chapters
        if self._selected_chapters:
            to_download = [chapters[i] for i in sorted(self._selected_chapters) if i < len(chapters)]
        else:
            self._log("[yellow]No chapters selected. Use 'a' to select all.[/yellow]")
            return

        total_chapters = len(to_download)
        self._log(f"Starting download of [bold]{total_chapters}[/bold] chapter(s)…")

        progress_bar = self.query_one("#progress-bar", ProgressBar)
        progress_bar.display = True
        progress_bar.update(total=total_chapters * 100, progress=0)

        client = await self._ensure_client()
        service = ComixService(client)

        for ch_idx, chapter in enumerate(to_download):
            self._set_status(f"Downloading {chapter.title} ({ch_idx + 1}/{total_chapters})…")
            self._log(f"[cyan]→ {chapter.title}[/cyan]")

            try:
                chapter_data = await service.get_chapter_images(chapter.url)
            except Exception as exc:
                self._log(f"  [red]✗ Parse failed: {exc}[/red]")
                continue

            if chapter_data is None:
                self._log("  [red]✗ No images found[/red]")
                continue

            def on_progress(
                p: DownloadProgress,
                _idx: int = ch_idx,
                _bar: ProgressBar = progress_bar,
            ) -> None:
                chapter_progress = (p.completed / p.total * 100) if p.total else 0
                overall = _idx * 100 + chapter_progress
                _bar.update(progress=overall)

            downloader = Downloader(client, on_progress=on_progress)
            try:
                image_dir = await downloader.download_chapter(
                    chapter_data.image_urls,
                    chapter_data.title,
                    chapter_data.chapter,
                    referer=chapter.url,
                )
            except RuntimeError as exc:
                self._log(f"  [red]✗ Download failed: {exc}[/red]")
                continue

            # Convert
            try:
                out = convert(image_dir, CONFIG.convert.default_format)
                self._log(f"  [green]✓ {out.name}[/green]")
            except RuntimeError as exc:
                self._log(f"  [yellow]⚠ Conversion: {exc}[/yellow]")

        progress_bar.update(progress=total_chapters * 100)
        self._set_status("Download complete!")
        self._log("[bold green]All downloads complete![/bold green]")

    # -- helpers ---------------------------------------------------------------

    def _toggle_chapter(self, row_index: int) -> None:
        if not self._series_info:
            return

        if row_index in self._selected_chapters:
            self._selected_chapters.discard(row_index)
            # We can't easily update a cell in Textual DataTable,
            # so we'll track selection state and show it in the log
            self._log(f"Deselected chapter {row_index + 1}")
        else:
            self._selected_chapters.add(row_index)
            self._log(f"Selected chapter {row_index + 1}")

        count = len(self._selected_chapters)
        self._set_status(f"{count} chapter(s) selected")

    def _toggle_all_chapters(self) -> None:
        if not self._series_info:
            return

        total = len(self._series_info.chapters)
        if len(self._selected_chapters) == total:
            self._selected_chapters.clear()
            self._log("Deselected all chapters")
        else:
            self._selected_chapters = set(range(total))
            self._log(f"Selected all {total} chapters")

        self._set_status(f"{len(self._selected_chapters)} chapter(s) selected")

    async def _ensure_client(self) -> CdpBrowser:
        if self._client is None:
            self._client = CdpBrowser()
            await self._client.start()
        return self._client

    def _set_status(self, text: str) -> None:
        self.query_one("#status-bar", Static).update(text)

    def _log(self, message: str) -> None:
        log = self.query_one("#log-panel", RichLog)
        log.write(message)

    async def _on_exit(self) -> None:
        if self._client:
            await self._client.close()
