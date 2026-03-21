# comix-downloader

A focused [comix.to](https://comix.to) manga downloader with Cloudflare bypass.

Built with **Python 3.11+**, **Playwright** (CDP connection), and **Rich** (CLI output).

## Features

- **Cloudflare bypass** — launches a real Chrome instance via CDP, no automation detection
- **REST API integration** — uses comix.to's v2 API directly, no HTML scraping
- **Interactive CLI** — main menu with search, download, and settings
- **Parallel downloads** — concurrent chapter and image downloads
- **PDF / CBZ output** — convert downloaded images to PDF or CBZ archives
- **Persistent settings** — saves preferences to `~/.config/comix-dl/settings.json`

## Requirements

- Python 3.11+
- Google Chrome (used for Cloudflare bypass)
- Playwright (`pip install playwright && playwright install chromium`)

## Install

```bash
git clone https://github.com/0xH4KU/comix-downloader.git
cd comix-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
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

### Search & Download Flow

1. Enter a search query
2. Select a manga from the results
3. View available chapters
4. Select chapters to download (`all`, `1-5`, `1,3,5`)
5. Choose output format (`pdf`, `cbz`, `both`)
6. Chapters are downloaded in parallel with progress bars

### Settings

Accessible from the main menu (`3`). Configurable options:

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
comix-dl --doctor
```

Checks Python version, dependencies, Chrome availability, and output directory.

## How It Works

1. **Chrome CDP** — comix-dl launches a real Chrome subprocess with `--remote-debugging-port`, then connects via Playwright's `connect_over_cdp`. No `--enable-automation` flag, so Cloudflare sees a normal browser.

2. **CF Clearance** — on first run, Chrome opens visibly. If a Cloudflare challenge appears, the user solves it once. The Chrome profile is persisted at `~/.config/comix-dl/chrome-profile/`, so subsequent runs pass automatically.

3. **REST API** — all data comes from comix.to's v2 REST API:
   - `GET /api/v2/manga?keyword=...` — search
   - `GET /api/v2/manga/{hash_id}` — manga info
   - `GET /api/v2/manga/{hash_id}/chapters` — chapter list
   - `GET /api/v2/chapters/{chapter_id}` — chapter images (URLs included)

4. **Download** — image URLs from the API are fetched via `page.evaluate(fetch())` inside Chrome's page context, preserving all cookies/headers.

5. **Convert** — downloaded images are packaged into PDF (via Pillow) or CBZ (zip archive).

## Project Structure

```
src/comix_dl/
  __init__.py         # Package version
  __main__.py         # python -m comix_dl entry point
  cli.py              # Interactive CLI with main menu
  cdp_browser.py      # Chrome CDP connection (CF bypass)
  comix_service.py    # REST API client (search, chapters, images)
  downloader.py       # Concurrent image downloader
  converters.py       # PDF / CBZ conversion
  config.py           # Default configuration dataclasses
  settings.py         # Persistent user settings (JSON)
  parser.py           # HTML chapter parser (legacy, kept as fallback)
  tui.py              # Textual TUI (alternative interface)
```

## License

MIT
