# Architecture

## Overview

comix-downloader is a desktop-first manga downloader for `comix.to`. It uses a real Chrome instance over CDP to survive Cloudflare, then fetches protected API metadata and image bytes through that browser session.

The codebase is split into four practical layers:

1. Presentation: `core/cli/__init__.py`, `core/cli/interactive.py`, `core/cli/display.py`
2. CLI workflow glue: `core/cli/flows.py`
3. Application use cases: `core/application/query_usecase.py`, `core/application/download_usecase.py`, `core/application/cleanup_usecase.py`, `core/application/download_reporting.py`, `core/application/session.py`
4. Site and infrastructure: `sites/`, `core/downloader.py`, `core/converters.py`, `core/engines/`, `core/settings.py`, `core/history.py`, `core/fileio.py`, `core/notify.py`, `core/errors.py`, `core/logging_utils.py`

The important boundary is that `core/` should stay site-agnostic. comix.to-specific API parsing, duplicate rules, Cloudflare probes, and scrambled image rendering live under `sites/`.

## Runtime Topology

```text
User
  |
  v
core/cli/__init__.py
  |
  +--> core/cli/interactive.py
  +--> core/cli/display.py
  +--> core/cli/flows.py
           |
           +--> core/application/query_usecase.py
           +--> core/application/download_usecase.py
           +--> core/application/cleanup_usecase.py
           +--> core/application/session.py
                    |
                    +--> sites registry
                    +--> sites/comix_to.py facade
                    |      +--> comix_to_api.py
                    |      +--> comix_to_parsing.py
                    |      +--> comix_to_dedup.py
                    |      +--> comix_to_url.py
                    |      +--> comix_to_browser.py
                    |
                    +--> core/downloader.py
                    +--> core/converters.py
                    +--> core/history.py
                    +--> core/notify.py
                    +--> core/engines/cdp_browser.py
                            |
                            v
                     core/engines/browser_session.py
                            |
                            v
                   Chrome subprocess + Playwright CDP
                            |
                            v
                         comix.to
```

## Browser Stack

### `core/engines/browser_session.py`

`BrowserSessionManager` owns Chrome lifecycle and pooled browser resources:

- launches Chrome with `--remote-debugging-port`
- applies a single-instance lock file under the config directory
- connects Playwright over CDP
- owns the main page plus lazily-created pooled download pages
- applies timeout boundaries to connect, page creation, navigation, and `page.evaluate()`
- replaces dead pooled pages instead of re-queuing broken objects
- cleans up only the Chrome started by the current Python process

### `core/engines/cdp_browser.py`

`CdpBrowser` layers Cloudflare-aware request flow on top of `BrowserSessionManager`:

- ensures clearance before browser-backed API/image requests
- detects renewed challenges and HTTP 403 responses
- resets cached clearance once and retries once
- fetches bytes/JSON via `page.evaluate(fetch())`
- exposes hook registration for site-specific request transformers, service probes, and scrambled image renderers

`CdpBrowser` does not hardcode comix.to API paths or image-rendering JavaScript. Those are installed by the active site adapter during application-session setup.

## Site Adapter Stack

### `sites/base.py`

`Engine` is the transport protocol consumed by adapters. `SiteAdapter` is the framework contract for search, series lookup, chapter images, lifecycle hooks, mirror probing, and deduplication.

Lifecycle has two phases:

- `configure_engine(engine)` runs before Cloudflare clearance and registers browser hooks such as service probes and scrambled-image renderers.
- `on_engine_ready(engine)` runs after clearance and page-pool warm-up, before caller-visible requests. comix.to uses this to install the packaged JavaScript API client hook.

### `sites/comix_to.py`

`ComixToAdapter` is now a facade. It wires the framework contract to focused helper modules and registers a singleton with the site registry. Each application session calls `new_session()` so per-run caches stay isolated.

### `sites/comix_to_api.py`

`ComixToApiClient` owns comix.to request orchestration:

- search API calls
- direct series lookup and slug fallback
- chapter pagination
- image-count prefetch for duplicate groups
- chapter detail payload cache
- user-facing `RemoteApiError` wrapping

The cache is session-scoped through the adapter instance returned by `new_session()`.

