# Forking comix-downloader for a new site

This document is for the case where comix.to is gone (DMCA, rebrand,
re-architecture, infinite Cloudflare loop) and you want to keep the
machinery — Chrome-over-CDP, Cloudflare clearance, page pool,
parallel downloader, atomic resume, PDF/CBZ conversion, structured
logging, history, settings — but point it at a different site.

If you only want to **contribute changes back** to the comix.to
adapter, you do not need this document. Read `CONTRIBUTING.md`
instead.

## The mental model

This repository is two things stacked on top of each other:

```
comix_dl/
  core/    ← framework: site-agnostic. fork keeps this verbatim.
  sites/   ← reference adapter: comix.to. fork replaces this.
```

The boundary is small and explicit:

- **Framework code** in `comix_dl.core` only ever talks to a
  `SiteAdapter` (defined in `comix_dl/sites/base.py`). It does not
  know about HTTP endpoints, JSON schemas, signing rules, or
  Cloudflare quirks beyond "the engine knows how to clear it".
- **Site code** in `comix_dl.sites.<your_site>` implements the
  `SiteAdapter` protocol. Every URL, schema field, and signing IIFE
  lives in this module.

The framework is intentionally written so that *deleting
`sites/comix_to.py` and dropping a new file in its place produces a
working comix-dl-shaped tool for a different site*. The only files
outside `sites/` that mention comix.to today are user-facing strings
(README, CLI help text, install scripts) which belong to the
identity of the project, not its mechanics.

## Step-by-step fork

### 1. Clone and rename

```bash
git clone https://github.com/0xH4KU/comix-downloader.git xxx-downloader
cd xxx-downloader
git remote remove origin   # avoid pushing back to the original repo
```

Pick a name. The framework was built around the literal string
`comix_dl` as the package import path; either keep it (cheaper) or
rename it (cleaner). To rename:

```bash
git mv src/comix_dl src/xxx_dl
# then in your editor, project-wide replace:
#   comix_dl  →  xxx_dl
#   comix-downloader  →  xxx-downloader
#   comix-dl  →  xxx-dl
```

Files that need a hand-edit afterwards:

- `pyproject.toml` (`name`, `[project.scripts]`)
- `README.md`
- `install.sh`, `install.ps1` (paths and printed messages)
- `tests/conftest.py` if it sets the `sys.path` to a literal `src/comix_dl`

**Do not touch** anything inside `core/`. The framework uses relative
imports for cross-module references and absolute imports of the form
`comix_dl.core.X` only at module boundaries — your project-wide
replace handles those.

### 2. Drop the comix.to adapter

```bash
git rm src/xxx_dl/sites/comix_to.py
git rm tests/test_comix_to_adapter.py
```

Edit `src/xxx_dl/sites/__init__.py`. Change the side-effect import at
the bottom from `comix_to` to your new module:

```python
from xxx_dl.sites import your_site as _your_site  # noqa: E402, F401
```

### 3. Write the new adapter

Copy `sites/_template.py` to `sites/<your_site>.py` (e.g.
`sites/manga_x.py`) and fill it in. The template walks through every
required method with TODO markers and inline examples.

The `SiteAdapter` contract is:

| Method | Purpose |
|--|--|
| `name`, `mirrors`, `needs_browser` | Identification + transport hint |
| `matches_url(url)` | Recognise URLs that belong to this site |
| `parse_identifier(url_or_slug)` | Normalise input into a canonical id |
| `on_engine_ready(engine)` | One-time setup after CF clearance |
| `probe_alive(engine)` | Cheap reachability check for mirror selection |
| `search(engine, query, limit)` | Keyword search |
| `get_series(engine, identifier)` | Series detail + chapter list |
| `get_chapter_images(engine, chapter_id)` | Image URLs for one chapter |
| `deduplicate(chapters)` | Site-specific duplicate collapsing |

End the module with:

```python
from xxx_dl.sites import register
adapter = YourSiteAdapter()
register(adapter)
```

### 4. Test it

The framework expects:

- All adapter calls return models from `comix_dl.core.models`
  (`SearchResult`, `ChapterInfo`, etc.). Do not invent new ones —
  the CLI / persistence / reporting layers consume these.
- Adapter functions raise `RemoteApiError` for user-meaningful remote
  failures, never bare `Exception`. The CLI surfaces these directly.
- `on_engine_ready` may register URL transformers via
  `engine.register_url_transformer(iife_js_string)`; each IIFE is
  replayed against every browser page (main + pool). This is how
  the comix.to adapter installs `/chapters` request signing — see
  `sites/comix_to.py:_SIGNING_TRANSFORMER_IIFE` for the full
  reference implementation.

Suggested smoke test:

```bash
# search
xxx-dl search "your test query"

# direct download
xxx-dl download "<a real url on your site>" --chapters 1

# diagnostics
xxx-dl doctor
```

