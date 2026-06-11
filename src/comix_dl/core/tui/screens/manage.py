"""Management panes for the TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from comix_dl.core.tui.app import TuiControllerLike


class DownloadsPane(Widget):
    """Downloaded manga management pane."""

    def __init__(self, controller: TuiControllerLike) -> None:
        super().__init__()
        self.controller = controller

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Downloads", classes="pane-title")
            yield Static("No downloaded manga found.", classes="muted")


class HistoryPane(Widget):
    """Download history pane."""

    def __init__(self, controller: TuiControllerLike) -> None:
        super().__init__()
        self.controller = controller

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("History", classes="pane-title")
            yield Static("No download history.", classes="muted")


class SettingsPane(Widget):
    """Settings summary pane."""

    def __init__(self, controller: TuiControllerLike) -> None:
        super().__init__()
        self.controller = controller

    def compose(self) -> ComposeResult:
        settings = self.controller.load_settings()
        with Vertical():
            yield Static("Settings", classes="pane-title")
            yield Static(f"Output: {settings.output_dir}")
            yield Static(f"Format: {settings.default_format}")
