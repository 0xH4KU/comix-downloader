# Architecture

## Overview

comix-downloader uses a Chrome CDP connection to bypass Cloudflare protection on comix.to, then interacts with the site's REST API v2 to search, list chapters, and fetch image URLs. Images are downloaded concurrently via a page pool and packaged into PDF or CBZ.

## Component Diagram

```
                    User
                      |
                 [cli.py] ---- argparse + Interactive menu
                      |
         +-----------+-----------+
         |           |           |
   [settings.py] [comix_service] [converters.py]
         |           |           |
         |     [cdp_browser.py]  [Pillow / ZIP]
         |           |
    settings.json  Chrome (subprocess)
                     |
              Playwright (CDP)
                     |
               comix.to API
```

## Key Components

### `cdp_browser.py` — Cloudflare Bypass

The core of the CF bypass strategy:

1. Launches Google Chrome as a **subprocess** with `--remote-debugging-port` (dynamic port selection)
2. Does NOT use `--enable-automation` — Chrome appears as a normal user browser
3. Chrome starts **hidden** (`--window-position=-32000,-32000`); only brought forward if CF challenge needs manual solving
4. Connects via Playwright's `connect_over_cdp()` to control the browser programmatically
5. All network requests go through `page.evaluate(fetch())`, inheriting Chrome's real TLS fingerprint, cookies, and headers
6. **Binary data** (images) transferred as **base64** for 3-4x less overhead than JSON arrays
7. **Page pool** — multiple browser pages for parallel downloads without contention
8. **Graceful shutdown** — `atexit` handler ensures Chrome is cleaned up even on crash

This defeats CF's multi-layer detection:
- **JS challenge** — Chrome executes it natively
- **TLS fingerprint (JA3/JA4)** — real Chrome TLS stack, not httpx/curl
- **Automation detection** — no `navigator.webdriver` flag, no automation banner

A persistent Chrome profile at `~/.config/comix-dl/chrome-profile/` preserves CF clearance cookies across runs.

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

### `downloader.py` — Concurrent Image Downloader

- Downloads images via `CdpBrowser.get_bytes()` (fetch inside Chrome page, base64 encoded)
- Concurrency controlled by `asyncio.Semaphore` (default: 8 images at once)
- Automatic retry with exponential backoff
- **Resume support** — skips existing images, writes `.complete` marker on chapter completion
- File extension detection from URL or magic bytes (including AVIF)

### `converters.py` — PDF / CBZ

- **PDF**: Pillow-based, processes images in batches to limit memory usage
- **CBZ**: ZIP archive with no compression (standard comic book format)

### `settings.py` — Persistent Configuration

- Settings stored as JSON at `~/.config/comix-dl/settings.json`
- Loaded at startup and **synced to CONFIG** so all modules use user's values
- Controls: output directory, default format, concurrency, retry count

### `cli.py` — CLI Interface

- **argparse-based** with subcommands: `search`, `download`, `doctor`, `settings`
- Interactive main menu loop with Rich TUI elements
- Non-interactive mode: `comix-dl download URL --chapters 1-5 --format cbz`
- Quick search: `comix-dl "query"` (no subcommand needed)
- **Ctrl+C handling** — graceful shutdown, finishes current downloads then stops
- Download summary panel with elapsed time and success/skip/fail counts

## Data Flow

```
Search: User query → API search → SearchResult list → user selects

Download: hash_id → API chapters → user selects → for each chapter:
            chapter_id → API images → image URLs → parallel fetch (page pool) → disk

Resume:  chapter_dir/.complete exists? → skip
         image file already exists?    → skip

Convert: image directory → PDF/CBZ → output file
```

## Threading Model

All operations are async (`asyncio`). The only subprocess is Chrome itself. Image downloads run as concurrent async tasks limited by semaphore, using a pool of browser pages for parallelism. Chapter downloads are also parallelized (default: 2 concurrent).

## Why Not httpx / curl_cffi?

Cloudflare ties `cf_clearance` cookies to:
1. User-Agent string
2. TLS fingerprint (JA3/JA4 hash)
3. Browser fingerprint

External HTTP clients (httpx, curl, curl_cffi) have different TLS fingerprints than Chrome, even when impersonating Chrome's UA. The only reliable bypass is using the actual Chrome TLS stack via CDP.
