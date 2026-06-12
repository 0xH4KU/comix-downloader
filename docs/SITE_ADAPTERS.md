# Site Adapter Guide

Site adapters translate one remote site into the framework models used by the CLI and downloader.

## Contract

Adapters implement `SiteAdapter` from `src/comix_dl/sites/base.py`.

Required responsibilities:

- `matches_url(url)`: claim URLs for this site
- `parse_identifier(url_or_slug)`: extract the canonical site identifier
- `configure_engine(engine)`: install pre-clearance browser hooks
- `on_engine_ready(engine)`: install post-clearance setup such as request signing
- `probe_alive(engine)`: verify the selected mirror is usable
- `search(engine, query, limit=20)`: return `SearchResult` values
- `get_series(engine, identifier)`: return `SeriesInfo`
- `get_chapter_images(engine, chapter_id)`: return `ChapterImages` or `None`
- `deduplicate(chapters)`: apply site-specific duplicate rules

Adapters may provide `new_session()`. If present, `open_application_session()` uses it so caches and other per-run state do not leak through the registry singleton.

## Engine Hooks

`configure_engine(engine)` runs before Cloudflare clearance. Use it for hooks that must exist while clearance is being verified:

- `engine.register_service_access_probe(probe)`
- `engine.register_scrambled_image_renderer(renderer)`

`on_engine_ready(engine)` runs after clearance and page pool setup. Use it for request transformers and API client setup:

- `engine.register_url_transformer(iife_js)`

The comix.to adapter registers:

- a service probe from `comix_to_browser.py`
- a DOM-first scrambled image renderer from `comix_to_browser.py`, with a legacy renderer fallback
- a packaged frontend API client hook from `sites/assets/comix_api_client.js`

## comix.to Module Map

- `comix_to.py`: adapter facade and compatibility wrappers
- `comix_to_api.py`: API request orchestration and session-scoped cache
- `comix_to_parsing.py`: raw JSON to framework models
- `comix_to_dedup.py`: duplicate chapter rules and reports
- `comix_to_url.py`: URL matching and identifier parsing
- `comix_to_browser.py`: Cloudflare API probe, reader-DOM scrambled capture, and legacy renderer fallback

## Model Expectations

Use framework models from `core/models.py`:

- `SearchResult.hash_id` stores the canonical site identifier even when the underlying site calls it something else.
- `ChapterInfo.chapter_id` is currently `int` for compatibility with comix.to.
- `ChapterImages.pages` carries `ChapterPage` metadata for scrambled images, including the reader URL and zero-based page index needed for DOM capture.
- `DedupDecision` should explain why variants were dropped in terms a user can understand.

## Testing Expectations

For adapter changes, add focused tests that do not need live network access.

Good tests:

- fixture-driven API JSON parsing
- direct helper tests for URL parsing and deduplication
- fake engine tests for search, series lookup, chapter images, and fallback behavior
- browser hook tests with mocked `CdpBrowser` methods

Avoid live comix.to calls in unit tests. Use smoke/manual testing separately when remote behavior has changed.

## Adding Another Site

Start from `src/comix_dl/sites/_template.py`, then:

1. Create a focused adapter module under `src/comix_dl/sites/`.
2. Keep site-specific API parsing and browser hooks outside `core/`.
3. Register the adapter through `sites.register(adapter)`.
4. Add registry and adapter tests.
5. Update docs if the new adapter changes CLI behavior or supported sites.
