# Changelog

## 0.4.4 - 2026-06-12

- Added an optional full-screen Textual TUI via `comix-dl tui`, including guided search, chapter filtering and selection, live download progress, clickable navigation, a status log drawer, library/history/settings panes, and cleanup actions.
- Hardened TUI behavior around cancellation, stale series loads, navigation shortcuts, search focus, download navigation preservation, and controller lifecycle cleanup.
- Refactored comix.to scrambled image handling to prefer reader-DOM capture over private renderer calls, while keeping the legacy renderer as a fallback.
- Reused the hydrated reader DOM for multiple scrambled pages in the same chapter to reduce repeated reader navigation.
- Added reader metadata for scrambled pages, deeper doctor/live-smoke coverage, TUI boundary tests, packaging checks for the TUI stylesheet, and expanded browser renderer regression coverage.
- Documented the TUI, DOM-first scrambled capture, and current CLI/TUI/application boundaries.

## 0.4.3 - 2026-06-07

- Previous stable release.
