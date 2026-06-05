# Smaller Boundaries Design

## Goal

Reduce the size and coupling of the comix.to implementation while preserving the user-facing CLI commands and download behavior.

## Scope

This refactor keeps `comix-dl search`, `download`, `info`, `list`, `clean`, `history`, `doctor`, and `settings` behavior compatible. It may adjust internal protocols and session wiring where those changes remove stale abstractions or site-specific code from framework modules.

## Architecture

The comix.to adapter will become a thin coordinator. URL handling, JSON payload parsing, chapter deduplication, API access/cache, Cloudflare service probing, and scrambled image rendering will live in focused modules under `src/comix_dl/sites/`.

`CdpBrowser` remains the browser transport for Cloudflare-aware JSON and byte requests, but it will stop hardcoding comix.to endpoint probes and comix.to canvas-rendering JavaScript. Site adapters will configure those site-specific hooks before the first request, then run existing `on_engine_ready` setup after clearance.

Application sessions will use a session-scoped adapter instance when the adapter supplies one. This keeps per-run caches from leaking through the process-level registry singleton.

## Components

- `comix_to_url.py`: host matching, identifier parsing, absolute URL construction, title slug extraction.
- `comix_to_parsing.py`: response unwrapping, primitive coercion, taxonomy/person parsing, search/chapter/page parsing.
- `comix_to_dedup.py`: pure chapter deduplication and human-readable `DedupDecision` generation.
- `comix_to_api.py`: comix.to API request orchestration and per-session chapter payload cache.
- `comix_to_browser.py`: comix.to browser hooks for service access probing and scrambled image rendering.
- `comix_to.py`: adapter facade that wires the focused helpers into the framework `SiteAdapter` contract.

## Error Handling

Remote API errors continue to surface as `RemoteApiError` at the adapter boundary. Image payload failures remain recoverable and return `None` from `get_chapter_images`. Browser-level Cloudflare and timeout errors stay typed in `comix_dl.core.errors`.

## Testing

Existing tests continue to cover end-to-end adapter behavior. New focused tests pin the helper modules directly so future maintainers can reason about each unit without constructing the full adapter.

The refactor uses red-green steps:

- Add tests that import the new helper modules and fail while modules are missing.
- Move code into the new modules until those tests pass.
- Update adapter and browser tests for the new hook registration behavior.
- Run the full suite before claiming completion.

## Documentation

Update architecture docs to remove stale `comix_service.py` references. Add a contributor onboarding guide and a site adapter development guide so new maintainers can find the main seams quickly.
