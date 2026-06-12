"""Textual app shell for comix-downloader."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol, cast

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

from comix_dl import __version__
from comix_dl.core.tui.controller import TuiController
from comix_dl.core.tui.state import DownloadNavigationState, LogDrawerState, SeriesNavigationState

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from comix_dl.core.models import SeriesInfo
    from comix_dl.core.settings import Settings


class StatusLog(Static):
    """Collapsed or expanded status/log drawer above the Footer."""

    expanded: reactive[bool] = reactive(False)

    def __init__(self, *, widget_id: str | None = None) -> None:
        super().__init__("", id=widget_id)
        self.state = LogDrawerState()

    @property
    def renderable(self) -> object:
        return self.content

    def push(self, message: str) -> None:
        self.state.push(message)
        self._sync()

    def toggle(self) -> None:
        self.state.toggle()
        self.expanded = self.state.expanded
        self._sync()

    def _sync(self) -> None:
        self.set_class(self.state.expanded, "expanded")
        if self.state.expanded:
            self.update("\n".join(self.state.visible_messages))
            return
        self.update(self.state.latest)


StatusBar = StatusLog


class NavigationItem(Static):
    """One clickable navigation row."""

    BINDINGS: ClassVar = [("enter", "select", "Select")]
    can_focus = True

    class Selected(Message):
        """Posted when a navigation row is selected."""

        def __init__(self, destination: str) -> None:
            super().__init__()
            self.destination = destination

    def __init__(self, label: str, destination: str, *, active: bool) -> None:
        classes = "nav-item active" if active else "nav-item"
        super().__init__(label, id=f"nav-{destination.lower()}", classes=classes)
        self.destination = destination

    def on_click(self) -> None:
        self.post_message(self.Selected(self.destination))

    def action_select(self) -> None:
        self.post_message(self.Selected(self.destination))


class NavigationRail(Static):
    """Clickable sidebar navigation with state-aware workflow destinations."""

    _WORKFLOW: ClassVar[tuple[str, ...]] = ("Search", "Chapters", "Download")
    _TOOLS: ClassVar[tuple[str, ...]] = ("Library", "History", "Settings")

    def __init__(self, *, widget_id: str | None = None) -> None:
        super().__init__("", id=widget_id)
        self.active = "Search"
        self.available: set[str] = {"Search", *self._TOOLS}

    @property
    def rendered_text(self) -> str:
        return self._render_text()

    def compose(self) -> ComposeResult:
        yield Static("WORKFLOW", classes="nav-section")
        for destination in self._WORKFLOW:
            if destination in self.available:
                yield NavigationItem(destination, destination, active=destination == self.active)
        yield Static("TOOLS", classes="nav-section")
        for destination in self._TOOLS:
            yield NavigationItem(destination, destination, active=destination == self.active)

    def set_state(self, *, active: str, available: set[str]) -> None:
        self.active = active
        self.available = set(available)
        self.refresh(recompose=True)

    def _render_text(self) -> str:
        lines = ["WORKFLOW"]
        lines.extend(destination for destination in self._WORKFLOW if destination in self.available)
        lines.append("TOOLS")
        lines.extend(self._TOOLS)
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
        ("o", "toggle_logs", "Logs"),
    ]

    def __init__(self, *, controller: TuiControllerLike | None = None, mirror: str | None = None) -> None:
        super().__init__()
        self.controller = controller or cast("TuiControllerLike", TuiController(mirror=mirror))
        self._series_state: SeriesNavigationState | None = None
        self._download_state: DownloadNavigationState | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="layout"):
            yield NavigationRail(widget_id="sidebar")
            yield Container(id="screen-host")
        yield StatusLog(widget_id="status-log")
        yield Footer()

    async def on_mount(self) -> None:
        await self._open_controller()
        await self.action_show_search()

    async def _open_controller(self) -> None:
        try:
            await self.controller.open()
        except Exception as exc:
            self.set_status(f"Session failed: {exc}")
            return
        self.set_status("Ready to search")

    def available_destinations(self) -> set[str]:
        """Return destinations visible in the sidebar."""
        destinations = {"Search", "Library", "History", "Settings"}
        if self._series_state is not None:
            destinations.add("Chapters")
        if self._download_state is not None:
            destinations.add("Download")
        return destinations

    def refresh_navigation(self, active: str) -> None:
        self.query_one("#sidebar", NavigationRail).set_state(
            active=active,
            available=self.available_destinations(),
        )

    def set_active_view(self, active: str) -> None:
        self.refresh_navigation(active)

    def set_status(self, message: str) -> None:
        self.query_one("#status-log", StatusLog).push(message)

    def append_log(self, message: str) -> None:
        self.set_status(message)

    def set_loaded_series(self, series: SeriesInfo) -> SeriesNavigationState:
        """Store a loaded series and reveal the Chapters destination."""
        self._series_state = SeriesNavigationState.from_series(
            series,
            default_format=self.controller.settings.default_format,
        )
        self.refresh_navigation("Chapters")
        return self._series_state

    def action_toggle_logs(self) -> None:
        self.query_one("#status-log", StatusLog).toggle()

    @on(NavigationItem.Selected)
    async def _navigation_selected(self, event: NavigationItem.Selected) -> None:
        destination = event.destination
        if destination == "Search":
            await self.action_show_search()
        elif destination == "Chapters":
            await self.action_show_chapters()
        elif destination == "Library":
            await self.action_show_downloads()
        elif destination == "History":
            await self.action_show_history()
        elif destination == "Settings":
            await self.action_show_settings()

    async def _replace_screen(self, widget: object) -> None:
        """Replace the main pane and clear stale focus from the previous pane."""
        from textual.widget import Widget

        self.set_focus(None)
        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(cast("Widget", widget))

    async def on_unmount(self) -> None:
        await self.controller.close()

    async def action_show_search(self) -> None:
        from comix_dl.core.tui.screens.search import SearchScreen

        self.set_active_view("Search")
        self.set_status("Ready to search")
        await self._replace_screen(SearchScreen(self.controller))

    async def action_show_downloads(self) -> None:
        from comix_dl.core.tui.screens.manage import DownloadsPane

        self.set_active_view("Library")
        self.set_status("Viewing library")
        await self._replace_screen(DownloadsPane(self.controller))

    async def action_show_chapters(self) -> None:
        from comix_dl.core.tui.screens.series import SeriesPane

        if self._series_state is None:
            self.set_status("Search and select a manga before opening Chapters.")
            return
        self.set_active_view("Chapters")
        self.set_status(f"Series loaded: {self._series_state.series.title}")
        await self._replace_screen(SeriesPane(self.controller, self._series_state))

    async def action_show_history(self) -> None:
        from comix_dl.core.tui.screens.manage import HistoryPane

        self.set_active_view("History")
        self.set_status("Viewing history")
        await self._replace_screen(HistoryPane(self.controller))

    async def action_show_settings(self) -> None:
        from comix_dl.core.tui.screens.manage import SettingsPane

        self.set_active_view("Settings")
        self.set_status("Viewing settings")
        await self._replace_screen(SettingsPane(self.controller))
