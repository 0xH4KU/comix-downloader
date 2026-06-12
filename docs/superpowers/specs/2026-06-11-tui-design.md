# Full-Screen TUI Design

## Goal

Add an optional full-screen terminal UI for comix-downloader that feels like a native interactive app while preserving the existing scriptable CLI.

## Scope

This design adds `comix-dl tui` as a new entry point. Existing commands such as `comix-dl search`, `comix-dl download`, `comix-dl info`, `comix-dl list`, `comix-dl clean`, `comix-dl history`, `comix-dl doctor`, and `comix-dl settings` remain compatible.

The first TUI release focuses on the main user journey:

- Search for a series.
- Inspect search results and selected series metadata.
- Filter and multi-select chapters.
- Choose output format.
- Download with live per-chapter progress.
- Review completion, partial failures, and cleanup options.

Secondary panes for downloads, history, and settings should be included only where they can reuse existing repositories and use cases directly. Deep diagnostics can remain in the CLI until it can be represented as first-class TUI state.

## Non-Goals

- Do not replace the plain CLI or make the default `comix-dl` command full-screen.
- Do not render the TUI by calling existing prompt-based CLI flows.
- Do not embed a browser preview of manga pages.
- Do not add a second persistence model for downloads, history, or settings.
- Do not make the TUI depend on a live network session for unit tests.

## Product Standard

The TUI must not feel like a wrapper around the old prompts. The standard is:

- Direct application-layer integration: screens call `ApplicationSession` or thin TUI controllers, not `flow_search()`, `Prompt.ask()`, or Rich progress renderers.
- Stable app state: search results, selected series, chapter selection, active download rows, and errors are stored as TUI state, not inferred from printed output.
- Non-blocking interaction: search, metadata fetch, and download work run in background workers so the interface can repaint, cancel, and show status.
- Keyboard-first operation: common actions have visible bindings and sensible focus behavior.
- Visible feedback: loading, empty, error, Cloudflare/browser readiness, progress, partial, skipped, and converted states are shown in fixed UI regions.
- Recoverable errors: remote API errors, no-result searches, invalid selections, partial downloads, and conversion failures return users to an actionable screen.

## Architecture

Add a new `comix_dl.core.tui` package beside the existing CLI package.

The TUI package owns only presentation state and screen orchestration. It consumes the existing application boundary:

- `open_application_session()` creates the browser-backed session.
- `ApplicationSession.search()` performs search.
- `ApplicationSession.load_series()` loads metadata and chapters.
- `ApplicationSession.download()` runs downloads and emits `DownloadChapterEvent`.
- `list_downloaded_series()`, `build_cleanup_plan()`, and `apply_cleanup_plan()` drive management panes.
- `HistoryRepository` and `SettingsRepository` drive history and settings panes.

The TUI should have a small controller layer between Textual screens and application use cases. This keeps Textual widgets thin and makes behavior testable without running a terminal UI.

## Components

### CLI Entry Point

`comix-dl tui` launches the full-screen app. It accepts the global `--mirror`, `--debug`, and `--quiet` flags where those flags already apply. `--quiet` only suppresses the update notice before the app starts; the TUI itself still shows status inside the UI.

### TUI Controller

The controller opens and closes one `ApplicationSession`, exposes async methods for search, series loading, and downloads, and owns a shutdown flag for cancellation. It has no Textual dependency, which allows fast unit testing.

### TUI State

Pure state helpers manage:

- Chapter filtering with the same `+term` and `-term` semantics users already know.
- Multi-selection, select all, clear selection, and selected chapter extraction.
- Per-chapter download row updates from `DownloadChapterEvent`.
- Download summary formatting for the final panel.

### Textual App Shell

The app shell provides a persistent header, footer, sidebar navigation, and status strip. It opens the controller in the background and shows browser/session readiness without freezing the UI.

### Screens

Search screen:

- Query input.
- Result table.
- Loading/empty/error state.
- Enter opens the selected result.

Series screen:

- Metadata panel.
- Chapter table with number, title, language, image count, and selected marker.
- Filter input.
- Format selector.
- Select all, clear selection, and start download actions.

Download screen:

- Batch summary.
- Per-chapter table.
- Progress bars or percentage text per row.
- Status details for skipped, partial, failed, conversion failed, and converted rows.
- Cancel action that requests graceful shutdown.
- Cleanup action after successful conversions when cleanup candidates exist.

Management panes:

- Downloads pane reads `list_downloaded_series()`.
- History pane reads `HistoryRepository.list_entries()`.
- Settings pane edits `Settings` through `SettingsRepository` and refreshes runtime config on the next session.

## Data Flow

1. App starts from `comix-dl tui`.
2. `run_tui()` lazily imports Textual and constructs `ComixTuiApp`.
3. App opens a `TuiController` worker.
4. Search screen calls `controller.search(query)`.
5. Result selection calls `controller.load_series(result.hash_id)`.
6. Series screen builds a `ChapterSelectionState`.
7. Start download creates a `DownloadRequest` and pushes the download screen.
8. Download screen calls `controller.download(request, on_event=...)`.
9. Each `DownloadChapterEvent` updates `DownloadRowsState`.
10. Completion stores the `DownloadSummary`, shows cleanup options, and leaves the app in a navigable state.

## Error Handling

Remote API errors are caught at screen boundaries and shown as actionable status text. Search failures leave the query intact. Series load failures return to search results. Download failures update affected rows and keep the final summary visible.

Cancellation should request graceful shutdown through the same `is_shutdown` contract the CLI uses. It must not kill Chrome blindly or cancel file writes in the middle of conversion.

If Textual cannot be imported, `comix-dl tui` should print one clear error and return a non-zero exit code. Normal installs include Textual, so this path mostly protects editable or broken environments.

## Testing

Use tests at three levels:

- Pure state tests for filtering, selection, and download event reduction.
- Controller tests with fake async sessions to prove the TUI calls application methods directly.
- Textual smoke tests with fake controllers and `run_test()` to verify key screens mount, accept input, and navigate without opening Chrome.

Add a guard test that no module under `comix_dl.core.tui` imports prompt-based CLI flow modules.

## Documentation

Update README usage with `comix-dl tui`, explain that the TUI is optional, and keep scriptable examples first-class.

Update contributor docs with the new package boundary: CLI owns prompt/Rich output, TUI owns Textual screens, application owns use cases.

## Acceptance Criteria

- `comix-dl tui` launches a full-screen Textual app.
- Search, series load, chapter filter/selection, format choice, and download progress work without calling existing CLI prompt flows.
- Existing CLI commands remain compatible.
- Tests cover pure TUI state, controller behavior, CLI dispatch, and Textual screen smoke behavior.
- The TUI package has no imports from `comix_dl.core.cli.flows`, `comix_dl.core.cli.flow_prompts`, `comix_dl.core.cli.interactive`, or `comix_dl.core.cli.download_progress`.
- Full verification passes with pytest, ruff, and mypy.
