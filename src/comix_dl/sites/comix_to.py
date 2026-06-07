"""comix.to site adapter facade.

This module wires the framework ``SiteAdapter`` contract to focused
comix.to helpers. The heavy lifting lives in sibling modules:

* ``comix_to_url`` for URL matching and identifier parsing
* ``comix_to_parsing`` for API payload parsing
* ``comix_to_dedup`` for chapter duplicate resolution
* ``comix_to_api`` for request orchestration and per-session cache
"""

from __future__ import annotations

import importlib.resources
import logging
from typing import TYPE_CHECKING

from comix_dl.core.errors import BrowserTimeoutError, Http403Error
from comix_dl.sites import register
from comix_dl.sites.comix_to_api import ComixToApiClient
from comix_dl.sites.comix_to_browser import probe_service_access, render_scrambled_image
from comix_dl.sites.comix_to_dedup import (
    build_dedup_decision,
    deduplicate_chapters,
    format_dedup_variant,
    pick_best,
)
from comix_dl.sites.comix_to_parsing import (
    extract_image_pages,
    extract_image_urls,
    names_from_people,
    parse_chapter_items,
    titles_from_taxonomy,
    unwrap_object_result,
)
from comix_dl.sites.comix_to_url import absolute_site_url, matches_comix_url, parse_identifier, slug_from_title_url

if TYPE_CHECKING:
    from comix_dl.core.models import ChapterImages, ChapterInfo, ChapterPage, DedupDecision, SearchResult, SeriesInfo
    from comix_dl.sites.base import Engine


logger = logging.getLogger(__name__)


def _load_text_asset(name: str) -> str:
    """Load a packaged text asset used by the comix.to adapter."""
    return importlib.resources.files("comix_dl.sites.assets").joinpath(name).read_text(encoding="utf-8")


_COMIX_API_CLIENT_IIFE = _load_text_asset("comix_api_client.js")


def _describe_api_error(exc: Exception, *, action: str) -> str:
    """Format a high-value remote failure mode for user-facing logs."""
    if isinstance(exc, Http403Error):
        return (
            f"{action} failed: API request was blocked by HTTP 403. "
            "Cloudflare clearance may have expired."
        )
    if isinstance(exc, BrowserTimeoutError):
        return f"{action} failed: API request timed out. {exc}"
    return f"{action} failed: {exc}"


def _describe_api_error_for_client(exc: Exception, action: str) -> str:
    """Adapter-local bridge for the API client's simpler callback shape."""
    return _describe_api_error(exc, action=action)


