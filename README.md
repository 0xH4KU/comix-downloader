# comix-downloader

[![Version](https://img.shields.io/badge/version-0.3.32-blue?style=flat-square)](https://github.com/0xH4KU/comix-downloader)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/0xH4KU/comix-downloader?style=flat-square)](https://github.com/0xH4KU/comix-downloader/commits)

A focused [comix.to](https://comix.to) manga downloader with Cloudflare bypass.

Built with **Python 3.11+**, **Playwright** (CDP connection), and **Rich** (CLI output).

## Features

- **Cloudflare bypass** — launches a real Chrome instance via CDP, no automation detection
- **REST API integration** — uses comix.to's v2 API directly, no HTML scraping
- **Interactive & non-interactive CLI** — main menu, quick search, or full CLI flags
- **Parallel downloads** — concurrent chapter and image downloads with a bounded page pool sized to the configured image concurrency
- **Bounded browser operations** — CDP connect, page navigation, HTML reads, and in-browser fetches fail with explicit timeouts instead of hanging indefinitely
- **Clearance self-healing** — HTTP 403 or a renewed Cloudflare challenge resets cached clearance, re-checks the session, and retries once before failing clearly
- **Dead-page eviction** — closed browser pages are discarded and replaced instead of being returned to the pool
- **Single-instance browser lock** — a second comix-dl process is rejected cleanly instead of racing over the same Chrome profile
- **Lifecycle split** — `BrowserSessionManager` owns Chrome startup, page pooling, and cleanup while `CdpBrowser` focuses on Cloudflare-aware request flow
- **Explicit runtime config** — user settings are normalized into a per-run `AppConfig` and injected into runtime components instead of mutating process-global state
- **Resume / skip** — automatically skips already-downloaded chapters and images
- **Corrupt-page recovery** — invalid existing image files are discarded and re-downloaded instead of being trusted by resume
- **No false-success conversion** — chapters with failed page downloads stay unconverted and are reported as partial instead of completed
- **Partial-state manifest** — incomplete chapters keep a machine-readable `chapter.state.json` for diagnostics and future recovery
- **Recovery-safe reruns** — stale temp artifacts are cleaned up and partial chapters resume from the missing pages instead of restarting from scratch
- **Cheaper resume scans** — existing chapter files are indexed once per run instead of re-scanning the directory for every page
- **Smart dedup** — chapter dedup keeps language variants distinct and only collapses true same-language duplicates by image count
- **Sharper failure diagnostics** — Cloudflare expiry, API 403, image timeouts, page-pool exhaustion, and PDF merge-backend gaps now surface as targeted errors
- **Typed domain errors** — Cloudflare, remote API, partial-download, conversion, and configuration failures now have distinct exception types instead of collapsing into generic `RuntimeError`
- **Reliable large PDF merge** — normal installs now include `pypdf`, so multi-batch PDF output works without hidden extra dependencies
- **Rate limiting** — randomized download delays to avoid triggering anti-scraping (toggleable)
- **PDF / CBZ output** — convert downloaded images to PDF or CBZ archives
- **Image optimization** — optional WebP conversion for 40-60% size savings (on by default)
- **Download speed stats** — shows total size and average speed in download summary
- **Desktop notifications** — system notification on download completion (macOS/Linux)
- **Download history** — tracks what you've downloaded with `comix-dl history`
- **Persistent settings** — saves preferences to `~/.config/comix-dl/settings.json`

## Platform Support

| Platform | Support | Notes |
|----------|---------|-------|
| **macOS** | ✅ Fully supported | |
| **Windows** | ✅ Fully supported | Requires Chrome + Python |
| **Linux (Desktop)** | ✅ Fully supported | Ubuntu Desktop, Fedora, etc. |
| **Linux (Headless VPS)** | ⚠️ Limited | Requires xvfb + manual CF cookie setup, not recommended |
| **WSL2** | ⚠️ Experimental | WSLg may work, Chrome path needs manual config |

> **Why desktop only?** comix-dl needs a visible Chrome window to solve Cloudflare challenges on first run. After the first clearance, cookies are cached and subsequent runs are automatic — but a GUI environment is still needed when cookies expire.

## Requirements

- Python 3.11+
- Google Chrome (used for Cloudflare bypass)
- Playwright (`pip install playwright && playwright install chromium`)

## Quick Install (one-click)

```bash
curl -fsSL https://raw.githubusercontent.com/0xH4KU/comix-downloader/main/install.sh | bash
```

This will:
- Auto-detect Python 3.11+ and Chrome
- Clone the repo to `~/.local/share/comix-dl`
- Create an isolated venv and install all dependencies
- Install Playwright Chromium
- Add `comix-dl` to your PATH

After install, use `comix-dl` from **any directory**.

### Windows (PowerShell)

```powershell
irm https://raw.githubusercontent.com/0xH4KU/comix-downloader/main/install.ps1 | iex
```

## Manual Install

```bash
git clone https://github.com/0xH4KU/comix-downloader.git
cd comix-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
```

Normal installs now pull in `pypdf`, which is the default merge backend for large multi-batch PDF output.

For contributor workflow, local quality gates, and regression-test expectations, see `CONTRIBUTING.md`.

## Update / Uninstall

```bash
# Update: re-run the install script
curl -fsSL https://raw.githubusercontent.com/0xH4KU/comix-downloader/main/install.sh | bash

# Uninstall
comix-dl-uninstall
# or
bash install.sh --uninstall  # Linux/macOS
.\install.ps1 -Uninstall     # Windows
```

## Usage

### Interactive Mode (recommended)

```bash
# Launch main menu
comix-dl

# Quick search shortcut
comix-dl "manga name"
```

The main menu offers:

```
  1  Search manga
  2  Download by URL
  3  My downloads
  4  Download history
  5  Settings
  6  Doctor (diagnostics)
  q  Exit
```

### Non-Interactive Mode

```bash
# Search
comix-dl search "manga name"

# Download specific chapters as CBZ
comix-dl download "manga-slug" --chapters 1-5 --format cbz

# Download all chapters to a custom directory
comix-dl download "https://comix.to/manga/some-manga" --chapters all --format pdf --output ~/Comics

# Download without image optimization
comix-dl download "manga-slug" --no-optimize

# Show manga info without downloading
comix-dl info "https://comix.to/manga/some-manga"

# List downloaded manga
comix-dl list

# Clean up raw image directories (after conversion)
comix-dl clean
comix-dl clean --force    # Skip confirmation

# View download history
comix-dl history
comix-dl history clear    # Purge all history

# Quiet mode (for scripting, errors only)
comix-dl -q download "manga-slug"
```

### Search & Download Flow

1. Enter a search query
2. Select a manga from the results (`1` for direct, `1i` to show info first)
3. View available chapters (with page counts and automatic deduplication)
4. **Filter chapters** by keyword (optional — press Enter to skip)
5. Select chapters to download (`all`, `1-5`, `1,3,5`)
6. Choose output format (`pdf`, `cbz`, `both`)
7. Chapters are downloaded in parallel with progress bars
8. Already-downloaded chapters are automatically skipped (resume)
9. Cleanup prompt — choose to remove raw image directories after conversion

### Chapter Filter

After the chapter list is displayed, you can filter before selecting:

| Command | Effect | Example |
|---------|--------|---------|
| `+keyword` | Keep only chapters matching the keyword | `+stage` |
| `-keyword` | Remove chapters matching the keyword | `-extra` |
| `+key1 +key2` | Keep chapters matching **any** keyword (OR) | `+stage +extra` |
| `-key1 -key2` | Remove chapters matching **any** keyword | `-stage -extra` |
| `u` | Undo last filter | |
| `r` | Reset to original list | |
| Enter | Done filtering, proceed to selection | |

Filters are case-insensitive and match anywhere in the chapter title. If a filter removes all chapters, it auto-resets. The filtered list is re-displayed after each operation.

### Settings

Accessible from the main menu (`5`) or `comix-dl settings`. Configurable options:

| Setting              | Default                          | Description                         |
| -------------------- | -------------------------------- | ----------------------------------- |
| Download directory   | `~/Downloads/comix-dl`           | Where files are saved               |
| Default format       | `pdf`                            | Output format (pdf/cbz/both)        |
| Concurrent chapters  | `2`                              | Chapters downloaded in parallel     |
| Concurrent images    | `8`                              | Images per chapter in parallel, and browser page-pool size |
| Max retries          | `3`                              | Retry count for failed images       |
| Download delay       | `on`                             | Random delays to avoid rate limits  |
| Optimize images      | `on`                             | Convert images to WebP before packaging |

Settings persist to `~/.config/comix-dl/settings.json`.

### Diagnostics

```bash
comix-dl doctor
```

Checks Python version, dependencies, Chrome availability, and output directory.

## How It Works

1. **Chrome CDP** — `BrowserSessionManager` launches a real Chrome subprocess with `--remote-debugging-port` (dynamic port to avoid conflicts), then connects via Playwright's `connect_over_cdp`. No `--enable-automation` flag, so Cloudflare sees a normal browser. Chrome starts hidden off-screen and only moves forward if a manual CF challenge needs solving. A single-instance lock file prevents a second comix-dl process from starting a competing browser session against the same persisted profile.

2. **CF Clearance** — on first run, if a Cloudflare challenge appears, Chrome moves to the foreground for the user to solve it once. The Chrome profile is persisted at `~/.config/comix-dl/chrome-profile/`, so subsequent runs pass automatically. An `asyncio.Lock` prevents concurrent tasks from triggering duplicate CF checks. If a later API/image request starts returning `HTTP 403` or a challenge page reappears, comix-dl drops its cached clearance state, reacquires clearance once, and retries the request once before surfacing a clear failure.

3. **REST API** — all data comes from comix.to's v2 REST API:
   - `GET /api/v2/manga?keyword=...` — search
   - `GET /api/v2/manga/{hash_id}` — manga info
   - `GET /api/v2/manga/{hash_id}/chapters` — chapter list
   - `GET /api/v2/chapters/{chapter_id}` — chapter images

4. **Smart Dedup** — the API often returns duplicate entries for the same chapter (from different uploaders). comix-dl groups chapters by number, language, and subtitle, then keeps the same-language duplicate with the most images. Chapters with the same number but different subtitles (e.g. "Chapter 0 - Volume 11" vs "Chapter 0 - Volume 12") or different languages are correctly treated as distinct content.

5. **Download** — image URLs are fetched via `page.evaluate(fetch())` inside Chrome's page context. A **page pool** sized from the `Concurrent images` setting enables parallel downloads while keeping the main browser page reserved for navigation and Cloudflare handling. If all pooled pages are busy, requests wait for a pooled page instead of racing on the shared main page. Closed or stale pooled pages are discarded and replaced instead of being silently returned to circulation. Binary data uses **base64 encoding** (3-4x less overhead than JSON arrays). CDP connect, navigation, and in-browser fetch calls all use explicit timeouts, so stalled browser operations fail fast instead of hanging forever. Random delays between requests avoid rate limiting.

6. **Resume** — each chapter directory gets a `.complete` marker only after every page succeeds. Re-running the same download skips completed chapters and resumes partially-downloaded ones. Existing image files are validated before reuse, invalid files are re-downloaded, stale temp artifacts are cleaned up, and incomplete chapters keep `chapter.state.json` until a later successful rerun clears it.

7. **Convert** — only fully successful chapters are packaged into PDF or CBZ. Large chapters are rendered in batches and merged with the bundled `pypdf` backend (or `pikepdf` if you install it), so a normal install can emit full PDFs without hidden setup. If the merge backend is missing in a broken environment, conversion fails fast instead of emitting a truncated file.

8. **Graceful shutdown** — `Ctrl+C` finishes current downloads then stops. An `atexit` handler ensures the Chrome started by the current Python process is cleaned up even on crashes, without using a shared global PID kill path.

## Project Structure

``` 
src/comix_dl/
  __init__.py         # Package version
  __main__.py         # python -m comix_dl entry point
  cli/__init__.py     # CLI entry, parser, signal handling
  cli/flows.py        # Search/download/info/cleanup workflows
  cli/interactive.py  # Settings, history, chapter selection UI
  cli/display.py      # Rich display helpers
  browser_session.py  # Chrome lifecycle, CDP connection, page pool
  cdp_browser.py      # Cloudflare clearance + browser-side request orchestration
  comix_service.py    # REST API client (search, chapters, dedup)
  downloader.py       # Concurrent image downloader with resume
  fileio.py           # Atomic file write helpers
  converters.py       # PDF / CBZ conversion + image optimization
  config.py           # Default configuration dataclasses
  settings.py         # Persistent user settings (JSON)
  history.py          # Download history tracking
  notify.py           # Desktop notifications (macOS/Linux)
```

## License

MIT
