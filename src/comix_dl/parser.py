"""Parse comix.to chapter pages to extract image URLs.

Supports two extraction strategies inherited from the Bato engine:
1. Modern script — ``const imgHttps = [...]`` in inline ``<script>`` tags.
2. Qwik/JSON — ``<script type="qwik/json">`` with base-36 token resolution.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import Tag

logger = logging.getLogger(__name__)

_IMG_HTTPS_PATTERN = re.compile(r"const\s+imgHttps\s*=\s*(\[[\s\S]*?\])\s*;", re.IGNORECASE)
_TOKEN_PATTERN = re.compile(r"^[0-9a-z]+$")


@dataclass(frozen=True)
class ChapterData:
    """Parsed chapter information."""

    title: str
    chapter: str
    image_urls: list[str]


def parse_chapter(html: str, url: str) -> ChapterData | None:
    """Extract chapter data from raw HTML.

    Args:
        html: Full HTML content of the chapter page.
        url: The URL the HTML was fetched from (for logging).

    Returns:
        ``ChapterData`` on success, ``None`` if parsing fails.
    """
    soup = BeautifulSoup(html, "html.parser")

    result = _parse_modern_script(soup)
    if result is not None:
        return result

    result = _parse_qwik_payload(soup, url)
    if result is not None:
        return result

    logger.warning("No parser strategy could extract data from %s", url)
    return None


# -- strategy 1: modern script -----------------------------------------------


def _parse_modern_script(soup: BeautifulSoup) -> ChapterData | None:
    """Extract images from ``const imgHttps = [...]`` in inline scripts."""
    for script_tag in soup.find_all("script"):
        if not isinstance(script_tag, Tag):
            continue

        content = script_tag.string or script_tag.get_text()
        if not content:
            continue

        match = _IMG_HTTPS_PATTERN.search(content)
        if not match:
            continue

        try:
            image_urls = json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.debug("Invalid JSON in imgHttps payload")
            continue

        if not isinstance(image_urls, list):
            continue

        filtered = [u for u in image_urls if isinstance(u, str) and u]
        if not filtered:
            continue

        title = _extract_js_string(content, "local_text_sub") or "Manga"
        chapter = _extract_js_string(content, "local_text_epi") or "Chapter"

        return ChapterData(
            title=_sanitize(title),
            chapter=_sanitize(chapter),
            image_urls=filtered,
        )

    return None


# -- strategy 2: qwik/json ---------------------------------------------------


def _parse_qwik_payload(soup: BeautifulSoup, url: str) -> ChapterData | None:
    """Extract images from ``<script type="qwik/json">`` with token resolution."""
    script_tag = soup.find("script", {"type": "qwik/json"})
    if not isinstance(script_tag, Tag):
        return None

    script_content = script_tag.string
    if script_content is None:
        return None

    try:
        data = json.loads(script_content)
    except json.JSONDecodeError:
        logger.exception("Invalid qwik/json in %s", url)
        return None

    objs = data.get("objs", [])
    if not isinstance(objs, list):
        return None

    cache: dict[str, Any] = {}
    chapter_state = next(
        (
            obj
            for obj in objs
            if isinstance(obj, dict) and obj.get("chapterData") and obj.get("comicData")
        ),
        None,
    )
    if not isinstance(chapter_state, dict):
        return None

    chapter_data = _resolve(chapter_state.get("chapterData"), objs, cache)
    comic_data = _resolve(chapter_state.get("comicData"), objs, cache)

    if not isinstance(chapter_data, dict) or not isinstance(comic_data, dict):
        return None

    image_file = _resolve(chapter_data.get("imageFile"), objs, cache)
    image_urls = _resolve(image_file.get("urlList"), objs, cache) if isinstance(image_file, dict) else image_file

    if not isinstance(image_urls, list):
        return None

    filtered = [u for u in image_urls if isinstance(u, str) and u]
    if not filtered:
        return None

    title = comic_data.get("name") or comic_data.get("title") or "Manga"
    chapter = chapter_data.get("dname") or chapter_data.get("title") or "Chapter"

    return ChapterData(
        title=_sanitize(str(title)),
        chapter=_sanitize(str(chapter)),
        image_urls=filtered,
    )


# -- shared helpers -----------------------------------------------------------


def _resolve(value: Any, objs: list[Any], cache: dict[str, Any]) -> Any:
    """Recursively resolve base-36 token references in the qwik objs array."""
    if isinstance(value, str):
        cached = cache.get(value)
        if cached is not None:
            return cached

        if _TOKEN_PATTERN.match(value):
            try:
                index = int(value, 36)
            except ValueError:
                cache[value] = value
                return value

            if 0 <= index < len(objs):
                resolved = objs[index]
                if resolved == value:
                    cache[value] = resolved
                    return resolved
                result = _resolve(resolved, objs, cache)
                cache[value] = result
                return result

        cache[value] = value
        return value

    if isinstance(value, list):
        return [_resolve(item, objs, cache) for item in value]

    if isinstance(value, dict):
        return {key: _resolve(val, objs, cache) for key, val in value.items()}

    return value


def _extract_js_string(content: str, variable_name: str) -> str | None:
    """Extract a string variable value from JavaScript source."""
    pattern = re.compile(rf"const\s+{re.escape(variable_name)}\s*=\s*(['\"])(.*?)\1\s*;", re.DOTALL)
    match = pattern.search(content)
    return match.group(2) if match else None


def _sanitize(name: str) -> str:
    """Return a filesystem-friendly version of *name*."""
    candidate = name.replace(":", " - ")
    candidate = candidate.replace("\n", " ").replace("\r", " ")
    candidate = re.sub(r'[\\/*?"<>|]', " ", candidate)
    candidate = candidate.replace("_", " ")
    candidate = re.sub(r"\s+", " ", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate)
    return candidate.strip(" .") or "item"
