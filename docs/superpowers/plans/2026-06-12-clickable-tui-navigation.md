# Clickable TUI Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Textual TUI sidebar genuinely clickable, hide unavailable workflow destinations, and move runtime messages into a collapsible status/log drawer above the Footer.

**Architecture:** Keep `app.py` responsible for shell navigation and shared status/log state. Add small pure state helpers in `state.py` for log, series navigation, and download navigation state so screens can be rebuilt without losing user work. Screens continue to own their task UI and communicate back to the shell through explicit app methods.

**Tech Stack:** Python 3.11+, Textual 8.2, pytest, pytest-asyncio, existing fake-controller TUI tests.

---

## File Structure

- Modify `src/comix_dl/core/tui/state.py`
  - Add `LogDrawerState`, `SeriesNavigationState`, and `DownloadNavigationState`.
  - Keep existing `ChapterSelectionState`, `DownloadRequest`, and `DownloadRowsState` behavior.

- Modify `src/comix_dl/core/tui/app.py`
  - Replace the static sidebar text with clickable `NavigationItem` widgets inside `NavigationRail`.
  - Add `StatusLog` as the Footer-adjacent collapsed/expanded status-log drawer.
  - Add shell state and shell API methods for active view, logs, loaded series, and current download.
  - Route sidebar clicks to app actions.

- Modify `src/comix_dl/core/tui/screens/search.py`
  - Move search status updates into the shell status/log drawer.
  - Notify the shell when a series loads so Chapters becomes visible.

- Modify `src/comix_dl/core/tui/screens/series.py`
  - Accept and mutate a `SeriesNavigationState`.
  - Preserve chapter filter, selection, and format across navigation.
  - Notify the shell when a download request exists so Download becomes visible.

- Modify `src/comix_dl/core/tui/screens/download.py`
  - Accept and mutate a `DownloadNavigationState`.
  - Avoid restarting a finished or in-progress download when returning via navigation.
  - Move completion, cancellation, failure, and cleanup messages into the shell status/log drawer.

- Modify `src/comix_dl/core/tui/styles.tcss`
  - Style clickable nav items, section labels, active nav state, and the status/log drawer.

- Modify `tests/test_tui_state.py`
  - Add pure tests for log drawer state and navigation-state initialization.

- Modify `tests/test_tui_screens.py`
  - Update existing status/sidebar expectations.
  - Add tests for clickable sidebar destinations, hidden workflow items, log drawer toggling, and state preservation.

---

## Task 1: Add Pure Navigation And Log State

**Files:**
- Modify: `tests/test_tui_state.py`
- Modify: `src/comix_dl/core/tui/state.py`

- [ ] **Step 1: Add failing tests for log drawer and navigation state**

Append these tests to `tests/test_tui_state.py`:

```python
from comix_dl.core.models import ChapterInfo, SeriesInfo
from comix_dl.core.tui.state import (
    DownloadNavigationState,
    DownloadRequest,
    LogDrawerState,
    SeriesNavigationState,
)


def _series_for_navigation() -> SeriesInfo:
    return SeriesInfo(
        title="Series A",
        authors=["Author A"],
        genres=["Action"],
        description="A short description",
        chapters=[
            ChapterInfo(title="Chapter 1", chapter_id=1, number="1", image_count=10),
            ChapterInfo(title="Chapter 2 Extra", chapter_id=2, number="2", image_count=12),
        ],
        url="https://comix.to/manga/series-a",
        hash_id="a",
    )


def test_log_drawer_state_tracks_latest_and_recent_messages() -> None:
    state = LogDrawerState(max_messages=3)

    state.push("Ready to search")
    state.push("Searching for series")
    state.push("2 results found")
    state.push("Series loaded: Series A")

    assert state.latest == "Series loaded: Series A"
    assert state.visible_messages == [
        "Searching for series",
        "2 results found",
        "Series loaded: Series A",
    ]


def test_log_drawer_state_toggles_expanded() -> None:
    state = LogDrawerState()

    assert state.expanded is False

    state.toggle()

    assert state.expanded is True


def test_series_navigation_state_preserves_selection_filter_and_format() -> None:
    series = _series_for_navigation()
    state = SeriesNavigationState.from_series(series, default_format="pdf")

    state.selection.apply_filter("+extra")
    state.selection.select_visible()
    state.format_value = "cbz"

    assert state.series.title == "Series A"
    assert state.selection.selected_count == 1
    assert len(state.selection.visible_chapters) == 1
    assert state.format_value == "cbz"


def test_download_navigation_state_starts_ready_with_rows() -> None:
    series = _series_for_navigation()
    request = DownloadRequest(
        series_title=series.title,
        chapters=tuple(series.chapters[:1]),
        fmt="pdf",
        optimize=True,
    )

    state = DownloadNavigationState.from_request(request)

    assert state.request is request
    assert state.phase == "ready"
    assert state.status_text == "Preparing download..."
    assert list(state.rows.rows) == [1]
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_state.py::test_log_drawer_state_tracks_latest_and_recent_messages tests/test_tui_state.py::test_log_drawer_state_toggles_expanded tests/test_tui_state.py::test_series_navigation_state_preserves_selection_filter_and_format tests/test_tui_state.py::test_download_navigation_state_starts_ready_with_rows -q
```

