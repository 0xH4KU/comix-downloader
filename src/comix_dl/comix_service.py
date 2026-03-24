"""comix.to service layer — search, series info, and chapter images.

Uses the REST API v2 at ``/api/v2/``:

- Search: ``GET /api/v2/manga?keyword=...``
- Manga info: ``GET /api/v2/manga/{hash_id}``
- Chapters: ``GET /api/v2/manga/{hash_id}/chapters``
- Chapter images: ``GET /api/v2/chapters/{chapter_id}``
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from comix_dl.config import CONFIG, AppConfig

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
    name: str = ""  # subtitle (e.g. "Dear Little Brother")
    language: str = "en"
    image_count: int = 0


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

    def __init__(self, client: CdpBrowser, config: AppConfig | None = None) -> None:
        self._client = client
        self._config = config or CONFIG
        self._base = self._config.service.base_url

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


        limit = 100
        all_chapters: list[ChapterInfo] = []
        page = 1

        # Fetch chapter list pages sequentially until exhausted
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

            all_chapters.extend(self._parse_chapter_items(items))

            if len(items) < limit:
                break
            page += 1

        # Sort + deduplicate
        all_chapters.sort(key=lambda c: c.number)
        all_chapters = await self._deduplicate_chapters(all_chapters)

        # Fetch image counts in parallel for the final (deduplicated) list
        missing = [ch for ch in all_chapters if ch.image_count == 0]
        if missing:
            logger.info("Fetching image counts for %d chapter(s)…", len(missing))

            async def _fetch_count(ch: ChapterInfo) -> None:
                ch.image_count = await self._get_image_count(ch.chapter_id)

            await asyncio.gather(*[_fetch_count(ch) for ch in missing])

        logger.info("Fetched %d chapters for '%s'", len(all_chapters), hash_id)
        return all_chapters

    def _parse_chapter_items(self, items: list[dict[str, object]]) -> list[ChapterInfo]:
        """Parse raw API chapter items into ChapterInfo objects."""
        chapters: list[ChapterInfo] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("chapter_id", 0)
            chapter_id = int(raw_id) if isinstance(raw_id, (int, float, str)) else 0
            raw_num = item.get("number", 0)
            number = float(raw_num) if isinstance(raw_num, (int, float, str)) else 0.0
            name = str(item.get("name", "") or "")
            lang = str(item.get("language", "en") or "en")
            pages_count = item.get("pages_count", 0)

            if chapter_id:
                label = f"Chapter {number}"
                if name:
                    label += f" - {name}"
                chapters.append(ChapterInfo(
                    title=label,
                    chapter_id=chapter_id,
                    number=number,
                    name=name,
                    language=lang,
                    image_count=pages_count if isinstance(pages_count, int) else 0,
                ))
        return chapters

    async def _deduplicate_chapters(self, chapters: list[ChapterInfo]) -> list[ChapterInfo]:
        """Remove duplicate chapters, keeping the one with the most images.

        Chapters with the same number but *different* subtitles are treated as
        distinct content (e.g. "Chapter 0 - Volume 11" vs "Chapter 0 - Volume 12").
        Only chapters with the same number AND the same (or missing) subtitle are
        considered true duplicates.

        Uses ``image_count`` from the chapter list API (``pages_count`` field).
        Only falls back to per-chapter API calls if ``pages_count`` was missing.
        """
        if not chapters:
            return chapters

        groups: dict[float, list[ChapterInfo]] = defaultdict(list)
        for ch in chapters:
            groups[ch.number].append(ch)

        result: list[ChapterInfo] = []
        dup_count = 0

        for _num, chs in groups.items():
            if len(chs) == 1:
                result.append(chs[0])
                continue

            # Multiple entries — sub-group by name
            named: dict[str, list[ChapterInfo]] = defaultdict(list)
            unnamed: list[ChapterInfo] = []
            for ch in chs:
                if ch.name:
                    named[ch.name].append(ch)
                else:
                    unnamed.append(ch)

            if not named:
                best = await self._pick_best(unnamed)
                result.append(best)
                dup_count += len(unnamed) - 1
            else:
                for name_group in named.values():
                    if len(name_group) == 1:
                        result.append(name_group[0])
                    else:
                        best = await self._pick_best(name_group)
                        result.append(best)
                        dup_count += len(name_group) - 1
                dup_count += len(unnamed)

        result.sort(key=lambda c: c.number)
        if dup_count:
            logger.info("Removed %d duplicate chapter(s)", dup_count)

        return result

    async def _pick_best(self, candidates: list[ChapterInfo]) -> ChapterInfo:
        """From a list of true duplicates, pick the one with the most images.

        Uses ``image_count`` already populated from the list API's ``pages_count``.
        Falls back to per-chapter API calls only if all counts are 0.
        """
        # Check if we already have counts from pages_count
        has_counts = any(ch.image_count > 0 for ch in candidates)

        if not has_counts:
            # pages_count was missing — fetch individually
            for ch in candidates:
                ch.image_count = await self._get_image_count(ch.chapter_id)

        # Pick the one with the most images (tie-break: longer title)
        return max(candidates, key=lambda ch: (ch.image_count, len(ch.title)))

    async def _get_image_count(self, chapter_id: int) -> int:
        """Fetch the number of images in a chapter (lightweight dedup check)."""
        api_url = f"{self._base}/api/v2/chapters/{chapter_id}"
        try:
            resp = await self._client.get_json(api_url)
            data = resp.get("result", {})
            if isinstance(data, dict):
                images = data.get("images", [])
                if isinstance(images, list):
                    return len(images)
        except Exception as exc:
            logger.debug("Failed to get image count for chapter %d: %s", chapter_id, exc)
        return 0

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
