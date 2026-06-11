"""Series detail and chapter selection pane."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol, cast

from textual import on
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Select, Static

from comix_dl.core.tui.state import ChapterSelectionState, DownloadRequest, OutputFormat

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from comix_dl.core.models import SeriesInfo
    from comix_dl.core.settings import Settings


class SeriesController(Protocol):
    """Controller surface used by the series pane."""

    settings: Settings


class SeriesTitle(Static):
    """Static title with a stable renderable test surface."""

    @property
    def renderable(self) -> object:
        return self.content


class SeriesPane(Widget):
    """Series detail pane with filterable chapter table."""

    BINDINGS: ClassVar = [
        ("space", "toggle", "Select"),
        ("a", "select_visible", "All"),
        ("x", "clear_visible", "Clear"),
        ("/", "focus_filter", "Filter"),
        ("f", "cycle_format", "Format"),
        ("d", "start_download", "Download"),
    ]

    def __init__(self, controller: object, series: SeriesInfo) -> None:
        super().__init__()
        self.controller = cast("SeriesController", controller)
        self.series = series
        self.selection = ChapterSelectionState.from_chapters(series.chapters)
        self.format_value: OutputFormat = self._initial_format(self.controller.settings.default_format)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield SeriesTitle(self.series.title, id="series-title", classes="pane-title")
            yield Static(self._metadata_text(), id="series-meta", classes="muted")
            with Horizontal(id="chapter-tools"):
                yield Input(placeholder="+stage -extra", id="chapter-filter")
                yield Select[OutputFormat](
                    [("PDF", "pdf"), ("CBZ", "cbz"), ("Both", "both")],
                    value=self.format_value,
                    id="format-select",
                )
                yield Button("Download", id="download-button", variant="primary")
            yield Static("", id="series-status", classes="muted")
            table: DataTable[object] = DataTable(id="chapters")
            table.cursor_type = "row"
            yield table

    def on_mount(self) -> None:
        table = self.query_one("#chapters", DataTable)
        table.add_columns("Sel", "#", "Title", "Lang", "Pages")
        self._refresh_table()
        table.focus()

    def _metadata_text(self) -> str:
        authors = ", ".join(self.series.authors) if self.series.authors else "Unknown author"
        genres = ", ".join(self.series.genres[:5]) if self.series.genres else "No genres"
        return f"{authors} · {genres} · {len(self.series.chapters)} chapter(s)"

    def _initial_format(self, raw_format: str) -> OutputFormat:
        if raw_format in {"pdf", "cbz", "both"}:
            return cast("OutputFormat", raw_format)
        return "pdf"

    def _refresh_table(self) -> None:
        table = self.query_one("#chapters", DataTable)
        cursor_row = max(table.cursor_row, 0)
        table.clear()
        for chapter in self.selection.visible_chapters:
            selected = "*" if chapter.chapter_id in self.selection.selected_ids else ""
            table.add_row(
                selected,
                chapter.number,
                chapter.title,
                chapter.language,
                str(chapter.image_count or ""),
                key=str(chapter.chapter_id),
            )
        if self.selection.visible_chapters:
            table.move_cursor(row=min(cursor_row, len(self.selection.visible_chapters) - 1))
        self._refresh_status()

    def _refresh_status(self) -> None:
        message = self.selection.status or f"{len(self.selection.visible_chapters)} chapter(s) visible."
        self.query_one("#series-status", Static).update(
            f"{message} {self.selection.selected_count} selected. Format: {self.format_value.upper()}."
        )

    @on(Input.Submitted, "#chapter-filter")
    def _apply_filter(self, event: Input.Submitted) -> None:
        self.selection.apply_filter(event.value)
        self._refresh_table()
        self.query_one("#chapters", DataTable).focus()

    @on(Select.Changed, "#format-select")
    def _format_changed(self, event: Select.Changed) -> None:
        if event.value in {"pdf", "cbz", "both"}:
            self.format_value = cast("OutputFormat", event.value)
            self._refresh_status()

    @on(Button.Pressed, "#download-button")
    async def _button_download(self, event: Button.Pressed) -> None:
        if event.button.id == "download-button":
            await self.action_start_download()

    def action_focus_filter(self) -> None:
        self.query_one("#chapter-filter", Input).focus()

    async def action_toggle(self, attribute_name: str = "") -> None:
        table = self.query_one("#chapters", DataTable)
        if table.cursor_row < 0 or table.cursor_row >= len(self.selection.visible_chapters):
            return
        chapter = self.selection.visible_chapters[table.cursor_row]
        self.selection.toggle(chapter.chapter_id)
        self._refresh_table()

    def action_select_visible(self) -> None:
        self.selection.select_visible()
        self._refresh_table()

    def action_clear_visible(self) -> None:
        self.selection.clear_visible()
        self._refresh_table()

    def action_cycle_format(self) -> None:
        formats: list[OutputFormat] = ["pdf", "cbz", "both"]
        current_index = formats.index(self.format_value)
        self.format_value = formats[(current_index + 1) % len(formats)]
        self.query_one("#format-select", Select).value = self.format_value
        self._refresh_status()

    async def action_start_download(self) -> None:
        selected = self.selection.selected_chapters
        if not selected:
            self.query_one("#series-status", Static).update("Select at least one chapter before downloading.")
            return

        request = DownloadRequest(
            series_title=self.series.title,
            chapters=tuple(selected),
            fmt=self.format_value,
            optimize=self.controller.settings.optimize_images,
        )
        from comix_dl.core.tui.screens.download import DownloadPane

        host = self.app.query_one("#screen-host")
        await host.remove_children()
        await host.mount(DownloadPane(self.controller, request))
