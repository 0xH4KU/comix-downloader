"""Tests for application query use cases."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from comix_dl.application.query_usecase import extract_slug, load_series, resolve_series_from_input, search_series
from comix_dl.comix_service import SearchResult, SeriesInfo
from comix_dl.errors import RemoteApiError


def _series(title: str = "Test Series") -> SeriesInfo:
    return SeriesInfo(
        title=title,
        authors=[],
        genres=[],
        description="desc",
        chapters=[],
        url="https://comix.to/manga/test-series",
        hash_id="hash-1",
    )


class TestExtractSlug:
    def test_extracts_last_path_segment_from_url(self) -> None:
        assert extract_slug("https://comix.to/manga/test-series/") == "test-series"

    def test_keeps_plain_slug(self) -> None:
        assert extract_slug("test-series") == "test-series"


@pytest.mark.asyncio
async def test_search_series_delegates_to_service() -> None:
    service = AsyncMock()
    expected = [SearchResult(title="One", url="https://comix.to/manga/one", slug="one", hash_id="hash-1")]
    service.search.return_value = expected

    result = await search_series(service, "one", limit=5)

    assert result == expected
    service.search.assert_awaited_once_with("one", limit=5)


@pytest.mark.asyncio
async def test_load_series_delegates_to_service() -> None:
    service = AsyncMock()
    expected = _series()
    service.get_series.return_value = expected

    result = await load_series(service, "hash-1")

    assert result is expected
    service.get_series.assert_awaited_once_with("hash-1")


@pytest.mark.asyncio
async def test_resolve_series_returns_direct_match() -> None:
    service = AsyncMock()
    expected = _series()
    service.get_series_by_slug.return_value = expected

    result = await resolve_series_from_input(service, "https://comix.to/manga/test-series")

    assert result.slug == "test-series"
    assert result.series is expected
    assert result.suggestions == []
    service.search.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_series_uses_search_fallback_for_exact_slug_match() -> None:
    service = AsyncMock()
    expected = _series("Fallback Series")
    service.get_series_by_slug.side_effect = RemoteApiError("not found")
    service.search.return_value = [
        SearchResult(
            title="Fallback Series",
            url="https://comix.to/manga/fallback-series",
            slug="fallback-series",
            hash_id="hash-2",
        )
    ]
    service.get_series.return_value = expected

    result = await resolve_series_from_input(service, "fallback-series")

    assert result.slug == "fallback-series"
    assert result.series is expected
    assert len(result.suggestions) == 1
    service.search.assert_awaited_once_with("fallback-series", limit=10)
    service.get_series.assert_awaited_once_with("hash-2")


@pytest.mark.asyncio
async def test_resolve_series_returns_suggestions_when_exact_match_is_missing() -> None:
    service = AsyncMock()
    service.get_series_by_slug.side_effect = RemoteApiError("not found")
    service.search.return_value = [
        SearchResult(
            title="Maybe This One",
            url="https://comix.to/manga/maybe-this-one",
            slug="maybe-this-one",
            hash_id="hash-3",
        )
    ]

    result = await resolve_series_from_input(service, "unknown-slug")

    assert result.slug == "unknown-slug"
    assert result.series is None
    assert [item.slug for item in result.suggestions] == ["maybe-this-one"]
    service.get_series.assert_not_called()
