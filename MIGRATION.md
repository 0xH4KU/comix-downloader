# Migration Notes

## Scope

This document is for maintainers or local automation that still assumes the old monolithic CLI/runtime design. It summarizes the behavioral and structural shifts introduced across the refactor slices up to `v0.3.41`.

## Runtime Behavior Changes

- Large multi-batch PDF output now depends on the bundled runtime `pypdf` package; normal installs no longer need a hidden extra merge dependency.
- Partial chapter downloads no longer convert, no longer write success history, and no longer trigger success notifications.
- Resume logic now trusts only validated image files and `chapter.state.json`; corrupt or stale artifacts are removed and re-downloaded.
- Browser startup uses a single-instance lock file per config directory instead of trying to kill a global Chrome PID.
- Cloudflare expiry and HTTP 403 now trigger one explicit clearance refresh path instead of silently reusing stale browser state.

## Architecture Changes

- The process-global mutable `CONFIG` singleton is gone. Runtime settings are normalized once into `AppConfig` and then injected explicitly.
- CLI parsing/dispatch lives in `src/comix_dl/cli/__init__.py`; application orchestration lives in `src/comix_dl/application/`.
- Browser responsibilities are split between `browser_session.py` and `cdp_browser.py`.
- Settings, history, and JSON-like state files now go through dedicated repositories or atomic-write helpers instead of open-coded file writes.
- Shared download summary wording is centralized in `application/download_reporting.py` so CLI, history, and notifications do not drift.

## Maintainer Action Items

- If local scripts imported or mutated the old global config, switch them to `load_runtime()` or `build_runtime_config()`.
- If tooling assumed a partially downloaded chapter could still yield PDF/CBZ output, update that assumption: only `complete` chapters convert.
- If wrappers parsed ad hoc CLI summary strings, prefer the normalized history/reporting output instead.
- If release automation still uses a `45%` coverage gate, update it to `70%`.

## Reading Order

When updating old mental models, use this order:

1. `ARCHITECTURE.md` for the current layer boundaries
2. `DEVELOPMENT.md` for local commands and quality gates
3. `CONTRIBUTING.md` for regression-test and PR expectations
4. `RELEASE_CHECKLIST.md` for versioned slice release procedure
