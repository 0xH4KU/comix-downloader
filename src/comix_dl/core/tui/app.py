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


class NavigationRail(Static):
    """Sidebar navigation with active destination rendering."""

    _ITEMS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("Search", "1 Search"),
        ("Chapters", "2 Chapters"),
        ("Download", "3 Download"),
        ("Library", "Library"),
        ("History", "History"),
        ("Settings", "Settings"),
    )

    active: str = "Search"

    def __init__(self, *, widget_id: str | None = None) -> None:
        super().__init__("", id=widget_id)

    @property
    def rendered_text(self) -> str:
        return str(self.content)

    def on_mount(self) -> None:
        self.set_active(self.active)

    def set_active(self, active: str) -> None:
        self.active = active
        self.set_classes(" ".join(name for name, _label in self._ITEMS if name == active))
        self.update(self._render_text())

    def _render_text(self) -> str:
        lines: list[str] = []
        for name, label in self._ITEMS:
            marker = ">" if name == self.active else " "
            lines.append(f"{marker} {label}")
        return "\n".join(lines)


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
            yield NavigationRail(widget_id="sidebar")
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
        status.update("Ready to search")

    def set_active_view(self, active: str) -> None:
        self.query_one("#sidebar", NavigationRail).set_active(active)

    def set_status(self, message: str) -> None:
        self.query_one("#status", StatusBar).update(message)

    async def on_unmount(self) -> None:
        await self.controller.close()

    async def action_show_search(self) -> None:
        from comix_dl.core.tui.screens.search import SearchScreen

        self.set_active_view("Search")
        self.set_status("Ready to search")
        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(SearchScreen(self.controller))

    async def action_show_downloads(self) -> None:
        from comix_dl.core.tui.screens.manage import DownloadsPane

        self.set_active_view("Library")
        self.set_status("Viewing library")
        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(DownloadsPane(self.controller))

    async def action_show_history(self) -> None:
        from comix_dl.core.tui.screens.manage import HistoryPane

        self.set_active_view("History")
        self.set_status("Viewing history")
        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(HistoryPane(self.controller))

    async def action_show_settings(self) -> None:
        from comix_dl.core.tui.screens.manage import SettingsPane

        self.set_active_view("Settings")
        self.set_status("Viewing settings")
        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(SettingsPane(self.controller))
