"""JSON parsing helpers for comix.to API payloads."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from comix_dl.core.models import ChapterInfo, ChapterPage, SearchResult, normalize_chapter_number
from comix_dl.sites.comix_to_url import absolute_site_url, slug_from_title_url


def coerce_positive_int(value: object) -> int | None:
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


def coerce_api_status(value: object) -> int | None:
    """Best-effort integer coercion for remote API status fields."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float, str)):
            return int(value)
    except (TypeError, ValueError):
        return None
    return None


def unwrap_object_result(response: dict[str, object]) -> dict[str, object]:
    """Return the object stored in result, or the response itself when unwrapped."""
    raw = response.get("result", response)
    return raw if isinstance(raw, dict) else {}


def parse_search_results(response: dict[str, object], *, base_url: str) -> list[SearchResult]:
    """Parse a search response into framework search results."""
    results: list[SearchResult] = []
    result_obj = response.get("result", response)
    items = result_obj.get("items", []) if isinstance(result_obj, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        hid = str(item.get("hid", "") or item.get("hash_id", "") or "")
        url = str(item.get("url", "") or "")
        slug = slug_from_title_url(url, hid)
        if title and hid:
            series_url = absolute_site_url(base_url, url) if url else f"{base_url}/title/{hid}"
            results.append(SearchResult(
                title=str(title),
                url=series_url,
                slug=slug,
                hash_id=hid,
            ))
    return results


def parse_chapter_items(items: list[dict[str, object]]) -> list[ChapterInfo]:
    """Parse chapter-listing items into framework chapter metadata."""
    chapters: list[ChapterInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id", item.get("chapter_id", 0))
        chapter_id = coerce_positive_int(raw_id) or 0
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


def format_chapter_label(data: dict[str, object]) -> str:
    """Build the display/download label for a chapter detail payload."""
    number = normalize_chapter_number(data.get("number", 0))
    name = str(data.get("name", "") or "")
    label = f"Chapter {number}"
    if name:
        label += f" - {name}"
    return label


def extract_image_urls(
    data: dict[str, object],
    *,
    base_url: str | None = None,
) -> list[str] | None:
    """Extract page image URLs from v1 and legacy chapter payloads."""
    pages = extract_image_pages(data, base_url=base_url)
    if pages is None:
        return None
    return [page.url for page in pages]


def extract_image_pages(
    data: dict[str, object],
    *,
    base_url: str | None = None,
) -> list[ChapterPage] | None:
    """Extract page image metadata from v1 and legacy chapter payloads."""
    chapter_url = absolute_site_url(base_url or "", str(data.get("url", "") or "")) if base_url else ""
    pages = data.get("pages")
    if isinstance(pages, dict):
        return _extract_v1_page_items(pages, chapter_url=chapter_url or None)
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


def _extract_v1_page_items(
    pages: dict[str, object],
    *,
    chapter_url: str | None,
) -> list[ChapterPage] | None:
    base_url = str(pages.get("baseUrl", "") or "")
    items = pages.get("items", [])
    if not isinstance(items, list):
        return None
    image_pages: list[ChapterPage] = []
    for index, item in enumerate(items):
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
                width=coerce_positive_int(item.get("width")),
                height=coerce_positive_int(item.get("height")),
                scrambled=scrambled,
                reader_url=chapter_url if scrambled else None,
                page_index=index if scrambled else None,
            ))
    return image_pages


def titles_from_taxonomy(raw_items: object) -> list[str]:
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


def names_from_people(raw_items: object) -> list[str]:
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


__all__ = [
    "coerce_api_status",
    "coerce_positive_int",
    "extract_image_pages",
    "extract_image_urls",
    "format_chapter_label",
    "names_from_people",
    "parse_chapter_items",
    "parse_search_results",
    "titles_from_taxonomy",
    "unwrap_object_result",
]
