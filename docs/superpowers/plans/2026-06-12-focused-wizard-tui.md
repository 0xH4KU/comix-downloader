# Focused Wizard TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh the Textual TUI into a beginner-friendly focused wizard with clearer shell navigation, screen guidance, status messaging, and download completion actions.

**Architecture:** Keep the current `comix_dl.core.tui` boundaries: `app.py` owns shell/navigation/session status, individual screen modules own their task UI, and `state.py` owns pure reducer/filtering state. Add only small local presentation helpers and app methods where screens need to update shared shell status or active navigation.

**Tech Stack:** Python 3.11, Textual 8.2, pytest, pytest-asyncio, ruff, mypy.

---

## File Structure

- Modify `src/comix_dl/core/tui/app.py`
  - Add a structured sidebar widget/state surface.
  - Add app methods for setting active navigation and shared status text.
  - Rename the downloads navigation destination to Library while preserving the existing `action_show_downloads` binding behavior.

- Modify `src/comix_dl/core/tui/styles.tcss`
  - Add focused wizard layout styles for sidebar items, page headers, helper text, panels, status messages, and management empty states.

- Modify `src/comix_dl/core/tui/screens/search.py`
  - Add helper text and friendlier status messages.
  - Update shell status/active step when results load and when a series opens.

- Modify `src/comix_dl/core/tui/screens/series.py`
  - Add selection summary, filter helper text, and clearer no-selection messaging.
  - Update shell status/active step for chapter selection and download handoff.

- Modify `src/comix_dl/core/tui/screens/download.py`
  - Add batch summary above the table.
  - Update top-level status for running, cancellation, completion, partials, failures, and cleanup.
  - Update shell status/active step.

- Modify `src/comix_dl/core/tui/screens/manage.py`
  - Rename Downloads copy to Library.
  - Add empty states for Library and History.
  - Present Settings as labeled rows.

- Modify `tests/test_tui_screens.py`
  - Add failing tests for active navigation, friendlier Search/Series/Download states, no-selection validation, management empty states, and settings labels.
  - Update existing expectations where labels intentionally change.

## Task 1: Shell Navigation And Shared Status

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `src/comix_dl/core/tui/app.py`
- Modify: `src/comix_dl/core/tui/styles.tcss`

- [ ] **Step 1: Write failing tests for shell active navigation and session status**

Add imports and assertions in `tests/test_tui_screens.py`:

```python
from comix_dl.core.tui.app import ComixTuiApp, NavigationRail, StatusBar
```

Add tests:

```python
@pytest.mark.asyncio
async def test_shell_starts_with_focused_wizard_navigation(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        rail = app.query_one("#sidebar", NavigationRail)

        assert "1 Search" in str(rail.renderable)
        assert "2 Chapters" in str(rail.renderable)
        assert "3 Download" in str(rail.renderable)
        assert "Library" in str(rail.renderable)
        assert "Search" in rail.classes
        assert app.query_one("#status", StatusBar).renderable == "Ready to search"


@pytest.mark.asyncio
async def test_shell_navigation_updates_for_management_panes(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("escape")
        await pilot.press("d")
        await pilot.pause()
        rail = app.query_one("#sidebar", NavigationRail)
        assert "Library" in rail.classes
        assert app.query_one("#status", StatusBar).renderable == "Viewing library"

        await pilot.press("h")
        await pilot.pause()
        assert "History" in app.query_one("#sidebar", NavigationRail).classes
        assert app.query_one("#status", StatusBar).renderable == "Viewing history"

        await pilot.press("g")
        await pilot.pause()
        assert "Settings" in app.query_one("#sidebar", NavigationRail).classes
        assert app.query_one("#status", StatusBar).renderable == "Viewing settings"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_screens.py::test_shell_starts_with_focused_wizard_navigation tests/test_tui_screens.py::test_shell_navigation_updates_for_management_panes -q
```

Expected: FAIL because `NavigationRail` does not exist and the old sidebar/status text is still used.

- [ ] **Step 3: Implement shell navigation state**

In `src/comix_dl/core/tui/app.py`, add a `NavigationRail` `Static` subclass and app helpers:

```python
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

    @property
    def renderable(self) -> object:
        return self.content

    def set_active(self, active: str) -> None:
        self.active = active
        self.set_classes(" ".join(name for name, _label in self._ITEMS if name == active))
        self.update(self._render())

    def _render(self) -> str:
        lines: list[str] = []
        for name, label in self._ITEMS:
            marker = ">" if name == self.active else " "
            lines.append(f"{marker} {label}")
        return "\n".join(lines)
```

Update `compose()` to yield `NavigationRail(id="sidebar")` instead of the raw `Static`.

Add app helpers:

```python
    def set_active_view(self, active: str) -> None:
        self.query_one("#sidebar", NavigationRail).set_active(active)

    def set_status(self, message: str) -> None:
        self.query_one("#status", StatusBar).update(message)
```

Update `_open_controller()` success status to `Ready to search`.

Update navigation actions:

```python
    async def action_show_search(self) -> None:
        self.set_active_view("Search")
        self.set_status("Ready to search")
        ...

    async def action_show_downloads(self) -> None:
        self.set_active_view("Library")
        self.set_status("Viewing library")
        ...
```

Do the same for History and Settings.

- [ ] **Step 4: Add styles for active navigation**

In `styles.tcss`, update `#sidebar` and add active class styles:

```css
#sidebar {
    width: 20;
    min-width: 18;
    padding: 1 1;
    background: $panel;
    color: $text-muted;
    border-right: solid $primary;
}

#sidebar.Search,
#sidebar.Chapters,
#sidebar.Download,
#sidebar.Library,
#sidebar.History,
#sidebar.Settings {
    text-style: bold;
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_screens.py::test_shell_starts_with_focused_wizard_navigation tests/test_tui_screens.py::test_shell_navigation_updates_for_management_panes -q
```

Expected: PASS.

## Task 2: Search Screen Guidance

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `src/comix_dl/core/tui/screens/search.py`

- [ ] **Step 1: Write failing tests for friendly search copy and shell status**

Add tests:

```python
@pytest.mark.asyncio
async def test_search_screen_guides_empty_query(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("enter")
        await pilot.pause()

        assert "Type a manga name to begin" in str(app.query_one("#search-help").renderable)
        assert str(app.query_one("#search-status").renderable) == "Type a manga name, then press Enter to search."
        assert app.query_one("#status", StatusBar).renderable == "Ready to search"


@pytest.mark.asyncio
async def test_search_results_update_global_status(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.search.return_value = [
        SearchResult(title="Series A", url="https://comix.to/manga/series-a", slug="series-a", hash_id="a"),
        SearchResult(title="Series B", url="https://comix.to/manga/series-b", slug="series-b", hash_id="b"),
    ]
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.click("#search-input")
        await pilot.press(*"series")
        await pilot.press("enter")
        await pilot.pause()

        assert str(app.query_one("#search-status").renderable) == "2 results found. Select a row and press Enter to open it."
        assert app.query_one("#status", StatusBar).renderable == "2 results found"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_screens.py::test_search_screen_guides_empty_query tests/test_tui_screens.py::test_search_results_update_global_status -q
```

Expected: FAIL because `#search-help` does not exist and the status text is old.

- [ ] **Step 3: Implement search helper text and status updates**

In `search.py`, update `compose()`:

```python
yield Static("Search", classes="pane-title")
yield Static("Type a manga name to begin. Results will appear below.", id="search-help", classes="muted helper-text")
yield SearchInput(placeholder="Manga title", id="search-input")
yield Static("Type a manga name, then press Enter to search.", id="search-status", classes="muted")
```

Update empty query handling:

```python
self.query_one("#search-status", Static).update("Type a manga name, then press Enter to search.")
self.app.set_status("Ready to search")
```

Update `_search()`:

```python
status.update(f"Searching for '{query}'...")
self.app.set_status(f"Searching for '{query}'")
...
status.update("No results found. Try a different title.")
self.app.set_status("No results found")
...
status.update(f"{len(self.results)} results found. Select a row and press Enter to open it.")
self.app.set_status(f"{len(self.results)} results found")
```

Before mounting `SeriesPane`, call:

```python
self.app.set_active_view("Chapters")
self.app.set_status(f"Series loaded: {info.title}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_screens.py::test_search_screen_guides_empty_query tests/test_tui_screens.py::test_search_results_update_global_status tests/test_search_screen_submits_query_and_renders_results tests/test_result_enter_opens_series_pane -q
```

Expected: PASS.

## Task 3: Series Screen Selection Guidance

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `src/comix_dl/core/tui/screens/series.py`

- [ ] **Step 1: Write failing tests for selection summary and no-selection validation**

Add tests:

```python
@pytest.mark.asyncio
async def test_series_screen_shows_selection_guidance(tmp_path: Path) -> None:
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

        assert "Use +extra to keep matches or -extra to exclude them" in str(app.query_one("#filter-help").renderable)
        assert str(app.query_one("#selection-summary").renderable) == "0 selected from 2 visible chapters."
        assert app.query_one("#status", StatusBar).renderable == "Series loaded: Series A"


@pytest.mark.asyncio
async def test_series_download_without_selection_is_actionable(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.load_series.return_value = _series()
    from comix_dl.core.tui.screens.series import SeriesPane

    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        host = app.query_one("#screen-host")
        await host.remove_children()
        await host.mount(SeriesPane(controller, _series()))
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        assert str(app.query_one("#series-status").renderable) == "Select at least one chapter with Space, then press D to download."
        assert app.query_one("#status", StatusBar).renderable == "Select chapters before downloading"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_screens.py::test_series_screen_shows_selection_guidance tests/test_tui_screens.py::test_series_download_without_selection_is_actionable -q
```

