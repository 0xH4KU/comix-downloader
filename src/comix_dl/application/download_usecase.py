"""Application use case for chapter download orchestration."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from comix_dl.application.download_reporting import build_download_report
from comix_dl.converters import convert_async
from comix_dl.downloader import Downloader, DownloadProgress, ensure_complete_download
from comix_dl.errors import ConversionError, PartialDownloadError
from comix_dl.history import HistoryRepository
from comix_dl.logging_utils import log_context
from comix_dl.notify import send_notification

if TYPE_CHECKING:
    from pathlib import Path

    from comix_dl.cdp_browser import CdpBrowser
    from comix_dl.comix_service import ChapterInfo, ComixService
    from comix_dl.config import AppConfig


logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _ChapterOutcome:
    """Result of processing one chapter — returned by _process_one_chapter."""

    status: Literal["completed", "skipped", "partial", "failed"]
    total_bytes: int = 0
    issue: DownloadIssue | None = None


async def _process_one_chapter(
    chapter: ChapterInfo,
    *,
    browser: CdpBrowser,
    service: ComixService,
    series_title: str,
    output_dir: Path,
    fmt: str,
    config: AppConfig,
    optimize: bool,
    on_event: DownloadEventHandler | None,
) -> _ChapterOutcome:
    """Download, validate, and convert a single chapter.

    Returns a frozen outcome so the caller can aggregate results safely.
    """
    downloader = Downloader(browser, output_dir=output_dir, config=config)
    chapter_start = time.monotonic()

    def _log(status: str, *, bytes_downloaded: int, message: str | None = None) -> None:
        logger.info(
            "chapter_download_finished",
            extra=log_context(
                series=series_title,
                chapter_id=chapter.chapter_id,
                chapter_title=chapter.title,
                status=status,
                bytes=bytes_downloaded,
                retry_count=downloader.retry_count,
                elapsed=time.monotonic() - chapter_start,
                message=message,
            ),
        )

    # Already complete on disk — skip
    if downloader.is_chapter_complete(series_title, chapter.title):
        _log("skipped", bytes_downloaded=0)
        _emit(on_event, DownloadChapterEvent(
            chapter_id=chapter.chapter_id, chapter_title=chapter.title,
            kind="skipped", completed=1, total=1,
        ))
        return _ChapterOutcome(status="skipped")

    _emit(on_event, DownloadChapterEvent(
        chapter_id=chapter.chapter_id, chapter_title=chapter.title, kind="started",
    ))

    # Fetch image URLs
    chapter_data = await service.get_chapter_images(chapter.chapter_id)
    if chapter_data is None:
        msg = "no images available from remote API"
        _log("missing_images", bytes_downloaded=0, message=msg)
        _emit(on_event, DownloadChapterEvent(
            chapter_id=chapter.chapter_id, chapter_title=chapter.title, kind="missing_images",
        ))
        return _ChapterOutcome(
            status="failed",
            issue=DownloadIssue(chapter_title=chapter.title, kind="missing_images", message=msg),
        )

    total = len(chapter_data.image_urls)
    _emit(on_event, DownloadChapterEvent(
        chapter_id=chapter.chapter_id, chapter_title=chapter.title, kind="planned", total=total,
    ))

    # Wire up progress callback
    def _on_progress(progress: DownloadProgress) -> None:
        _emit(on_event, DownloadChapterEvent(
            chapter_id=chapter.chapter_id, chapter_title=chapter.title,
            kind="progress", completed=progress.completed, total=progress.total,
        ))

    downloader._on_progress = _on_progress
    download_result = await downloader.download_chapter(
        chapter_data.image_urls, series_title, chapter_data.chapter_label,
    )
    dl_bytes = downloader.bytes_downloaded

    # All images failed
    if download_result.status == "failed":
        msg = "all image downloads failed"
        _log("failed", bytes_downloaded=dl_bytes, message=msg)
        _emit(on_event, DownloadChapterEvent(
            chapter_id=chapter.chapter_id, chapter_title=chapter.title, kind="failed",
        ))
        return _ChapterOutcome(
            status="failed", total_bytes=dl_bytes,
            issue=DownloadIssue(chapter_title=chapter.title, kind="failed", message=msg),
        )

    # Partial — some images failed
    try:
        ensure_complete_download(download_result, chapter_title=chapter.title)
    except PartialDownloadError as exc:
        _log("partial", bytes_downloaded=dl_bytes, message=str(exc))
        _emit(on_event, DownloadChapterEvent(
            chapter_id=chapter.chapter_id, chapter_title=chapter.title,
            kind="partial", message=str(exc),
        ))
        return _ChapterOutcome(
            status="partial", total_bytes=dl_bytes,
            issue=DownloadIssue(chapter_title=chapter.title, kind="partial", message=str(exc)),
        )

    # Convert
    try:
        output = await convert_async(download_result.chapter_dir, fmt, optimize=optimize, config=config)
        _log("converted", bytes_downloaded=dl_bytes, message=output.name)
        _emit(on_event, DownloadChapterEvent(
            chapter_id=chapter.chapter_id, chapter_title=chapter.title,
            kind="converted", output_name=output.name,
        ))
        return _ChapterOutcome(status="completed", total_bytes=dl_bytes)
    except ConversionError as exc:
        _log("conversion_failed", bytes_downloaded=dl_bytes, message=str(exc))
        _emit(on_event, DownloadChapterEvent(
            chapter_id=chapter.chapter_id, chapter_title=chapter.title,
            kind="conversion_failed", message=str(exc),
        ))
        return _ChapterOutcome(
            status="failed", total_bytes=dl_bytes,
            issue=DownloadIssue(chapter_title=chapter.title, kind="conversion_failed", message=str(exc)),
        )


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
    history = history_repository or HistoryRepository()
    notify = notifier or send_notification

    def should_stop() -> bool:
        return is_shutdown() if is_shutdown is not None else False

    sem = asyncio.Semaphore(config.download.max_concurrent_chapters)

    async def _run_one(chapter: ChapterInfo) -> _ChapterOutcome | None:
        if should_stop():
            return None
        async with sem:
            if should_stop():
                return None
            outcome = await _process_one_chapter(
                chapter,
                browser=browser,
                service=service,
                series_title=series_title,
                output_dir=output_dir,
                fmt=fmt,
                config=config,
                optimize=optimize,
                on_event=on_event,
            )
            chapter_delay = config.download.chapter_delay
            if chapter_delay > 0:
                await asyncio.sleep(random.uniform(chapter_delay * 0.5, chapter_delay * 1.5))
            return outcome

    raw_outcomes = await asyncio.gather(*[_run_one(ch) for ch in chapters])
    outcomes = [o for o in raw_outcomes if o is not None]

    # Aggregate results from returned outcomes — no shared mutable state
    completed_ok = sum(1 for o in outcomes if o.status == "completed")
    skipped_count = sum(1 for o in outcomes if o.status == "skipped")
    partial_count = sum(1 for o in outcomes if o.status == "partial")
    failed_count = sum(1 for o in outcomes if o.status == "failed")
    total_bytes = sum(o.total_bytes for o in outcomes)
    issues = sorted(
        [o.issue for o in outcomes if o.issue is not None],
        key=lambda issue: (issue.chapter_title, issue.kind, issue.message),
    )

    elapsed = time.monotonic() - start_time
    summary = DownloadSummary(
        total_chapters=len(chapters),
        completed=completed_ok,
        skipped=skipped_count,
        partial=partial_count,
        failed=failed_count,
        total_bytes=total_bytes,
        elapsed_seconds=elapsed,
        issues=tuple(issues),
    )
    report = build_download_report(summary)
    logger.info(
        "download_batch_finished",
        extra=log_context(
            series=series_title,
            status="degraded" if summary.partial or summary.failed else "ok",
            bytes=summary.total_bytes,
            elapsed=summary.elapsed_seconds,
            completed=summary.completed,
            skipped=summary.skipped,
            partial=summary.partial,
            failed=summary.failed,
        ),
    )

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
