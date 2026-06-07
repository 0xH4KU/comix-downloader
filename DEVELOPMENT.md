# Development Guide

## Setup

```bash
git clone https://github.com/0xH4KU/comix-downloader.git
cd comix-downloader
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

The editable install includes the runtime `pypdf` dependency, so large PDF chapters do not require a separate merge-backend install.

Settings are loaded into a per-run `AppConfig` via `build_runtime_config()` and injected into runtime components. `load_settings()` now returns normalized settings only; it does not mutate any process-global config object.
Download tuning is profile-driven: `desktop`, `low_resource`, and `ci` presets map to effective chapter/image concurrency and delay values, while `custom` preserves explicit user overrides.

## Project Layout

``` 
comix-downloader/
  src/comix_dl/
    __init__.py           # Version fallback
    __main__.py           # python -m entry point
    core/
      application/
        query_usecase.py    # Slug/query resolution and lookup rules
        download_usecase.py # Download orchestration + event emission
        cleanup_usecase.py  # Listing and cleanup planning
        download_reporting.py # Shared summary and issue formatting
        session.py          # Runtime/session wiring for CLI adapters
      engines/
        browser_session.py  # Chrome lifecycle, locks, CDP, page pool
        cdp_browser.py      # Cloudflare-aware browser request client
      cli/
        __init__.py         # CLI entry, parser, signal handling
        doctor.py           # Local and browser-backed diagnostics
        download_progress.py # Download progress and summary rendering
        flow_prompts.py     # Series metadata panels and chapter selection prompts
        flows.py            # Search/download/list/clean flow orchestration
        interactive.py      # Interactive settings/history/filter UI
        display.py          # Rich tables and formatting
      config.py             # AppConfig dataclasses used for runtime injection
      converters.py         # PDF / CBZ conversion with bounded PDF batching
      downloader.py         # Image downloader
      errors.py             # Domain error types
      fileio.py             # Atomic file write helpers
      history.py            # Download history persistence
      logging_utils.py      # Structured logging formatter/helpers
      notify.py             # Desktop notifications
      settings.py           # Persistent settings
    sites/
      base.py               # SiteAdapter / Engine protocols
      comix_to.py           # comix.to adapter facade
      comix_to_api.py       # API orchestration + session-scoped cache
      comix_to_browser.py   # Service probe + scrambled image renderer
      comix_to_parsing.py   # JSON parsing helpers
      comix_to_dedup.py     # Chapter deduplication rules
  tests/                  # Test suite
  README.md
  ARCHITECTURE.md
  CONTRIBUTING.md
  DEVELOPMENT.md
  TODO.md
  pyproject.toml
```

## Running

```bash
# Main menu
comix-dl

# Quick search
comix-dl "manga name"

# Diagnostics
comix-dl doctor
comix-dl doctor --deep

# Debug logging
comix-dl --debug
```

## Quality Checks

```bash
# Lint
ruff check .

# Type check
mypy src/comix_dl/ --no-error-summary

# Docs/version consistency
python3 scripts/check_docs_consistency.py

# Test
pytest

# Optional browser-backed live contract smoke test
COMIX_DL_LIVE=1 pytest tests/test_live_smoke.py -q

# Coverage gate (matches CI)
pytest --cov=comix_dl --cov-report=term-missing --cov-fail-under=70

# Full local gate
ruff check . && mypy src/comix_dl/ --no-error-summary && python3 scripts/check_docs_consistency.py && pytest --cov=comix_dl --cov-report=term-missing --cov-fail-under=70
```

Notes:
- Running `pytest` from the repository root now imports from `src/` directly, so an editable install is not required just to collect tests.
- Low-level localhost socket tests auto-skip in restricted sandboxes that do not allow binding TCP ports.
- Live remote smoke tests are skipped unless `COMIX_DL_LIVE=1` is set. They verify search, series metadata, chapter image payloads, and one sample image fetch against the real site.
- Current high-risk module baselines are tracked in CI: `cli/__init__.py` 98%, `cli/flows.py` 82%, `cli/download_progress.py` 100%, `cli/flow_prompts.py` 100%, `cdp_browser.py` 84%, `converters.py` 73%.
- `MIGRATION.md` captures maintainer-facing upgrade notes; `RELEASE_CHECKLIST.md` defines the slice release order and final verification sequence.
- Search/info smoke tests should verify that API failures surface explicit `RemoteApiError` text rather than falling through to empty-result messaging.
- Browser smoke tests should verify startup keeps a single visible tab until pooled download pages are actually needed.

## Key Concepts

### Cloudflare Bypass

The bypass works by launching a real Chrome instance and connecting via CDP:

```python
# BrowserSessionManager launches Chrome itself (no automation flags)
port = 9222  # or a dynamically-selected free port
subprocess.Popen(["chrome", f"--remote-debugging-port={port}", ...])

# Then connect via Playwright
browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
```

`BrowserSessionManager` owns Chrome startup, lock handling, and pooled pages. `CdpBrowser` layers Cloudflare clearance and retry behavior on top. Requests are made through the page context using `page.evaluate(fetch(...))`, which inherits Chrome's real cookies and TLS fingerprint.

### API Identifiers

comix.to uses several identifiers:

- `hash_id` (e.g. `a1b2`) — primary key for API lookups
- `slug` (e.g. `some-manga`) — URL-friendly name, NOT used for API calls
- `manga_id` (e.g. `1234`) — numeric ID, NOT used for API calls
- `chapter_id` (e.g. `5678901`) — used for chapter image lookup

### Adding New Features

1. **New comix.to API call** — add method to `ComixToApiClient` in `sites/comix_to_api.py`
2. **New CLI command** — add parser wiring in `src/comix_dl/core/cli/__init__.py`; keep orchestration/runtime setup in `src/comix_dl/core/application/` and leave `src/comix_dl/core/cli/flows.py` as a presentation adapter
3. **New output format** — add converter in `converters.py`
   If it touches PDF batching, keep temp-workspace cleanup and batch-size tests green.
4. **New setting** — add field to `Settings` in `settings.py`
   If it affects runtime tuning, either map it into an existing profile or update the profile-resolution tests.
5. **New user-meaningful failure mode** — add or reuse a domain error in `errors.py`, then catch/render it at the CLI boundary
6. **New dedup rule** — update `sites/comix_to_dedup.py` to emit `DedupDecision` entries and keep the CLI dedup report aligned with the actual rule
7. **New download summary wording** — update `core/application/download_reporting.py` and keep CLI/history/notification tests aligned with the shared report output
8. **New download-path log field** — update `logging_utils.py` and the download/use-case tests so structured logging stays stable

## Commit Conventions

```
feat: Add EPUB export
fix: Handle empty chapter list
docs: Update API endpoint docs
refactor: Extract download logic
```
