# Architecture

## Overview

comix-downloader uses a Chrome CDP connection to bypass Cloudflare protection on comix.to, then interacts with the site's REST API v2 to search, list chapters, and fetch image URLs. `browser_session.py` owns Chrome lifecycle, lock handling, timeouts, and pooled pages; `cdp_browser.py` layers Cloudflare detection and request retry logic on top. Images are downloaded concurrently via that page pool and packaged into PDF or CBZ.

## Component Diagram

```
                    User
                      |
                 [cli.py] ---- argparse + Interactive menu
                      |
         +-----------+-----------+-----------+
         |           |           |           |
   [settings.py] [comix_service] [converters.py] [history.py]
         |           |           |           |
         |     [cdp_browser.py]  [Pillow]   history.json
         |           |
         |    [browser_session.py]
         |           |
     settings.json  Chrome (subprocess)
                     |              [notify.py]
              Playwright (CDP)       |
                     |          osascript /
               comix.to API     notify-send
```

## Key Components

### `browser_session.py` — Chrome Session Lifecycle

This layer owns the state that was previously packed into one monolithic browser client:

1. Launches Google Chrome as a **subprocess** with `--remote-debugging-port` (dynamic port selection)
2. Applies the **single-instance lock** so only one comix-dl process can reuse the persisted Chrome profile at a time
3. Connects Playwright via `connect_over_cdp()` and owns the main page plus the pooled download pages
4. Wraps CDP connect, page creation, navigation, and `page.evaluate()` in **explicit timeout boundaries**
5. Replaces dead pooled pages instead of silently re-queuing them
6. Handles **graceful shutdown** and `atexit` cleanup for the Chrome started by the current Python process only

### `cdp_browser.py` — Cloudflare-Aware Request Client

This layer now focuses on Cloudflare-sensitive behavior and browser-side request orchestration:

1. Does NOT use `--enable-automation` — Chrome appears as a normal user browser
2. Chrome starts **hidden** (`--window-position=-32000,-32000`); only brought forward if CF challenge needs manual solving
3. All network requests go through `page.evaluate(fetch())`, inheriting Chrome's real TLS fingerprint, cookies, and headers
4. **Binary data** (images) transferred as **base64** for 3-4x less overhead than JSON arrays
5. **Clearance self-healing** — if API/image requests start returning HTTP 403 or a challenge page reappears, cached clearance is reset, reacquired once, and the request is retried once
6. Keeps Cloudflare logic separate from Chrome startup/page-pool lifecycle so the session layer stays testable and smaller

This defeats CF's multi-layer detection:
- **JS challenge** — Chrome executes it natively
- **TLS fingerprint (JA3/JA4)** — real Chrome TLS stack, not httpx/curl
- **Automation detection** — no `navigator.webdriver` flag, no automation banner

A persistent Chrome profile at `~/.config/comix-dl/chrome-profile/` preserves CF clearance cookies across runs, while the browser client can invalidate its cached `_cf_cleared` state and reacquire clearance if the session expires mid-run.

### `comix_service.py` — REST API Client

Communicates with comix.to's v2 REST API:

| Endpoint                                  | Method | Purpose          |
| ----------------------------------------- | ------ | ---------------- |
| `/api/v2/manga?keyword=...`               | GET    | Search           |
| `/api/v2/manga/{hash_id}`                 | GET    | Manga details    |
| `/api/v2/manga/{hash_id}/chapters`        | GET    | Chapter list     |
| `/api/v2/chapters/{chapter_id}`           | GET    | Chapter images   |

Key identifiers:
- `hash_id` (e.g. `a1b2`) — used for manga lookups (NOT the slug)
- `chapter_id` (e.g. `5678901`) — used for chapter image retrieval
- `slug` (e.g. `some-manga`) — used only for user-facing URLs

Deduplication rules:
- Chapters are grouped by chapter number first, then by language and subtitle.
- Same-number chapters with different subtitles or different languages are preserved as distinct content.
- Only same-language duplicates compete on `image_count`, with the largest upload kept.

