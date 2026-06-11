"""Textual app shell for comix-downloader."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol, cast

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Footer, Header, Static

from comix_dl import __version__
from comix_dl.core.tui.controller import TuiController

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from comix_dl.core.settings import Settings


class StatusBar(Static):
    """Static status line with a stable renderable test surface."""

    @property
    def renderable(self) -> object:
        return self.content


class TuiControllerLike(Protocol):
    """Controller surface required by the Textual shell and stub panes."""

    settings: Settings
    output_dir: Path

    async def open(self) -> None:
        """Open the application session."""

    async def close(self) -> None:
        """Close the application session."""

    def list_downloads(self) -> Sequence[object]:
        """Return downloaded series."""

    def history_entries(self) -> Sequence[object]:
        """Return download history entries."""

    def load_settings(self) -> Settings:
        """Load persisted settings."""


class ComixTuiApp(App[int]):
    """Full-screen terminal UI."""

    CSS_PATH = "styles.tcss"
    TITLE = "comix-downloader"
    SUB_TITLE = f"v{__version__}"
    BINDINGS: ClassVar = [
        ("ctrl+c", "quit", "Quit"),
        ("q", "quit", "Quit"),
        ("s", "show_search", "Search"),
        ("d", "show_downloads", "Downloads"),
        ("h", "show_history", "History"),
        ("g", "show_settings", "Settings"),
    ]

    def __init__(self, *, controller: TuiControllerLike | None = None, mirror: str | None = None) -> None:
        super().__init__()
        self.controller = controller or cast("TuiControllerLike", TuiController(mirror=mirror))

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="layout"):
            yield Static("Search\nDownloads\nHistory\nSettings", id="sidebar")
            yield Container(id="screen-host")
        yield StatusBar("Opening session...", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        await self._open_controller()
        await self.action_show_search()

    async def _open_controller(self) -> None:
        status = self.query_one("#status", Static)
        try:
            await self.controller.open()
        except Exception as exc:
            status.update(f"Session failed: {exc}")
            return
        status.update("Ready")

    async def on_unmount(self) -> None:
        await self.controller.close()

    async def action_show_search(self) -> None:
        from comix_dl.core.tui.screens.search import SearchScreen

        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(SearchScreen(self.controller))

    async def action_show_downloads(self) -> None:
        from comix_dl.core.tui.screens.manage import DownloadsPane

        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(DownloadsPane(self.controller))

    async def action_show_history(self) -> None:
        from comix_dl.core.tui.screens.manage import HistoryPane

        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(HistoryPane(self.controller))

    async def action_show_settings(self) -> None:
        from comix_dl.core.tui.screens.manage import SettingsPane

        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(SettingsPane(self.controller))