### `sites/comix_to_parsing.py`

Pure parsing helpers normalize raw JSON into framework models:

- search results
- chapter list items
- chapter image pages
- taxonomy/person names
- API status coercion and response unwrapping

### `sites/comix_to_dedup.py`

Pure deduplication rules collapse duplicate chapter variants by number, language, subtitle, and page count. It also builds `DedupDecision` records so the CLI can show what was kept and dropped.

### `sites/comix_to_browser.py`

comix.to-specific browser hooks live here:

- service-access probe for Cloudflare clearance validation
- scrambled image canvas renderer
- cache-bust retry for decode failures

This keeps `CdpBrowser` reusable for future adapters.

## Download State Model

`Downloader` is responsible for safe image persistence and resumable chapter state:

- image bytes are fetched through `Engine.get_bytes()` or `Engine.get_scrambled_image_bytes()`
- per-image concurrency is limited by `download.max_concurrent_images`
- existing chapter files are indexed once up front for resume checks
- existing files are validated before reuse
- image writes are atomic
- partial/failed chapters write `chapter.state.json`
- only fully successful chapters get a `.complete` marker

`ChapterDownloadResult` ends in exactly one of four states: `complete`, `partial`, `failed`, or `skipped`.

## Workflow Orchestration

### `core/application/query_usecase.py`

The query use case isolates lookup rules:

- normalize URL or slug input into an adapter identifier
- run search queries
- load a series by canonical identifier
- resolve slug input through direct lookup first, then search fallback

### `core/application/download_usecase.py`

The download use case owns batch chapter orchestration:

- bounded concurrent chapter scheduling
- per-chapter progress event emission
- conversion gating so partial chapters never package
- final summary aggregation
- history recording
- completion notification

The presentation boundary is the event callback. The use case does not know about Rich progress objects.

### `core/application/download_reporting.py`

Download reporting centralizes count ordering, byte formatting, issue preview lines, and notification body construction so CLI panels, history, and desktop notifications do not drift.

### `core/application/cleanup_usecase.py`

Cleanup planning is separated from CLI rendering:

- list downloaded series summaries
- detect cleanup-safe raw image directories
- compute reclaimable bytes
- apply deletion plans and report failures

### `core/application/session.py`

Runtime/session setup is centralized:

- load normalized settings and runtime config
- select the active mirror
- create a session-scoped adapter
- open `CdpBrowser`
- let the adapter configure pre-clearance hooks
- register the post-clearance adapter setup/probe hook
- expose a small `ApplicationSession` to CLI flows

## Persistence

Settings are stored in `~/.config/comix-dl/settings.json`; history is stored in `~/.config/comix-dl/history.json`; mirror state is stored in `~/.config/comix-dl/mirror_state.json`. These stores use atomic writes through `core/fileio.py`.

## Data Flow

```text
Search
  settings.json
    -> SettingsRepository.load()
    -> build_runtime_config()
  query
    -> ApplicationSession.search()
    -> SiteAdapter.search()
    -> ComixToApiClient.search()
    -> SearchResult list

Download
  selected series
    -> ApplicationSession.resolve_series()
    -> SiteAdapter.get_series()
    -> ComixToApiClient.get_series()
    -> selected chapters
    -> download_usecase.download_chapters()
    -> Downloader.download_chapter()
    -> converters.convert()
    -> DownloadSummary
    -> history.record_download()
    -> notify.send_notification()

Resume / Recovery
  chapter dir
    -> .complete present -> skip safely
    -> chapter.state.json present -> inspect partial state
    -> existing files -> validate image bytes
    -> missing/corrupt pages -> re-download
```

## Known Debt

- `core/application/download_usecase.py` still talks to history and notification infrastructure directly instead of using ports for both.
- `core/cli/flows.py` still mixes prompt policy and Rich rendering.
- `ComixToAdapter` keeps compatibility wrappers for old private helper methods; these can be removed in a future major cleanup once forks have moved to the focused helper modules.
- Automatic mirror switching after a failed probe is still deferred; the current run continues with the selected mirror and records the outcome.

The purpose of this document is to describe the current system honestly, not to preserve old plans.
