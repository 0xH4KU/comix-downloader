"""comix.to site adapter.

Implements the :class:`~comix_dl.sites.base.SiteAdapter` protocol against
the comix.to v2 REST API. URL handling, JSON schema parsing, and
chapter deduplication rules are all comix.to-specific and live here so
the framework code stays site-agnostic.

Endpoints used:

- ``GET /api/v2/manga?keyword=...`` — search
- ``GET /api/v2/manga/{slug_or_hash}`` — series detail
- ``GET /api/v2/manga/{hash_id}/chapters`` — paginated chapter list
- ``GET /api/v2/chapters/{chapter_id}`` — chapter image URLs

This module registers a singleton adapter instance with the
framework registry at import time.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

from comix_dl.core.errors import BrowserTimeoutError, Http403Error, RemoteApiError
from comix_dl.core.models import (
    ChapterImages,
    ChapterInfo,
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


# JS IIFE registered with the engine after Cloudflare clearance. Each
# page (main + pool) evaluates this once at install time:
#
#   1. If the comix.to signing function is not yet on this page, scan
#      the loaded Next.js chunks for the API client bundle and pull
#      out the obfuscated signing IIFE the site itself uses.
#   2. eval the IIFE so window.__comixSign(method, path, options) is
#      available on this page.
#   3. Push a URL transformer onto window.__comixUrlTransformers that
#      signs ``/chapters`` requests on every outbound fetch.
#
# Hardening of the IIFE extraction step (hash + disk cache + sanity
# whitelist) is deferred to a follow-up patch (todo.md / "待討論"
# section). Today: we either get a fresh IIFE on every page or fail
# loudly with a clear error message.
_SIGNING_TRANSFORMER_IIFE = """
(async function() {
    try {
        if (typeof window.__comixSign !== 'function') {
            const scripts = document.querySelectorAll('script[src*="_next/static/chunks"]');
            for (const script of scripts) {
                try {
                    const resp = await fetch(script.src);
                    const text = await resp.text();
                    if (!text.includes('baseUrl:"https://comix.to/api/v2/"')) continue;
                    const classIdx = text.indexOf('class n extends Error{response');
                    if (classIdx === -1) continue;
                    const iifeStart = text.lastIndexOf('let i=', classIdx);
                    if (iifeStart === -1) continue;
                    const iifeEndIdx = text.substring(iifeStart, classIdx).lastIndexOf('}();');
                    if (iifeEndIdx === -1) continue;
                    const iife = text.substring(iifeStart, iifeStart + iifeEndIdx + 4);
                    eval('window.__comixSign = ' + iife.substring('let i='.length));
                    if (typeof window.__comixSign === 'function') break;
                } catch (e) { continue; }
            }
        }
        if (typeof window.__comixSign !== 'function') {
            console.warn('[comix-dl] could not extract /chapters signing function; '
                       + 'chapter listings may return HTTP 403.');
            return;
        }
        window.__comixUrlTransformers = window.__comixUrlTransformers || [];
        window.__comixUrlTransformers.push(function(method, url) {
            if (!url.includes('/chapters')) return url;
            try {
                const u = new URL(url);
                const path = u.pathname.replace(new RegExp("^/api/v2"), '');
                const queryObj = {};
                u.searchParams.forEach((v, k) => { queryObj[k] = isNaN(v) ? v : Number(v); });
                const signed = window.__comixSign('GET', path, { query: queryObj });
                const newUrl = new URL(u.origin + u.pathname);
                for (const [k, v] of Object.entries(signed.query)) {
                    newUrl.searchParams.set(k, String(v));
                }
                return newUrl.toString();
            } catch (e) { return url; }
        });
    } catch (e) {
        console.warn('[comix-dl] signing transformer install failed:', e);
    }
})();
"""


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


def _coerce_int(value: object) -> int | None:
    """Best-effort integer coercion for remote API scalar fields."""
    try:
        if isinstance(value, (int, float, str)):
            return int(value)
    except (TypeError, ValueError):
        return None
    return None


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
        """Install the comix.to URL signing transformer.

        The transformer IIFE is replayed against every browser page
        the engine creates (main and pool); that IIFE extracts the
        site's own signing function from its Next.js chunks and
        registers a URL transformer that signs ``/chapters`` requests
        before they leave the page.
        """
        engine.register_url_transformer(_SIGNING_TRANSFORMER_IIFE)

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
        """Extract a canonical comix.to slug or hash_id from input.

        Accepts either a full ``https://comix.to/manga/<slug>`` URL or
        a bare slug / hash. Returns ``None`` for empty input or URLs
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
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0].lower() == "manga":
                return parts[1] or None
            # Final path segment fallback (older URL shapes).
            return token.rstrip("/").split("/")[-1] or None
        # Bare slug or hash — accept verbatim.
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
            f"{base}/api/v2/manga"
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
        result_obj = resp.get("result", {})
        items = result_obj.get("items", []) if isinstance(result_obj, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            slug = item.get("slug", "")
            hash_id = item.get("hash_id", "")
            if title and hash_id:
                series_url = f"{base}/manga/{slug or hash_id}"
                results.append(SearchResult(
                    title=title, url=series_url, slug=slug, hash_id=hash_id,
                ))
        logger.info("Search '%s': %d results", query, len(results))
        return results

    async def get_series(self, engine: Engine, identifier: str) -> SeriesInfo:
        """Resolve *identifier* (slug or hash_id) into a full SeriesInfo.

        Tries direct lookup first, falling back to keyword search if
        the identifier is a slug not recognised by the manga endpoint.
        """
        base = self.mirrors[0]
        # Direct lookup (works for both slug and hash_id targets).
        info_resp: dict[str, object] | None = None
        try:
            info_resp = await engine.get_json(f"{base}/api/v2/manga/{identifier}")
        except Exception:
            logger.debug("Direct lookup failed for '%s', trying search fallback", identifier)

        data: dict[str, object] = {}
        if info_resp is not None:
            raw = info_resp.get("result", {})
            if isinstance(raw, dict):
                data = raw

        hash_id = str(data.get("hash_id", "") or "")
        if not hash_id:
            # Fallback: search and match by slug.
            results = await self.search(engine, identifier, limit=10)
            matched = next((r for r in results if r.slug == identifier), None)
            if matched is None:
                raise RemoteApiError(f"Could not find manga with identifier '{identifier}'")
            try:
                info_resp = await engine.get_json(f"{base}/api/v2/manga/{matched.hash_id}")
            except Exception as exc:
                raise RemoteApiError(
                    _describe_api_error(exc, action=f"Fetch series info for '{matched.hash_id}'"),
                ) from exc
            raw = info_resp.get("result", {}) if isinstance(info_resp, dict) else {}
            data = raw if isinstance(raw, dict) else {}
            hash_id = str(data.get("hash_id", "") or matched.hash_id)

        title = str(data.get("title", "") or hash_id)
        slug = str(data.get("slug", "") or "")
        synopsis = str(data.get("synopsis", "") or data.get("description", "") or "")

        chapters, dedup_decisions = await self._fetch_chapters(engine, hash_id)

        return SeriesInfo(
            title=title,
            authors=[],
            genres=[],
            description=synopsis,
            chapters=chapters,
            dedup_decisions=dedup_decisions,
            url=f"{base}/manga/{slug or hash_id}",
            hash_id=hash_id,
        )

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
        images = data.get("images", [])
        if not isinstance(images, list):
            logger.warning("Invalid image payload for chapter %d", chapter_id)
            return None

        label = f"Chapter {number}"
        if name:
            label += f" - {name}"

        image_urls: list[str] = []
        for img in images:
            if not isinstance(img, dict):
                continue
            url = img.get("url")
            if isinstance(url, str) and url:
                image_urls.append(url)
        if not image_urls:
            logger.warning("No images found for chapter %d", chapter_id)
            return None

        return ChapterImages(title=label, chapter_label=label, image_urls=image_urls)

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
        base = self.mirrors[0]
        limit = 100
        all_chapters: list[ChapterInfo] = []
        page = 1
        while True:
            api_url = f"{base}/api/v2/manga/{hash_id}/chapters?limit={limit}&page={page}"
            try:
                resp = await engine.get_json(api_url)
            except Exception as exc:
                message = _describe_api_error(exc, action=f"Fetch chapter list page {page} for '{hash_id}'")
                logger.error("%s", message)
                raise RemoteApiError(message) from exc

            result_obj = resp.get("result", {})
            api_status = resp.get("status")
            if api_status is not None:
                status_code = _coerce_int(api_status)
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
            items = result_obj.get("items", []) if isinstance(result_obj, dict) else []
            if not isinstance(items, list) or not items:
                break
            all_chapters.extend(self._parse_chapter_items(items))
            if len(items) < limit:
                break
            page += 1

        all_chapters.sort(key=lambda c: c.number_sort_key)

        # Fill missing image counts only for duplicate-number groups where
        # image_count affects dedup choice. Unique chapters can stay lazy.
        by_number: dict[str, list[ChapterInfo]] = defaultdict(list)
        for ch in all_chapters:
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

        deduped, decisions = self.deduplicate(all_chapters)
        logger.info("Fetched %d chapters for '%s'", len(deduped), hash_id)
        return deduped, decisions

    @staticmethod
    def _parse_chapter_items(items: list[dict[str, object]]) -> list[ChapterInfo]:
        chapters: list[ChapterInfo] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            chapter_id = _coerce_int(item.get("chapter_id", 0)) or 0
            raw_num = item.get("number", 0)
            number = normalize_chapter_number(raw_num)
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

    async def _get_chapter_payload(
        self,
        engine: Engine,
        chapter_id: int,
    ) -> dict[str, object] | None:
        """Fetch and memoize a chapter detail payload by chapter id."""
        if chapter_id in self._chapter_payload_cache:
            return self._chapter_payload_cache[chapter_id]
        base = self.mirrors[0]
        api_url = f"{base}/api/v2/chapters/{chapter_id}"
        resp = await engine.get_json(api_url)
        data = resp.get("result", {})
        payload = data if isinstance(data, dict) else None
        self._chapter_payload_cache[chapter_id] = payload
        return payload

    async def _get_image_count(self, engine: Engine, chapter_id: int) -> int:
        try:
            data = await self._get_chapter_payload(engine, chapter_id)
            if data is None:
                return 0
            images = data.get("images", [])
            if isinstance(images, list):
                return len(images)
        except Exception as exc:
            logger.debug("Failed to get image count for chapter %d: %s", chapter_id, exc)
        return 0


# Module-level singleton registered with the framework.
adapter = ComixToAdapter()
register(adapter)


__all__ = ["ComixToAdapter", "adapter"]
