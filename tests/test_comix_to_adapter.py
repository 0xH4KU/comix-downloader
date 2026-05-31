"""Tests for the comix.to SiteAdapter — parsing, dedup, search, and chapter fetch."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from comix_dl.core.errors import BrowserTimeoutError, Http403Error, RemoteApiError
from comix_dl.core.models import (
    ChapterImages,
    ChapterInfo,
    SearchResult,
    SeriesInfo,
)
from comix_dl.sites.base import SiteAdapter
from comix_dl.sites.comix_to import ComixToAdapter

if TYPE_CHECKING:
    from unittest.mock import AsyncMock


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestSiteAdapterConformance:
    """Pin ComixToAdapter to the SiteAdapter contract.

    A regression here means the adapter has drifted from the framework
    contract — typically because someone renamed or removed a method
    that the CLI / application layers depend on. Catch it at import
    time rather than at the first call site failure.
    """

    def test_satisfies_site_adapter_protocol(self) -> None:
        adapter = ComixToAdapter()
        assert isinstance(adapter, SiteAdapter)

    def test_required_attributes_exist(self) -> None:
        adapter = ComixToAdapter()
        assert isinstance(adapter.name, str) and adapter.name
        assert isinstance(adapter.mirrors, list) and adapter.mirrors
        assert all(isinstance(m, str) and m.startswith("https://") for m in adapter.mirrors)
        assert isinstance(adapter.needs_browser, bool)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chapter(
    number: object,
    chapter_id: int = 0,
    name: str = "",
    image_count: int = 0,
    language: str = "en",
) -> ChapterInfo:
    label = f"Chapter {number}"
    if name:
        label += f" - {name}"
    return ChapterInfo(
        title=label,
        chapter_id=chapter_id or 100,
        number=number,
        name=name,
        language=language,
        image_count=image_count,
    )


def _adapter() -> ComixToAdapter:
    return ComixToAdapter()


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------

class TestOnEngineReady:
    async def test_registers_signing_transformer(self) -> None:
        adapter = _adapter()
        registered: list[str] = []

        class FakeEngine:
            def register_url_transformer(self, iife_js: str) -> None:
                registered.append(iife_js)

        await adapter.on_engine_ready(FakeEngine())

        assert len(registered) == 1
        iife = registered[0]
        # IIFE must include the signing-extraction logic and push a
        # transformer onto the global array; both are required for
        # /chapters requests to be signed.
        assert "window.__comixSign" in iife
        assert "window.__comixUrlTransformers" in iife
        assert "/chapters" in iife


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

class TestUrlHandling:
    def test_matches_canonical_host(self) -> None:
        a = _adapter()
        assert a.matches_url("https://comix.to/manga/x") is True
        assert a.matches_url("https://www.comix.to/manga/x") is True
        assert a.matches_url("https://other.example/manga/x") is False
        assert a.matches_url("not a url") is False

    def test_parse_identifier_from_url(self) -> None:
        a = _adapter()
        assert a.parse_identifier("https://comix.to/manga/test-series") == "test-series"
        assert a.parse_identifier("https://comix.to/manga/test-series/") == "test-series"

    def test_parse_identifier_from_bare_slug(self) -> None:
        a = _adapter()
        assert a.parse_identifier("test-series") == "test-series"

    def test_parse_identifier_returns_none_for_other_hosts(self) -> None:
        a = _adapter()
        assert a.parse_identifier("https://other.example/manga/x") is None

    def test_parse_identifier_empty_returns_none(self) -> None:
        a = _adapter()
        assert a.parse_identifier("") is None
        assert a.parse_identifier("   ") is None


# ---------------------------------------------------------------------------
# _parse_chapter_items
# ---------------------------------------------------------------------------

class TestParseChapterItems:
    def test_basic_parsing(self) -> None:
        items = [
            {"chapter_id": 100, "number": 1, "name": "Intro", "language": "en", "pages_count": 20},
            {"chapter_id": 200, "number": 2, "name": "", "language": "en", "pages_count": 15},
        ]
        result = ComixToAdapter._parse_chapter_items(items)
        assert len(result) == 2
        assert result[0].chapter_id == 100
        assert result[0].number == "1"
        assert result[0].name == "Intro"
        assert result[0].image_count == 20
        assert result[0].title == "Chapter 1 - Intro"
        assert result[1].title == "Chapter 2"

    def test_empty_list(self) -> None:
        assert ComixToAdapter._parse_chapter_items([]) == []

    def test_non_dict_items_skipped(self) -> None:
        items = ["not a dict", 42, None]  # type: ignore[list-item]
        assert ComixToAdapter._parse_chapter_items(items) == []

    def test_missing_chapter_id_skipped(self) -> None:
        items = [{"number": 1, "name": "test"}]  # no chapter_id → defaults to 0 → skipped
        assert ComixToAdapter._parse_chapter_items(items) == []

    def test_non_int_pages_count(self) -> None:
        items = [{"chapter_id": 1, "number": 1, "pages_count": "invalid"}]
        result = ComixToAdapter._parse_chapter_items(items)
        assert len(result) == 1
        assert result[0].image_count == 0

    def test_invalid_chapter_id_is_skipped(self) -> None:
        items = [
            {"chapter_id": "not-an-int", "number": 1, "pages_count": 12},
            {"chapter_id": 2, "number": 2, "pages_count": 10},
        ]

        result = ComixToAdapter._parse_chapter_items(items)

        assert len(result) == 1
        assert result[0].chapter_id == 2


# ---------------------------------------------------------------------------
# deduplicate (now sync)
# ---------------------------------------------------------------------------

class TestDeduplicate:
    def test_no_duplicates_unchanged(self) -> None:
        chapters = [_make_chapter(1, 100), _make_chapter(2, 200), _make_chapter(3, 300)]
        result, _ = _adapter().deduplicate(chapters)
        assert len(result) == 3

    def test_empty_list(self) -> None:
        result, decisions = _adapter().deduplicate([])
        assert result == []
        assert decisions == []

    def test_same_number_different_name_kept(self) -> None:
        chapters = [
            _make_chapter(0, 100, name="Volume 11", image_count=20),
            _make_chapter(0, 200, name="Volume 12", image_count=25),
        ]
        result, _ = _adapter().deduplicate(chapters)
        assert len(result) == 2

    def test_true_duplicates_keeps_most_images(self) -> None:
        chapters = [
            _make_chapter(5, 100, image_count=10),
            _make_chapter(5, 200, image_count=25),
            _make_chapter(5, 300, image_count=15),
        ]
        result, _ = _adapter().deduplicate(chapters)
        assert len(result) == 1
        assert result[0].chapter_id == 200

    def test_same_number_same_name_different_language_kept(self) -> None:
        chapters = [
            _make_chapter(7, 100, name="Special", image_count=20, language="en"),
            _make_chapter(7, 200, name="Special", image_count=25, language="es"),
        ]
        result, _ = _adapter().deduplicate(chapters)
        assert len(result) == 2
        assert {ch.language for ch in result} == {"en", "es"}

    def test_same_number_unnamed_different_language_kept(self) -> None:
        chapters = [
            _make_chapter(8, 100, image_count=20, language="en"),
            _make_chapter(8, 200, image_count=25, language="jp"),
        ]
        result, _ = _adapter().deduplicate(chapters)
        assert len(result) == 2
        assert {ch.language for ch in result} == {"en", "jp"}

    def test_same_number_same_name_same_language_keeps_most_images(self) -> None:
        chapters = [
            _make_chapter(9, 100, name="Finale", image_count=10, language="en"),
            _make_chapter(9, 200, name="Finale", image_count=30, language="en"),
        ]
        result, _ = _adapter().deduplicate(chapters)
        assert len(result) == 1
        assert result[0].chapter_id == 200

    def test_result_sorted_by_number(self) -> None:
        chapters = [_make_chapter(3, 300), _make_chapter(1, 100), _make_chapter(2, 200)]
        result, _ = _adapter().deduplicate(chapters)
        assert [ch.number for ch in result] == ["1", "2", "3"]

    def test_decimal_numbers_sort_naturally(self) -> None:
        chapters = [
            _make_chapter("10.5", 300),
            _make_chapter("2", 100),
            _make_chapter("2.1", 200),
        ]
        result, _ = _adapter().deduplicate(chapters)
        assert [ch.number for ch in result] == ["2", "2.1", "10.5"]

    def test_report_records_kept_and_dropped_variants(self) -> None:
        chapters = [
            _make_chapter(5, 100, image_count=10),
            _make_chapter(5, 200, image_count=25),
            _make_chapter(5, 300, image_count=15),
        ]
        result, decisions = _adapter().deduplicate(chapters)
        assert len(result) == 1
        assert len(decisions) == 1
        assert decisions[0].chapter_number == "5"
        assert "highest page count" in decisions[0].reason
        assert "id=200" in decisions[0].kept[0]
        assert {item.split("id=")[1].rstrip("]") for item in decisions[0].dropped} == {"100", "300"}

    def test_report_explains_unnamed_variants_dropped_when_named_exists(self) -> None:
        chapters = [
            _make_chapter(7, 100, name="Special", image_count=20, language="en"),
            _make_chapter(7, 200, image_count=15, language="en"),
        ]
        result, decisions = _adapter().deduplicate(chapters)
        assert len(result) == 1
        assert len(decisions) == 1
        assert "named variants exist" in decisions[0].reason
        assert "Special" in decisions[0].kept[0]
        assert "id=200" in decisions[0].dropped[0]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestSearch:
    async def test_parses_search_response(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "items": [
                    {"title": "One Piece", "slug": "one-piece", "hash_id": "abc123"},
                    {"title": "Naruto", "slug": "naruto", "hash_id": "def456"},
                ],
            },
        }
        results = await _adapter().search(mock_browser, "test")
        assert len(results) == 2
        assert results[0].title == "One Piece"
        assert results[0].hash_id == "abc123"
        assert results[0].slug == "one-piece"
        assert "one-piece" in results[0].url

    async def test_empty_result(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {"result": {"items": []}}
        results = await _adapter().search(mock_browser, "nonexistent")
        assert results == []

    async def test_missing_hash_id_skipped(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "items": [
                    {"title": "No Hash", "slug": "no-hash"},
                    {"title": "Has Hash", "slug": "has-hash", "hash_id": "abc"},
                ],
            },
        }
        results = await _adapter().search(mock_browser, "test")
        assert len(results) == 1
        assert results[0].title == "Has Hash"

    async def test_api_error_raises_remote_api_error(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.side_effect = Exception("Network error")
        with pytest.raises(RemoteApiError, match=r"Search for 'test' failed: Network error"):
            await _adapter().search(mock_browser, "test")

    async def test_403_error_raises_remote_api_error(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.side_effect = Http403Error("HTTP 403 Forbidden")
        with pytest.raises(
            RemoteApiError,
            match=(
                r"Search for 'test' failed: API request was blocked by HTTP 403\. "
                r"Cloudflare clearance may have expired\."
            ),
        ):
            await _adapter().search(mock_browser, "test")

    async def test_403_error_logs_clearance_hint(
        self, mock_browser: AsyncMock, caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_browser.get_json.side_effect = Http403Error("HTTP 403 Forbidden")
        with pytest.raises(RemoteApiError):
            await _adapter().search(mock_browser, "test")
        assert "Cloudflare clearance may have expired." in caplog.text


# ---------------------------------------------------------------------------
# get_chapter_images
# ---------------------------------------------------------------------------

class TestGetChapterImages:
    async def test_parses_image_urls(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "number": 5,
                "name": "The Beginning",
                "images": [
                    {"url": "https://cdn.example.com/img1.webp"},
                    {"url": "https://cdn.example.com/img2.webp"},
                    {"url": "https://cdn.example.com/img3.webp"},
                ],
            },
        }
        result = await _adapter().get_chapter_images(mock_browser, 12345)
        assert result is not None
        assert len(result.image_urls) == 3
        assert result.chapter_label == "Chapter 5 - The Beginning"

    async def test_normalizes_chapter_label_number(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "number": 1.0,
                "name": "",
                "images": [{"url": "https://cdn.example.com/img1.webp"}],
            },
        }
        result = await _adapter().get_chapter_images(mock_browser, 12345)
        assert result is not None
        assert result.chapter_label == "Chapter 1"
        assert result.title == "Chapter 1"

    async def test_empty_images_returns_none(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {"number": 1, "name": "", "images": []},
        }
        result = await _adapter().get_chapter_images(mock_browser, 12345)
        assert result is None

    async def test_invalid_image_entries_filtered(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "number": 1,
                "name": "",
                "images": [
                    {"url": "https://valid.com/img.webp"},
                    {"not_url": "missing"},
                    "not_a_dict",
                    {"url": ""},
                ],
            },
        }
        result = await _adapter().get_chapter_images(mock_browser, 12345)
        assert result is not None
        assert len(result.image_urls) == 1

    async def test_api_error_returns_none(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.side_effect = Exception("timeout")
        result = await _adapter().get_chapter_images(mock_browser, 12345)
        assert result is None

    async def test_timeout_logs_clear_error(
        self, mock_browser: AsyncMock, caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_browser.get_json.side_effect = BrowserTimeoutError(
            "Reading response timed out after 5000ms.",
        )
        result = await _adapter().get_chapter_images(mock_browser, 12345)
        assert result is None
        assert "API request timed out." in caplog.text

    async def test_reuses_cached_chapter_payload(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "number": 5,
                "name": "The Beginning",
                "images": [
                    {"url": "https://cdn.example.com/img1.webp"},
                    {"url": "https://cdn.example.com/img2.webp"},
                ],
            },
        }
        adapter = _adapter()
        image_count = await adapter._get_image_count(mock_browser, 12345)
        result = await adapter.get_chapter_images(mock_browser, 12345)
        assert image_count == 2
        assert result is not None
        assert result.chapter_label == "Chapter 5 - The Beginning"
        assert mock_browser.get_json.await_count == 1


# ---------------------------------------------------------------------------
# get_series error path
# ---------------------------------------------------------------------------

class TestGetSeries:
    async def test_404_falls_back_to_search_then_raises_when_missing(
        self, mock_browser: AsyncMock,
    ) -> None:
        mock_browser.get_json.return_value = {"result": {"items": []}}
        with pytest.raises(RemoteApiError, match="Could not find manga"):
            await _adapter().get_series(mock_browser, "missing-slug")

    async def test_chapter_listing_invalid_status_raises_remote_api_error(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.side_effect = [
            {"result": {"title": "Series", "hash_id": "hash", "slug": "series"}},
            {"status": "not-a-number", "result": {"items": []}},
        ]

        with pytest.raises(RemoteApiError, match="invalid API status"):
            await _adapter().get_series(mock_browser, "hash")

    async def test_chapter_listing_page_failure_raises_remote_api_error(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.side_effect = [
            {"result": {"title": "Series", "hash_id": "hash", "slug": "series"}},
            {
                "result": {
                    "items": [
                        {"chapter_id": i, "number": i, "pages_count": 1}
                        for i in range(1, 101)
                    ],
                },
            },
            TimeoutError("page 2 timed out"),
        ]

        with pytest.raises(RemoteApiError, match="Fetch chapter list page 2"):
            await _adapter().get_series(mock_browser, "hash")

    async def test_fetch_chapters_prefetches_counts_only_for_duplicate_groups(
        self, mock_browser: AsyncMock,
    ) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "items": [
                    {"chapter_id": 1, "number": 1, "pages_count": 0},
                    {"chapter_id": 2, "number": 2, "pages_count": 0},
                    {"chapter_id": 3, "number": 2, "pages_count": 0},
                ],
            },
        }
        adapter = _adapter()
        requested_counts: list[int] = []

        async def fake_count(_engine: object, chapter_id: int) -> int:
            requested_counts.append(chapter_id)
            return chapter_id

        adapter._get_image_count = fake_count  # type: ignore[method-assign]

        chapters, _ = await adapter._fetch_chapters(mock_browser, "hash")

        assert requested_counts == [2, 3]
        assert {chapter.chapter_id: chapter.image_count for chapter in chapters} == {
            1: 0,
            3: 3,
        }


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class TestDataClasses:
    def test_search_result(self) -> None:
        r = SearchResult(title="Test", url="https://example.com", slug="test", hash_id="abc")
        assert r.title == "Test"
        assert r.hash_id == "abc"

    def test_chapter_info_defaults(self) -> None:
        ch = ChapterInfo(title="Ch 1", chapter_id=100, number=1)
        assert ch.number == "1"
        assert ch.name == ""
        assert ch.language == "en"
        assert ch.image_count == 0

    def test_chapter_images(self) -> None:
        ci = ChapterImages(title="Ch 1", chapter_label="Chapter 1", image_urls=["a", "b"])
        assert len(ci.image_urls) == 2

    def test_series_info(self) -> None:
        si = SeriesInfo(
            title="Test Manga",
            authors=["Author"],
            genres=["Action"],
            description="Desc",
            chapters=[],
            url="https://example.com",
            hash_id="abc",
        )
        assert si.title == "Test Manga"
        assert si.chapters == []
