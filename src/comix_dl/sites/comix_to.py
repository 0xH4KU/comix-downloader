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


# JS IIFE registered with the engine after Cloudflare clearance. The
# current comix.to frontend is a Vite app whose API helper owns both
# request signing and encrypted-response decoding. Each browser page
# imports same-origin frontend modules and dynamically selects the
# export that looks like the API client, avoiding brittle dependency on
# minified export names.
_COMIX_API_CLIENT_IIFE = """
(async function() {
    try {
        window.__comixUrlTransformers = window.__comixUrlTransformers || [];

        function __comixLooksLikeApiClient(value) {
            return value
                && typeof value === 'object'
                && typeof value.get === 'function'
                && typeof value.post === 'function';
        }

        window.__comixGetApiClient = window.__comixGetApiClient || async function() {
            if (window.__comixApiClient) return window.__comixApiClient;

            const scripts = Array.from(document.querySelectorAll('script[src]'));
            const moduleUrls = [];
            for (const script of scripts) {
                const src = script.getAttribute('src') || '';
                if (src.includes('/assets/build/') && src.endsWith('.js')) {
                    moduleUrls.push(new URL(src, window.location.href).href);
                }
            }

            for (const moduleUrl of moduleUrls) {
                try {
                    const mod = await import(moduleUrl);
                    for (const exported of Object.values(mod)) {
                        if (__comixLooksLikeApiClient(exported)) {
                            window.__comixApiClient = exported;
                            return window.__comixApiClient;
                        }
                    }
                } catch (e) { continue; }
            }

            const mainScript = scripts.find((script) => {
                const src = script.getAttribute('src') || '';
                return src.includes('/assets/build/')
                    && src.includes('/dist/main-')
                    && src.endsWith('.js');
            });
            if (mainScript) {
                try {
                    const moduleUrl = new URL(mainScript.getAttribute('src'), window.location.href).href;
                    const resp = await fetch(moduleUrl);
                    const text = await resp.text();
                    const envMatch = text.match(/from"\\.\\/(env-[^"]+\\.js)"/);
                    if (envMatch) {
                        const envUrl = new URL(envMatch[1], moduleUrl).href;
                        const mod = await import(envUrl);
                        for (const exported of Object.values(mod)) {
                            if (__comixLooksLikeApiClient(exported)) {
                                window.__comixApiClient = exported;
                                return window.__comixApiClient;
                            }
                        }
                    }
                } catch (e) { /* fall through to fetch fallback */ }
            }

            return null;
        };

        window.__comixJsonRequest = async function(method, url, body) {
            const u = new URL(url, window.location.origin);
            if (u.origin !== window.location.origin || !u.pathname.startsWith('/api/v1/')) {
                return { __handled: false };
            }

            const path = u.pathname.replace(/^\\/api\\/v1/, '') || '/';
            const params = {};
            u.searchParams.forEach((rawValue, key) => {
                const numeric = rawValue !== '' ? Number(rawValue) : NaN;
                const value = Number.isNaN(numeric) ? rawValue : numeric;
                if (Object.prototype.hasOwnProperty.call(params, key)) {
                    params[key] = Array.isArray(params[key])
                        ? [...params[key], value]
                        : [params[key], value];
                } else {
                    params[key] = value;
                }
            });

            const api = await window.__comixGetApiClient();
            if (!api) return { __handled: false };

            const config = Object.keys(params).length ? { params } : undefined;
            const upper = String(method || 'GET').toUpperCase();

            try {
                if (upper === 'GET') {
                    return { __handled: true, data: await api.get(path, config) };
                }
                if (upper === 'POST') {
                    return { __handled: true, data: await api.post(path, body, config) };
                }
                if (upper === 'PUT') {
                    return { __handled: true, data: await api.put(path, body, config) };
                }
                if (upper === 'PATCH') {
                    return { __handled: true, data: await api.patch(path, body, config) };
                }
                if (upper === 'DELETE') {
                    return { __handled: true, data: await api.delete(path, config) };
                }
            } catch (e) {
                const status = e && e.response && e.response.status;
                if (status) throw new Error(`HTTP ${status}`);
                throw e;
            }
            return { __handled: false };
        };
    } catch (e) {
        console.warn('[comix-dl] comix.to API client hook install failed:', e);
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
        base = self.mirrors[0]
        info_resp: dict[str, object] | None = None
        try:
            info_resp = await engine.get_json(f"{base}/api/v1/manga/{identifier}")
        except Exception:
            logger.debug("Direct lookup failed for '%s', trying search fallback", identifier)

        data: dict[str, object] = {}
        if info_resp is not None:
            raw = info_resp.get("result", info_resp)
            if isinstance(raw, dict):
                data = raw

        hash_id = str(data.get("hid", "") or data.get("hash_id", "") or "")
        if not hash_id:
            # Fallback: search and match by slug.
            results = await self.search(engine, identifier, limit=10)
            matched = next((
                r for r in results
                if r.hash_id == identifier or r.slug == identifier or r.slug.startswith(f"{identifier}-")
            ), None)
            if matched is not None:
                try:
                    info_resp = await engine.get_json(f"{base}/api/v1/manga/{matched.hash_id}")
                except Exception as exc:
                    raise RemoteApiError(
                        _describe_api_error(exc, action=f"Fetch series info for '{matched.hash_id}'"),
                    ) from exc
            elif "-" in identifier:
                prefix = identifier.split("-", 1)[0]
                try:
                    info_resp = await engine.get_json(f"{base}/api/v1/manga/{prefix}")
                except Exception as exc:
                    raise RemoteApiError(f"Could not find manga with identifier '{identifier}'") from exc
            else:
                raise RemoteApiError(f"Could not find manga with identifier '{identifier}'")
            raw = info_resp.get("result", info_resp) if isinstance(info_resp, dict) else {}
            data = raw if isinstance(raw, dict) else {}
            hash_id = str(data.get("hid", "") or data.get("hash_id", "") or "")
            if not hash_id and matched is not None:
                hash_id = matched.hash_id
            if not hash_id:
                raise RemoteApiError(f"Could not find manga with identifier '{identifier}'")

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
        base = self.mirrors[0]
        limit = 100
        all_chapters: list[ChapterInfo] = []
        page = 1
        while True:
            api_url = f"{base}/api/v1/manga/{hash_id}/chapters?limit={limit}&page={page}"
            try:
                resp = await engine.get_json(api_url)
            except Exception as exc:
                message = _describe_api_error(exc, action=f"Fetch chapter list page {page} for '{hash_id}'")
                logger.error("%s", message)
                raise RemoteApiError(message) from exc

            result_obj = resp.get("result", resp)
            api_status = resp.get("status")
            if api_status is not None:
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
