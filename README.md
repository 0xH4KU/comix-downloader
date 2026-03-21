# comix-downloader

A focused [comix.to](https://comix.to) manga downloader with Cloudflare bypass.

Built with **Python 3.11+**, **Playwright** (CDP connection), and **Rich** (CLI output).

## Features

- **Cloudflare bypass** — launches a real Chrome instance via CDP, no automation detection
- **REST API integration** — uses comix.to's v2 API directly, no HTML scraping
- **Interactive & non-interactive CLI** — main menu, quick search, or full CLI flags
- **Parallel downloads** — concurrent chapter and image downloads with page pool
- **Resume / skip** — automatically skips already-downloaded chapters and images
- **PDF / CBZ output** — convert downloaded images to PDF or CBZ archives
- **Persistent settings** — saves preferences to `~/.config/comix-dl/settings.json`

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
3. View available chapters
4. Select chapters to download (`all`, `1-5`, `1,3,5`)
5. Choose output format (`pdf`, `cbz`, `both`)
6. Chapters are downloaded in parallel with progress bars
7. Already-downloaded chapters are automatically skipped

### Settings

Accessible from the main menu (`3`) or `comix-dl settings`. Configurable options:

| Setting              | Default                          | Description                    |
| -------------------- | -------------------------------- | ------------------------------ |
| Download directory   | `~/Downloads/comix-dl`           | Where files are saved          |
| Default format       | `pdf`                            | Output format (pdf/cbz/both)   |
| Concurrent chapters  | `2`                              | Chapters downloaded in parallel|
| Concurrent images    | `8`                              | Images per chapter in parallel |
| Max retries          | `3`                              | Retry count for failed images  |

Settings persist to `~/.config/comix-dl/settings.json`.

### Diagnostics

```bash
comix-dl doctor
```

Checks Python version, dependencies, Chrome availability, and output directory.

## How It Works

1. **Chrome CDP** — comix-dl launches a real Chrome subprocess with `--remote-debugging-port`, then connects via Playwright's `connect_over_cdp`. No `--enable-automation` flag, so Cloudflare sees a normal browser. Chrome is hidden off-screen and only brought forward if a manual CF challenge needs solving.

2. **CF Clearance** — on first run, if a Cloudflare challenge appears, Chrome moves to the foreground for the user to solve it once. The Chrome profile is persisted at `~/.config/comix-dl/chrome-profile/`, so subsequent runs pass automatically.

3. **REST API** — all data comes from comix.to's v2 REST API:
   - `GET /api/v2/manga?keyword=...` — search
   - `GET /api/v2/manga/{hash_id}` — manga info
   - `GET /api/v2/manga/{hash_id}/chapters` — chapter list
   - `GET /api/v2/chapters/{chapter_id}` — chapter images (URLs included)

4. **Download** — image URLs from the API are fetched via `page.evaluate(fetch())` inside Chrome's page context using a page pool for parallelism. Binary data is transferred via base64 encoding for efficiency.

5. **Resume** — each chapter directory gets a `.complete` marker after successful download. Re-running the same download skips completed chapters and resumes partially-downloaded ones.

6. **Convert** — downloaded images are packaged into PDF (via Pillow) or CBZ (zip archive).

## Project Structure

```
src/comix_dl/
  __init__.py         # Package version
  __main__.py         # python -m comix_dl entry point
  cli.py              # Interactive & non-interactive CLI
  cdp_browser.py      # Chrome CDP connection (CF bypass, page pool)
  comix_service.py    # REST API client (search, chapters, images)
  downloader.py       # Concurrent image downloader with resume
  converters.py       # PDF / CBZ conversion
  config.py           # Default configuration dataclasses
  settings.py         # Persistent user settings (JSON)
```

## License

MIT
