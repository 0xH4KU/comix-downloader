# Clickable TUI Navigation And Log Drawer Redesign

## Goal

Refine the new Textual TUI so the sidebar behaves like real navigation and runtime messages no longer crowd the main task area.

The current TUI shows a left sidebar that looks clickable, but it is rendered as static text. It also allows status and log-like messages to compete with the Search input area. This pass makes the visible UI contract explicit: every visible sidebar destination should be actionable, and operational messages should live in a dedicated status/log surface.

## Scope

This design builds on the focused wizard TUI and updates only the `comix_dl.core.tui` package.

In scope:

- Replace static sidebar text with clickable navigation items.
- Hide workflow destinations until their backing state exists.
- Remove numeric `1/2/3` prefixes from workflow labels.
- Keep Library, History, and Settings always visible and clickable.
- Add a status/log drawer above the Textual Footer.
- Move search, load, download, cleanup, and error messages into the status/log surface.
- Keep the existing controller/application boundary and fake-controller Textual tests.

Out of scope:

- Replacing the CLI or prompt-based flows.
- Adding image previews.
- Adding persistent log storage.
- Reworking download or search use cases.
- Building a full dashboard or multi-pane operator console.

## Design Direction

Use a state-aware app navigation rail.

The sidebar has two sections:

```text
WORKFLOW
Search
Chapters       # only visible after a series is loaded
Download       # only visible after a download request exists or while viewing download progress

TOOLS
Library
History
Settings
```

The numeric workflow prefixes are removed. The sidebar is not a tutorial step list; it is a list of destinations that can currently be opened.

Every visible destination must be actionable with mouse and keyboard. If a screen cannot be opened because it lacks required state, it should not be visible in the sidebar.

## Navigation Behavior

### Search

Search is always visible.

Clicking Search:

- Opens the Search screen.
- Marks Search active.
- Leaves any loaded series state available if the app later wants to return to Chapters, but Search itself remains the primary new-task entry point.

### Chapters

Chapters is hidden until a series is loaded.

Clicking Chapters after it appears:

- Reopens the current series chapter-selection screen.
- Marks Chapters active.
- Preserves the current chapter filter, selection, and format when navigating away to Library, History, Settings, or Search and then back to Chapters.

The app should treat this as owned navigation state, not incidental widget state. Implementation can either keep a reusable pane model or store the loaded `SeriesInfo` plus chapter-selection state outside the widget, but the user-visible behavior is that sidebar navigation does not wipe the current chapter-selection work.

### Download

Download is hidden until there is a current download request or active download pane.

Clicking Download:

- Returns to the current download progress/completion screen while the request is still relevant.
- Marks Download active.

Download navigation is limited to the currently mounted or most recent download pane for this TUI session. It does not support historical download sessions.

### Tools

Library, History, and Settings are always visible and clickable.

They are secondary destinations and do not depend on workflow state. Opening them should not clear the loaded series or current workflow state unless implementation later proves that preserving state causes confusion.

## Status And Log Drawer

The app keeps Textual's Footer as the bottom-most shortcut hint line.

Above the Footer, add a dedicated status/log drawer:

- Collapsed height: one line.
- Expanded height: 3-5 recent log lines.
- It never overlaps or replaces the Footer.
- It never appears next to the Manga title input.
- It shows concise operational messages for the current session.

Recommended toggle key: `o` for output/log output. Avoid `l` because it reads naturally as Library.

The collapsed line should show the latest meaningful status:

- `Ready to search`
- `Searching for "naruto"`
- `3 results found`
- `Series loaded: One Piece`
- `Select at least one chapter before downloading`
- `Downloading 8 chapters`
- `Download complete`
- `Cleanup removed 3 raw folders`

The expanded drawer should show the recent message history in newest-last order, capped to a small number of lines.

## Screen Design

### Search

The Search screen should focus only on finding manga:

- Title: `Search`
- Helper text: `Type a manga name, then press Enter.`
- Search input near the top.
- Result table below.

Search progress, empty-query guidance, empty results, failed searches, and result counts move to the status/log drawer. The Search input should not be crowded by log text.

Selecting a result loads the series and reveals the Chapters destination in the sidebar.

### Chapters

The Chapters screen appears after a series is loaded:

- Title: current manga title.
- Metadata line with author, selected genres, and chapter count.
- Selection summary, such as `0 selected from 48 visible chapters`.
- Filter input with `+term` and `-term` guidance.
- Format selector and Download button on the same control row.
- Chapter table as the main content.

No-selection download attempts should update the status/log drawer with a clear recovery message.

Starting a valid download reveals the Download destination and opens the Download screen.

### Download

The Download screen focuses on batch progress:

- Title: `Download`.
- Batch summary with manga title, chapter count, output format, and current state.
- Per-chapter table with title, status, progress, and detail.
- Cancel while running.
- Cleanup after completion when available.

Completion, cancellation, failure, partial-failure, and cleanup messages should update the status/log drawer. The main screen keeps structured progress and action controls.

The Download destination represents only the current or most recent download task from this TUI session. It is not a historical download-session browser.

## Architecture

Keep the existing boundaries:

- `app.py` owns the shell, navigation state, status/log drawer, current loaded series, and current download destination state.
- `screens/search.py` owns search input, search results, and result selection.
- `screens/series.py` owns chapter filtering, selection, format, and download request creation.
- `screens/download.py` owns live download progress, cancellation, completion, and cleanup actions.
- `screens/manage.py` owns Library, History, and Settings views.
- `state.py` continues to own pure state helpers.

Navigation should be implemented through explicit app methods rather than screen modules directly manipulating unrelated widgets. The implementation must provide a small shell API for responsibilities like:

- `set_active_view(...)`
- `set_status(...)`
- `append_log(...)`
- `set_loaded_series(...)`
- `set_current_download(...)`

The implementation plan can choose exact method names, but these responsibilities should remain explicit instead of being hidden in ad hoc widget queries.

## Error Handling

Errors stay recoverable:

- Search errors keep the query and existing result area available.
- Series load errors keep the previous Search screen and result table when possible.
- Empty filters preserve the prior visible chapter list and log the message.
- Download errors stop the running state, disable Cancel, and log a top-level failure.
- Cleanup errors report removed and failed counts in plain language.

The status/log drawer should not become a stack trace dump. User-facing text stays short and actionable.

## Testing

Add or update Textual smoke tests for:

- Sidebar renders section labels and omits numeric workflow prefixes.
- Only Search plus tools are visible on first mount.
- Sidebar visible items are clickable.
- Chapters appears only after loading a series.
- Download appears only after starting a download.
- Opening Library/History/Settings does not lose available workflow destinations.
- Status/log drawer renders the latest status above Footer.
- Log drawer toggles expanded/collapsed state without hiding the Footer.
- Search status messages move to the drawer.
- No-selection download validation updates the drawer.
- Download completion and cleanup messages update the drawer.

Pure state tests should remain focused on filtering, selection, and download row reducers. Boundary tests should continue to assert that TUI modules do not import prompt-based CLI flows.

## Acceptance Criteria

- The sidebar is clickable.
- The sidebar never displays unavailable workflow destinations.
- `Search`, `Chapters`, and `Download` have no `1/2/3` prefixes.
- Library, History, and Settings remain always visible and clickable.
- Runtime status/log text is separated from the Search input and main task controls.
- The status/log drawer sits above the Footer and does not conflict with shortcut hints.
- The TUI remains keyboard-friendly and mouse-friendly.
- Existing application/session/download boundaries remain intact.
- Relevant TUI tests pass.
