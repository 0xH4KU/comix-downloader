"""Application use case for chapter download orchestration."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from comix_dl.application.download_reporting import build_download_report
from comix_dl.converters import convert
from comix_dl.downloader import Downloader, DownloadProgress, ensure_complete_download
from comix_dl.errors import ConversionError, PartialDownloadError
from comix_dl.history import HistoryRepository
from comix_dl.notify import send_notification

if TYPE_CHECKING:
    from pathlib import Path

    from comix_dl.cdp_browser import CdpBrowser
    from comix_dl.comix_service import ChapterInfo, ComixService
    from comix_dl.config import AppConfig


DownloadEventKind = Literal[
    "skipped",
    "started",
    "planned",
    "progress",
    "missing_images",
    "failed",
    "partial",
    "converted",
    "conversion_failed",
]
DownloadEventHandler = Callable[["DownloadChapterEvent"], None]
ShutdownCheck = Callable[[], bool]
Notifier = Callable[[str, str], None]
DownloadIssueKind = Literal["missing_images", "failed", "partial", "conversion_failed"]


@dataclass
class DownloadChapterEvent:
    """UI-facing event emitted while a chapter is processed."""

    chapter_id: int
    chapter_title: str
    kind: DownloadEventKind
    completed: int = 0
    total: int | None = None
    output_name: str | None = None
    message: str | None = None


@dataclass
class DownloadSummary:
    """Aggregate result for a batch chapter download run."""

    total_chapters: int
    completed: int
    skipped: int
    partial: int
    failed: int
    total_bytes: int
    elapsed_seconds: float
    issues: tuple[DownloadIssue, ...] = ()


@dataclass(frozen=True)
class DownloadIssue:
    """A normalized chapter-level issue for summary/reporting output."""

    chapter_title: str
    kind: DownloadIssueKind
    message: str


def _emit(on_event: DownloadEventHandler | None, event: DownloadChapterEvent) -> None:
    """Emit a download event if a handler is installed."""
    if on_event is not None:
        on_event(event)


async def download_chapters(
    browser: CdpBrowser,
    service: ComixService,
    *,
    series_title: str,
    chapters: list[ChapterInfo],
    output_dir: Path,
    fmt: str,
    config: AppConfig,
    optimize: bool,
    on_event: DownloadEventHandler | None = None,
    is_shutdown: ShutdownCheck | None = None,
    history_repository: HistoryRepository | None = None,
    notifier: Notifier | None = None,
) -> DownloadSummary:
    """Download, convert, record, and notify for a list of chapters."""
    start_time = time.monotonic()
    completed_ok = 0
    skipped_count = 0
    partial_count = 0
    failed_count = 0
    total_bytes = 0
    issues: list[DownloadIssue] = []
    history = history_repository or HistoryRepository()
    notify = notifier or send_notification

    def should_stop() -> bool:
        return is_shutdown() if is_shutdown is not None else False

    sem = asyncio.Semaphore(config.download.max_concurrent_chapters)

    async def _one(chapter: ChapterInfo) -> None:
        nonlocal completed_ok, skipped_count, partial_count, failed_count, total_bytes

        if should_stop():
            return

        async with sem:
            if should_stop():
                return

            downloader = Downloader(browser, output_dir=output_dir, config=config)

            if downloader.is_chapter_complete(series_title, chapter.title):
                skipped_count += 1
                _emit(
                    on_event,
                    DownloadChapterEvent(
                        chapter_id=chapter.chapter_id,
                        chapter_title=chapter.title,
                        kind="skipped",
                        completed=1,
                        total=1,
                    ),
                )
                return

            _emit(
                on_event,
                DownloadChapterEvent(
                    chapter_id=chapter.chapter_id,
                    chapter_title=chapter.title,
                    kind="started",
                ),
            )

            chapter_data = await service.get_chapter_images(chapter.chapter_id)
            if chapter_data is None:
                failed_count += 1
                issues.append(
                    DownloadIssue(
                        chapter_title=chapter.title,
                        kind="missing_images",
                        message="no images available from remote API",
                    )
                )
                _emit(
                    on_event,
                    DownloadChapterEvent(
                        chapter_id=chapter.chapter_id,
                        chapter_title=chapter.title,
                        kind="missing_images",
                    ),
                )
                return

            total = len(chapter_data.image_urls)
            _emit(
                on_event,
                DownloadChapterEvent(
                    chapter_id=chapter.chapter_id,
                    chapter_title=chapter.title,
                    kind="planned",
                    total=total,
                ),
            )

            def on_progress(progress: DownloadProgress) -> None:
                _emit(
                    on_event,
                    DownloadChapterEvent(
                        chapter_id=chapter.chapter_id,
                        chapter_title=chapter.title,
                        kind="progress",
                        completed=progress.completed,
                        total=progress.total,
                    ),
                )

            downloader._on_progress = on_progress
            download_result = await downloader.download_chapter(
                chapter_data.image_urls,
                series_title,
                chapter_data.chapter_label,
            )
            total_bytes += downloader.bytes_downloaded

            if download_result.status == "failed":
                failed_count += 1
                issues.append(
                    DownloadIssue(
                        chapter_title=chapter.title,
                        kind="failed",
                        message="all image downloads failed",
                    )
                )
                _emit(
                    on_event,
                    DownloadChapterEvent(
                        chapter_id=chapter.chapter_id,
                        chapter_title=chapter.title,
                        kind="failed",
                    ),
                )
                return

            try:
                ensure_complete_download(download_result, chapter_title=chapter.title)
            except PartialDownloadError as exc:
                partial_count += 1
                issues.append(
                    DownloadIssue(
                        chapter_title=chapter.title,
                        kind="partial",
                        message=str(exc),
                    )
                )
                _emit(
                    on_event,
                    DownloadChapterEvent(
                        chapter_id=chapter.chapter_id,
                        chapter_title=chapter.title,
                        kind="partial",
                        message=str(exc),
                    ),
                )
                return

            try:
                output = convert(download_result.chapter_dir, fmt, optimize=optimize, config=config)
                completed_ok += 1
                _emit(
                    on_event,
                    DownloadChapterEvent(
                        chapter_id=chapter.chapter_id,
                        chapter_title=chapter.title,
                        kind="converted",
                        output_name=output.name,
                    ),
                )
            except ConversionError as exc:
                failed_count += 1
                issues.append(
                    DownloadIssue(
                        chapter_title=chapter.title,
                        kind="conversion_failed",
                        message=str(exc),
                    )
                )
                _emit(
                    on_event,
                    DownloadChapterEvent(
                        chapter_id=chapter.chapter_id,
                        chapter_title=chapter.title,
                        kind="conversion_failed",
                        message=str(exc),
                    ),
                )

            chapter_delay = config.download.chapter_delay
            if chapter_delay > 0:
                await asyncio.sleep(random.uniform(chapter_delay * 0.5, chapter_delay * 1.5))

    await asyncio.gather(*[_one(chapter) for chapter in chapters])

    elapsed = time.monotonic() - start_time
    summary = DownloadSummary(
        total_chapters=len(chapters),
        completed=completed_ok,
        skipped=skipped_count,
        partial=partial_count,
        failed=failed_count,
        total_bytes=total_bytes,
        elapsed_seconds=elapsed,
        issues=tuple(sorted(issues, key=lambda issue: (issue.chapter_title, issue.kind, issue.message))),
    )
    report = build_download_report(summary)

    history.record_download(
        title=series_title,
        chapters_count=summary.total_chapters,
        fmt=fmt,
        total_size_bytes=summary.total_bytes,
        completed=summary.completed,
        partial=summary.partial,
        failed=summary.failed,
        skipped=summary.skipped,
        summary_text=report.summary_text,
        issues=list(report.issue_lines),
    )

    if summary.total_chapters > 0:
        notify(f"comix-dl: {series_title}", report.notification_body)

    return summary
