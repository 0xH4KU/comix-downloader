# Focused Wizard TUI Redesign

## Goal

Make the new Textual TUI feel simple, clear, and friendly for first-time users while preserving the existing application boundary and tested TUI controller.

The redesign combines a practical UX pass with a light shell refresh. It should improve the whole journey, not only repaint individual widgets:

- Search for a manga.
- Pick a result.
- Review the selected series.
- Filter and select chapters.
- Choose the output format.
- Download chapters with understandable progress and completion actions.
- Visit Library, History, and Settings without feeling lost.

## Scope

This work updates the existing `comix_dl.core.tui` package. It keeps `ComixTuiApp`, `TuiController`, and the current Search, Series, Download, and management panes, but changes their layout, copy, state presentation, and shell navigation.

The target design direction is a focused wizard:

- One main task per screen.
- Stronger titles and short helper text.
- Obvious next-step status messages.
- Fewer controls competing for attention at the same time.
- Simple visual grouping with restrained borders, spacing, and semantic colors.
- Keyboard-first operation with visible but concise shortcut hints.

## Non-Goals

- Do not replace the existing CLI or change scriptable command behavior.
- Do not rewrite the application/session/download use cases.
- Do not add a new persistence model.
- Do not introduce a complex dashboard layout or dense operator console.
- Do not add image/page previews.
- Do not require a live network session for TUI smoke tests.

## Product Principles

The TUI should feel like a guided app, not a collection of raw tables.

Each screen should answer three questions without the user reading documentation:

1. Where am I?
2. What can I do here?
3. What should I do next?

The interface should prefer clear status text over cleverness. It is acceptable for the TUI to be a little more verbal than a power-user console because the chosen direction is beginner-friendly.

## App Shell

Replace the current plain sidebar text with a lightweight flow navigation:

- `1 Search`
- `2 Chapters`
- `3 Download`
- `Library`
- `History`
- `Settings`

The active destination should be visually distinct. Flow steps that are not currently available should remain visible but read as inactive or contextual, so the user can understand the shape of the workflow without assuming every step is clickable at all times.

The shell should keep a persistent status strip, but the status content should be more useful than `Ready`. It should summarize the current session state, such as:

- `Ready to search`
- `3 results found`
- `Series loaded: One Piece`
- `Downloading 8 chapters`
- `Download complete`

The footer should keep keyboard bindings visible, but the main screens should also include short helper text near the relevant task so first-time users do not need to decode the footer.

## Search Screen

The Search screen should be a calm first step.

Layout:

- Title: `Search`
- Helper text: short explanation that the user can type a manga name and press Enter.
- Search input.
- Status line for loading, empty, error, and result count states.
- Result table.

Behavior:

- Empty submission shows a friendly prompt instead of a terse validation error.
- During search, the input stays visible and the status says what is happening.
- No-result and failed-search states keep the query intact.
- When results exist, the status explicitly says that Enter opens the selected row.
- Opening a result should update the shell state so the user sees they moved from Search to Chapters.

## Series And Chapter Selection Screen

The Series screen becomes the guided chapter selection step.

Layout:

- Title: the series title.
- Short metadata summary with author, genre count or selected genres, and chapter count.
- Selection summary, such as `0 selected from 48 visible chapters`.
- Filter input with helper text for `+term` and `-term`.
- Format selector near the download action.
- Chapter table.
- Status/help line with the common actions: Space selects a row, `A` selects visible rows, `X` clears visible rows, `D` starts download.

Behavior:

- The table remains keyboard-first and keeps the current selection model.
- Filtering should update the visible chapter count and selection summary.
- If a filter matches nothing, preserve the existing recovery behavior but improve the message.
- Starting a download with no selected chapters should show a friendly, actionable message.
- Starting a valid download should move the shell state to Download.

## Download Screen

The Download screen should show batch progress first and row details second.

Layout:

- Title: `Download`
- Batch summary that includes series title, selected chapter count, and current overall state.
- A compact status/progress summary above the table.
- Per-chapter table with title, status, progress, and detail.
- Action row for Cancel while running and Cleanup after completion when available.

