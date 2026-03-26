"""Tests for comix_dl.comix_service — data parsing, dedup, and API response handling."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from comix_dl.comix_service import ChapterImages, ChapterInfo, ComixService, SearchResult, SeriesInfo
from comix_dl.errors import RemoteApiError

if TYPE_CHECKING:
    from unittest.mock import AsyncMock


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


def _make_service(mock_browser: AsyncMock) -> ComixService:
    return ComixService(mock_browser)


# ---------------------------------------------------------------------------
# _parse_chapter_items
# ---------------------------------------------------------------------------

class TestParseChapterItems:
    def test_basic_parsing(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        items = [
            {"chapter_id": 100, "number": 1, "name": "Intro", "language": "en", "pages_count": 20},
            {"chapter_id": 200, "number": 2, "name": "", "language": "en", "pages_count": 15},
        ]
        result = svc._parse_chapter_items(items)
        assert len(result) == 2
        assert result[0].chapter_id == 100
        assert result[0].number == "1"
        assert result[0].name == "Intro"
        assert result[0].image_count == 20
        assert result[0].title == "Chapter 1 - Intro"
        assert result[1].title == "Chapter 2"

    def test_empty_list(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        assert svc._parse_chapter_items([]) == []

    def test_non_dict_items_skipped(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        items = ["not a dict", 42, None]  # type: ignore[list-item]
        assert svc._parse_chapter_items(items) == []

    def test_missing_chapter_id_skipped(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        items = [{"number": 1, "name": "test"}]  # no chapter_id → defaults to 0 → skipped
        assert svc._parse_chapter_items(items) == []

    def test_non_int_pages_count(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        items = [{"chapter_id": 1, "number": 1, "pages_count": "invalid"}]
        result = svc._parse_chapter_items(items)
        assert len(result) == 1
        assert result[0].image_count == 0  # non-int treated as 0


# ---------------------------------------------------------------------------
# _deduplicate_chapters
# ---------------------------------------------------------------------------

class TestDeduplicateChapters:
    async def test_no_duplicates_unchanged(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        chapters = [_make_chapter(1, 100), _make_chapter(2, 200), _make_chapter(3, 300)]
        result = await svc._deduplicate_chapters(chapters)
        assert len(result) == 3

    async def test_empty_list(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        result = await svc._deduplicate_chapters([])
        assert result == []

    async def test_same_number_different_name_kept(self, mock_browser: AsyncMock):
        """Chapters 0 - Volume 11 and 0 - Volume 12 are different content."""
        svc = _make_service(mock_browser)
        chapters = [
            _make_chapter(0, 100, name="Volume 11", image_count=20),
            _make_chapter(0, 200, name="Volume 12", image_count=25),
        ]
        result = await svc._deduplicate_chapters(chapters)
        assert len(result) == 2

    async def test_true_duplicates_keeps_most_images(self, mock_browser: AsyncMock):
        """Same number, no name → true duplicate, keep the one with more images."""
        svc = _make_service(mock_browser)
        chapters = [
            _make_chapter(5, 100, image_count=10),
            _make_chapter(5, 200, image_count=25),
            _make_chapter(5, 300, image_count=15),
        ]
        result = await svc._deduplicate_chapters(chapters)
        assert len(result) == 1
        assert result[0].chapter_id == 200  # most images

    async def test_same_number_same_name_different_language_kept(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        chapters = [
            _make_chapter(7, 100, name="Special", image_count=20, language="en"),
            _make_chapter(7, 200, name="Special", image_count=25, language="es"),
        ]

        result = await svc._deduplicate_chapters(chapters)

        assert len(result) == 2
        assert {ch.language for ch in result} == {"en", "es"}

    async def test_same_number_unnamed_different_language_kept(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        chapters = [
            _make_chapter(8, 100, image_count=20, language="en"),
            _make_chapter(8, 200, image_count=25, language="jp"),
        ]

        result = await svc._deduplicate_chapters(chapters)

        assert len(result) == 2
        assert {ch.language for ch in result} == {"en", "jp"}

    async def test_same_number_same_name_same_language_keeps_most_images(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        chapters = [
            _make_chapter(9, 100, name="Finale", image_count=10, language="en"),
            _make_chapter(9, 200, name="Finale", image_count=30, language="en"),
        ]

        result = await svc._deduplicate_chapters(chapters)

        assert len(result) == 1
        assert result[0].chapter_id == 200

    async def test_result_sorted_by_number(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        chapters = [
            _make_chapter(3, 300),
            _make_chapter(1, 100),
            _make_chapter(2, 200),
        ]
        result = await svc._deduplicate_chapters(chapters)
        assert [ch.number for ch in result] == ["1", "2", "3"]

    async def test_decimal_numbers_sort_naturally(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        chapters = [
            _make_chapter("10.5", 300),
            _make_chapter("2", 100),
            _make_chapter("2.1", 200),
        ]

        result = await svc._deduplicate_chapters(chapters)

        assert [ch.number for ch in result] == ["2", "2.1", "10.5"]

    async def test_report_records_kept_and_dropped_variants(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        chapters = [
            _make_chapter(5, 100, image_count=10),
            _make_chapter(5, 200, image_count=25),
            _make_chapter(5, 300, image_count=15),
        ]

        result, decisions = await svc._deduplicate_chapters_with_report(chapters)

        assert len(result) == 1
        assert len(decisions) == 1
        assert decisions[0].chapter_number == "5"
        assert "highest page count" in decisions[0].reason
        assert "id=200" in decisions[0].kept[0]
        assert {item.split("id=")[1].rstrip("]") for item in decisions[0].dropped} == {"100", "300"}

    async def test_report_explains_unnamed_variants_dropped_when_named_exists(self, mock_browser: AsyncMock):
        svc = _make_service(mock_browser)
        chapters = [
            _make_chapter(7, 100, name="Special", image_count=20, language="en"),
            _make_chapter(7, 200, image_count=15, language="en"),
        ]

        result, decisions = await svc._deduplicate_chapters_with_report(chapters)

        assert len(result) == 1
        assert len(decisions) == 1
        assert "named variants exist" in decisions[0].reason
        assert "Special" in decisions[0].kept[0]
        assert "id=200" in decisions[0].dropped[0]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestSearch:
    async def test_parses_search_response(self, mock_browser: AsyncMock):
        mock_browser.get_json.return_value = {
            "result": {
                "items": [
                    {"title": "One Piece", "slug": "one-piece", "hash_id": "abc123"},
                    {"title": "Naruto", "slug": "naruto", "hash_id": "def456"},
                ]
            }
        }
        svc = _make_service(mock_browser)
        results = await svc.search("test")
        assert len(results) == 2
        assert results[0].title == "One Piece"
        assert results[0].hash_id == "abc123"
        assert results[0].slug == "one-piece"
        assert "one-piece" in results[0].url
        assert mock_browser.get_json.await_args.kwargs["use_page_pool"] is True

    async def test_empty_result(self, mock_browser: AsyncMock):
        mock_browser.get_json.return_value = {"result": {"items": []}}
        svc = _make_service(mock_browser)
        results = await svc.search("nonexistent")
        assert results == []

    async def test_missing_hash_id_skipped(self, mock_browser: AsyncMock):
        mock_browser.get_json.return_value = {
            "result": {
                "items": [
                    {"title": "No Hash", "slug": "no-hash"},  # no hash_id
                    {"title": "Has Hash", "slug": "has-hash", "hash_id": "abc"},
                ]
            }
        }
        svc = _make_service(mock_browser)
        results = await svc.search("test")
        assert len(results) == 1
        assert results[0].title == "Has Hash"

    async def test_api_error_raises_remote_api_error(self, mock_browser: AsyncMock):
        mock_browser.get_json.side_effect = Exception("Network error")
        svc = _make_service(mock_browser)
        with pytest.raises(RemoteApiError, match=r"Search for 'test' failed: Network error"):
            await svc.search("test")

    async def test_403_error_raises_remote_api_error(self, mock_browser: AsyncMock):
        mock_browser.get_json.side_effect = Exception("HTTP 403 Forbidden")
        svc = _make_service(mock_browser)
        with pytest.raises(
            RemoteApiError,
            match=(
                r"Search for 'test' failed: API request was blocked by HTTP 403\. "
                r"Cloudflare clearance may have expired\."
            ),
        ):
            await svc.search("test")

    async def test_403_error_logs_clearance_hint(self, mock_browser: AsyncMock, caplog: pytest.LogCaptureFixture):
        mock_browser.get_json.side_effect = Exception("HTTP 403 Forbidden")
        svc = _make_service(mock_browser)

        with pytest.raises(RemoteApiError):
            await svc.search("test")

        assert "Cloudflare clearance may have expired." in caplog.text


# ---------------------------------------------------------------------------
# get_chapter_images
# ---------------------------------------------------------------------------

class TestGetChapterImages:
    async def test_parses_image_urls(self, mock_browser: AsyncMock):
        mock_browser.get_json.return_value = {
            "result": {
                "number": 5,
                "name": "The Beginning",
                "images": [
                    {"url": "https://cdn.example.com/img1.webp"},
                    {"url": "https://cdn.example.com/img2.webp"},
                    {"url": "https://cdn.example.com/img3.webp"},
                ],
            }
        }
        svc = _make_service(mock_browser)
        result = await svc.get_chapter_images(12345)
        assert result is not None
        assert len(result.image_urls) == 3
        assert result.chapter_label == "Chapter 5 - The Beginning"
        assert mock_browser.get_json.await_args.kwargs["use_page_pool"] is True

    async def test_normalizes_chapter_label_number_from_detail_payload(self, mock_browser: AsyncMock):
        mock_browser.get_json.return_value = {
            "result": {
                "number": 1.0,
                "name": "",
                "images": [{"url": "https://cdn.example.com/img1.webp"}],
            }
        }
        svc = _make_service(mock_browser)

        result = await svc.get_chapter_images(12345)

        assert result is not None
        assert result.chapter_label == "Chapter 1"
        assert result.title == "Chapter 1"

    async def test_empty_images_returns_none(self, mock_browser: AsyncMock):
        mock_browser.get_json.return_value = {
            "result": {"number": 1, "name": "", "images": []}
        }
        svc = _make_service(mock_browser)
        result = await svc.get_chapter_images(12345)
        assert result is None

    async def test_invalid_image_entries_filtered(self, mock_browser: AsyncMock):
        mock_browser.get_json.return_value = {
            "result": {
                "number": 1,
                "name": "",
                "images": [
                    {"url": "https://valid.com/img.webp"},
                    {"not_url": "missing"},  # no "url" key
                    "not_a_dict",  # not a dict
                    {"url": ""},  # empty url
                ],
            }
        }
        svc = _make_service(mock_browser)
        result = await svc.get_chapter_images(12345)
        assert result is not None
        assert len(result.image_urls) == 1

    async def test_api_error_returns_none(self, mock_browser: AsyncMock):
        mock_browser.get_json.side_effect = Exception("timeout")
        svc = _make_service(mock_browser)
        result = await svc.get_chapter_images(12345)
        assert result is None

    async def test_timeout_logs_clear_error(self, mock_browser: AsyncMock, caplog: pytest.LogCaptureFixture):
        mock_browser.get_json.side_effect = Exception("Reading response timed out after 5000ms.")
        svc = _make_service(mock_browser)

        result = await svc.get_chapter_images(12345)

        assert result is None
        assert "API request timed out." in caplog.text


class TestGetSeries:
    async def test_403_raises_remote_api_error(self, mock_browser: AsyncMock):
        mock_browser.get_json.side_effect = Exception("HTTP 403 Forbidden")
        svc = _make_service(mock_browser)

        with pytest.raises(
            RemoteApiError,
            match=(
                r"Fetch series info for 'abc' failed: API request was blocked by HTTP 403\. "
                r"Cloudflare clearance may have expired\."
            ),
        ):
            await svc.get_series("abc")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class TestDataClasses:
    def test_search_result(self):
        r = SearchResult(title="Test", url="https://example.com", slug="test", hash_id="abc")
        assert r.title == "Test"
        assert r.hash_id == "abc"

    def test_chapter_info_defaults(self):
        ch = ChapterInfo(title="Ch 1", chapter_id=100, number=1)
        assert ch.number == "1"
        assert ch.name == ""
        assert ch.language == "en"
        assert ch.image_count == 0

    def test_chapter_images(self):
        ci = ChapterImages(title="Ch 1", chapter_label="Chapter 1", image_urls=["a", "b"])
        assert len(ci.image_urls) == 2

    def test_series_info(self):
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
