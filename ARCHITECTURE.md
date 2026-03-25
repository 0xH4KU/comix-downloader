# Architecture

## Overview

comix-downloader is a desktop-first manga downloader for `comix.to`. It uses a real Chrome instance over CDP to survive Cloudflare, then fetches API metadata and image bytes through that browser session. The current codebase is split across four practical layers:

1. Presentation: `cli/__init__.py`, `cli/interactive.py`, `cli/display.py`
2. Application use cases: `application/query_usecase.py`, `application/download_usecase.py`, `application/cleanup_usecase.py`, `application/download_reporting.py`, `application/session.py`
3. Workflow/presentation glue: `cli/flows.py`
4. Domain/service logic and infrastructure: `comix_service.py`, `downloader.py`, `converters.py`, `browser_session.py`, `cdp_browser.py`, `settings.py`, `history.py`, `fileio.py`, `notify.py`, `errors.py`, `logging_utils.py`

This is the real structure today, not an aspirational diagram. The application layer now owns query/download/cleanup orchestration plus runtime/session wiring, while `cli/flows.py` has become a thinner presentation-oriented adapter. The remaining debt is that interactive control flow and Rich rendering are still coupled in that adapter.

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
           +--> application/query_usecase.py
           +--> application/download_usecase.py
           +--> application/cleanup_usecase.py
           +--> application/session.py
            |
            +--> comix_service.py
            +--> downloader.py
           +--> converters.py
           +--> history.py
           +--> notify.py
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
- Prefers live challenge signals over stale `cf_clearance` cookies when deciding whether manual solve is needed
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
- The batch size is explicitly bounded by `convert.pdf_batch_size`
- Large-PDF temp artifacts live inside one isolated temporary workspace that is removed after merge or failure
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

### `application/query_usecase.py`

The query use case isolates series lookup rules that were previously duplicated in CLI flows:

- normalize URL or slug input into a canonical slug
- run search queries
- load a series by `hash_id`
- resolve slug input through direct lookup first, then search fallback

This gives the CLI a single lookup contract instead of open-coded fallback logic.

### `application/download_usecase.py`

The download use case owns the batch chapter workflow:

- bounded concurrent chapter scheduling
- per-chapter progress event emission for the presentation layer
- conversion gating so partial chapters never package
- final summary aggregation
- history recording
- completion notification

The key boundary is the event callback. The use case no longer needs Rich progress objects, but the CLI can still render a detailed progress view.

### `application/download_reporting.py`

Download reporting now has a dedicated formatting layer built on top of the canonical `DownloadSummary` result:

- stable count ordering for summary text
- shared byte-size formatting
- normalized issue lines
- a notification body derived from the same summary data

This matters because CLI panels, persisted history, and desktop notifications no longer drift independently when result wording changes.

### `application/cleanup_usecase.py`

Cleanup planning is now separated from the CLI:

- list downloaded series summaries
- detect cleanup-safe raw image directories
- compute aggregate reclaimable bytes
- apply deletion plans and report failures

This keeps filesystem scanning and deletion rules out of presentation code.

### `application/session.py`

Runtime/session setup for CLI commands is now centralized here:

- load normalized settings and runtime config
- resolve the effective output directory
- open the browser/session boundary
- build the `ComixService`
- expose a small browser-backed session object to the CLI layer

That removes browser/service/bootstrap code from command-dispatch paths and keeps `cli/__init__.py` focused on parsing and routing.

### `cli/flows.py`

`cli/flows.py` is no longer the core orchestration center, but it still owns interactive flow control and Rich rendering:

- Rich progress rendering
- prompt loops and selection parsing
- search result / metadata / dedup presentation
- cleanup confirmation prompts
- CLI-boundary rendering for `RemoteApiError`, so API failures are surfaced directly instead of being flattened into empty-state UI

That is materially better than before, but not the final end-state. The CLI adapter still mixes interaction policy with rendering.

## Persistence

### `settings.py`

Settings are stored in `~/.config/comix-dl/settings.json` and written atomically. `SettingsRepository` owns load/save/default fallback behavior, schema-version handling, and value normalization. It also builds a per-run `AppConfig` from persisted settings so runtime components can receive configuration by constructor injection instead of reading hidden global state.

Only active user-facing controls remain wired here:

- output directory
- default format
- concurrency profile (`desktop`, `low_resource`, `ci`, `custom`)
- chapter concurrency (for `custom` profile)
- image concurrency (for `custom` profile)
- retry count
- rate-limit delay toggle (for `custom` profile)
- image optimization toggle

The profile mechanism is important because it turns environment-sensitive tuning into data instead of ad hoc code changes. Legacy settings with non-default concurrency values are migrated to `custom` so existing behavior is preserved.

### `history.py`

Download history is stored in `~/.config/comix-dl/history.json` and written atomically. `HistoryRepository` owns load, sort, trim, append, and clear behavior. Entries record:

- title
- chapter count
- output format
- total bytes
- counts for completed / partial / failed / skipped chapters

History records only the final workflow summary, not raw per-image diagnostics.
They now also persist normalized summary text and chapter-level issue lines derived from the shared download report.

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

## Observability

`logging_utils.py` now installs a structured formatter at CLI startup. High-value download-path logs emit stable JSON context fields instead of burying identifiers inside free-form strings.

Current structured fields include at least:

- `series`
- `chapter_id`
- `chapter_title`
- `retry_count`
- `status`
- `bytes`
- `elapsed`

This is intentionally lightweight: it keeps the standard library logging stack, but makes downstream filtering and debugging materially easier.

## Release Docs

Two repository-level documents now carry the operational context that does not belong inside module docs:

- `MIGRATION.md` explains what changed for maintainers moving from the old global-config, partial-success, monolithic-CLI design to the current layered runtime
- `RELEASE_CHECKLIST.md` is the source of truth for versioned slice release steps, validation order, and closeout checks

## Known Debt

The following debts remain real and are intentionally documented here:

- `application/download_usecase.py` still talks to history and notification infrastructure directly instead of going through abstract ports
- CLI still renders several failures with generic text instead of a single centralized error presenter
- `browser_session.py`, `cli/interactive.py`, and `notify.py` remain the main low-coverage areas after the 70% gate raise

The point of this document is to describe the current system honestly so the next refactor slices have a stable reference point.