Behavior:

- Running state should make it clear that work is active.
- Cancellation should acknowledge the request and explain that active chapter work is stopping gracefully.
- Completion should give the user a next step: cleanup raw folders, return to Search, or inspect Library/History.
- Partial failures and conversion failures should be reflected in the top-level summary, not only hidden in table rows.
- Cleanup success and failure should use plain language and disable the cleanup action after use.

## Management Screens

Library, History, and Settings are secondary screens. They should look consistent with the guided shell without pretending to be primary workflow steps.

Library:

- Rename the Downloads pane label to Library in navigation and title copy.
- Show empty state text when no downloads are present.
- Keep the table for existing downloads.

History:

- Show empty state text when no history exists.
- Keep the table focused on recent entries.

Settings:

- Present settings as readable rows with labels.
- Include a note that changes requiring a new session take effect next time if editing remains out of scope for this pass.

## Visual Style

Use a simple, terminal-native visual system:

- Slightly stronger page titles.
- Muted helper text below titles.
- Bordered panels only where they clarify grouping.
- A clear active nav state.
- Semantic colors for success, warning, error, and muted text.
- Stable dimensions for sidebar, status line, input rows, and tables.

Avoid decorative gradients, complex cards, or dashboard-style metric walls. The result should feel clean and legible rather than flashy.

## Architecture

Keep the existing package boundary:

- `app.py` owns the shell, navigation, and shared session status.
- `screens/search.py` owns search task UI and result selection.
- `screens/series.py` owns series metadata, chapter filtering, selection, and download request creation.
- `screens/download.py` owns live download state display, cancellation, completion, and cleanup.
- `screens/manage.py` owns Library, History, and Settings summaries.
- `state.py` continues to own pure filtering, selection, request, and download row state helpers.

Add small presentation helpers only when they reduce duplication across screens. Prefer local helper methods over a broad new component system unless the duplication is already visible in multiple panes.

The TUI must continue to call the controller/application layer directly. It must not call prompt-based CLI flows, Rich prompt helpers, or CLI progress renderers.

## Data Flow

1. App starts and opens the controller session.
2. Shell status becomes `Ready to search`.
3. Search submits a query through `controller.search()`.
4. Search results update the result table and shell status.
5. Row selection calls `controller.load_series()`.
6. Series screen initializes chapter selection state and marks the Chapters step active.
7. Download request is built from selected chapters and chosen format.
8. Download screen marks the Download step active and calls `controller.download()`.
9. Download events update pure row state and the visible batch summary.
10. Completion checks cleanup availability and updates next-step actions.

## Error Handling

Errors should stay on the screen where the user can recover:

- Search errors keep the query and result area available.
- Series load errors keep the previous search results visible when possible.
- Filter errors or empty matches explain what happened and keep the previous visible chapters.
- Download exceptions stop the running state, disable Cancel, and show a top-level failure message.
- Cleanup failures report how many folders were removed and how many failed.

Messages should be short, concrete, and actionable.

## Testing

Update or add tests at the existing levels:

- Pure state tests remain focused on filtering, selection, and download event reduction.
- Textual smoke tests cover shell active navigation, friendly empty/error/status text, search result navigation, chapter selection, no-selection download validation, download completion, and cleanup action state.
- Boundary tests continue to prove that the TUI does not import prompt-based CLI flow modules.

The test suite should use fake controllers and Textual `run_test()` as it does now, without opening Chrome or performing live network work.

## Acceptance Criteria

- The TUI shell shows a clear flow navigation with active state.
- Search, Chapters, Download, Library, History, and Settings use consistent titles, helper text, and status messages.
- Search clearly guides the user from query to selected result.
- Chapter selection clearly communicates filtering, selected count, format, and download readiness.
- Download progress includes a top-level batch summary and clear completion/cancellation/cleanup messaging.
- Library and History include useful empty states.
- Existing CLI behavior remains unchanged.
- Existing TUI controller/application boundaries remain intact.
- Relevant pytest coverage passes for TUI state, screens, controller boundaries, and CLI dispatch.
