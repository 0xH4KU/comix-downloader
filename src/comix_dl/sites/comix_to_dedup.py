"""Chapter deduplication rules for comix.to."""

from __future__ import annotations

import logging
from collections import defaultdict

from comix_dl.core.models import ChapterInfo, DedupDecision

logger = logging.getLogger(__name__)


def deduplicate_chapters(chapters: list[ChapterInfo]) -> tuple[list[ChapterInfo], list[DedupDecision]]:
    """Collapse comix.to chapter duplicates by number / language / subtitle."""
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
        kept, group_decisions = _resolve_number_group(chapter_number, chs)
        result.extend(kept)
        decisions.extend(group_decisions)

    result.sort(key=lambda c: c.number_sort_key)
    dup_count = len(chapters) - len(result)
    if dup_count:
        logger.info("Removed %d duplicate chapter(s)", dup_count)
    return result, decisions


def format_dedup_variant(chapter: ChapterInfo) -> str:
    """Format a variant for human-readable dedup reporting."""
    pages = f"{chapter.image_count}p" if chapter.image_count > 0 else "pages=?"
    return f"{chapter.title} [{chapter.language}, {pages}, id={chapter.chapter_id}]"


def build_dedup_decision(
    *,
    chapter_number: str,
    reason: str,
    kept: list[ChapterInfo],
    dropped: list[ChapterInfo],
) -> DedupDecision:
    """Build a framework dedup decision for one duplicate group."""
    return DedupDecision(
        chapter_number=chapter_number,
        reason=reason,
        kept=tuple(format_dedup_variant(ch) for ch in kept),
        dropped=tuple(format_dedup_variant(ch) for ch in dropped),
    )


def _resolve_number_group(
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
        return _resolve_unnamed_only(chapter_number, unnamed)
    return _resolve_named_with_unnamed(chapter_number, named, unnamed)


def _resolve_unnamed_only(
    chapter_number: str,
    unnamed: dict[str, list[ChapterInfo]],
) -> tuple[list[ChapterInfo], list[DedupDecision]]:
    result: list[ChapterInfo] = []
    decisions: list[DedupDecision] = []
    for language_group in unnamed.values():
        best = pick_best(language_group)
        result.append(best)
        dropped = [ch for ch in language_group if ch is not best]
        if dropped:
            decisions.append(build_dedup_decision(
                chapter_number=chapter_number,
                reason="same-language duplicate; kept the variant with the highest page count",
                kept=[best],
                dropped=dropped,
            ))
    return result, decisions


def _resolve_named_with_unnamed(
    chapter_number: str,
    named: dict[tuple[str, str], list[ChapterInfo]],
    unnamed: dict[str, list[ChapterInfo]],
) -> tuple[list[ChapterInfo], list[DedupDecision]]:
    result: list[ChapterInfo] = []
    decisions: list[DedupDecision] = []
    kept_for_number: list[ChapterInfo] = []
    for name_group in named.values():
        best = pick_best(name_group)
        result.append(best)
        kept_for_number.append(best)
        dropped = [ch for ch in name_group if ch is not best]
        if dropped:
            decisions.append(build_dedup_decision(
                chapter_number=chapter_number,
                reason="same-language duplicate with the same subtitle; kept the highest page count",
                kept=[best],
                dropped=dropped,
            ))
    dropped_unnamed = [ch for lang_group in unnamed.values() for ch in lang_group]
    if dropped_unnamed:
        decisions.append(build_dedup_decision(
            chapter_number=chapter_number,
            reason="unnamed uploads were dropped because named variants exist for this chapter number",
            kept=kept_for_number,
            dropped=dropped_unnamed,
        ))
    return result, decisions


def pick_best(candidates: list[ChapterInfo]) -> ChapterInfo:
    """Pick the chapter with the highest page count (tie-break by title length)."""
    return max(candidates, key=lambda ch: (ch.image_count, len(ch.title)))


__all__ = [
    "build_dedup_decision",
    "deduplicate_chapters",
    "format_dedup_variant",
    "pick_best",
]