Expected: FAIL because `#filter-help` and `#selection-summary` do not exist and no-selection text is old.

- [ ] **Step 3: Implement series guidance and shell status updates**

In `series.py`, update `compose()` after metadata:

```python
yield Static(self._selection_summary_text(), id="selection-summary", classes="muted")
yield Static("Use +extra to keep matches or -extra to exclude them.", id="filter-help", classes="muted helper-text")
```

Add helper:

```python
def _selection_summary_text(self) -> str:
    return f"{self.selection.selected_count} selected from {len(self.selection.visible_chapters)} visible chapters."
```

Update `_refresh_status()`:

```python
self.query_one("#selection-summary", Static).update(self._selection_summary_text())
message = self.selection.status or "Space selects a row. A selects visible rows. X clears visible rows. D starts download."
self.query_one("#series-status", Static).update(f"{message} Format: {self.format_value.upper()}.")
```

In `on_mount()`, call:

```python
if hasattr(self.app, "set_active_view"):
    self.app.set_active_view("Chapters")
if hasattr(self.app, "set_status"):
    self.app.set_status(f"Series loaded: {self.series.title}")
```

In no-selection download branch:

```python
self.query_one("#series-status", Static).update("Select at least one chapter with Space, then press D to download.")
self.app.set_status("Select chapters before downloading")
```

Before mounting `DownloadPane`:

```python
self.app.set_active_view("Download")
self.app.set_status(f"Downloading {len(selected)} chapter(s)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_screens.py::test_series_screen_shows_selection_guidance tests/test_tui_screens.py::test_series_download_without_selection_is_actionable tests/test_series_pane_filters_selects_and_starts_download -q
```

Expected: PASS.

## Task 4: Download Batch Summary And Completion Actions

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `src/comix_dl/core/tui/screens/download.py`

- [ ] **Step 1: Write failing tests for batch summary and completion messaging**

Add tests:

```python
@pytest.mark.asyncio
async def test_download_screen_shows_batch_summary(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.download.return_value = _make_summary(completed=1, total_bytes=2048, elapsed_seconds=2.0)
    from comix_dl.core.tui.screens.download import DownloadPane
    from comix_dl.core.tui.state import DownloadRequest

    series = _series()
    request = DownloadRequest(series_title=series.title, chapters=tuple(series.chapters[:1]), fmt="pdf", optimize=True)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(120, 36)) as pilot:
        host = app.query_one("#screen-host")
        await host.remove_children()
        await host.mount(DownloadPane(controller, request))
        await pilot.pause()

        assert str(app.query_one("#download-title", DownloadTitle).renderable) == "Download"
        assert "Series A · 1 chapter(s) · PDF" in str(app.query_one("#download-summary").renderable)
        assert "Next: cleanup raw folders, return to Search, or inspect Library." in str(
            app.query_one("#download-status", DownloadStatus).renderable
        )
        assert app.query_one("#status", StatusBar).renderable == "Download complete"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_screens.py::test_download_screen_shows_batch_summary -q
```

Expected: FAIL because `#download-summary` does not exist and the old title/status are used.

- [ ] **Step 3: Implement batch summary and global status**

In `download.py`, update title and add summary:

```python
yield DownloadTitle("Download", id="download-title", classes="pane-title")
yield Static(self._batch_summary("Preparing"), id="download-summary", classes="muted")
```

Add helper:

```python
def _batch_summary(self, state: str) -> str:
    return f"{self.request.series_title} · {len(self.request.chapters)} chapter(s) · {self.request.fmt.upper()} · {state}"
```

Add:

```python
def _set_summary(self, state: str) -> None:
    self.query_one("#download-summary", Static).update(self._batch_summary(state))
```

In `on_mount()`, set app active/status:

```python
self.app.set_active_view("Download")
self.app.set_status(f"Downloading {len(self.request.chapters)} chapter(s)")
```

In `_run_download()`:

```python
self._set_summary("Running")
self.app.set_status(f"Downloading {len(self.request.chapters)} chapter(s)")
...
self._set_summary("Complete")
self.app.set_status("Download complete")
status = f"Download complete: {summary_line}. Next: cleanup raw folders, return to Search, or inspect Library."
```

In exception branch:

```python
self._set_summary("Failed")
self.app.set_status("Download failed")
```

In `action_cancel()`:

