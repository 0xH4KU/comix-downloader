# Development Guide

## Setup

```bash
git clone https://github.com/0xH4KU/comix-downloader.git
cd comix-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

## Project Layout

```
comix-downloader/
  src/comix_dl/           # Main package
    __init__.py           # Version
    __main__.py           # python -m entry point
    cdp_browser.py        # Chrome CDP connection
    cli.py                # Interactive CLI
    comix_service.py      # REST API client
    config.py             # Default config dataclasses
    converters.py         # PDF / CBZ conversion
    downloader.py         # Image downloader
    parser.py             # HTML parser (legacy)
    settings.py           # Persistent settings
    tui.py                # Textual TUI (alternative)
  README.md
  ARCHITECTURE.md
  DEVELOPMENT.md
  pyproject.toml
```

## Running

```bash
# Main menu
comix-dl

# Quick search
comix-dl "manga name"

# Diagnostics
comix-dl --doctor

# Debug logging
comix-dl --debug
```

## Quality Checks

```bash
# Lint
ruff check .

# Type check
mypy src/comix_dl/ --no-error-summary

# Both
ruff check . && mypy src/comix_dl/ --no-error-summary
```

## Key Concepts

### Cloudflare Bypass

The bypass works by launching a real Chrome instance and connecting via CDP:

```python
# We launch Chrome ourselves (no automation flags)
subprocess.Popen(["chrome", "--remote-debugging-port=9222", ...])

# Then connect via Playwright
browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
```

Requests are made through the page context using `page.evaluate(fetch(...))`, which inherits Chrome's real cookies and TLS fingerprint.

### API Identifiers

comix.to uses several identifiers:

- `hash_id` (e.g. `a1b2`) — primary key for API lookups
- `slug` (e.g. `some-manga`) — URL-friendly name, NOT used for API calls
- `manga_id` (e.g. `1234`) — numeric ID, NOT used for API calls
- `chapter_id` (e.g. `5678901`) — used for chapter image lookup

### Adding New Features

1. **New API call** — add method to `ComixService` in `comix_service.py`
2. **New CLI command** — add to the main menu in `cli.py`
3. **New output format** — add converter in `converters.py`
4. **New setting** — add field to `Settings` in `settings.py`

## Commit Conventions

```
feat: Add EPUB export
fix: Handle empty chapter list
docs: Update API endpoint docs
refactor: Extract download logic
```
