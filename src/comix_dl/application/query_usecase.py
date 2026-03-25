"""Application use cases for series lookup and query resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from comix_dl.errors import RemoteApiError

if TYPE_CHECKING:
    from comix_dl.comix_service import ComixService, SearchResult, SeriesInfo


@dataclass
class SeriesLookupResult:
    """Result of resolving a user-supplied URL or slug."""

    slug: str
    series: SeriesInfo | None
    suggestions: list[SearchResult]


def extract_slug(url_or_slug: str) -> str:
    """Normalize a user-provided manga URL or slug into a slug token."""
    return url_or_slug.rstrip("/").split("/")[-1]


async def search_series(
    service: ComixService,
    query: str,
    *,
    limit: int = 20,
) -> list[SearchResult]:
    """Search for a series by keyword."""
    return await service.search(query, limit=limit)


async def load_series(service: ComixService, hash_id: str) -> SeriesInfo:
    """Load a fully-hydrated series by hash ID."""
    return await service.get_series(hash_id)


async def resolve_series_from_input(service: ComixService, url_or_slug: str) -> SeriesLookupResult:
    """Resolve a series from a URL or slug with search suggestions fallback."""
    slug = extract_slug(url_or_slug)
    try:
        series = await service.get_series_by_slug(slug)
        return SeriesLookupResult(slug=slug, series=series, suggestions=[])
    except RemoteApiError:
        suggestions = await service.search(slug, limit=10)
        matched = next((result for result in suggestions if result.slug == slug), None)
        if matched is not None:
            series = await service.get_series(matched.hash_id)
            return SeriesLookupResult(slug=slug, series=series, suggestions=suggestions)
        return SeriesLookupResult(slug=slug, series=None, suggestions=suggestions)