If `doctor` prints your adapter name and a recent successful probe,
the wiring works.

## Things that bite forks

**The signing IIFE extraction is brittle on purpose.** The comix.to
adapter does not implement HMAC by hand — it `eval`s the site's own
JS. That works because comix.to's bundle structure is stable enough
to anchor on string literals (`baseUrl:"https://..."`,
`class n extends Error{response`, `let i=`, `}();`). Your site's
bundle is almost certainly different. Three options:

1. If your site has no signing, delete the entire IIFE block and
   leave `on_engine_ready` empty.
2. If your site signs simply (e.g. fixed header), implement signing
   in pure JS in your transformer — no extraction needed.
3. If your site has obfuscated client-side signing like comix.to,
   adapt `_SIGNING_TRANSFORMER_IIFE` by changing the anchor strings
   and the eval prefix. Test on a fresh page first; the chunk URL
   pattern (`_next/static/chunks`) is Next.js-specific.

Either way, see todo.md for the IIFE hardening backlog item that
adds hash + disk cache + sanity whitelist around the eval call. If
your site is high-stakes you should land that hardening before
shipping.

**Cloudflare assumptions.** The engine assumes a real Chrome session
can solve any CF challenge once, then ride the persisted cookie. If
your site uses a different anti-bot vendor (DataDome, PerimeterX,
hCaptcha standalone) the CF code path will not help; you will need
to extend `core/engines/cdp_browser.py` with the new vendor's
detection and clearance flow. That work is not adapter-level — it
goes into core, alongside the existing CF helpers.

**Schema drift.** `core/models.py` has fields named for comix.to
concepts (`hash_id`, `chapter_id` as `int`). They are intentionally
generic enough to hold whatever your site uses (UUID strings, slugs,
numeric IDs cast to `int`), but if your site cannot fit the int
chapter_id constraint you will need to widen the dataclass and the
`SiteAdapter.get_chapter_images` signature in `sites/base.py`. Both
are framework-level changes — keep them in a separate commit so
future merges with the upstream framework stay clean.

**`needs_browser=False` is not yet supported.** The framework
currently only ships `CdpBrowser` as an engine. If your site is plain
HTTP / no anti-bot, the planned `HttpEngine` (todo.md) is the right
home for that fast path. Until it lands, set `needs_browser=True`
and accept the Chrome dependency.

**Mirror probe vs site reality.** The default `probe_alive`
implementation in `_template.py` does a single `engine.fetch_page`
of the base URL. That is good enough to detect a dead host but does
not verify that the API or signing pipeline actually works. If your
site has a cheap "ping" endpoint, override `probe_alive` to call it
and check for the expected JSON shape — this turns mirror selection
from "the host responds" into "the host actually serves the data
we need".

## What you keep, what you replace

Quick map for fork audits:

| Path | Action |
|--|--|
| `core/` | Keep verbatim |
| `sites/base.py` | Keep verbatim (the contract) |
| `sites/_template.py` | Keep as reference; do not register |
| `sites/comix_to.py` | Delete; replace with your adapter |
| `sites/__init__.py` | Edit one line (the side-effect import) |
| `tests/test_comix_to_adapter.py` | Delete; replace with your adapter's tests |
| `tests/test_*` (other) | Keep — they test the framework |
| `pyproject.toml` | Edit name + entry point |
| `README.md`, install scripts, `MIGRATION.md` | Rewrite for your site |
| `CONTRIBUTING.md` | Edit if your contribution flow differs |
| `FORKING.md` (this file) | Keep as-is so the next fork is easier |

If a `grep -r "comix" core/` ever shows anything other than docstring
examples, that is a framework leak — file an upstream issue or fix
it locally before shipping. The comix.to-shaped strings in `core/`
right now are intentionally constrained to:

- `core/models.py` docstring examples
- `core/cli/__init__.py` argparse description
- `core/engines/cdp_browser.py` probe-path comment (planned move
  in F-5 follow-up)

Everything else in `core/` is genuinely site-agnostic.

## Working with upstream after forking

If you want to keep pulling framework improvements from the parent
repo:

```bash
git remote add upstream https://github.com/0xH4KU/comix-downloader.git
git fetch upstream main
git merge upstream/main   # conflicts will mostly be in sites/
```

Good fork hygiene:

- Confine site-specific commits to `sites/` and metadata files. Then
  framework patches drop in cleanly.
- Re-run `pytest` and `ruff` after any merge — type errors caused
  by signature drift in `SiteAdapter` are the first thing to fail.
- If you widened a model field (e.g. chapter_id to str) for your
  site, expect framework merges to need manual reconciliation. Keep
  those widenings as separate commits so you can replay them on
  top of the fresh upstream.
