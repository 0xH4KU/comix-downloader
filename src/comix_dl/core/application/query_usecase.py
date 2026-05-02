"""Application use cases for series lookup and query resolution.

These functions consume a :class:`~comix_dl.sites.base.SiteAdapter`
plus an :class:`~comix_dl.sites.base.Engine` rather than a concrete
service implementation; that keeps the application layer agnostic to
the site currently in use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from comix_dl.core.errors import RemoteApiError

if TYPE_CHECKING:
    from comix_dl.core.models import SearchResult, SeriesInfo
    from comix_dl.sites.base import Engine, SiteAdapter


@dataclass
class SeriesLookupResult:
    """Result of resolving a user-supplied URL or slug."""

    slug: str
    series: SeriesInfo | None
    suggestions: list[SearchResult]


def _fallback_extract_slug(url_or_slug: str) -> str:
    """Last-resort slug extraction when the adapter rejects the input."""
    return url_or_slug.rstrip("/").split("/")[-1]


async def search_series(
    adapter: SiteAdapter,
    engine: Engine,
    query: str,
    *,
    limit: int = 20,
) -> list[SearchResult]:
    """Search for a series via the active site adapter."""
    return await adapter.search(engine, query, limit=limit)


async def load_series(
    adapter: SiteAdapter,
    engine: Engine,
    identifier: str,
) -> SeriesInfo:
    """Load a fully-hydrated series by canonical identifier."""
    return await adapter.get_series(engine, identifier)


async def resolve_series_from_input(
    adapter: SiteAdapter,
    engine: Engine,
    url_or_slug: str,
) -> SeriesLookupResult:
    """Resolve a series from a URL or slug with search-suggestion fallback.

    Asks the adapter to extract a canonical identifier first; if the
    input does not look like a site URL, falls back to using the last
    path segment so plain user-typed slugs still work.
    """
    identifier = adapter.parse_identifier(url_or_slug) or _fallback_extract_slug(url_or_slug)
    try:
        series = await adapter.get_series(engine, identifier)
        return SeriesLookupResult(slug=identifier, series=series, suggestions=[])
    except RemoteApiError:
        suggestions = await adapter.search(engine, identifier, limit=10)
        matched = next((r for r in suggestions if r.slug == identifier), None)
        if matched is not None:
            series = await adapter.get_series(engine, matched.hash_id)
            return SeriesLookupResult(slug=identifier, series=series, suggestions=suggestions)
        return SeriesLookupResult(slug=identifier, series=None, suggestions=suggestions)
