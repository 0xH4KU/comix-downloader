# Architecture

## Overview

comix-downloader is a desktop-first manga downloader for `comix.to`. It uses a real Chrome instance over CDP to survive Cloudflare, then fetches API metadata and image bytes through that browser session. The current codebase is split across four practical layers:

1. Presentation: `cli/__init__.py`, `cli/interactive.py`, `cli/display.py`
2. Workflow orchestration: `cli/flows.py`
3. Domain/service logic: `comix_service.py`, `downloader.py`, `converters.py`
4. Infrastructure: `browser_session.py`, `cdp_browser.py`, `settings.py`, `history.py`, `fileio.py`, `notify.py`, `errors.py`

This is the real structure today, not the target end-state. There is still no dedicated application layer, and `cli/flows.py` remains the main orchestration hotspot.

At process start, the CLI loads persisted settings once, builds a per-run `AppConfig`, and passes that config explicitly into the browser, service, downloader, and converter stack. Runtime behavior no longer depends on mutating a process-global config singleton.

## Runtime Topology

```text
User
  |
  v
cli/__init__.py
  |
  +--> cli/interactive.py
  +--> cli/display.py
  +--> cli/flows.py
           |
           +--> comix_service.py
           +--> downloader.py
           +--> converters.py
           +--> history.py
           +--> notify.py
           |
           +--> cdp_browser.py
                    |
                    v
             browser_session.py
                    |
                    v
           Chrome subprocess + Playwright CDP
                    |
                    v
                 comix.to
```

## Browser Stack

### `browser_session.py`

`BrowserSessionManager` owns Chrome lifecycle and pooled browser resources:

- Launches Chrome with `--remote-debugging-port`
- Applies a single-instance lock file under the config directory
- Connects Playwright over CDP
- Owns the main page plus the pooled download pages
- Applies timeout boundaries to connect, page creation, navigation, and `page.evaluate()`
- Replaces dead pooled pages instead of re-queuing broken objects
- Cleans up only the Chrome started by the current Python process

This separation matters because lifecycle logic is stateful and failure-prone. Keeping it isolated reduces the blast radius when changing Cloudflare handling or request logic.

### `cdp_browser.py`

`CdpBrowser` now sits above `BrowserSessionManager` and focuses on Cloudflare-aware request flow:

- Ensures clearance before browser-backed API/image requests
- Detects renewed challenges and HTTP 403 responses
- Resets cached clearance once and retries once
- Fetches bytes/JSON via `page.evaluate(fetch())`
- Keeps Cloudflare heuristics separate from Chrome startup and shutdown

This layering makes the browser subsystem testable in two slices:

- Session tests: locks, page pool, dead-page replacement, timeout wiring
- Cloudflare/request tests: challenge detection, retry behavior, request orchestration

## Service and Download Layer

### `comix_service.py`

The service client talks to the `comix.to` v2 REST API and normalizes chapter metadata:

- Search and series detail lookup use `hash_id`, not slug
- Chapter image lookup uses `chapter_id`
- Chapter numbers are preserved as normalized strings and sorted via a dedicated natural-sort key instead of `float`
- Deduplication keeps language variants distinct
- Same-language duplicates compete on `image_count`
- Deduplication now emits a `DedupDecision` report so the CLI can show which variants were dropped and why

### `downloader.py`

`Downloader` is responsible for safe image persistence and resumable chapter state:

- Image bytes are fetched through `CdpBrowser.get_bytes()`
- Per-image concurrency is limited by `download.max_concurrent_images`
- Existing chapter files are indexed once up front for O(1) resume checks
- Existing files are validated by magic bytes before reuse
- Image writes are atomic via temp files and `os.replace()`
- Partial/failed chapters write `chapter.state.json`
- Only fully successful chapters get a `.complete` marker

### `converters.py`

`converters.py` packages only complete chapter directories into user-facing archives:

- CBZ output is a direct stored archive of the validated image set
- Large PDF output is rendered in batches to cap memory use
- Multi-batch PDF merge uses the bundled `pypdf` runtime dependency by default
- `pikepdf` remains an optional faster backend when present
- Missing merge support is treated as a hard failure instead of producing a truncated PDF

### `errors.py`

Core workflow failures now have explicit domain error types instead of relying on generic `RuntimeError`:

- `ConfigurationError`
- `CloudflareChallengeError`
- `RemoteApiError`
- `PartialDownloadError`
- `ConversionError`

This keeps orchestration code readable and makes future application-layer extraction less dependent on fragile string matching.

## Download State Model

The downloader now has an explicit result model instead of inferring success from scattered counters.

### `ChapterDownloadResult`

Each chapter ends in exactly one of four states:

- `complete`
- `partial`
- `failed`
- `skipped`

The result carries:

- total pages
- downloaded pages
- skipped pages
- failed pages
- failed filenames

That result is the contract used by the orchestration layer to decide what is safe to do next.

### Recovery Artifacts

`chapter.state.json` records the last known partial state:

- timestamp
- title / chapter label
- final chapter status
- counts for downloaded, skipped, and failed pages
- failed page filename, source URL, and last error

This file is the source of truth for interrupted or degraded runs. It prevents the old failure mode where a chapter looked successful simply because some files existed on disk.

## Workflow Orchestration

### `cli/flows.py`

`cli/flows.py` is currently the orchestration center. It still does too much:

- browser/session creation
- service calls
- chapter download coordination
- conversion
- history recording
- notifications
- cleanup prompts
- Rich progress rendering
- dedup decision presentation

This is the main architecture debt left in the project. The code works, but maintenance cost remains high because presentation concerns and business workflow are still tangled together.

## Persistence

### `settings.py`

Settings are stored in `~/.config/comix-dl/settings.json` and written atomically. `SettingsRepository` owns load/save/default fallback behavior, schema-version handling, and value normalization. It also builds a per-run `AppConfig` from persisted settings so runtime components can receive configuration by constructor injection instead of reading hidden global state.

Only active user-facing controls remain wired here:

- output directory
- default format
- chapter concurrency
- image concurrency
- retry count
- rate-limit delay toggle
- image optimization toggle

### `history.py`

Download history is stored in `~/.config/comix-dl/history.json` and written atomically. `HistoryRepository` owns load, sort, trim, append, and clear behavior. Entries record:

- title
- chapter count
- output format
- total bytes
- counts for completed / partial / failed / skipped chapters

History records only the final workflow summary, not raw per-image diagnostics.

### `fileio.py`

`fileio.py` provides the atomic write primitives used by settings, history, and partial chapter state files. That consolidation is important because corruption prevention is an infrastructure concern, not a per-feature detail.

## Data Flow

```text
Search
  settings.json
    -> SettingsRepository.load()
    -> SettingsRepository.build_runtime_config()
  user query
    -> comix_service.search()
    -> SearchResult list
    -> user selection

Download
  settings.json
    -> SettingsRepository.load()
    -> SettingsRepository.build_runtime_config()
  selected series
    -> comix_service.get_chapters()
    -> cli/flows.py schedules chapter tasks
    -> downloader.download_chapter()
    -> ChapterDownloadResult
    -> complete only: converters.convert()
    -> workflow summary
    -> history.record_download()
    -> notify.send_notification()

Resume / Recovery
  chapter dir
    -> .complete present -> skip safely
    -> chapter.state.json present -> inspect partial state
    -> existing files -> validate magic bytes
    -> missing/corrupt pages -> re-download
```

## Availability Boundaries

The current implementation has several explicit high-availability boundaries:

- Single-instance Chrome profile lock prevents cross-process profile corruption
- Page pool size is bounded and tied to configured image concurrency
- Browser operations fail with explicit timeouts instead of hanging forever
- Cloudflare expiry is retried once through a clearance reset path
- Dead pooled pages are evicted and replaced
- Atomic writes prevent half-written settings, history, and image files from being treated as valid state
- High-risk failure modes now emit targeted diagnostics instead of generic transport errors

These boundaries are the difference between a recoverable run and silent damage.

## Known Debt

The following debts remain real and are intentionally documented here:

- `cli/flows.py` still mixes orchestration, UI, and infrastructure calls
- Runtime config is still threaded manually through `cli/flows.py` instead of dedicated application/use-case boundaries
- CLI still renders several failures with generic text instead of a single centralized error presenter
- Overall test coverage is still below the desired long-term threshold

The point of this document is to describe the current system honestly so the next refactor slices have a stable reference point.