Expected: FAIL with import errors for `LogDrawerState`, `SeriesNavigationState`, and `DownloadNavigationState`.

- [ ] **Step 3: Implement the state helpers**

In `src/comix_dl/core/tui/state.py`, update imports:

```python
from dataclasses import dataclass, field, replace
```

Extend the `TYPE_CHECKING` block:

```python
    from comix_dl.core.models import ChapterInfo, SeriesInfo
```

Add these types after `DownloadStatus`:

```python
DownloadPhase = Literal["ready", "running", "cancelling", "failed", "complete"]
```

Add these dataclasses after `DownloadRequest`:

```python
@dataclass
class LogDrawerState:
    """Recent shell messages for the status/log drawer."""

    messages: list[str] = field(default_factory=list)
    max_messages: int = 5
    expanded: bool = False

    @property
    def latest(self) -> str:
        """Return the most recent message or an empty fallback."""
        if not self.messages:
            return ""
        return self.messages[-1]

    @property
    def visible_messages(self) -> list[str]:
        """Return the capped message history visible in expanded mode."""
        return self.messages[-self.max_messages :]

    def push(self, message: str) -> None:
        """Append one non-empty message to the log."""
        clean = message.strip()
        if not clean:
            return
        self.messages.append(clean)

    def toggle(self) -> None:
        """Toggle expanded/collapsed log display."""
        self.expanded = not self.expanded


@dataclass
class SeriesNavigationState:
    """State needed to restore the current chapter-selection screen."""

    series: SeriesInfo
    selection: ChapterSelectionState
    format_value: OutputFormat

    @classmethod
    def from_series(cls, series: SeriesInfo, *, default_format: str) -> SeriesNavigationState:
        """Create restorable chapter-selection state for a loaded series."""
        format_value: OutputFormat = "pdf"
        if default_format in {"pdf", "cbz", "both"}:
            format_value = cast("OutputFormat", default_format)
        return cls(
            series=series,
            selection=ChapterSelectionState.from_chapters(series.chapters),
            format_value=format_value,
        )


@dataclass
class DownloadNavigationState:
    """State needed to restore the current download screen without restarting it."""

    request: DownloadRequest
    rows: DownloadRowsState
    phase: DownloadPhase = "ready"
    status_text: str = "Preparing download..."
    cleanup_available: bool = False

    @classmethod
    def from_request(cls, request: DownloadRequest) -> DownloadNavigationState:
        """Create restorable download state for one download request."""
        return cls(
            request=request,
            rows=DownloadRowsState.from_chapters(list(request.chapters)),
        )
```

