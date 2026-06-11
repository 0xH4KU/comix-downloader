"""Series detail and chapter selection pane."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from comix_dl.core.tui.state import ChapterSelectionState

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from comix_dl.core.models import SeriesInfo


class SeriesController(Protocol):
    """Controller surface reserved for series-pane actions."""


class SeriesTitle(Static):
    """Static title with a stable renderable test surface."""

    @property
    def renderable(self) -> object:
        return self.content


class SeriesPane(Widget):
    """Series detail pane with chapter table."""

    def __init__(self, controller: SeriesController, series: SeriesInfo) -> None:
        super().__init__()
        self.controller = controller
        self.series = series
        self.selection = ChapterSelectionState.from_chapters(series.chapters)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield SeriesTitle(self.series.title, id="series-title", classes="pane-title")
            yield Static(self._metadata_text(), id="series-meta", classes="muted")
            yield Static("Use Space to select chapters. Use / to filter.", id="series-status", classes="muted")
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

    def _refresh_table(self) -> None:
        table = self.query_one("#chapters", DataTable)
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
