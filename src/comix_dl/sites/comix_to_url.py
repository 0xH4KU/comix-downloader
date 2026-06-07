"""URL helpers for the comix.to site adapter."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

_URL_HOST_PATTERN = re.compile(r"^(?:www\.)?comix\.to$", re.IGNORECASE)


def matches_comix_url(url: str) -> bool:
    """Return whether *url* points at a known comix.to mirror host."""
    try:
        parsed = urlparse(url.strip())
    except (TypeError, ValueError):
        return False
    host = parsed.hostname or ""
    return bool(_URL_HOST_PATTERN.match(host))


def parse_identifier(url_or_slug: str) -> str | None:
    """Extract a canonical comix.to hid, legacy slug, or bare identifier."""
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
        tail = token.rstrip("/").split("/")[-1]
        return tail or None
    return token


def absolute_site_url(base: str, url: str) -> str:
    """Resolve a site-relative URL against *base*."""
    if not url:
        return ""
    return urljoin(base.rstrip("/") + "/", url)


def slug_from_title_url(url: str, hid: str) -> str:
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


__all__ = [
    "absolute_site_url",
    "matches_comix_url",
    "parse_identifier",
    "slug_from_title_url",
]
