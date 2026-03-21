"""comix.to service layer — search, series info, and chapter images.

Uses the REST API v2 at ``/api/v2/``:

- Search: ``GET /api/v2/manga?keyword=...``
- Manga info: ``GET /api/v2/manga/{hash_id}``
- Chapters: ``GET /api/v2/manga/{hash_id}/chapters``
- Chapter images: ``GET /api/v2/chapters/{chapter_id}``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from comix_dl.config import CONFIG

if TYPE_CHECKING:
    from comix_dl.cdp_browser import CdpBrowser

logger = logging.getLogger(__name__)


# -- data classes -------------------------------------------------------------


@dataclass
class SearchResult:
    """A single search result."""

    title: str
    url: str
    slug: str
    hash_id: str


@dataclass
class ChapterInfo:
    """Chapter metadata."""

    title: str
    chapter_id: int
    number: float
    language: str = "en"


@dataclass
class ChapterImages:
    """Chapter images from the API."""

    title: str
    chapter_label: str
    image_urls: list[str]


@dataclass
class SeriesInfo:
    """Full series metadata with chapters."""

    title: str
    authors: list[str]
    genres: list[str]
    description: str
    chapters: list[ChapterInfo]
    url: str
    hash_id: str


# -- service ------------------------------------------------------------------


class ComixService:
    """High-level API for interacting with comix.to."""

    def __init__(self, client: CdpBrowser) -> None:
        self._client = client
        self._base = CONFIG.service.base_url

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Search for manga series by keyword."""
        api_url = (
            f"{self._base}/api/v2/manga"
            f"?keyword={quote(query)}"
            f"&order[relevance]=desc"
            f"&limit={limit}"
        )

        try:
            resp = await self._client.get_json(api_url)
        except Exception as exc:
            msg = str(exc)
            if "403" in msg:
                logger.error(
                    "Access denied (HTTP 403). CF cookies may have expired. "
                    "Try running again to refresh."
                )
            else:
                logger.error("Search failed: %s", exc)
            return []

        results: list[SearchResult] = []
        result_obj = resp.get("result", {})
        items = result_obj.get("items", []) if isinstance(result_obj, dict) else []

        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            slug = item.get("slug", "")
            hash_id = item.get("hash_id", "")
            if title and hash_id:
                url = f"{self._base}/manga/{slug or hash_id}"
                results.append(SearchResult(
                    title=title, url=url, slug=slug, hash_id=hash_id,
                ))

        logger.info("Search '%s': %d results", query, len(results))
        return results

    async def get_series(self, hash_id: str) -> SeriesInfo:
        """Fetch series info and chapter list by hash_id."""
        # Fetch manga details
        api_url = f"{self._base}/api/v2/manga/{hash_id}"
        try:
            info_resp = await self._client.get_json(api_url)
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch series info: {exc}") from exc

        data = info_resp.get("result", {})
        if not isinstance(data, dict):
            data = {}

        title = data.get("title", "") or hash_id
        slug = data.get("slug", "")
        synopsis = data.get("synopsis", "") or data.get("description", "") or ""

        # Fetch chapters
        chapters = await self._fetch_chapters(hash_id)

        return SeriesInfo(
            title=title,
            authors=[],
            genres=[],
            description=synopsis,
            chapters=chapters,
            url=f"{self._base}/manga/{slug or hash_id}",
            hash_id=hash_id,
        )

    async def _fetch_chapters(self, hash_id: str) -> list[ChapterInfo]:
        """Fetch all chapters for a manga by hash_id."""
        chapters: list[ChapterInfo] = []
        page = 1
        limit = 100

        while True:
            api_url = (
                f"{self._base}/api/v2/manga/{hash_id}/chapters"
                f"?limit={limit}&page={page}"
            )
            try:
                resp = await self._client.get_json(api_url)
            except Exception as exc:
                logger.error("Failed to fetch chapters (page %d): %s", page, exc)
                break

            result_obj = resp.get("result", {})
            items = result_obj.get("items", []) if isinstance(result_obj, dict) else []
            if not isinstance(items, list) or not items:
                break

            for item in items:
                if not isinstance(item, dict):
                    continue
                chapter_id = item.get("chapter_id", 0)
                number = item.get("number", 0)
                name = item.get("name", "")
                lang = item.get("language", "en")

                if chapter_id:
                    label = f"Chapter {number}"
                    if name:
                        label += f" - {name}"
                    chapters.append(ChapterInfo(
                        title=label,
                        chapter_id=chapter_id,
                        number=number,
                        language=lang,
                    ))

            # Pagination
            total = result_obj.get("total", 0) if isinstance(result_obj, dict) else 0
            if isinstance(total, int) and total > 0 and len(chapters) >= total:
                break
            if len(items) < limit:
                break
            page += 1

        # Sort by chapter number
        chapters.sort(key=lambda c: c.number)
        logger.info("Fetched %d chapters for '%s'", len(chapters), hash_id)
        return chapters

    async def get_chapter_images(self, chapter_id: int) -> ChapterImages | None:
        """Fetch chapter images by chapter_id.

        Returns image URLs directly from the API — no HTML parsing needed.
        """
        api_url = f"{self._base}/api/v2/chapters/{chapter_id}"

        try:
            resp = await self._client.get_json(api_url)
        except Exception as exc:
            logger.error("Failed to fetch chapter %d: %s", chapter_id, exc)
            return None

        data = resp.get("result", {})
        if not isinstance(data, dict):
            return None

        number = data.get("number", 0)
        name = data.get("name", "")
        images = data.get("images", [])

        label = f"Chapter {number}"
        if name:
            label += f" - {name}"

        image_urls = [
            img["url"]
            for img in images
            if isinstance(img, dict) and img.get("url")
        ]

        if not image_urls:
            logger.warning("No images found for chapter %d", chapter_id)
            return None

        return ChapterImages(
            title=label,
            chapter_label=label,
            image_urls=image_urls,
        )