- [ ] **Step 4: Run the state tests to verify they pass**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_state.py::test_log_drawer_state_tracks_latest_and_recent_messages tests/test_tui_state.py::test_log_drawer_state_toggles_expanded tests/test_tui_state.py::test_series_navigation_state_preserves_selection_filter_and_format tests/test_tui_state.py::test_download_navigation_state_starts_ready_with_rows -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add src/comix_dl/core/tui/state.py tests/test_tui_state.py
git commit -m "feat: add tui navigation state"
```

---

## Task 2: Replace Static Sidebar With Clickable State-Aware Navigation

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `src/comix_dl/core/tui/app.py`
- Modify: `src/comix_dl/core/tui/styles.tcss`

- [ ] **Step 1: Update shell navigation tests to describe the new sidebar**

In `tests/test_tui_screens.py`, update the import from `comix_dl.core.tui.app`:

```python
from comix_dl.core.tui.app import ComixTuiApp, NavigationRail, StatusLog
```

Replace `test_shell_starts_with_focused_wizard_navigation` with:

```python
@pytest.mark.asyncio
async def test_shell_starts_with_clickable_state_aware_navigation(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        rail = app.query_one("#sidebar", NavigationRail)

        assert "WORKFLOW" in rail.rendered_text
        assert "Search" in rail.rendered_text
        assert "Chapters" not in rail.rendered_text
        assert "Download" not in rail.rendered_text
        assert "TOOLS" in rail.rendered_text
        assert "Library" in rail.rendered_text
        assert "History" in rail.rendered_text
        assert "Settings" in rail.rendered_text
        assert "1 Search" not in rail.rendered_text
        assert app.query_one("#status-log", StatusLog).renderable == "Ready to search"
```

Replace `test_shell_navigation_updates_for_management_panes` with:

```python
@pytest.mark.asyncio
async def test_sidebar_clicks_open_management_panes(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.click("#nav-library")
        await pilot.pause()
        assert app.query_one("#downloads-table", DataTable).row_count == 0
        assert app.query_one("#status-log", StatusLog).renderable == "Viewing library"

        await pilot.click("#nav-history")
        await pilot.pause()
        assert app.query_one("#history-table", DataTable).row_count == 0
        assert app.query_one("#status-log", StatusLog).renderable == "Viewing history"

        await pilot.click("#nav-settings")
        await pilot.pause()
        assert str(app.query_one("#settings-output", SettingsOutput).renderable) == f"Output folder: {tmp_path}"
        assert app.query_one("#status-log", StatusLog).renderable == "Viewing settings"
```

- [ ] **Step 2: Run the shell tests to verify they fail**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_screens.py::test_shell_starts_with_clickable_state_aware_navigation tests/test_tui_screens.py::test_sidebar_clicks_open_management_panes -q
```

Expected: FAIL because the sidebar is still static text and `StatusLog` does not exist.

- [ ] **Step 3: Implement `NavigationItem`, `NavigationRail`, and `StatusLog`**

In `src/comix_dl/core/tui/app.py`, update imports:

```python
from textual import on
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

from comix_dl.core.tui.state import LogDrawerState
```

Replace the current `StatusBar` and `NavigationRail` classes with:

```python
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


class NavigationItem(Static):
    """One clickable navigation row."""

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
```

- [ ] **Step 4: Wire the new shell widgets and management click routing**

In `ComixTuiApp.BINDINGS`, add the log toggle and keep the existing tool shortcuts:

```python
        ("o", "toggle_logs", "Logs"),
```

In `compose()`, replace the old status widget:

```python
        yield StatusLog(widget_id="status-log")
        yield Footer()
```

Add these methods to `ComixTuiApp`:

```python
    def available_destinations(self) -> set[str]:
        """Return destinations visible in the sidebar."""
        return {"Search", "Library", "History", "Settings"}

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

    def action_toggle_logs(self) -> None:
        self.query_one("#status-log", StatusLog).toggle()

    @on(NavigationItem.Selected)
    async def _navigation_selected(self, event: NavigationItem.Selected) -> None:
        destination = event.destination
        if destination == "Search":
            await self.action_show_search()
        elif destination == "Library":
            await self.action_show_downloads()
        elif destination == "History":
            await self.action_show_history()
        elif destination == "Settings":
            await self.action_show_settings()
```

Update `_open_controller()` to use `self.set_status("Ready to search")` on success.

Update `action_show_search`, `action_show_downloads`, `action_show_history`, and `action_show_settings` to call `set_active_view(...)` and `set_status(...)`. Keep `action_show_downloads` as the method name for compatibility, but set the active view to `Library`.

- [ ] **Step 5: Style navigation and the log drawer**

In `src/comix_dl/core/tui/styles.tcss`, replace the old sidebar active class block with:

```css
#sidebar {
    width: 22;
    min-width: 18;
    height: 1fr;
    padding: 1 1;
    background: $panel;
    color: $text-muted;
    border-right: solid $primary;
}

.nav-section {
    text-style: bold;
    color: $primary;
    margin-top: 1;
}

.nav-item {
    height: 1;
    padding: 0 1;
    color: $text-muted;
}

.nav-item:hover,
.nav-item:focus {
    background: $boost;
    color: $text;
}

.nav-item.active {
    background: $primary-darken-2;
    color: $text;
    text-style: bold;
}

#status-log {
    height: 1;
    padding: 0 1;
    background: $panel;
    color: $text-muted;
    border-top: solid $surface-lighten-1;
}

#status-log.expanded {
    height: 5;
}
```

Remove or replace the old `#status` rules.

- [ ] **Step 6: Run the shell tests to verify they pass**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_screens.py::test_shell_starts_with_clickable_state_aware_navigation tests/test_tui_screens.py::test_sidebar_clicks_open_management_panes -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add src/comix_dl/core/tui/app.py src/comix_dl/core/tui/styles.tcss tests/test_tui_screens.py
git commit -m "feat: make tui sidebar clickable"
```

---

## Task 3: Move Search Messages Into The Status/Log Drawer

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `src/comix_dl/core/tui/screens/search.py`

- [ ] **Step 1: Update search tests for the drawer**

Replace status-bar assertions in these tests:

- `test_app_mounts_search_screen_and_opens_controller`
- `test_search_screen_guides_empty_query`
- `test_search_results_update_global_status`
- `test_result_enter_opens_series_pane`
- `test_search_input_escape_allows_global_navigation_shortcut`

Use `StatusLog` and `#status-log`. For example, update `test_search_screen_guides_empty_query` to:

```python
@pytest.mark.asyncio
async def test_search_screen_guides_empty_query(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("enter")
        await pilot.pause()

        assert "Type a manga name to begin" in str(app.query_one("#search-help", Static).content)
        assert app.query_one("#status-log", StatusLog).renderable == (
            "Type a manga name, then press Enter to search."
        )
```

Update `test_search_results_update_global_status` to assert:

```python
assert app.query_one("#status-log", StatusLog).renderable == "2 results found"
```

- [ ] **Step 2: Run the updated search tests to verify they fail**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_screens.py::test_search_screen_guides_empty_query tests/test_tui_screens.py::test_search_results_update_global_status -q
```

Expected: FAIL while `SearchScreen` still updates `#search-status`.

- [ ] **Step 3: Update `SearchApp` protocol and Search screen status calls**

In `src/comix_dl/core/tui/screens/search.py`, update `SearchApp`:

```python
class SearchApp(Protocol):
    """App shell surface used by the search pane."""

    def set_active_view(self, active: str) -> None:
        """Set the active shell navigation destination."""

    def set_status(self, message: str) -> None:
        """Set the shared shell status text."""

    def set_loaded_series(self, series: SeriesInfo) -> None:
        """Store the current loaded series for navigation."""
```

Keep `#search-status` out of `compose()` by removing:

```python
yield Static("Type a manga name, then press Enter to search.", id="search-status", classes="muted")
```

Update `_submit_search()` empty-query branch:

```python
        if not query:
            self.shell.set_status("Type a manga name, then press Enter to search.")
            return
```

Update `_search()` to remove direct `#search-status` access and use drawer messages:

```python
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
```

Keep result rows as they are, then finish with:

```python
        self.shell.set_status(f"{len(self.results)} results found")
        table.focus()
```

Update invalid selection and series-load messages:

```python
            self.shell.set_status("Invalid result selection.")
            return
```

In `_load_series()`, use:

```python
        self.shell.set_status(f"Loading {result.title}")
```

After loading:

```python
        self.shell.set_loaded_series(info)
        self.shell.set_active_view("Chapters")
        self.shell.set_status(f"Series loaded: {info.title}")
```

- [ ] **Step 4: Run search-focused tests**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_screens.py::test_search_screen_guides_empty_query tests/test_tui_screens.py::test_search_results_update_global_status tests/test_result_enter_opens_series_pane -q
```

Expected: `test_search_screen_guides_empty_query` and `test_search_results_update_global_status` PASS. `test_result_enter_opens_series_pane` fails with `AttributeError` for `set_loaded_series` until Task 4 Step 3 adds series navigation state.

- [ ] **Step 5: Do not commit yet**

Task 3 intentionally depends on Task 4 for `set_loaded_series`. Leave these changes uncommitted and continue to Task 4. Task 4 Step 6 commits the Search and Chapters changes together.

---

## Task 4: Reveal Chapters After Series Load And Preserve Chapter State

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `src/comix_dl/core/tui/app.py`
- Modify: `src/comix_dl/core/tui/screens/series.py`

- [ ] **Step 1: Add failing tests for Chapters visibility and preservation**

Append these tests to `tests/test_tui_screens.py`:

```python
@pytest.mark.asyncio
async def test_chapters_navigation_appears_after_series_load(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    controller.load_series.return_value = _series()
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()

        assert "Chapters" not in app.query_one("#sidebar", NavigationRail).rendered_text

        await pilot.press("enter")
        await pilot.pause()

        assert "Chapters" in app.query_one("#sidebar", NavigationRail).rendered_text
        assert "1 Search" not in app.query_one("#sidebar", NavigationRail).rendered_text


@pytest.mark.asyncio
async def test_chapters_sidebar_click_restores_filter_selection_and_format(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    controller.load_series.return_value = _series()
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("/")
        await pilot.press(*"+extra")
        await pilot.press("enter")
        await pilot.press("space")
        await pilot.press("f")
        await pilot.click("#nav-library")
        await pilot.pause()
        await pilot.click("#nav-chapters")
        await pilot.pause()

        assert str(app.query_one("#selection-summary", Static).content) == "1 selected from 1 visible chapters."
        assert "Format: CBZ." in str(app.query_one("#series-status", Static).content)
```

- [ ] **Step 2: Run the new Chapters tests to verify they fail**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_screens.py::test_chapters_navigation_appears_after_series_load tests/test_tui_screens.py::test_chapters_sidebar_click_restores_filter_selection_and_format -q
```

Expected: FAIL because Chapters is not state-aware or clickable yet.

- [ ] **Step 3: Add series state to the app shell**

In `src/comix_dl/core/tui/app.py`, import:

```python
from comix_dl.core.tui.state import DownloadNavigationState, LogDrawerState, SeriesNavigationState
```

In the `TYPE_CHECKING` block, add:

```python
    from comix_dl.core.models import SeriesInfo
    from comix_dl.core.tui.state import OutputFormat
```

In `ComixTuiApp.__init__`, add:

```python
        self._series_state: SeriesNavigationState | None = None
        self._download_state: DownloadNavigationState | None = None
```

Update `available_destinations()`:

```python
    def available_destinations(self) -> set[str]:
        """Return destinations visible in the sidebar."""
        destinations = {"Search", "Library", "History", "Settings"}
        if self._series_state is not None:
            destinations.add("Chapters")
        if self._download_state is not None:
            destinations.add("Download")
        return destinations
```

Add:

```python
    def set_loaded_series(self, series: SeriesInfo) -> SeriesNavigationState:
        """Store a loaded series and reveal the Chapters destination."""
        self._series_state = SeriesNavigationState.from_series(
            series,
            default_format=self.controller.settings.default_format,
        )
        self.refresh_navigation("Chapters")
        return self._series_state
```

Update `_navigation_selected()`:

```python
        elif destination == "Chapters":
            await self.action_show_chapters()
```

Add:

```python
    async def action_show_chapters(self) -> None:
        from comix_dl.core.tui.screens.series import SeriesPane

        if self._series_state is None:
            self.set_status("Search and select a manga before opening Chapters.")
            return
        self.set_active_view("Chapters")
        self.set_status(f"Series loaded: {self._series_state.series.title}")
        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(SeriesPane(self.controller, self._series_state))
```

- [ ] **Step 4: Update `SeriesPane` to accept `SeriesNavigationState`**

In `src/comix_dl/core/tui/screens/series.py`, import:

```python
from comix_dl.core.tui.state import ChapterSelectionState, DownloadRequest, OutputFormat, SeriesNavigationState
```

Change `SeriesPane.__init__`:

```python
    def __init__(self, controller: object, state: SeriesNavigationState) -> None:
        super().__init__()
        self.controller = cast("SeriesController", controller)
        self.state = state
        self.series = state.series
        self.selection = state.selection
        self.format_value = state.format_value
```

Update `_format_changed()`:

```python
            self.format_value = cast("OutputFormat", event.value)
            self.state.format_value = self.format_value
            self._refresh_status()
```

Update `action_cycle_format()` after assigning `self.format_value`:

```python
        self.state.format_value = self.format_value
```

The selection object is already shared, so filter and selection changes persist through `self.selection`.

Update places that instantiate `SeriesPane` in tests and Search to pass a `SeriesNavigationState`.

In tests that mount SeriesPane directly, create state with:

```python
from comix_dl.core.tui.state import SeriesNavigationState

series_state = SeriesNavigationState.from_series(_series(), default_format="pdf")
await host.mount(SeriesPane(controller, series_state))
```

- [ ] **Step 5: Run Chapters tests and affected direct SeriesPane tests**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_screens.py::test_chapters_navigation_appears_after_series_load tests/test_tui_screens.py::test_chapters_sidebar_click_restores_filter_selection_and_format tests/test_tui_screens.py::test_series_download_without_selection_is_actionable tests/test_tui_screens.py::test_series_pane_filters_selects_and_starts_download -q
```

Expected: PASS.

- [ ] **Step 6: Commit Tasks 3 and 4 if Task 3 was not committed**

Run:

```bash
git add src/comix_dl/core/tui/app.py src/comix_dl/core/tui/screens/search.py src/comix_dl/core/tui/screens/series.py tests/test_tui_screens.py
git commit -m "feat: reveal tui chapters navigation"
```

---

## Task 5: Reveal Download Navigation And Preserve Download State

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `src/comix_dl/core/tui/app.py`
- Modify: `src/comix_dl/core/tui/screens/series.py`
- Modify: `src/comix_dl/core/tui/screens/download.py`

- [ ] **Step 1: Add failing tests for Download visibility and no restart**

Append these tests to `tests/test_tui_screens.py`:

```python
@pytest.mark.asyncio
async def test_download_navigation_appears_after_starting_download(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a")
    ]
    controller.load_series.return_value = _series()
    controller.download.return_value = _make_summary(completed=1)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("space")
        await pilot.press("d")
        await pilot.pause()

        assert "Download" in app.query_one("#sidebar", NavigationRail).rendered_text
        assert app.query_one("#status-log", StatusLog).renderable == "Download complete"


@pytest.mark.asyncio
async def test_download_sidebar_click_restores_completed_download_without_restart(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.download.return_value = _make_summary(completed=1)
    from comix_dl.core.tui.state import DownloadNavigationState, DownloadRequest

    series = _series()
    request = DownloadRequest(series_title=series.title, chapters=tuple(series.chapters[:1]), fmt="pdf", optimize=True)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        app.set_current_download(DownloadNavigationState.from_request(request))
        await app.action_show_download()
        await pilot.pause()

        assert controller.download.await_count == 1

        await pilot.click("#nav-library")
        await pilot.pause()
        await pilot.click("#nav-download")
        await pilot.pause()

        assert controller.download.await_count == 1
        assert "Complete" in str(app.query_one("#download-summary", Static).content)
```

- [ ] **Step 2: Run the new Download tests to verify they fail**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_screens.py::test_download_navigation_appears_after_starting_download tests/test_tui_screens.py::test_download_sidebar_click_restores_completed_download_without_restart -q
```

Expected: FAIL because Download navigation state is not wired yet.

- [ ] **Step 3: Add download state to the app shell**

In `ComixTuiApp`, add:

```python
    def set_current_download(self, state: DownloadNavigationState) -> None:
        """Store the current download state and reveal Download navigation."""
        self._download_state = state
        self.refresh_navigation("Download")
```

Update `_navigation_selected()`:

```python
        elif destination == "Download":
            await self.action_show_download()
```

Add:

```python
    async def action_show_download(self) -> None:
        from comix_dl.core.tui.screens.download import DownloadPane

        if self._download_state is None:
            self.set_status("Start a download before opening Download.")
            return
        self.set_active_view("Download")
        host = self.query_one("#screen-host", Container)
        await host.remove_children()
        await host.mount(DownloadPane(self.controller, self._download_state))
```

- [ ] **Step 4: Update SeriesPane to create DownloadNavigationState**

In `src/comix_dl/core/tui/screens/series.py`, update imports:

```python
from comix_dl.core.tui.state import (
    ChapterSelectionState,
    DownloadNavigationState,
    DownloadRequest,
    OutputFormat,
    SeriesNavigationState,
)
```

Update `SeriesApp` protocol:

```python
    def set_current_download(self, state: DownloadNavigationState) -> None:
        """Store the current download state for navigation."""
```

In `action_start_download()`, replace direct `DownloadPane` construction with:

```python
        download_state = DownloadNavigationState.from_request(request)
        self.shell.set_current_download(download_state)
        from comix_dl.core.tui.screens.download import DownloadPane

        host = self.app.query_one("#screen-host")
        self.shell.set_active_view("Download")
        self.shell.set_status(f"Downloading {len(selected)} chapter(s)")
        await host.remove_children()
        await host.mount(DownloadPane(self.controller, download_state))
```

- [ ] **Step 5: Update DownloadPane to use DownloadNavigationState**

In `src/comix_dl/core/tui/screens/download.py`, update imports:

```python
from comix_dl.core.tui.state import DownloadNavigationState, format_summary_line
```

Change `DownloadPane.__init__`:

```python
    def __init__(self, controller: object, state: DownloadNavigationState) -> None:
        super().__init__()
        self.controller = cast("DownloadController", controller)
        self.state = state
        self.request = state.request
        self.rows = state.rows
        self._cleanup_plan: CleanupPlan | None = None
```

Update `compose()` to use stored status:

```python
            yield DownloadStatus(self.state.status_text, id="download-status", classes="muted")
```

Update `on_mount()`:

```python
        self._refresh_table()
        table.focus()
        self.shell.set_active_view("Download")
        if self.state.phase == "ready":
            self.shell.set_status(f"Downloading {len(self.request.chapters)} chapter(s)")
            self.run_worker(
                self._run_download(),
                name="download",
                group="download",
                exclusive=True,
                exit_on_error=False,
            )
            return

        self._set_summary(self.state.phase.capitalize())
        self._set_status(self.state.status_text)
        self.query_one("#cancel-button", Button).disabled = self.state.phase != "running"
        self.query_one("#cleanup-button", Button).disabled = not self.state.cleanup_available
```

Update `_set_status()`:

```python
        self.state.status_text = message
        self.query_one("#download-status", Static).update(message)
```

Update `_run_download()` state transitions:

```python
        self.state.phase = "running"
```

In the exception block:

```python
            self.state.phase = "failed"
```

On successful completion:

```python
        self.state.phase = "complete"
```

When cleanup is available:

```python
            self.state.cleanup_available = True
```

When no cleanup is available:

```python
        self.state.cleanup_available = False
```

Update `action_cancel()`:

```python
        self.state.phase = "cancelling"
```

Update `action_cleanup()` after disabling the button:

```python
        self.state.cleanup_available = False
```

- [ ] **Step 6: Run Download navigation tests**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_screens.py::test_download_navigation_appears_after_starting_download tests/test_tui_screens.py::test_download_sidebar_click_restores_completed_download_without_restart tests/test_tui_screens.py::test_download_pane_runs_download_and_renders_summary tests/test_tui_screens.py::test_download_screen_shows_batch_summary -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

Run:

```bash
git add src/comix_dl/core/tui/app.py src/comix_dl/core/tui/screens/series.py src/comix_dl/core/tui/screens/download.py tests/test_tui_screens.py
git commit -m "feat: preserve tui download navigation"
```

---

## Task 6: Finish Status/Log Drawer Behavior Across Screens

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `src/comix_dl/core/tui/app.py`
- Modify: `src/comix_dl/core/tui/screens/series.py`
- Modify: `src/comix_dl/core/tui/screens/download.py`

- [ ] **Step 1: Add tests for log drawer toggle and validation messages**

Append these tests to `tests/test_tui_screens.py`:

```python
@pytest.mark.asyncio
async def test_status_log_toggles_above_footer(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        log = app.query_one("#status-log", StatusLog)

        assert "expanded" not in log.classes
        assert log.renderable == "Ready to search"

        app.set_status("Second message")
        app.set_status("Third message")
        await pilot.press("o")
        await pilot.pause()

        assert "expanded" in log.classes
        assert "Ready to search" in str(log.renderable)
        assert "Third message" in str(log.renderable)


@pytest.mark.asyncio
async def test_no_selection_download_message_uses_status_log(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    from comix_dl.core.tui.screens.series import SeriesPane
    from comix_dl.core.tui.state import SeriesNavigationState

    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        series_state = SeriesNavigationState.from_series(_series(), default_format="pdf")
        app._series_state = series_state
        host = app.query_one("#screen-host")
        await host.remove_children()
        await host.mount(SeriesPane(controller, series_state))
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        assert app.query_one("#status-log", StatusLog).renderable == "Select at least one chapter before downloading."
```

- [ ] **Step 2: Run the new status/log tests to verify they fail**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_screens.py::test_status_log_toggles_above_footer tests/test_tui_screens.py::test_no_selection_download_message_uses_status_log -q
```

Expected: FAIL until the no-selection message and toggle rendering are fully wired.

- [ ] **Step 3: Update Series no-selection message**

In `SeriesPane.action_start_download()`, replace the current `#series-status` no-selection message with:

```python
            message = "Select at least one chapter before downloading."
            self.query_one("#series-status", Static).update(message)
            self.shell.set_status(message)
            return
```

- [ ] **Step 4: Ensure StatusLog renders expanded history with first message retained**

In `StatusLog.__init__`, keep `LogDrawerState(max_messages=5)`.

In `_open_controller()`, use:

```python
        self.set_status("Ready to search")
```

This ensures the initial message is part of the log history.

- [ ] **Step 5: Route Download status messages to the drawer**

In `DownloadPane._set_status()`, append:

```python
        self.shell.set_status(message)
```

When this creates duplicate messages during `_run_download()`, keep the user-facing result correct by calling `_set_status()` for visible download messages and removing separate `self.shell.set_status(...)` calls that repeat the same text in `_run_download()`, `action_cancel()`, and cleanup paths.

- [ ] **Step 6: Run the status/log tests**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_screens.py::test_status_log_toggles_above_footer tests/test_tui_screens.py::test_no_selection_download_message_uses_status_log tests/test_tui_screens.py::test_download_pane_runs_download_and_renders_summary tests/test_tui_screens.py::test_download_cleanup_button_applies_plan_and_renders_result -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 6**

Run:

```bash
git add src/comix_dl/core/tui/app.py src/comix_dl/core/tui/screens/series.py src/comix_dl/core/tui/screens/download.py tests/test_tui_screens.py
git commit -m "feat: add tui status log drawer"
```

---

## Task 7: Update Remaining Tests And Verify Boundaries

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `tests/test_tui_boundaries.py` only if the boundary test needs path updates.

- [ ] **Step 1: Run all TUI tests to find stale expectations**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_state.py tests/test_tui_screens.py tests/test_tui_boundaries.py tests/test_tui_cli.py -q
```

Expected: FAIL only if stale test expectations remain. Use the failure output to locate any remaining references to `StatusBar`, `#status`, `1 Search`, direct `SeriesPane(controller, _series())`, or direct `DownloadPane(controller, request)`.

- [ ] **Step 2: Replace remaining stale status assertions**

In `tests/test_tui_screens.py`, replace:

```python
app.query_one("#status", StatusBar).renderable
```

with:

```python
app.query_one("#status-log", StatusLog).renderable
```

Replace imports of `StatusBar` with `StatusLog`.

- [ ] **Step 3: Replace remaining direct `SeriesPane` construction**

For each direct `SeriesPane(controller, _series())`, use:

```python
series_state = SeriesNavigationState.from_series(_series(), default_format="pdf")
await host.mount(SeriesPane(controller, series_state))
```

At the top of `tests/test_tui_screens.py`, ensure this import is present:

```python
from comix_dl.core.tui.state import SeriesNavigationState
```

- [ ] **Step 4: Replace remaining direct `DownloadPane` construction**

For each direct `DownloadPane(controller, request)`, use:

```python
download_state = DownloadNavigationState.from_request(request)
await host.mount(DownloadPane(controller, download_state))
```

At the top of `tests/test_tui_screens.py`, ensure this import is present:

```python
from comix_dl.core.tui.state import DownloadNavigationState
```

- [ ] **Step 5: Run all TUI tests again**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_state.py tests/test_tui_screens.py tests/test_tui_boundaries.py tests/test_tui_cli.py -q
```

Expected: PASS.

- [ ] **Step 6: Run formatting and linting for touched files**

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m ruff check src/comix_dl/core/tui tests/test_tui_state.py tests/test_tui_screens.py
```

Expected: PASS.

- [ ] **Step 7: Commit test cleanup when files changed**

Run:

```bash
git add tests/test_tui_state.py tests/test_tui_screens.py tests/test_tui_boundaries.py
if ! git diff --cached --quiet; then git commit -m "test: update tui navigation coverage"; fi
```

Expected: creates a commit when Task 7 staged changes, and does nothing when Task 7 did not change test files.

---

## Task 8: Manual TUI Smoke Check

**Files:**
- No planned file edits.

- [ ] **Step 1: Run the TUI locally**

Run:

```bash
PYTHONPATH=src /Users/HAKU/github/comix-downloader/.venv/bin/python -m comix_dl tui
```

Expected:

- Sidebar shows `WORKFLOW`, `Search`, `TOOLS`, `Library`, `History`, `Settings`.
- Sidebar does not show `Chapters` or `Download` on first launch.
- Footer stays at the bottom.
- Status/log drawer sits directly above Footer.

- [ ] **Step 2: Exercise keyboard-only navigation**

Use:

```text
s
d
h
g
o
o
q
```

Expected:

- `s` opens Search.
- `d` opens Library.
- `h` opens History.
- `g` opens Settings.
- `o` expands and collapses the status/log drawer.
- `q` exits.

- [ ] **Step 3: Exercise mouse navigation in Textual test or live terminal**

Click visible sidebar rows:

```text
Search
Library
History
Settings
```

Expected: each visible item opens its destination. There are no visible unavailable workflow rows.

- [ ] **Step 4: Stop the TUI and record manual smoke result**

Press `q` to stop the TUI. Record one of these exact final-report notes:

```text
Manual TUI smoke passed.
```

or:

```text
Manual live TUI was not run; automated TUI tests passed.
```

---

## Final Verification

Run:

```bash
/Users/HAKU/github/comix-downloader/.venv/bin/python -m pytest tests/test_tui_state.py tests/test_tui_screens.py tests/test_tui_boundaries.py tests/test_tui_cli.py -q
/Users/HAKU/github/comix-downloader/.venv/bin/python -m ruff check src/comix_dl/core/tui tests/test_tui_state.py tests/test_tui_screens.py
git status --short
```

Expected:

- TUI pytest subset passes.
- Ruff passes.
- `git status --short` shows no unstaged changes after the final implementation commit, or only intentional uncommitted changes if the user asked not to commit.
