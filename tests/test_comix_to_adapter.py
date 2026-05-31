"""Tests for the comix.to SiteAdapter — parsing, dedup, search, and chapter fetch."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from comix_dl.core.errors import BrowserTimeoutError, Http403Error, RemoteApiError
from comix_dl.core.models import (
    ChapterImages,
    ChapterInfo,
    ChapterPage,
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
    async def test_registers_frontend_api_client_hook(self) -> None:
        adapter = _adapter()
        registered: list[str] = []

        class FakeEngine:
            def register_url_transformer(self, iife_js: str) -> None:
                registered.append(iife_js)

        await adapter.on_engine_ready(FakeEngine())

        assert len(registered) == 1
        iife = registered[0]
        # IIFE must import the site's frontend modules and expose the
        # JSON request hook consumed by CdpBrowser so protected v1
        # endpoints keep using the site's live request client for
        # signing and encrypted-response decoding.
        assert "window.__comixJsonRequest" in iife
        assert "window.__comixGetApiClient" in iife
        assert "window.__comixUrlTransformers" in iife
        assert "import(moduleUrl)" in iife
        assert "/api/v1/" in iife
        assert "Could not find comix.to API client export" not in iife


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

class TestUrlHandling:
    def test_matches_canonical_host(self) -> None:
        a = _adapter()
        assert a.matches_url("https://comix.to/title/x") is True
        assert a.matches_url("https://www.comix.to/manga/x") is True
        assert a.matches_url("https://other.example/manga/x") is False
        assert a.matches_url("not a url") is False

    def test_parse_identifier_from_url(self) -> None:
        a = _adapter()
        assert a.parse_identifier("https://comix.to/title/lzdj-omori") == "lzdj"
        assert a.parse_identifier("https://comix.to/title/lzdj-omori/") == "lzdj"

    def test_parse_identifier_preserves_legacy_manga_slug(self) -> None:
        a = _adapter()
        assert a.parse_identifier("https://comix.to/manga/test-series") == "test-series"

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
            {"id": 100, "number": 1, "name": "Intro", "language": "en", "pagesCount": 20},
            {"id": 200, "number": 2, "name": "", "language": "en", "pagesCount": 15},
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
        items = [{"number": 1, "name": "test"}]  # no id/chapter_id -> defaults to 0 -> skipped
        assert ComixToAdapter._parse_chapter_items(items) == []

    def test_non_int_pages_count(self) -> None:
        items = [{"id": 1, "number": 1, "pagesCount": "invalid"}]
        result = ComixToAdapter._parse_chapter_items(items)
        assert len(result) == 1
        assert result[0].image_count == 0

    def test_invalid_chapter_id_is_skipped(self) -> None:
        items = [
            {"chapter_id": "not-an-int", "number": 1, "pages_count": 12},
            {"id": "bad-id", "number": 2, "pagesCount": 10},
            {"chapter_id": 2, "number": 3, "pages_count": 8},
        ]

        result = ComixToAdapter._parse_chapter_items(items)

        assert len(result) == 1
        assert result[0].chapter_id == 2

    def test_legacy_chapter_id_still_supported(self) -> None:
        items = [{"chapter_id": 9, "number": 1, "pages_count": 3}]
        result = ComixToAdapter._parse_chapter_items(items)
        assert len(result) == 1
        assert result[0].chapter_id == 9
        assert result[0].image_count == 3


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
                    {
                        "title": "OMORI",
                        "hid": "lzdj",
                        "url": "/title/lzdj-omori",
                    },
                    {
                        "title": "Naruto",
                        "hid": "def456",
                        "url": "https://comix.to/title/def456-naruto",
                    },
                ],
            },
        }
        results = await _adapter().search(mock_browser, "test")
        assert len(results) == 2
        assert results[0].title == "OMORI"
        assert results[0].hash_id == "lzdj"
        assert results[0].slug == "omori"
        assert results[0].url == "https://comix.to/title/lzdj-omori"

    async def test_empty_result(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {"result": {"items": []}}
        results = await _adapter().search(mock_browser, "nonexistent")
        assert results == []

    async def test_missing_hid_skipped(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "items": [
                    {"title": "No Hash", "slug": "no-hash"},
                    {"title": "Has Hid", "hid": "abc", "url": "/title/abc-has-hid"},
                ],
            },
        }
        results = await _adapter().search(mock_browser, "test")
        assert len(results) == 1
        assert results[0].title == "Has Hid"

    async def test_accepts_unwrapped_search_response(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "items": [
                {"title": "OMORI", "hid": "lzdj", "url": "/title/lzdj-omori"},
            ],
        }
        results = await _adapter().search(mock_browser, "omori")
        assert len(results) == 1
        assert results[0].hash_id == "lzdj"

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
                "pages": {
                    "baseUrl": "https://cdn.example.com/chapter/",
                    "items": [
                        {"url": "img1.webp"},
                        {"url": "img2.webp"},
                        {"url": "img3.webp"},
                    ],
                },
            },
        }
        result = await _adapter().get_chapter_images(mock_browser, 12345)
        assert result is not None
        assert len(result.image_urls) == 3
        assert result.image_urls[0] == "https://cdn.example.com/chapter/img1.webp"
        assert result.chapter_label == "Chapter 5 - The Beginning"

    async def test_preserves_scrambled_page_metadata(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "number": 14,
                "name": "SWALLOW HOLLOW",
                "pages": {
                    "baseUrl": "https://static.comix.to/c0db/i/b/d4/",
                    "items": [
                        {"url": "plain.webp", "width": 800, "height": 1200},
                        {"url": "scrambled.webp", "width": 968, "height": 1378, "s": 1},
                    ],
                },
            },
        }

        result = await _adapter().get_chapter_images(mock_browser, 12345)

        assert result is not None
        assert result.image_urls == [
            "https://static.comix.to/c0db/i/b/d4/plain.webp",
            "https://static.comix.to/c0db/si/b/d4/scrambled.webp",
        ]
        assert result.pages == [
            ChapterPage(
                url="https://static.comix.to/c0db/i/b/d4/plain.webp",
                width=800,
                height=1200,
                scrambled=False,
            ),
            ChapterPage(
                url="https://static.comix.to/c0db/si/b/d4/scrambled.webp",
                width=968,
                height=1378,
                scrambled=True,
            ),
        ]

    async def test_normalizes_chapter_label_number(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "number": 1.0,
                "name": "",
                "pages": {"baseUrl": "https://cdn.example.com/", "items": [{"url": "img1.webp"}]},
            },
        }
        result = await _adapter().get_chapter_images(mock_browser, 12345)
        assert result is not None
        assert result.chapter_label == "Chapter 1"
        assert result.title == "Chapter 1"

    async def test_empty_images_returns_none(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {"number": 1, "name": "", "pages": {"baseUrl": "https://cdn.example.com/", "items": []}},
        }
        result = await _adapter().get_chapter_images(mock_browser, 12345)
        assert result is None

    async def test_invalid_image_entries_filtered(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "number": 1,
                "name": "",
                "pages": [
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

    async def test_legacy_images_still_supported(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "number": 1,
                "name": "",
                "images": [{"url": "https://cdn.example.com/img.webp"}],
            },
        }
        result = await _adapter().get_chapter_images(mock_browser, 12345)
        assert result is not None
        assert result.image_urls == ["https://cdn.example.com/img.webp"]

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
                "pages": {
                    "baseUrl": "https://cdn.example.com/",
                    "items": [
                        {"url": "img1.webp"},
                        {"url": "img2.webp"},
                    ],
                },
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
    async def test_parses_series_detail_and_chapters(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.side_effect = [
            {
                "result": {
                    "hid": "lzdj",
                    "title": "OMORI",
                    "synopsis": "After something changed...",
                    "url": "/title/lzdj-omori",
                    "authors": [{"name": "Omocat"}],
                    "genres": [{"title": "Drama"}, {"title": "Horror"}],
                },
            },
            {
                "result": {
                    "items": [
                        {
                            "id": 2459745,
                            "number": 2,
                            "name": "A Home For Flowers",
                            "language": "en",
                            "pagesCount": 1,
                        },
                    ],
                },
            },
            {
                "result": {
                    "number": 2,
                    "name": "A Home For Flowers",
                    "pages": {"baseUrl": "https://cdn.example.com/", "items": [{"url": "01.webp"}]},
                },
            },
        ]

        series = await _adapter().get_series(mock_browser, "lzdj")

        assert series.title == "OMORI"
        assert series.hash_id == "lzdj"
        assert series.url == "https://comix.to/title/lzdj-omori"
        assert series.authors == ["Omocat"]
        assert series.genres == ["Drama", "Horror"]
        assert len(series.chapters) == 1
        assert series.chapters[0].chapter_id == 2459745
        assert series.chapters[0].image_count == 1

    async def test_accepts_unwrapped_series_and_chapter_payloads(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.side_effect = [
            {
                "hid": "lzdj",
                "title": "OMORI",
                "url": "https://comix.to/title/lzdj-omori",
            },
            {"items": [{"id": 1, "number": 1, "name": "", "language": "en", "pagesCount": 1}]},
            {"number": 1, "name": "", "pages": {"baseUrl": "https://cdn.example.com/", "items": [{"url": "1.webp"}]}},
        ]

        series = await _adapter().get_series(mock_browser, "lzdj")

        assert series.hash_id == "lzdj"
        assert len(series.chapters) == 1
        assert series.chapters[0].image_count == 1

    async def test_404_falls_back_to_search_then_raises_when_missing(
        self, mock_browser: AsyncMock,
    ) -> None:
        mock_browser.get_json.return_value = {"result": {"items": []}}
        with pytest.raises(RemoteApiError, match="Could not find manga"):
            await _adapter().get_series(mock_browser, "missing-slug")

    async def test_chapter_listing_invalid_status_raises_remote_api_error(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.side_effect = [
            {"result": {"hid": "lzdj", "title": "OMORI", "url": "https://comix.to/title/lzdj-omori"}},
            {"status": "not-a-number", "result": {"items": []}},
        ]

        with pytest.raises(RemoteApiError, match="invalid API status"):
            await _adapter().get_series(mock_browser, "lzdj")

    async def test_chapter_listing_page_failure_raises_remote_api_error(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.side_effect = [
            {"result": {"hid": "lzdj", "title": "OMORI", "url": "https://comix.to/title/lzdj-omori"}},
            {
                "result": {
                    "items": [
                        {"id": i, "number": i, "pagesCount": 1}
                        for i in range(1, 101)
                    ],
                },
            },
            TimeoutError("page 2 timed out"),
        ]

        with pytest.raises(RemoteApiError, match="Fetch chapter list page 2"):
            await _adapter().get_series(mock_browser, "lzdj")

    async def test_fetch_chapters_prefetches_counts_only_for_duplicate_groups(
        self, mock_browser: AsyncMock,
    ) -> None:
        mock_browser.get_json.return_value = {
            "result": {
                "items": [
                    {"id": 1, "number": 1, "pagesCount": 0},
                    {"id": 2, "number": 2, "pagesCount": 0},
                    {"id": 3, "number": 2, "pagesCount": 0},
                ],
            },
        }
        adapter = _adapter()
        requested_counts: list[int] = []

        async def fake_count(_engine: object, chapter_id: int) -> int:
            requested_counts.append(chapter_id)
            return chapter_id

        adapter._get_image_count = fake_count  # type: ignore[method-assign]

        chapters, _ = await adapter._fetch_chapters(mock_browser, "lzdj")

        assert requested_counts == [2, 3]
        assert {chapter.chapter_id: chapter.image_count for chapter in chapters} == {
            1: 0,
            3: 3,
        }

    async def test_slug_fallback_uses_matched_search_hid(self, mock_browser: AsyncMock) -> None:
        mock_browser.get_json.side_effect = [
            Exception("404"),
            {
                "result": {
                    "items": [
                        {
                            "title": "OMORI",
                            "hid": "lzdj",
                            "url": "https://comix.to/title/lzdj-omori",
                        },
                    ],
                },
            },
            {"result": {"hid": "lzdj", "title": "OMORI", "url": "https://comix.to/title/lzdj-omori"}},
            {"result": {"items": []}},
        ]

        series = await _adapter().get_series(mock_browser, "omori")

        assert series.hash_id == "lzdj"
        awaited_urls = [call.args[0] for call in mock_browser.get_json.await_args_list]
        assert "https://comix.to/api/v1/manga/omori" in awaited_urls
        assert "https://comix.to/api/v1/manga/lzdj" in awaited_urls


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
        assert ci.pages == [
            ChapterPage(url="a"),
            ChapterPage(url="b"),
        ]

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