```python
self._set_summary("Cancelling")
self.app.set_status("Cancelling download")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_screens.py::test_download_screen_shows_batch_summary tests/test_download_pane_runs_download_and_renders_summary tests/test_download_cancel_requests_shutdown tests/test_download_cleanup_button_applies_plan_and_renders_result -q
```

Expected: PASS.

## Task 5: Management Empty States And Settings Rows

**Files:**
- Modify: `tests/test_tui_screens.py`
- Modify: `src/comix_dl/core/tui/screens/manage.py`

- [ ] **Step 1: Write failing tests for empty states and labels**

Add tests:

```python
@pytest.mark.asyncio
async def test_management_panes_show_empty_states(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("escape")
        await pilot.press("d")
        await pilot.pause()
        assert "No downloads yet. Completed manga will appear here." in str(app.query_one("#library-empty").renderable)

        await pilot.press("h")
        await pilot.pause()
        assert "No history yet. Finished downloads will be listed here." in str(app.query_one("#history-empty").renderable)


@pytest.mark.asyncio
async def test_settings_pane_uses_labeled_rows(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    app = ComixTuiApp(controller=controller)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("escape")
        await pilot.press("g")
        await pilot.pause()

        assert str(app.query_one("#settings-output", SettingsOutput).renderable) == f"Output folder: {tmp_path}"
        assert "Default format: pdf" in str(app.query_one("#settings-format").renderable)
        assert "Changes apply to the next app session." in str(app.query_one("#settings-note").renderable)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_screens.py::test_management_panes_show_empty_states tests/test_tui_screens.py::test_settings_pane_uses_labeled_rows -q
```

Expected: FAIL because empty state IDs and settings row IDs do not exist.

- [ ] **Step 3: Implement management empty states and labels**

In `manage.py`, update `DownloadsPane.compose()` title to `Library` and add:

```python
yield Static("", id="library-empty", classes="muted empty-state")
```

In `on_mount()`, after loading downloads:

```python
if not downloads:
    self.query_one("#library-empty", Static).update("No downloads yet. Completed manga will appear here.")
    return
self.query_one("#library-empty", Static).update("")
```

Update `HistoryPane.compose()` similarly:

```python
yield Static("", id="history-empty", classes="muted empty-state")
```

In `on_mount()`:

```python
if not entries:
    self.query_one("#history-empty", Static).update("No history yet. Finished downloads will be listed here.")
    return
self.query_one("#history-empty", Static).update("")
```

Update `SettingsPane.compose()`:

```python
yield SettingsOutput(f"Output folder: {settings.output_dir}", id="settings-output")
yield Static(f"Default format: {settings.default_format}", id="settings-format")
yield Static(f"Concurrency profile: {settings.concurrency_profile}", id="settings-concurrency")
yield Static(f"Optimize images: {settings.optimize_images}", id="settings-optimize")
yield Static("Changes apply to the next app session.", id="settings-note", classes="muted helper-text")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_screens.py::test_management_panes_show_empty_states tests/test_tui_screens.py::test_settings_pane_uses_labeled_rows tests/test_downloads_history_and_settings_panes_render_controller_data -q
```

Expected: PASS.

## Task 6: Focused Wizard Styling And Regression Verification

**Files:**
- Modify: `src/comix_dl/core/tui/styles.tcss`
- Modify: `tests/test_tui_screens.py` if existing expectations need intentional label updates.

- [ ] **Step 1: Update focused wizard styles**

Add CSS:

```css
.helper-text {
    margin-bottom: 1;
}

.empty-state {
    margin-bottom: 1;
}

#selection-summary,
#download-summary {
    margin-bottom: 1;
}

#series-meta {
    margin-bottom: 1;
}
```

Tune the existing input/tool rows only if tests or visual structure need it.

- [ ] **Step 2: Run TUI regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_tui_state.py tests/test_tui_screens.py tests/test_tui_boundaries.py tests/test_tui_cli.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full quality checks**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src tests
```

Expected: PASS for all commands.

- [ ] **Step 4: Commit implementation**

Run:

```bash
git add docs/superpowers/plans/2026-06-12-focused-wizard-tui.md src/comix_dl/core/tui tests/test_tui_screens.py
git commit -m "feat: refresh tui focused wizard flow"
```

Expected: commit succeeds on `feature/focused-wizard-tui`.

## Self-Review

- Spec coverage: Shell navigation, Search, Series, Download, management screens, visual style, architecture boundary, data flow, error handling, and testing all map to tasks above.
- Placeholder scan: No TBD/TODO/fill-in-later language remains.
- Type consistency: `NavigationRail`, `StatusBar`, `DownloadTitle`, `DownloadStatus`, and `SettingsOutput` match existing or planned classes. App helper methods are `set_active_view()` and `set_status()` consistently throughout.
