"""Framework-shared data models exchanged between the CLI / application
layers and a :class:`~comix_dl.sites.base.SiteAdapter`.

These dataclasses describe *what* a site adapter returns, not *how* it
fetches it. The framework owns them so the same display, persistence,
and orchestration code keeps working when the active adapter is
replaced.

Two helpers (:func:`normalize_chapter_number` and
:func:`chapter_number_sort_key`) live here too. They are not strictly
site-specific — natural-sort behaviour for chapter labels like
``"10.5"`` and ``"Vol 11"`` is what every adapter would re-implement
otherwise — and keeping them framework-side avoids duplicating the
logic across adapters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def normalize_chapter_number(raw_number: object) -> str:
    """Coerce *raw_number* into a stable string representation.

    Adapters consume chapter numbers from heterogeneous JSON payloads
    where the field may arrive as ``str``, ``int``, or ``float``. This
    function normalises any of those into a string while preserving
    user-meaningful distinctions like ``"10"`` vs ``"10.5"`` and
    avoiding silent float coercion that would lose precision.
    """
    if isinstance(raw_number, str):
        normalized = raw_number.strip()
    elif isinstance(raw_number, int):
        normalized = str(raw_number)
    elif isinstance(raw_number, float):
        normalized = format(raw_number, "g")
    else:
        normalized = "0"
    return normalized or "0"


def chapter_number_sort_key(number: str) -> tuple[tuple[int, str], ...]:
    """Build a stable natural-sort key for a chapter number string.

    Splits *number* into digit / non-digit runs and tags each run so
    that ``"2"`` sorts before ``"10"`` and ``"10.5"`` sorts between
    ``"10"`` and ``"11"``. The resulting tuple is suitable for use as a
    ``key=`` argument to :func:`sorted`.
    """
    tokens = re.findall(r"\d+|[^\d]+", number.lower())
    key: list[tuple[int, str]] = []
    for token in tokens:
        if token.isdigit():
            key.append((0, token.zfill(12)))
            continue
        cleaned = re.sub(r"[^a-z]+", "", token)
        if cleaned:
            key.append((1, cleaned))
    return tuple(key) or ((0, "000000000000"),)


@dataclass
class SearchResult:
    """A single search result.

    ``hash_id`` is the canonical identifier used by adapters that have
    such a concept (e.g. comix.to). For sites that identify series
    differently (UUID, slug-only, numeric id), adapters may store the
    canonical identifier in ``hash_id`` regardless of the underlying
    representation; the field name is preserved for backwards
    compatibility with persisted history records.
    """

    title: str
    url: str
    slug: str
    hash_id: str


@dataclass
class ChapterInfo:
    """Chapter metadata.

    The ``number`` field is normalised through
    :func:`normalize_chapter_number` and a derived ``number_sort_key``
    is precomputed in ``__post_init__`` so the framework can sort
    large chapter lists without recomputing keys.
    """

    title: str
    chapter_id: int
    number: str
    name: str = ""  # subtitle (e.g. "Dear Little Brother")
    language: str = "en"
    image_count: int = 0
    number_sort_key: tuple[tuple[int, str], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.number = normalize_chapter_number(self.number)
        self.number_sort_key = chapter_number_sort_key(self.number)


@dataclass
class ChapterPage:
    """A single resolved chapter page.

    ``scrambled`` marks comix.to pages that must be rendered through
    the site's reader canvas before saving. ``image_urls`` remains on
    :class:`ChapterImages` for compatibility with callers that only
    need plain URLs.
    """

    url: str
    width: int | None = None
    height: int | None = None
    scrambled: bool = False


@dataclass
class ChapterImages:
    """Resolved image pages for a single chapter."""

    title: str
    chapter_label: str
    image_urls: list[str]
    pages: list[ChapterPage] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.pages:
            self.image_urls = [page.url for page in self.pages]
            return
        self.pages = [ChapterPage(url=url) for url in self.image_urls]


@dataclass
class DedupDecision:
    """Human-readable explanation of a deduplication decision.

    Adapters that collapse duplicate chapters return a list of these
    so the CLI can surface what was kept and what was dropped before
    the user commits to a download.
    """

    chapter_number: str
    reason: str
    kept: tuple[str, ...]
    dropped: tuple[str, ...]


@dataclass
class SeriesInfo:
    """Full series metadata, including the sorted chapter list."""

    title: str
    authors: list[str]
    genres: list[str]
    description: str
    chapters: list[ChapterInfo]
    url: str
    hash_id: str
    dedup_decisions: list[DedupDecision] = field(default_factory=list)


__all__ = [
    "ChapterImages",
    "ChapterInfo",
    "ChapterPage",
    "DedupDecision",
    "SearchResult",
    "SeriesInfo",
    "chapter_number_sort_key",
    "normalize_chapter_number",
]