class ComixToAdapter:
    """SiteAdapter implementation for comix.to."""

    name = "comix.to"
    needs_browser = True

    def __init__(self) -> None:
        self.mirrors: list[str] = ["https://comix.to"]
        self._api = ComixToApiClient(
            self.mirrors,
            describe_api_error=_describe_api_error_for_client,
        )

    @property
    def _chapter_payload_cache(self) -> dict[int, dict[str, object] | None]:
        """Compatibility view over the session-scoped API cache."""
        return self._api.chapter_payload_cache

    def new_session(self) -> ComixToAdapter:
        """Return a fresh adapter instance for one application session."""
        session_adapter = ComixToAdapter()
        session_adapter.mirrors = list(self.mirrors)
        session_adapter._api = ComixToApiClient(
            session_adapter.mirrors,
            describe_api_error=_describe_api_error_for_client,
        )
        return session_adapter

    # -- Lifecycle hooks ----------------------------------------------------

    def configure_engine(self, engine: Engine) -> None:
        """Register comix.to browser hooks before Cloudflare clearance starts."""
        engine.register_service_access_probe(probe_service_access)
        engine.register_scrambled_image_renderer(render_scrambled_image)

    async def on_engine_ready(self, engine: Engine) -> None:
        """Install the comix.to frontend API-client request hook."""
        engine.register_url_transformer(_COMIX_API_CLIENT_IIFE)

    async def probe_alive(self, engine: Engine) -> bool:
        """Best-effort reachability probe used during mirror selection."""
        for mirror in self.mirrors:
            try:
                await engine.fetch_page(mirror)
            except Exception as exc:
                logger.debug("Probe of %s failed: %s", mirror, exc)
                continue
            return True
        return False

    # -- URL handling -------------------------------------------------------

    def matches_url(self, url: str) -> bool:
        """Return whether *url* points at a known comix.to mirror."""
        return matches_comix_url(url)

    def parse_identifier(self, url_or_slug: str) -> str | None:
        """Extract a canonical comix.to hid, legacy slug, or bare identifier."""
        return parse_identifier(url_or_slug)

    # -- Content operations -------------------------------------------------

    async def search(
        self,
        engine: Engine,
        query: str,
        *,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Search comix.to for series matching *query*."""
        return await self._api.search(engine, query, limit=limit)

    async def get_series(self, engine: Engine, identifier: str) -> SeriesInfo:
        """Resolve *identifier* into a full SeriesInfo."""
        return await self._api.get_series(engine, identifier)

    async def get_chapter_images(
        self,
        engine: Engine,
        chapter_id: int,
    ) -> ChapterImages | None:
        """Fetch the ordered image URL list for *chapter_id*."""
        return await self._api.get_chapter_images(engine, chapter_id)

    def deduplicate(
        self,
        chapters: list[ChapterInfo],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        """Collapse comix.to chapter duplicates by number / language / subtitle."""
        return deduplicate_chapters(chapters)

    # -- Compatibility helpers ---------------------------------------------

    async def _resolve_series_payload(
        self,
        engine: Engine,
        identifier: str,
    ) -> tuple[dict[str, object], str]:
        return await self._api.resolve_series_payload(engine, identifier)

    async def _fetch_series_payload_direct(self, engine: Engine, identifier: str) -> dict[str, object]:
        return await self._api.fetch_series_payload_direct(engine, identifier)

    async def _fetch_series_payload_fallback(
        self,
        engine: Engine,
        identifier: str,
    ) -> tuple[dict[str, object], str]:
        return await self._api.fetch_series_payload_fallback(engine, identifier)

    async def _find_search_match(self, engine: Engine, identifier: str) -> SearchResult | None:
        return await self._api.find_search_match(engine, identifier)

    async def _fetch_series_payload_by_hash(self, engine: Engine, hash_id: str) -> dict[str, object]:
        return await self._api.fetch_series_payload_by_hash(engine, hash_id)

    @staticmethod
    def _require_series_hash(
        data: dict[str, object],
        hash_id: str,
        identifier: str,
    ) -> tuple[dict[str, object], str]:
        return ComixToApiClient.require_series_hash(data, hash_id, identifier)

    async def _fetch_chapters(
        self,
        engine: Engine,
        hash_id: str,
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        all_chapters = await self._api.fetch_all_chapter_pages(engine, hash_id)
        all_chapters.sort(key=lambda c: c.number_sort_key)
        await self._fill_duplicate_image_counts(engine, all_chapters)

        deduped, decisions = self.deduplicate(all_chapters)
        logger.info("Fetched %d chapters for '%s'", len(deduped), hash_id)
        return deduped, decisions

    async def _fetch_all_chapter_pages(self, engine: Engine, hash_id: str) -> list[ChapterInfo]:
        return await self._api.fetch_all_chapter_pages(engine, hash_id)

    async def _fetch_chapter_page_items(
        self,
        engine: Engine,
        hash_id: str,
        *,
        page: int,
        limit: int,
    ) -> list[dict[str, object]]:
        return await self._api.fetch_chapter_page_items(engine, hash_id, page=page, limit=limit)

    @staticmethod
    def _raise_for_chapter_listing_status(
        resp: dict[str, object],
        *,
        hash_id: str,
        page: int,
    ) -> None:
        ComixToApiClient.raise_for_chapter_listing_status(resp, hash_id=hash_id, page=page)

    async def _fill_duplicate_image_counts(
        self,
        engine: Engine,
        chapters: list[ChapterInfo],
    ) -> None:
        by_number: dict[str, list[ChapterInfo]] = {}
        for chapter in chapters:
            by_number.setdefault(chapter.number, []).append(chapter)
        missing = [
            chapter
            for number_group in by_number.values()
            if len(number_group) > 1
            for chapter in number_group
            if chapter.image_count == 0
        ]
        for chapter in missing:
            chapter.image_count = await self._get_image_count(engine, chapter.chapter_id)

    async def _get_chapter_payload(
        self,
        engine: Engine,
        chapter_id: int,
    ) -> dict[str, object] | None:
        return await self._api.get_chapter_payload(engine, chapter_id)

    async def _get_image_count(self, engine: Engine, chapter_id: int) -> int:
        return await self._api.get_image_count(engine, chapter_id)

    @staticmethod
    def _unwrap_object_result(response: dict[str, object]) -> dict[str, object]:
        return unwrap_object_result(response)

    @staticmethod
    def _parse_chapter_items(items: list[dict[str, object]]) -> list[ChapterInfo]:
        return parse_chapter_items(items)

    @staticmethod
    def _format_dedup_variant(chapter: ChapterInfo) -> str:
        return format_dedup_variant(chapter)

    @classmethod
    def _build_dedup_decision(
        cls,
        *,
        chapter_number: str,
        reason: str,
        kept: list[ChapterInfo],
        dropped: list[ChapterInfo],
    ) -> DedupDecision:
        return build_dedup_decision(chapter_number=chapter_number, reason=reason, kept=kept, dropped=dropped)

    @classmethod
    def _resolve_number_group(
        cls,
        _chapter_number: str,
        chs: list[ChapterInfo],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        return deduplicate_chapters(chs)

    @classmethod
    def _resolve_unnamed_only(
        cls,
        _chapter_number: str,
        unnamed: dict[str, list[ChapterInfo]],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        chapters = [chapter for group in unnamed.values() for chapter in group]
        return deduplicate_chapters(chapters)

    @classmethod
    def _resolve_named_with_unnamed(
        cls,
        _chapter_number: str,
        named: dict[tuple[str, str], list[ChapterInfo]],
        unnamed: dict[str, list[ChapterInfo]],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        chapters = [chapter for group in named.values() for chapter in group]
        chapters.extend(chapter for group in unnamed.values() for chapter in group)
        return deduplicate_chapters(chapters)

    @staticmethod
    def _pick_best(candidates: list[ChapterInfo]) -> ChapterInfo:
        return pick_best(candidates)

    @staticmethod
    def _absolute_site_url(base: str, url: str) -> str:
        return absolute_site_url(base, url)

    @staticmethod
    def _slug_from_title_url(url: str, hid: str) -> str:
        return slug_from_title_url(url, hid)

    @staticmethod
    def _titles_from_taxonomy(raw_items: object) -> list[str]:
        return titles_from_taxonomy(raw_items)

    @staticmethod
    def _names_from_people(raw_items: object) -> list[str]:
        return names_from_people(raw_items)

    @classmethod
    def _extract_image_urls(cls, data: dict[str, object]) -> list[str] | None:
        return extract_image_urls(data)

    @staticmethod
    def _extract_image_pages(data: dict[str, object]) -> list[ChapterPage] | None:
        return extract_image_pages(data)

# Module-level singleton registered with the framework.
adapter = ComixToAdapter()
register(adapter)


__all__ = ["ComixToAdapter", "adapter"]
