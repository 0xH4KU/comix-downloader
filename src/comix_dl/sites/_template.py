"""SiteAdapter starter template — copy this when adding a new site.

How to use this file:

1. Copy the file to ``sites/<your_site>.py`` (e.g. ``sites/manga_x.py``).
2. Rename ``ExampleAdapter`` to something descriptive
   (e.g. ``MangaXAdapter``).
3. Replace the ``name`` and ``mirrors`` constants with the real site.
4. Implement each method as documented; remove TODO markers as you go.
5. Add ``register(adapter)`` at module bottom (the comix.to adapter
   does the same).
6. Wire the new module into ``sites/__init__.py`` so the registry
   picks it up at import time. Forks that fully replace comix.to
   typically delete the old import and add the new one in the same
   spot.
7. Drop ``sites/comix_to.py`` if this is a fork that no longer
   targets comix.to; the framework will pick up your single
   registered adapter automatically.

This file is named with a leading underscore so it does not register
itself as a runnable adapter — it is reference material only.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from comix_dl.core.errors import RemoteApiError

if TYPE_CHECKING:
    from comix_dl.core.models import (
        ChapterImages,
        ChapterInfo,
        DedupDecision,
        SearchResult,
        SeriesInfo,
    )
    from comix_dl.sites.base import Engine


logger = logging.getLogger(__name__)


# Hostnames the adapter claims. Used by ``matches_url`` below.
# Keep this in sync with ``mirrors`` so domain rotation works.
_HOST_PATTERN = re.compile(r"^(?:www\.)?example\.com$", re.IGNORECASE)


class ExampleAdapter:
    """Reference SiteAdapter implementation.

    Required attributes
    -------------------
    ``name``
        Stable, human-readable identifier shown by ``--site`` and the
        doctor command.
    ``mirrors``
        Ordered list of base URLs that point at the same logical
        backend. The framework probes them in order via
        :meth:`probe_alive` and remembers the first reachable one.
    ``needs_browser``
        ``True`` when the site requires Chrome (Cloudflare, anti-bot,
        dynamic signing). ``False`` enables the future HTTP fast path.
    """

    name = "example.com"
    needs_browser = True

    def __init__(self) -> None:
        # ``mirrors`` is an instance attribute so it satisfies the
        # SiteAdapter protocol's instance-variable expectation. The
        # default first entry is the canonical origin; add backup
        # domains here in priority order.
        self.mirrors: list[str] = ["https://example.com"]
        # Adapter-private state goes here. The framework constructs
        # the adapter once per process, so caches stay alive across
        # CLI commands within a single run.
        self._chapter_payload_cache: dict[int, dict[str, object] | None] = {}

    # -- URL handling -------------------------------------------------------

    def matches_url(self, url: str) -> bool:
        """Return whether *url* points at one of this site's mirrors."""
        try:
            parsed = urlparse(url.strip())
        except (TypeError, ValueError):
            return False
        return bool(_HOST_PATTERN.match(parsed.hostname or ""))

    def parse_identifier(self, url_or_slug: str) -> str | None:
        """Extract a canonical site-specific identifier.

        Accept either a full URL or a bare slug. Return ``None`` for
        empty or non-matching input so the CLI can fall through to a
        keyword search.
        """
        token = url_or_slug.strip()
        if not token:
            return None
        if "://" in token:
            try:
                parsed = urlparse(token)
            except (TypeError, ValueError):
                return None
            if not _HOST_PATTERN.match(parsed.hostname or ""):
                return None
            # TODO: replace ``manga`` with this site's path prefix.
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0].lower() == "manga":
                return parts[1] or None
            return token.rstrip("/").split("/")[-1] or None
        return token

    # -- Lifecycle hooks ----------------------------------------------------

    async def on_engine_ready(self, engine: Engine) -> None:
        """Run one-time setup against an engine that just came up.

        Called exactly once per session, after the engine has cleared
        any required challenges and warmed the page pool, before the
        first content request.

        Common uses:

        - Inject site-specific request signing via
          ``engine.register_url_transformer`` (see ``sites/comix_to.py``
          for a full IIFE-extraction example).
        - Pre-fetch session cookies, CSRF tokens, or feature flags.

        Trivial sites with no signing or auth can leave this method
        empty.
        """
        # Example: register a URL transformer that adds a static auth
        # header. The IIFE runs on every page (main + pool); each
        # callable in window.__comixUrlTransformers gets (method, url)
        # and returns either a (possibly modified) url or undefined.
        #
        # engine.register_url_transformer("""
        #     (function() {
        #         window.__comixUrlTransformers = window.__comixUrlTransformers || [];
        #         window.__comixUrlTransformers.push(function(method, url) {
        #             // ... rewrite url here ...
        #             return url;
        #         });
        #     })();
        # """)
        return None

    async def probe_alive(self, engine: Engine) -> bool:
        """Return whether the active mirror is currently reachable.

        Used during session startup to choose an active mirror from
        :attr:`mirrors`. Implementations should be cheap (a single
        request is preferred) and must not raise on ordinary network
        failures — return ``False`` instead.
        """
        for mirror in self.mirrors:
            try:
                await engine.fetch_page(mirror)
            except Exception as exc:
                logger.debug("Probe of %s failed: %s", mirror, exc)
                continue
            return True
        return False

    # -- Content operations -------------------------------------------------

    async def search(
        self,
        engine: Engine,
        query: str,
        *,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Run a keyword search and return at most *limit* hits.

        Use ``await engine.get_json(api_url)`` for JSON endpoints and
        ``await engine.fetch_page(html_url)`` when the site only
        exposes HTML. Wrap remote failures in
        :class:`~comix_dl.core.errors.RemoteApiError` with an
        actionable message.
        """
        # TODO: implement against your site's search endpoint.
        # base = self.mirrors[0]
        # api_url = f"{base}/api/search?q={quote(query)}&limit={limit}"
        # try:
        #     resp = await engine.get_json(api_url)
        # except Exception as exc:
        #     raise RemoteApiError(f"Search for {query!r} failed: {exc}") from exc
        # return [SearchResult(...) for item in resp["items"]]
        raise RemoteApiError("ExampleAdapter.search is not implemented yet.")

    async def get_series(self, engine: Engine, identifier: str) -> SeriesInfo:
        """Resolve *identifier* into a fully-hydrated series.

        ``identifier`` is whatever :meth:`parse_identifier` returned.
        Adapters typically:

        1. Hit the series-detail endpoint for metadata.
        2. Page through chapter listings.
        3. Optionally fill missing per-chapter ``image_count`` so
           :meth:`deduplicate` can run synchronously afterwards.
        """
        # TODO: implement against your site's series endpoint.
        raise RemoteApiError(f"ExampleAdapter.get_series({identifier!r}) is not implemented yet.")

    async def get_chapter_images(
        self,
        engine: Engine,
        chapter_id: int,
    ) -> ChapterImages | None:
        """Fetch the ordered image URL list for *chapter_id*.

        Returning ``None`` is the "missing images" signal — the
        framework treats it as a per-chapter failure rather than a
        hard error, so chapters without resolved images are skipped
        gracefully.
        """
        # TODO: implement against your site's chapter endpoint.
        return None

    # -- Site-specific behaviour --------------------------------------------

    def deduplicate(
        self,
        chapters: list[ChapterInfo],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        """Collapse duplicate chapters by site-specific rules.

        Adapters that have no duplicate problem can simply return
        ``(chapters, [])``. The comix.to adapter dedups by chapter
        number + language + subtitle; your site may use a single
        canonical chapter ID and not need any of that.

        The returned :class:`DedupDecision` list is shown in the CLI
        before download begins so users can see exactly which
        variants were dropped.
        """
        # Trivial pass-through: trust upstream IDs are already unique.
        return chapters, []


# Uncomment to make this template adapter live (do not commit this on
# real adapters — only on copies of this file renamed to a real site
# module). The framework's registry will then pick it up.
#
# from comix_dl.sites import register
# adapter = ExampleAdapter()
# register(adapter)
