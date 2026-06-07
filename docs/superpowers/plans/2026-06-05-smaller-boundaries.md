# Smaller Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the comix.to adapter and browser hooks into smaller, testable modules while preserving CLI behavior.

**Architecture:** Keep the CLI and application use cases stable. Move comix.to-specific URL, parsing, deduplication, API cache, service probe, and scrambled image rendering logic out of large framework files and into focused site modules. Add session-scoped adapter cloning to prevent singleton cache leakage.

**Tech Stack:** Python 3.11+, pytest, Playwright CDP boundary, Rich CLI, package data JavaScript assets.

---

## File Structure

- Create `src/comix_dl/sites/comix_to_url.py` for URL parsing and URL helpers.
- Create `src/comix_dl/sites/comix_to_parsing.py` for JSON normalization and model parsing.
- Create `src/comix_dl/sites/comix_to_dedup.py` for chapter deduplication.
- Create `src/comix_dl/sites/comix_to_api.py` for API request methods and chapter payload cache.
- Create `src/comix_dl/sites/comix_to_browser.py` for comix.to browser probe and scrambled image renderer.
- Modify `src/comix_dl/sites/comix_to.py` into a facade.
- Modify `src/comix_dl/sites/base.py` and `src/comix_dl/core/engines/cdp_browser.py` to support pre-clearance site hooks.
- Modify `src/comix_dl/core/application/session.py` to use session-scoped adapter instances.
- Update tests under `tests/test_comix_to_adapter.py`, `tests/test_cdp_browser.py`, and `tests/test_application_session.py`.
- Update `ARCHITECTURE.md`.
- Add `docs/DEVELOPER_ONBOARDING.md` and `docs/SITE_ADAPTERS.md`.

## Tasks

### Task 1: Focused comix.to helper modules

- [ ] Write failing tests that import URL, parsing, and dedup helpers directly.
- [ ] Move URL helpers from `comix_to.py` into `comix_to_url.py`.
- [ ] Move primitive coercion, response unwrapping, taxonomy/person parsing, search parsing, chapter item parsing, and image page extraction into `comix_to_parsing.py`.
- [ ] Move deduplication into `comix_to_dedup.py`.
- [ ] Keep compatibility aliases on `ComixToAdapter` only where existing tests or forks may reasonably touch private helpers.
- [ ] Run `tests/test_comix_to_adapter.py`.

### Task 2: Session-scoped API client/cache

- [ ] Add tests proving two application sessions use distinct comix.to adapter instances and distinct chapter payload caches.
- [ ] Create `ComixToApiClient` in `comix_to_api.py`.
- [ ] Move direct lookup, search fallback, chapter pagination, image-count prefetch, and chapter payload cache into the client.
- [ ] Add `ComixToAdapter.new_session()` and have `open_application_session()` use it when present.
- [ ] Run `tests/test_application_session.py tests/test_comix_to_adapter.py`.

### Task 3: Browser hook boundaries

- [ ] Add tests for browser-registered service probes and scrambled image renderers.
- [ ] Add generic registration methods to `CdpBrowser`.
- [ ] Move the comix.to API probe and scrambled canvas JavaScript into `comix_to_browser.py`.
- [ ] Add `ComixToAdapter.configure_engine()` to register those hooks before clearance.
- [ ] Keep `CdpBrowser.get_scrambled_image_bytes()` as the generic engine method used by `Downloader`.
- [ ] Run `tests/test_cdp_browser.py tests/test_downloader.py`.

### Task 4: Documentation

- [ ] Update `ARCHITECTURE.md` to describe the adapter modules and remove stale `comix_service.py` references.
- [ ] Add `docs/DEVELOPER_ONBOARDING.md` with project map, common commands, and change workflow.
- [ ] Add `docs/SITE_ADAPTERS.md` with adapter lifecycle, engine hooks, model contracts, and testing expectations.
- [ ] Run docs consistency checks if available.

### Task 5: Verification

- [ ] Run `.venv/bin/python -m pytest`.
- [ ] Run `.venv/bin/python -m ruff check .`.
- [ ] Run `.venv/bin/python -m mypy src`.
- [ ] Inspect `git diff --stat` and key changed files.
