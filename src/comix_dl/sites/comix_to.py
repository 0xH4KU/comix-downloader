"""comix.to site adapter.

Implements the :class:`~comix_dl.sites.base.SiteAdapter` protocol against
the comix.to v1 REST API. URL handling, JSON schema parsing, and
chapter deduplication rules are all comix.to-specific and live here so
the framework code stays site-agnostic.

Endpoints used:

- ``GET /api/v1/manga?keyword=...`` - search
- ``GET /api/v1/manga/{hid}`` - series detail
- ``GET /api/v1/manga/{hid}/chapters`` - paginated chapter list
- ``GET /api/v1/chapters/{chapter_id}`` - chapter image URLs

This module registers a singleton adapter instance with the
framework registry at import time.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin, urlparse

from comix_dl.core.errors import BrowserTimeoutError, Http403Error, RemoteApiError
from comix_dl.core.models import (
    ChapterImages,
    ChapterInfo,
    ChapterPage,
    DedupDecision,
    SearchResult,
    SeriesInfo,
    normalize_chapter_number,
)
from comix_dl.sites import register

if TYPE_CHECKING:
    from comix_dl.sites.base import Engine


logger = logging.getLogger(__name__)


_URL_HOST_PATTERN = re.compile(r"^(?:www\.)?comix\.to$", re.IGNORECASE)


def _load_text_asset(name: str) -> str:
    """Load a packaged text asset used by the comix.to adapter."""
    return importlib.resources.files("comix_dl.sites.assets").joinpath(name).read_text(encoding="utf-8")


# JS IIFE registered with the engine after Cloudflare clearance. The current
# comix.to frontend owns request signing and encrypted-response decoding, so the
# hook lives as a packaged JS asset instead of a large Python string literal.
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


def _coerce_positive_int(value: object) -> int | None:
    """Return a positive integer value when the API supplied one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _coerce_api_status(value: object) -> int | None:
    """Best-effort integer coercion for remote API status fields."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float, str)):
            return int(value)
    except (TypeError, ValueError):
        return None
    return None


def _unwrap_object_result(response: dict[str, object]) -> dict[str, object]:
    """Return the object stored in result, or the response itself when unwrapped."""
    raw = response.get("result", response)
    return raw if isinstance(raw, dict) else {}


class ComixToAdapter:
    """SiteAdapter implementation for comix.to."""

    name = "comix.to"
    needs_browser = True

    def __init__(self) -> None:
        # mirrors is an instance attribute so it satisfies the
        # SiteAdapter protocol's instance-variable expectation while
        # still defaulting to the canonical comix.to host. Future
        # mirror discovery (F-5) mutates this list at runtime.
        self.mirrors: list[str] = ["https://comix.to"]
        # Per-adapter caches. Engines are session-scoped so the cache
        # lifetime tracks the active session naturally; the framework
        # constructs one adapter instance for the whole process.
        self._chapter_payload_cache: dict[int, dict[str, object] | None] = {}

    # -- Lifecycle hooks ----------------------------------------------------

    async def on_engine_ready(self, engine: Engine) -> None:
        """Install the comix.to frontend API-client request hook.

        The hook IIFE is replayed against every browser page the
        engine creates (main and pool). It imports the site's current
        frontend modules and routes protected API JSON requests through
        the same client the reader uses, preserving request signing and
        encrypted-response decoding.
        """
        engine.register_url_transformer(_COMIX_API_CLIENT_IIFE)

    async def probe_alive(self, engine: Engine) -> bool:
        """Best-effort reachability probe used during mirror selection.

        F-5 will replace this with a service-aware probe that confirms
        Cloudflare clearance and JSON access; until then we accept any
        successful HTML fetch of the base URL.
        """
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
        try:
            parsed = urlparse(url.strip())
        except (TypeError, ValueError):
            return False
        host = parsed.hostname or ""
        return bool(_URL_HOST_PATTERN.match(host))

    def parse_identifier(self, url_or_slug: str) -> str | None:
        """Extract a canonical comix.to hid from input.

        Accepts either a full ``https://comix.to/title/<hid>-...`` URL,
        an older ``/manga/<slug>`` URL, or a bare slug / hid. Returns
        ``None`` for empty input or URLs
        whose host does not match a comix.to mirror.
        """
        token = url_or_slug.strip()
        if not token:
            return None
        if "://" in token:
            try:
                parsed = urlparse(token)
            except (TypeError, ValueError):
                return None
            host = parsed.hostname or ""
            if not _URL_HOST_PATTERN.match(host):
                return None
            parts = [part for part in parsed.path.strip("/").split("/") if part]
            if len(parts) >= 2 and parts[0].lower() == "title":
                return parts[1].split("-", 1)[0] or None
            if len(parts) >= 2 and parts[0].lower() == "manga":
                return parts[1] or None
            # Final path segment fallback (older URL shapes).
            tail = token.rstrip("/").split("/")[-1]
            return tail or None
        # Bare slug or hid - accept verbatim.
        return token

    # -- Content operations -------------------------------------------------

    async def search(
        self,
        engine: Engine,
        query: str,
        *,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Search comix.to for series matching *query*."""
        base = self.mirrors[0]
        api_url = (
            f"{base}/api/v1/manga"
            f"?keyword={quote(query)}"
            f"&order[relevance]=desc"
            f"&limit={limit}"
        )
        try:
            resp = await engine.get_json(api_url)
        except Exception as exc:
            message = _describe_api_error(exc, action=f"Search for '{query}'")
            logger.error("%s", message)
            raise RemoteApiError(message) from exc

        results: list[SearchResult] = []
        result_obj = resp.get("result", resp)
        items = result_obj.get("items", []) if isinstance(result_obj, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            hid = str(item.get("hid", "") or item.get("hash_id", "") or "")
            url = str(item.get("url", "") or "")
            slug = self._slug_from_title_url(url, hid)
            if title and hid:
                series_url = self._absolute_site_url(base, url) if url else f"{base}/title/{hid}"
                results.append(SearchResult(
                    title=str(title), url=series_url, slug=slug, hash_id=hid,
                ))
        logger.info("Search '%s': %d results", query, len(results))
        return results

    async def get_series(self, engine: Engine, identifier: str) -> SeriesInfo:
        """Resolve *identifier* (slug or hid) into a full SeriesInfo.

        The v1 API accepts the short ``hid`` directly. If the caller
        supplied an old slug that no longer resolves, fall back to
        search and use the matched result's hid.
        """
        data, hash_id = await self._resolve_series_payload(engine, identifier)
        base = self.mirrors[0]
        title = str(data.get("title", "") or hash_id)
        synopsis = str(data.get("synopsis", "") or data.get("description", "") or "")
        authors = self._names_from_people(data.get("authors", []))
        genres = self._titles_from_taxonomy(data.get("genres", []))
        series_url = self._absolute_site_url(base, str(data.get("url", "") or "")) or f"{base}/title/{hash_id}"

        chapters, dedup_decisions = await self._fetch_chapters(engine, hash_id)

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

    async def _resolve_series_payload(
        self,
        engine: Engine,
        identifier: str,
    ) -> tuple[dict[str, object], str]:
        """Resolve an identifier into raw series payload plus hash id."""
        data = await self._fetch_series_payload_direct(engine, identifier)
        hash_id = str(data.get("hid", "") or data.get("hash_id", "") or "")
        if not hash_id:
            data, hash_id = await self._fetch_series_payload_fallback(engine, identifier)
        return data, hash_id

    async def _fetch_series_payload_direct(self, engine: Engine, identifier: str) -> dict[str, object]:
        """Return direct series lookup payload, or an empty dict when lookup fails."""
        base = self.mirrors[0]
        try:
            info_resp = await engine.get_json(f"{base}/api/v1/manga/{identifier}")
        except Exception:
            logger.debug("Direct lookup failed for '%s', trying search fallback", identifier)
            return {}
        return self._unwrap_object_result(info_resp)

    async def _fetch_series_payload_fallback(
        self,
        engine: Engine,
        identifier: str,
    ) -> tuple[dict[str, object], str]:
        """Resolve old slugs through search or hid-prefix fallback."""
        matched = await self._find_search_match(engine, identifier)
        if matched is not None:
            data = await self._fetch_series_payload_by_hash(engine, matched.hash_id)
            hash_id = str(data.get("hid", "") or data.get("hash_id", "") or matched.hash_id)
            return self._require_series_hash(data, hash_id, identifier)

        if "-" in identifier:
            prefix = identifier.split("-", 1)[0]
            try:
                data = await self._fetch_series_payload_by_hash(engine, prefix)
            except RemoteApiError as exc:
                raise RemoteApiError(f"Could not find manga with identifier '{identifier}'") from exc
            hash_id = str(data.get("hid", "") or data.get("hash_id", "") or "")
            return self._require_series_hash(data, hash_id, identifier)

        raise RemoteApiError(f"Could not find manga with identifier '{identifier}'")

    async def _find_search_match(self, engine: Engine, identifier: str) -> SearchResult | None:
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

    async def _fetch_series_payload_by_hash(self, engine: Engine, hash_id: str) -> dict[str, object]:
        """Fetch a series payload by hash id and wrap remote failures."""
        base = self.mirrors[0]
        try:
            info_resp = await engine.get_json(f"{base}/api/v1/manga/{hash_id}")
        except Exception as exc:
            raise RemoteApiError(
                _describe_api_error(exc, action=f"Fetch series info for '{hash_id}'"),
            ) from exc
        return self._unwrap_object_result(info_resp)

    @staticmethod
    def _require_series_hash(
        data: dict[str, object],
        hash_id: str,
        identifier: str,
    ) -> tuple[dict[str, object], str]:
        """Return resolved data or raise the canonical missing-series error."""
        if not hash_id:
            raise RemoteApiError(f"Could not find manga with identifier '{identifier}'")
        return data, hash_id

    async def get_chapter_images(
        self,
        engine: Engine,
        chapter_id: int,
    ) -> ChapterImages | None:
        """Fetch the ordered image URL list for *chapter_id*."""
        try:
            data = await self._get_chapter_payload(engine, chapter_id)
        except Exception as exc:
            logger.error(
                "%s",
                _describe_api_error(exc, action=f"Fetch chapter images for {chapter_id}"),
            )
            return None
        if data is None:
            return None

        number = normalize_chapter_number(data.get("number", 0))
        name = str(data.get("name", "") or "")
        image_pages = self._extract_image_pages(data)
        if image_pages is None:
            logger.warning("Invalid image payload for chapter %d", chapter_id)
            return None

        label = f"Chapter {number}"
        if name:
            label += f" - {name}"

        if not image_pages:
            logger.warning("No images found for chapter %d", chapter_id)
            return None

        return ChapterImages(
            title=label,
            chapter_label=label,
            image_urls=[page.url for page in image_pages],
            pages=image_pages,
        )

    # -- Site-specific dedup rules -----------------------------------------

    def deduplicate(
        self,
        chapters: list[ChapterInfo],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        """Collapse comix.to chapter duplicates by number / language / subtitle.

        Pure function over already-populated ChapterInfo objects; the
        adapter's :meth:`get_series` flow ensures every chapter has a
        valid ``image_count`` before this call so dedup never needs to
        hit the API.
        """
        if not chapters:
            return chapters, []
        groups: dict[str, list[ChapterInfo]] = defaultdict(list)
        for ch in chapters:
            groups[ch.number].append(ch)

        result: list[ChapterInfo] = []
        decisions: list[DedupDecision] = []
        for chapter_number, chs in groups.items():
            if len(chs) == 1:
                result.append(chs[0])
                continue
            kept, group_decisions = self._resolve_number_group(chapter_number, chs)
            result.extend(kept)
            decisions.extend(group_decisions)

        result.sort(key=lambda c: c.number_sort_key)
        dup_count = len(chapters) - len(result)
        if dup_count:
            logger.info("Removed %d duplicate chapter(s)", dup_count)
        return result, decisions

    # -- Internal helpers ---------------------------------------------------

    async def _fetch_chapters(
        self,
        engine: Engine,
        hash_id: str,
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        """Fetch every chapter page, fill missing image counts, then dedup."""
        all_chapters = await self._fetch_all_chapter_pages(engine, hash_id)
        all_chapters.sort(key=lambda c: c.number_sort_key)
        await self._fill_duplicate_image_counts(engine, all_chapters)

        deduped, decisions = self.deduplicate(all_chapters)
        logger.info("Fetched %d chapters for '%s'", len(deduped), hash_id)
        return deduped, decisions

    async def _fetch_all_chapter_pages(self, engine: Engine, hash_id: str) -> list[ChapterInfo]:
        """Paginate through the chapter listing endpoint."""
        limit = 100
        chapters: list[ChapterInfo] = []
        page = 1
        while True:
            items = await self._fetch_chapter_page_items(engine, hash_id, page=page, limit=limit)
            if not items:
                break
            chapters.extend(self._parse_chapter_items(items))
            if len(items) < limit:
                break
            page += 1
        return chapters

    async def _fetch_chapter_page_items(
        self,
        engine: Engine,
        hash_id: str,
        *,
        page: int,
        limit: int,
    ) -> list[dict[str, object]]:
        """Fetch and validate one chapter-listing page."""
        base = self.mirrors[0]
        api_url = f"{base}/api/v1/manga/{hash_id}/chapters?limit={limit}&page={page}"
        try:
            resp = await engine.get_json(api_url)
        except Exception as exc:
            message = _describe_api_error(exc, action=f"Fetch chapter list page {page} for '{hash_id}'")
            logger.error("%s", message)
            raise RemoteApiError(message) from exc

        self._raise_for_chapter_listing_status(resp, hash_id=hash_id, page=page)
        result_obj = resp.get("result", resp)
        items = result_obj.get("items", []) if isinstance(result_obj, dict) else []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _raise_for_chapter_listing_status(
        resp: dict[str, object],
        *,
        hash_id: str,
        page: int,
    ) -> None:
        """Raise a user-facing API error when the chapter-list status is bad."""
        api_status = resp.get("status")
        if api_status is None:
            return
        status_code = _coerce_api_status(api_status)
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

    async def _fill_duplicate_image_counts(
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
            logger.info("Fetching image counts for %d duplicate chapter candidate(s)…", len(missing))
            count_sem = asyncio.Semaphore(10)

            async def _fetch_count(ch: ChapterInfo) -> None:
                async with count_sem:
                    ch.image_count = await self._get_image_count(engine, ch.chapter_id)

            await asyncio.gather(*[_fetch_count(ch) for ch in missing])

    @staticmethod
    def _unwrap_object_result(response: dict[str, object]) -> dict[str, object]:
        """Return the object stored in result, or the response itself when unwrapped."""
        return _unwrap_object_result(response)

    @staticmethod
    def _parse_chapter_items(items: list[dict[str, object]]) -> list[ChapterInfo]:
        chapters: list[ChapterInfo] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id", item.get("chapter_id", 0))
            chapter_id = _coerce_positive_int(raw_id) or 0
            raw_num = item.get("number", 0)
            number = normalize_chapter_number(raw_num)
            name = str(item.get("name", "") or "")
            lang = str(item.get("language", "en") or "en")
            pages_count = item.get("pages_count", item.get("pagesCount", 0))
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

    @staticmethod
    def _format_dedup_variant(chapter: ChapterInfo) -> str:
        pages = f"{chapter.image_count}p" if chapter.image_count > 0 else "pages=?"
        return f"{chapter.title} [{chapter.language}, {pages}, id={chapter.chapter_id}]"

    @classmethod
    def _build_dedup_decision(
        cls,
        *,
        chapter_number: str,
        reason: str,
        kept: list[ChapterInfo],
        dropped: list[ChapterInfo],
    ) -> DedupDecision:
        return DedupDecision(
            chapter_number=chapter_number,
            reason=reason,
            kept=tuple(cls._format_dedup_variant(ch) for ch in kept),
            dropped=tuple(cls._format_dedup_variant(ch) for ch in dropped),
        )

    @classmethod
    def _resolve_number_group(
        cls,
        chapter_number: str,
        chs: list[ChapterInfo],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        named: dict[tuple[str, str], list[ChapterInfo]] = defaultdict(list)
        unnamed: dict[str, list[ChapterInfo]] = defaultdict(list)
        for ch in chs:
            if ch.name:
                named[(ch.language, ch.name)].append(ch)
            else:
                unnamed[ch.language].append(ch)
        if not named:
            return cls._resolve_unnamed_only(chapter_number, unnamed)
        return cls._resolve_named_with_unnamed(chapter_number, named, unnamed)

    @classmethod
    def _resolve_unnamed_only(
        cls,
        chapter_number: str,
        unnamed: dict[str, list[ChapterInfo]],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        result: list[ChapterInfo] = []
        decisions: list[DedupDecision] = []
        for language_group in unnamed.values():
            best = cls._pick_best(language_group)
            result.append(best)
            dropped = [ch for ch in language_group if ch is not best]
            if dropped:
                decisions.append(cls._build_dedup_decision(
                    chapter_number=chapter_number,
                    reason="same-language duplicate; kept the variant with the highest page count",
                    kept=[best], dropped=dropped,
                ))
        return result, decisions

    @classmethod
    def _resolve_named_with_unnamed(
        cls,
        chapter_number: str,
        named: dict[tuple[str, str], list[ChapterInfo]],
        unnamed: dict[str, list[ChapterInfo]],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        result: list[ChapterInfo] = []
        decisions: list[DedupDecision] = []
        kept_for_number: list[ChapterInfo] = []
        for name_group in named.values():
            best = cls._pick_best(name_group)
            result.append(best)
            kept_for_number.append(best)
            dropped = [ch for ch in name_group if ch is not best]
            if dropped:
                decisions.append(cls._build_dedup_decision(
                    chapter_number=chapter_number,
                    reason="same-language duplicate with the same subtitle; kept the highest page count",
                    kept=[best], dropped=dropped,
                ))
        dropped_unnamed = [ch for lang_group in unnamed.values() for ch in lang_group]
        if dropped_unnamed:
            decisions.append(cls._build_dedup_decision(
                chapter_number=chapter_number,
                reason="unnamed uploads were dropped because named variants exist for this chapter number",
                kept=kept_for_number, dropped=dropped_unnamed,
            ))
        return result, decisions

    @staticmethod
    def _pick_best(candidates: list[ChapterInfo]) -> ChapterInfo:
        """Pick the chapter with the highest page count (tie-break by title length)."""
        return max(candidates, key=lambda ch: (ch.image_count, len(ch.title)))

    @staticmethod
    def _absolute_site_url(base: str, url: str) -> str:
        if not url:
            return ""
        return urljoin(base.rstrip("/") + "/", url)

    @staticmethod
    def _slug_from_title_url(url: str, hid: str) -> str:
        """Extract the human-readable title slug from a v1 title URL."""
        if not url:
            return hid
        try:
            tail = urlparse(url).path.strip("/").split("/")[-1]
        except (TypeError, ValueError):
            return hid
        if hid and tail.startswith(f"{hid}-"):
            return tail[len(hid) + 1:]
        return tail or hid

    @staticmethod
    def _titles_from_taxonomy(raw_items: object) -> list[str]:
        """Pull title/name strings from taxonomy objects such as genres."""
        if not isinstance(raw_items, list):
            return []
        values: list[str] = []
        for item in raw_items:
            if isinstance(item, str) and item:
                values.append(item)
            elif isinstance(item, dict):
                value = item.get("title") or item.get("name")
                if isinstance(value, str) and value:
                    values.append(value)
        return values

    @staticmethod
    def _names_from_people(raw_items: object) -> list[str]:
        """Pull display names from author/artist payloads when present."""
        if not isinstance(raw_items, list):
            return []
        values: list[str] = []
        for item in raw_items:
            if isinstance(item, str) and item:
                values.append(item)
            elif isinstance(item, dict):
                value = item.get("name") or item.get("title")
                if isinstance(value, str) and value:
                    values.append(value)
        return values

    @classmethod
    def _extract_image_urls(cls, data: dict[str, object]) -> list[str] | None:
        """Extract page image URLs from v1 and legacy chapter payloads."""
        pages = cls._extract_image_pages(data)
        if pages is None:
            return None
        return [page.url for page in pages]

    @staticmethod
    def _extract_image_pages(data: dict[str, object]) -> list[ChapterPage] | None:
        """Extract page image metadata from v1 and legacy chapter payloads."""
        pages = data.get("pages")
        if isinstance(pages, dict):
            base_url = str(pages.get("baseUrl", "") or "")
            items = pages.get("items", [])
            if not isinstance(items, list):
                return None
            image_pages: list[ChapterPage] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_url = item.get("url")
                if isinstance(raw_url, str) and raw_url:
                    image_base = base_url
                    scrambled = item.get("s") == 1
                    if scrambled:
                        image_base = re.sub(r"/i/(?=[bh])", "/si/", image_base)
                    image_pages.append(ChapterPage(
                        url=urljoin(image_base, raw_url),
                        width=_coerce_positive_int(item.get("width")),
                        height=_coerce_positive_int(item.get("height")),
                        scrambled=scrambled,
                    ))
            return image_pages
        if isinstance(pages, list):
            image_pages = []
            for item in pages:
                if not isinstance(item, dict):
                    continue
                raw_url = item.get("url")
                if isinstance(raw_url, str) and raw_url:
                    image_pages.append(ChapterPage(url=raw_url))
            return image_pages

        images = data.get("images", [])
        if not isinstance(images, list):
            return None
        image_pages = []
        for img in images:
            if not isinstance(img, dict):
                continue
            url = img.get("url")
            if isinstance(url, str) and url:
                image_pages.append(ChapterPage(url=url))
        return image_pages

    async def _get_chapter_payload(
        self,
        engine: Engine,
        chapter_id: int,
    ) -> dict[str, object] | None:
        """Fetch and memoize a chapter detail payload by chapter id."""
        if chapter_id in self._chapter_payload_cache:
            return self._chapter_payload_cache[chapter_id]
        base = self.mirrors[0]
        api_url = f"{base}/api/v1/chapters/{chapter_id}"
        resp = await engine.get_json(api_url)
        data = resp.get("result", resp)
        payload = data if isinstance(data, dict) else None
        self._chapter_payload_cache[chapter_id] = payload
        return payload

    async def _get_image_count(self, engine: Engine, chapter_id: int) -> int:
        try:
            data = await self._get_chapter_payload(engine, chapter_id)
            if data is None:
                return 0
            image_urls = self._extract_image_urls(data)
            if image_urls is not None:
                return len(image_urls)
        except Exception as exc:
            logger.debug("Failed to get image count for chapter %d: %s", chapter_id, exc)
        return 0


# Module-level singleton registered with the framework.
adapter = ComixToAdapter()
register(adapter)


__all__ = ["ComixToAdapter", "adapter"]
