"""Session-scoped comix.to API client."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING
from urllib.parse import quote

from comix_dl.core.errors import RemoteApiError
from comix_dl.core.models import ChapterImages, ChapterInfo, DedupDecision, SearchResult, SeriesInfo
from comix_dl.sites.comix_to_dedup import deduplicate_chapters
from comix_dl.sites.comix_to_parsing import (
    coerce_api_status,
    extract_image_pages,
    extract_image_urls,
    format_chapter_label,
    names_from_people,
    parse_chapter_items,
    parse_search_results,
    titles_from_taxonomy,
    unwrap_object_result,
)
from comix_dl.sites.comix_to_url import absolute_site_url

if TYPE_CHECKING:
    from collections.abc import Callable

    from comix_dl.sites.base import Engine


logger = logging.getLogger(__name__)


class ComixToApiClient:
    """API operations and per-session chapter payload cache for comix.to."""

    def __init__(
        self,
        mirrors: list[str],
        *,
        describe_api_error: Callable[[Exception, str], str],
    ) -> None:
        self._mirrors = mirrors
        self._describe_api_error = describe_api_error
        self._chapter_payload_cache: dict[int, dict[str, object] | None] = {}

    @property
    def chapter_payload_cache(self) -> dict[int, dict[str, object] | None]:
        """Expose the cache for tests and adapter diagnostics."""
        return self._chapter_payload_cache

    @property
    def base_url(self) -> str:
        return self._mirrors[0]

    async def search(
        self,
        engine: Engine,
        query: str,
        *,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Search comix.to for series matching *query*."""
        base = self.base_url
        api_url = (
            f"{base}/api/v1/manga"
            f"?keyword={quote(query)}"
            f"&order[relevance]=desc"
            f"&limit={limit}"
        )
        try:
            resp = await engine.get_json(api_url)
        except Exception as exc:
            message = self._describe_api_error(exc, f"Search for '{query}'")
            logger.error("%s", message)
            raise RemoteApiError(message) from exc

        results = parse_search_results(resp, base_url=base)
        logger.info("Search '%s': %d results", query, len(results))
        return results

    async def get_series(self, engine: Engine, identifier: str) -> SeriesInfo:
        """Resolve *identifier* into a full series model."""
        data, hash_id = await self.resolve_series_payload(engine, identifier)
        base = self.base_url
        title = str(data.get("title", "") or hash_id)
        synopsis = str(data.get("synopsis", "") or data.get("description", "") or "")
        authors = names_from_people(data.get("authors", []))
        genres = titles_from_taxonomy(data.get("genres", []))
        series_url = absolute_site_url(base, str(data.get("url", "") or "")) or f"{base}/title/{hash_id}"

        chapters, dedup_decisions = await self.fetch_chapters(engine, hash_id)

        return SeriesInfo(
            title=title,
            authors=authors,
            genres=genres,
            description=synopsis,
            chapters=chapters,
            dedup_decisions=dedup_decisions,
            url=series_url,
            hash_id=hash_id,
        )

    async def resolve_series_payload(
        self,
        engine: Engine,
        identifier: str,
    ) -> tuple[dict[str, object], str]:
        """Resolve an identifier into raw series payload plus hash id."""
        data = await self.fetch_series_payload_direct(engine, identifier)
        hash_id = str(data.get("hid", "") or data.get("hash_id", "") or "")
        if not hash_id:
            data, hash_id = await self.fetch_series_payload_fallback(engine, identifier)
        return data, hash_id

    async def fetch_series_payload_direct(self, engine: Engine, identifier: str) -> dict[str, object]:
        """Return direct series lookup payload, or an empty dict when lookup fails."""
        try:
            info_resp = await engine.get_json(f"{self.base_url}/api/v1/manga/{identifier}")
        except Exception:
            logger.debug("Direct lookup failed for '%s', trying search fallback", identifier)
            return {}
        return unwrap_object_result(info_resp)

    async def fetch_series_payload_fallback(
        self,
        engine: Engine,
        identifier: str,
    ) -> tuple[dict[str, object], str]:
        """Resolve old slugs through search or hid-prefix fallback."""
        matched = await self.find_search_match(engine, identifier)
        if matched is not None:
            data = await self.fetch_series_payload_by_hash(engine, matched.hash_id)
            hash_id = str(data.get("hid", "") or data.get("hash_id", "") or matched.hash_id)
            return self.require_series_hash(data, hash_id, identifier)

        if "-" in identifier:
            prefix = identifier.split("-", 1)[0]
            try:
                data = await self.fetch_series_payload_by_hash(engine, prefix)
            except RemoteApiError as exc:
                raise RemoteApiError(f"Could not find manga with identifier '{identifier}'") from exc
            hash_id = str(data.get("hid", "") or data.get("hash_id", "") or "")
            return self.require_series_hash(data, hash_id, identifier)

        raise RemoteApiError(f"Could not find manga with identifier '{identifier}'")

    async def find_search_match(self, engine: Engine, identifier: str) -> SearchResult | None:
        """Find a search result that matches the requested slug or hash id."""
        results = await self.search(engine, identifier, limit=10)
        return next((
            result for result in results
            if (
                result.hash_id == identifier
                or result.slug == identifier
                or result.slug.startswith(f"{identifier}-")
            )
        ), None)

    async def fetch_series_payload_by_hash(self, engine: Engine, hash_id: str) -> dict[str, object]:
        """Fetch a series payload by hash id and wrap remote failures."""
        try:
            info_resp = await engine.get_json(f"{self.base_url}/api/v1/manga/{hash_id}")
        except Exception as exc:
            raise RemoteApiError(
                self._describe_api_error(exc, f"Fetch series info for '{hash_id}'"),
            ) from exc
        return unwrap_object_result(info_resp)

    @staticmethod
    def require_series_hash(
        data: dict[str, object],
        hash_id: str,
        identifier: str,
    ) -> tuple[dict[str, object], str]:
        """Return resolved data or raise the canonical missing-series error."""
        if not hash_id:
            raise RemoteApiError(f"Could not find manga with identifier '{identifier}'")
        return data, hash_id

    async def get_chapter_images(self, engine: Engine, chapter_id: int) -> ChapterImages | None:
        """Fetch the ordered image URL list for *chapter_id*."""
        try:
            data = await self.get_chapter_payload(engine, chapter_id)
        except Exception as exc:
            logger.error(
                "%s",
                self._describe_api_error(exc, f"Fetch chapter images for {chapter_id}"),
            )
            return None
        if data is None:
            return None

        image_pages = extract_image_pages(data)
        if image_pages is None:
            logger.warning("Invalid image payload for chapter %d", chapter_id)
            return None

        label = format_chapter_label(data)
        if not image_pages:
            logger.warning("No images found for chapter %d", chapter_id)
            return None

        return ChapterImages(
            title=label,
            chapter_label=label,
            image_urls=[page.url for page in image_pages],
            pages=image_pages,
        )

    async def fetch_chapters(
        self,
        engine: Engine,
        hash_id: str,
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        """Fetch every chapter page, fill missing image counts, then dedup."""
        all_chapters = await self.fetch_all_chapter_pages(engine, hash_id)
        all_chapters.sort(key=lambda c: c.number_sort_key)
        await self.fill_duplicate_image_counts(engine, all_chapters)

        deduped, decisions = deduplicate_chapters(all_chapters)
        logger.info("Fetched %d chapters for '%s'", len(deduped), hash_id)
        return deduped, decisions

    async def fetch_all_chapter_pages(self, engine: Engine, hash_id: str) -> list[ChapterInfo]:
        """Paginate through the chapter listing endpoint."""
        limit = 100
        chapters: list[ChapterInfo] = []
        page = 1
        while True:
            items = await self.fetch_chapter_page_items(engine, hash_id, page=page, limit=limit)
            if not items:
                break
            chapters.extend(parse_chapter_items(items))
            if len(items) < limit:
                break
            page += 1
        return chapters

    async def fetch_chapter_page_items(
        self,
        engine: Engine,
        hash_id: str,
        *,
        page: int,
        limit: int,
    ) -> list[dict[str, object]]:
        """Fetch and validate one chapter-listing page."""
        api_url = f"{self.base_url}/api/v1/manga/{hash_id}/chapters?limit={limit}&page={page}"
        try:
            resp = await engine.get_json(api_url)
        except Exception as exc:
            message = self._describe_api_error(exc, f"Fetch chapter list page {page} for '{hash_id}'")
            logger.error("%s", message)
            raise RemoteApiError(message) from exc

        self.raise_for_chapter_listing_status(resp, hash_id=hash_id, page=page)
        result_obj = resp.get("result", resp)
        items = result_obj.get("items", []) if isinstance(result_obj, dict) else []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def raise_for_chapter_listing_status(
        resp: dict[str, object],
        *,
        hash_id: str,
        page: int,
    ) -> None:
        """Raise a user-facing API error when the chapter-list status is bad."""
        api_status = resp.get("status")
        if api_status is None:
            return
        status_code = coerce_api_status(api_status)
        if status_code is None:
            raise RemoteApiError(
                f"Fetch chapter list page {page} for '{hash_id}' failed: "
                f"invalid API status {api_status!r}.",
            )
        if status_code >= 400:
            raise RemoteApiError(
                f"Chapter listing returned API status {api_status} for '{hash_id}' "
                f"(message: {resp.get('message', 'unknown')}). Request signing may have failed.",
            )

    async def fill_duplicate_image_counts(
        self,
        engine: Engine,
        chapters: list[ChapterInfo],
    ) -> None:
        """Fetch missing image counts only where dedup choice depends on them."""
        by_number: dict[str, list[ChapterInfo]] = defaultdict(list)
        for ch in chapters:
            by_number[ch.number].append(ch)
        missing = [
            ch
            for number_group in by_number.values()
            if len(number_group) > 1
            for ch in number_group
            if ch.image_count == 0
        ]
        if missing:
            logger.info("Fetching image counts for %d duplicate chapter candidate(s)...", len(missing))
            count_sem = asyncio.Semaphore(10)

            async def _fetch_count(ch: ChapterInfo) -> None:
                async with count_sem:
                    ch.image_count = await self.get_image_count(engine, ch.chapter_id)

            await asyncio.gather(*[_fetch_count(ch) for ch in missing])

    async def get_chapter_payload(
        self,
        engine: Engine,
        chapter_id: int,
    ) -> dict[str, object] | None:
        """Fetch and memoize a chapter detail payload by chapter id."""
        if chapter_id in self._chapter_payload_cache:
            return self._chapter_payload_cache[chapter_id]
        api_url = f"{self.base_url}/api/v1/chapters/{chapter_id}"
        resp = await engine.get_json(api_url)
        data = resp.get("result", resp)
        payload = data if isinstance(data, dict) else None
        self._chapter_payload_cache[chapter_id] = payload
        return payload

    async def get_image_count(self, engine: Engine, chapter_id: int) -> int:
        """Return the number of images in a chapter detail payload."""
        try:
            data = await self.get_chapter_payload(engine, chapter_id)
            if data is None:
                return 0
            image_urls = extract_image_urls(data)
            if image_urls is not None:
                return len(image_urls)
        except Exception as exc:
            logger.debug("Failed to get image count for chapter %d: %s", chapter_id, exc)
        return 0


__all__ = ["ComixToApiClient"]
