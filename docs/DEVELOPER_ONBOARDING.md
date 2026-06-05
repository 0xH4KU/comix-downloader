# Developer Onboarding

This guide is the fastest route from clone to productive change.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

Run commands from the repository root.

## Core Commands

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src --no-error-summary
.venv/bin/python scripts/check_docs_consistency.py
```

For focused work, prefer the smallest relevant test file first, then run the full gate before finishing.

## Where To Start Reading

- `README.md`: user-facing CLI behavior
- `ARCHITECTURE.md`: current runtime boundaries
- `docs/SITE_ADAPTERS.md`: adapter lifecycle and helper modules
- `src/comix_dl/core/application/session.py`: runtime wiring
- `src/comix_dl/sites/comix_to.py`: reference adapter facade
- `src/comix_dl/core/application/download_usecase.py`: batch download orchestration
- `src/comix_dl/core/downloader.py`: image persistence and resume behavior

## Change Workflow

1. Run the relevant tests before changing code.
2. Add or update a focused test for the behavior or boundary you are touching.
3. Keep framework code in `core/` site-agnostic.
4. Put site-specific parsing, API fallback, browser hooks, and dedup rules under `sites/`.
5. Update docs when architecture, CLI behavior, settings, or adapter contracts change.
6. Run the full local quality gate before opening a PR.

## Common Tasks

Add a comix.to API parsing rule:

- update `src/comix_dl/sites/comix_to_parsing.py`
- add tests in `tests/test_comix_to_adapter.py`

Change duplicate chapter behavior:

- update `src/comix_dl/sites/comix_to_dedup.py`
- keep `DedupDecision` output aligned with the actual rule
- verify `tests/test_comix_to_adapter.py`

Change Cloudflare/browser request behavior:

- framework lifecycle and retry changes belong in `core/engines/cdp_browser.py` or `core/engines/browser_session.py`
- comix.to endpoint probes and image rendering belong in `sites/comix_to_browser.py`
- verify `tests/test_cdp_browser.py`

Change download completion behavior:

- update `core/downloader.py` or `core/application/download_usecase.py`
- cover partial, failed, skipped, and completed paths
- verify `tests/test_downloader.py` and `tests/test_download_usecase.py`

## Pitfalls

- Do not add comix.to API paths or reader JavaScript to `core/engines/cdp_browser.py`.
- Do not store per-run API cache on the registry singleton; use a session-scoped adapter/client.
- Do not package partial chapters. Conversion should only happen after a complete chapter download.
- Do not update CLI summary wording in only one place; use `core/application/download_reporting.py`.
