"""Search pane for the TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from comix_dl.core.tui.app import TuiControllerLike


class SearchScreen(Widget):
    """Search and result selection pane."""

    def __init__(self, controller: TuiControllerLike) -> None:
        super().__init__()
        self.controller = controller

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Search manga", classes="pane-title")
            yield Static("Enter a query to begin.", classes="muted")
