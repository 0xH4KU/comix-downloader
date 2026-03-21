# comix-downloader

A focused [comix.to](https://comix.to) manga downloader with Cloudflare bypass.

Built with **Python 3.11+**, **Playwright** (CDP connection), and **Rich** (CLI output).

## Features

- **Cloudflare bypass** — launches a real Chrome instance via CDP, no automation detection
- **REST API integration** — uses comix.to's v2 API directly, no HTML scraping
- **Interactive & non-interactive CLI** — main menu, quick search, or full CLI flags
- **Parallel downloads** — concurrent chapter and image downloads with page pool
- **Resume / skip** — automatically skips already-downloaded chapters and images
- **Smart dedup** — auto-detects duplicate chapter uploads, keeps the best version by image count
- **Rate limiting** — randomized download delays to avoid triggering anti-scraping (toggleable)
- **PDF / CBZ output** — convert downloaded images to PDF or CBZ archives
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

## Manual Install

```bash
git clone https://github.com/0xH4KU/comix-downloader.git
cd comix-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Update / Uninstall

```bash
# Update: re-run the install script
curl -fsSL https://raw.githubusercontent.com/0xH4KU/comix-downloader/main/install.sh | bash

# Uninstall
comix-dl-uninstall
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
  3  Settings
  4  Doctor (diagnostics)
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
```

### Search & Download Flow

1. Enter a search query
2. Select a manga from the results
3. View available chapters (with page counts and automatic deduplication)
4. Select chapters to download (`all`, `1-5`, `1,3,5`)
5. Choose output format (`pdf`, `cbz`, `both`)
6. Chapters are downloaded in parallel with progress bars
7. Already-downloaded chapters are automatically skipped (resume)

### Settings

Accessible from the main menu (`3`) or `comix-dl settings`. Configurable options:

| Setting              | Default                          | Description                         |
| -------------------- | -------------------------------- | ----------------------------------- |
| Download directory   | `~/Downloads/comix-dl`           | Where files are saved               |
| Default format       | `pdf`                            | Output format (pdf/cbz/both)        |
| Concurrent chapters  | `2`                              | Chapters downloaded in parallel     |
| Concurrent images    | `8`                              | Images per chapter in parallel      |
| Max retries          | `3`                              | Retry count for failed images       |
| Download delay       | `on`                             | Random delays to avoid rate limits  |

Settings persist to `~/.config/comix-dl/settings.json`.

### Diagnostics

```bash
comix-dl doctor
```

Checks Python version, dependencies, Chrome availability, and output directory.

## How It Works

1. **Chrome CDP** — comix-dl launches a real Chrome subprocess with `--remote-debugging-port` (dynamic port to avoid conflicts), then connects via Playwright's `connect_over_cdp`. No `--enable-automation` flag, so Cloudflare sees a normal browser. Chrome starts hidden off-screen and only moves forward if a manual CF challenge needs solving.

2. **CF Clearance** — on first run, if a Cloudflare challenge appears, Chrome moves to the foreground for the user to solve it once. The Chrome profile is persisted at `~/.config/comix-dl/chrome-profile/`, so subsequent runs pass automatically. An `asyncio.Lock` prevents concurrent tasks from triggering duplicate CF checks.

3. **REST API** — all data comes from comix.to's v2 REST API:
   - `GET /api/v2/manga?keyword=...` — search
   - `GET /api/v2/manga/{hash_id}` — manga info
   - `GET /api/v2/manga/{hash_id}/chapters` — chapter list
   - `GET /api/v2/chapters/{chapter_id}` — chapter images

4. **Smart Dedup** — the API often returns duplicate entries for the same chapter (from different uploaders). comix-dl groups chapters by number, fetches image counts for duplicates, and keeps the version with the most images. Chapters with the same number but different subtitles (e.g. "Chapter 0 - Volume 11" vs "Chapter 0 - Volume 12") are correctly treated as distinct content.

5. **Download** — image URLs are fetched via `page.evaluate(fetch())` inside Chrome's page context. A **page pool** (4 browser pages) enables true parallel downloads. Binary data uses **base64 encoding** (3-4x less overhead than JSON arrays). Random delays between requests avoid rate limiting.

6. **Resume** — each chapter directory gets a `.complete` marker after successful download. Re-running the same download skips completed chapters and resumes partially-downloaded ones.

7. **Convert** — downloaded images are packaged into PDF (via Pillow, processed in batches to limit memory) or CBZ (zip archive).

8. **Graceful shutdown** — `Ctrl+C` finishes current downloads then stops. An `atexit` handler ensures Chrome is cleaned up even on crashes.

## Project Structure

```
src/comix_dl/
  __init__.py         # Package version
  __main__.py         # python -m comix_dl entry point
  cli.py              # Interactive & non-interactive CLI (argparse)
  cdp_browser.py      # Chrome CDP connection (CF bypass, page pool)
  comix_service.py    # REST API client (search, chapters, dedup)
  downloader.py       # Concurrent image downloader with resume
  converters.py       # PDF / CBZ conversion
  config.py           # Default configuration dataclasses
  settings.py         # Persistent user settings (JSON)
```

## License

MIT
