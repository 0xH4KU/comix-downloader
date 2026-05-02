"""Framework / site contract.

This module defines two protocols the framework relies on but does not
implement:

* :class:`Engine` — the transport boundary used by adapters to read
  bytes / JSON / HTML from a remote site. Implemented by the framework
  (currently ``CdpBrowser`` over Playwright; later an ``HttpEngine``
  for sites that do not require a real browser).
* :class:`SiteAdapter` — the per-site logic that maps a user-supplied
  URL or query to concrete search / series / chapter operations, and
  injects any site-specific behaviour (request signing, dedup rules,
  Cloudflare quirks) into the engine.

The framework code (CLI, application use cases, downloader, converter)
should depend only on these protocols. A site adapter implementation
lives under :mod:`comix_dl.sites` and registers itself at import time
via :func:`comix_dl.sites.register`.

Design notes
------------

* ``parse_identifier`` returns a plain ``str`` rather than a wrapper
  type because adapters use heterogeneous identifier schemes (slug,
  hash_id, UUID). Wrapping adds no value at this stage.
* ``chapter_id`` is typed as ``int`` for backwards compatibility with
  the comix.to adapter. A future site whose chapter IDs are strings
  should widen this in the protocol; the change is intentionally
  deferred until a concrete second adapter exists.
* ``deduplicate`` is owned by the adapter because dedup rules are
  highly site-specific. The comix.to implementation deduplicates by
  chapter number + language + subtitle; another site might dedup by a
  single canonical chapter ID and never need any of that logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from comix_dl.comix_service import (
        ChapterImages,
        ChapterInfo,
        DedupDecision,
        SearchResult,
        SeriesInfo,
    )


@runtime_checkable
class Engine(Protocol):
    """Transport boundary used by site adapters.

    Implementations route requests to the underlying browser/HTTP
    plumbing while applying any registered request transformers (e.g.
    URL signing). Adapters MUST NOT assume which concrete
    implementation is in use; in particular they should not rely on
    the existence of a Chrome page pool, since the future
    ``HttpEngine`` will not have one.

    Lifecycle
    ---------

    The framework guarantees:

    * ``start()`` (if applicable) has run before any request method is
      called by the adapter.
    * The adapter's :meth:`SiteAdapter.on_engine_ready` has been
      awaited exactly once before the first content request, allowing
      the adapter to install signing functions, prime cookies, or run
      any other one-time setup.
    """

    async def get_json(self, url: str) -> dict[str, object]:
        """GET *url* and return a parsed JSON object."""
        ...

    async def get_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        """GET *url* and return the raw response body.

        ``referer`` is honoured when the underlying transport supports
        it (the CDP engine attaches it as an HTTP header inside the
        in-page fetch).
        """
        ...

    async def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        """POST *payload* as JSON to *url* and return the JSON response."""
        ...

    async def fetch_page(self, url: str) -> str:
        """Navigate to *url* and return the rendered HTML.

        Mainly useful for adapters that need to inspect or extract
        embedded JS / data attributes from the site itself.
        """
        ...


@runtime_checkable
class SiteAdapter(Protocol):
    """Per-site behaviour contract.

    Each adapter is a small, self-contained module that knows how to
    talk to one specific site. The framework holds the engine, runs
    the CLI, manages downloads and conversion; the adapter supplies
    the site-specific knowledge those layers need.

    Required attributes
    -------------------

    name
        Stable, human-readable identifier (e.g. ``"comix.to"``). Used
        by the CLI ``--site`` flag and by registry lookups.
    mirrors
        Ordered list of base URLs that point at the same logical
        backend. The framework probes these in order at session
        start and uses the first one that :meth:`probe_alive`
        accepts.
    needs_browser
        ``True`` when the site requires a real Chrome session for any
        of search / series / images (Cloudflare, anti-bot, dynamic
        signing). The framework uses this to decide whether to spin
        up the ``CdpBrowser`` engine or fall back to the lightweight
        HTTP engine.
    """

    name: str
    mirrors: list[str]
    needs_browser: bool

    # -- URL handling -------------------------------------------------

    def matches_url(self, url: str) -> bool:
        """Return whether *url* belongs to this site.

        Implementations should match across all known mirror domains
        and tolerate optional ``http(s)://`` and ``www.`` prefixes.
        """
        ...

    def parse_identifier(self, url_or_slug: str) -> str | None:
        """Extract a canonical site-specific identifier.

        Accepts either a full URL or a bare slug / hash. Returns
        ``None`` when the input does not look like a valid identifier
        for this site, allowing the CLI to fall through to a search
        flow.
        """
        ...

    # -- Lifecycle hooks ----------------------------------------------

    async def on_engine_ready(self, engine: Engine) -> None:
        """Run any one-time setup against an engine that just came up.

        Typical uses: install URL signing transformers, warm up
        cookies, fetch a CSRF token. Called exactly once per engine
        lifetime, after the engine has cleared any required
        challenges and before the first content request.
        """
        ...

    async def probe_alive(self, engine: Engine) -> bool:
        """Return whether the active mirror is currently reachable.

        Used during session startup to choose an active mirror from
        :attr:`mirrors`. Implementations should be cheap (a single
        HEAD-like request is preferred) and must not raise on
        ordinary network failures.
        """
        ...

    # -- Content operations -------------------------------------------

    async def search(
        self,
        engine: Engine,
        query: str,
        *,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Run a keyword search and return at most *limit* hits."""
        ...

    async def get_series(self, engine: Engine, identifier: str) -> SeriesInfo:
        """Fetch the full series object for a canonical *identifier*."""
        ...

    async def get_chapter_images(self, engine: Engine, chapter_id: int) -> ChapterImages:
        """Fetch the ordered image URL list for one chapter."""
        ...

    # -- Site-specific behaviour --------------------------------------

    def deduplicate(
        self,
        chapters: list[ChapterInfo],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        """Collapse duplicate chapters by site-specific rules.

        Returns the deduplicated list plus a list of human-readable
        :class:`DedupDecision` records that the CLI surfaces before
        download. Adapters that have no duplicate problem should
        return ``(chapters, [])``.
        """
        ...


__all__ = [
    "Engine",
    "SiteAdapter",
]
