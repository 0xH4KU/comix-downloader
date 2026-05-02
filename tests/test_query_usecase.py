"""Tests for application query use cases."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from comix_dl.core.application.query_usecase import (
    load_series,
    resolve_series_from_input,
    search_series,
)
from comix_dl.core.errors import RemoteApiError
from comix_dl.core.models import SearchResult, SeriesInfo


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


def _make_adapter(parse_result: str | None = None) -> AsyncMock:
    """Build an AsyncMock adapter with the SiteAdapter call surface.

    parse_result controls what ``parse_identifier`` returns; ``None``
    triggers the fallback last-path-segment extraction in the use case.
    """
    adapter = AsyncMock()
    adapter.parse_identifier = MagicMock(return_value=parse_result)
    return adapter


@pytest.mark.asyncio
async def test_search_series_delegates_to_adapter() -> None:
    adapter = _make_adapter()
    engine = MagicMock()
    expected = [SearchResult(title="One", url="https://comix.to/manga/one", slug="one", hash_id="hash-1")]
    adapter.search.return_value = expected

    result = await search_series(adapter, engine, "one", limit=5)

    assert result == expected
    adapter.search.assert_awaited_once_with(engine, "one", limit=5)


@pytest.mark.asyncio
async def test_load_series_delegates_to_adapter() -> None:
    adapter = _make_adapter()
    engine = MagicMock()
    expected = _series()
    adapter.get_series.return_value = expected

    result = await load_series(adapter, engine, "hash-1")

    assert result is expected
    adapter.get_series.assert_awaited_once_with(engine, "hash-1")


@pytest.mark.asyncio
async def test_resolve_series_returns_direct_match() -> None:
    adapter = _make_adapter(parse_result="test-series")
    engine = MagicMock()
    expected = _series()
    adapter.get_series.return_value = expected

    result = await resolve_series_from_input(
        adapter, engine, "https://comix.to/manga/test-series",
    )

    assert result.slug == "test-series"
    assert result.series is expected
    assert result.suggestions == []
    adapter.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_series_uses_search_fallback_for_exact_slug_match() -> None:
    adapter = _make_adapter(parse_result="fallback-series")
    engine = MagicMock()
    expected = _series("Fallback Series")
    adapter.get_series.side_effect = [
        RemoteApiError("not found"),
        expected,
    ]
    adapter.search.return_value = [
        SearchResult(
            title="Fallback Series",
            url="https://comix.to/manga/fallback-series",
            slug="fallback-series",
            hash_id="hash-2",
        ),
    ]

    result = await resolve_series_from_input(adapter, engine, "fallback-series")

    assert result.slug == "fallback-series"
    assert result.series is expected
    assert len(result.suggestions) == 1
    adapter.search.assert_awaited_once_with(engine, "fallback-series", limit=10)
    # Two get_series calls: the failed direct lookup + the recovery via the matched suggestion.
    assert adapter.get_series.await_count == 2
    adapter.get_series.assert_any_await(engine, "hash-2")


@pytest.mark.asyncio
async def test_resolve_series_returns_suggestions_when_exact_match_is_missing() -> None:
    adapter = _make_adapter(parse_result="unknown-slug")
    engine = MagicMock()
    adapter.get_series.side_effect = RemoteApiError("not found")
    adapter.search.return_value = [
        SearchResult(
            title="Maybe This One",
            url="https://comix.to/manga/maybe-this-one",
            slug="maybe-this-one",
            hash_id="hash-3",
        ),
    ]

    result = await resolve_series_from_input(adapter, engine, "unknown-slug")

    assert result.slug == "unknown-slug"
    assert result.series is None
    assert [item.slug for item in result.suggestions] == ["maybe-this-one"]
    # Only the failed direct lookup; no recovery since suggestions did not match.
    assert adapter.get_series.await_count == 1


@pytest.mark.asyncio
async def test_resolve_series_uses_fallback_extraction_when_adapter_returns_none() -> None:
    """If parse_identifier returns None, the use case falls back to the last path segment."""
    adapter = _make_adapter(parse_result=None)
    engine = MagicMock()
    adapter.get_series.return_value = _series()

    result = await resolve_series_from_input(
        adapter, engine, "https://other.example/foo/bar/last-bit/",
    )

    assert result.slug == "last-bit"
    adapter.get_series.assert_awaited_once_with(engine, "last-bit")
