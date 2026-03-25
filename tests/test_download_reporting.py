"""Tests for shared download reporting helpers."""

from __future__ import annotations

from comix_dl.application.download_reporting import build_download_report, format_download_counts
from comix_dl.application.download_usecase import DownloadIssue, DownloadSummary


def test_format_download_counts_uses_stable_order() -> None:
    assert format_download_counts(completed=2, skipped=1, partial=1, failed=0) == "2 downloaded, 1 skipped, 1 partial"


def test_build_download_report_includes_issue_preview_and_notification_excerpt() -> None:
    summary = DownloadSummary(
        total_chapters=3,
        completed=0,
        skipped=1,
        partial=1,
        failed=1,
        total_bytes=2048,
        elapsed_seconds=1.5,
        issues=(
            DownloadIssue(
                chapter_title="Chapter 2",
                kind="partial",
                message="Chapter 2 is incomplete: 1/2 pages failed.",
            ),
            DownloadIssue(
                chapter_title="Chapter 3",
                kind="failed",
                message="no images available from remote API",
            ),
        ),
    )

    report = build_download_report(summary)

    assert report.summary_text == "1 skipped, 1 partial, 1 failed"
    assert report.size_text == "2.0 KB"
    assert report.issue_lines == (
        "Chapter 2: Chapter 2 is incomplete: 1/2 pages failed.",
        "Chapter 3: no images available from remote API",
    )
    assert report.notification_body == (
        "1 skipped, 1 partial, 1 failed (2.0 KB) | "
        "Chapter 2: Chapter 2 is incomplete: 1/2 pages failed. | +1 more issue(s)"
    )


def test_preview_issue_lines_truncates_long_issue_list() -> None:
    summary = DownloadSummary(
        total_chapters=6,
        completed=0,
        skipped=0,
        partial=0,
        failed=6,
        total_bytes=0,
        elapsed_seconds=1.0,
        issues=tuple(
            DownloadIssue(chapter_title=f"Chapter {i}", kind="failed", message="all image downloads failed")
            for i in range(1, 7)
        ),
    )

    report = build_download_report(summary)

    assert report.preview_issue_lines(max_lines=3) == (
        "Chapter 1: all image downloads failed",
        "Chapter 2: all image downloads failed",
        "Chapter 3: all image downloads failed",
        "... and 3 more issue(s)",
    )