### `downloader.py` — Concurrent Image Downloader

- Downloads images via `CdpBrowser.get_bytes()` (fetch inside Chrome page, base64 encoded)
- Concurrency controlled by `asyncio.Semaphore` (default: 8 images at once)
- Automatic retry with exponential backoff
- **Resume support** — skips existing images, writes `.complete` marker only when every page succeeds
- **Atomic image writes** — downloaded pages are written via temp files and `os.replace()`
- **Resume validation** — existing image files must pass a magic-byte check before they are trusted
- **Partial-state manifest** — partial / failed chapters write `chapter.state.json` with failed pages
- **Temp-artifact cleanup** — stale `.part` and atomic hidden `.tmp` files are discarded on rerun before resume indexing
- **Single-pass resume index** — chapter directories are scanned once to index existing page files
- File extension detection from URL or magic bytes (including AVIF)

### `converters.py` — PDF / CBZ

- **PDF**: Pillow-based, processes images in batches to limit memory usage; fails fast if no PDF merge backend is available for multi-batch output
- **CBZ**: ZIP archive with no compression (standard comic book format)

### `settings.py` — Persistent Configuration

- Settings stored as JSON at `~/.config/comix-dl/settings.json`
- Writes use atomic replace to reduce config corruption on interruption
- Loaded at startup and **synced to CONFIG** so all modules use user's values
- Controls: output directory, default format, concurrency, retry count, image optimization

### `history.py` — Download History

- JSON storage at `~/.config/comix-dl/history.json`
- Writes use atomic replace to reduce history corruption on interruption
- Records each download session (title, chapter count, format, size, status)
- Auto-trims oldest entries at 500 max
- Accessed via `comix-dl history` / `comix-dl history clear`

### `notify.py` — Desktop Notifications

- Platform-aware: `osascript` on macOS, `notify-send` on Linux
- Best-effort, never raises — silently no-ops if tools unavailable
- Triggered after download completion

### `cli.py` — CLI Interface

- **argparse-based** with subcommands: `search`, `download`, `info`, `list`, `clean`, `history`, `doctor`, `settings`
- Interactive main menu loop with Rich TUI elements
- Non-interactive mode: `comix-dl download URL --chapters 1-5 --format cbz`
- Quick search: `comix-dl "query"` (no subcommand needed)
- **`--quiet` mode** — suppress all output for scripting
- **`--no-optimize`** — disable WebP image optimization
- **Ctrl+C handling** — graceful shutdown, finishes current downloads then stops
- Download summary panel with speed stats, size, and success/skip/fail counts

## Data Flow

```
Search: User query → API search → SearchResult list → user selects

Download: hash_id → API chapters → user selects → for each chapter:
            chapter_id → API images → image URLs → parallel fetch (page pool) → disk

Resume:  chapter_dir/.complete exists? → skip
         image file already exists?    → skip
         chapter.state.json            → inspect incomplete pages / errors

Convert: fully-complete image directory → (optional: optimize to WebP) → PDF/CBZ → output file

History: download finishes → record to history.json → send desktop notification
```

## Threading Model

All operations are async (`asyncio`). The only subprocess is Chrome itself. Image downloads run as concurrent async tasks limited by semaphore, using a browser page pool sized to `download.max_concurrent_images`. When all pooled pages are busy, requests wait for a pooled page rather than falling back to the shared main page. Chapter downloads are also parallelized (default: 2 concurrent). Browser-facing await points are wrapped in explicit timeout boundaries so stuck CDP connects, navigations, and `page.evaluate(fetch())` calls fail predictably.

## Why Not httpx / curl_cffi?

Cloudflare ties `cf_clearance` cookies to:
1. User-Agent string
2. TLS fingerprint (JA3/JA4 hash)
3. Browser fingerprint

External HTTP clients (httpx, curl, curl_cffi) have different TLS fingerprints than Chrome, even when impersonating Chrome's UA. The only reliable bypass is using the actual Chrome TLS stack via CDP.
