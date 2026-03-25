"""Tests for application download orchestration use case."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from comix_dl.application import download_usecase
from comix_dl.comix_service import ChapterImages, ChapterInfo
from comix_dl.config import AppConfig
from comix_dl.downloader import ChapterDownloadResult, DownloadProgress

if TYPE_CHECKING:
    from pathlib import Path


class RecordingHistoryRepository:
    """Capture history writes for assertions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_download(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def _config() -> AppConfig:
    config = AppConfig()
    config.download.max_concurrent_chapters = 1
    config.download.max_concurrent_images = 1
    config.download.image_delay = 0.0
    config.download.chapter_delay = 0.0
    return config


@pytest.mark.asyncio
async def test_download_chapters_emits_events_records_history_and_formats_notification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[download_usecase.DownloadChapterEvent] = []
    notifications: list[tuple[str, str]] = []
    history = RecordingHistoryRepository()

    class FakeDownloader:
        def __init__(self, _browser: object, output_dir: Path, config: AppConfig) -> None:
            self._output_dir = output_dir
            self._on_progress = None
            self.bytes_downloaded = 2048
            self.retry_count = 0

        def is_chapter_complete(self, _series_title: str, _chapter_title: str) -> bool:
            return False

        async def download_chapter(
            self,
            image_urls: list[str],
            title: str,
            chapter_label: str,
        ) -> ChapterDownloadResult:
            assert len(image_urls) == 2
            if self._on_progress is not None:
                self._on_progress(DownloadProgress(1, 2, 0, 0, "001", total_bytes=1024))
                self._on_progress(DownloadProgress(2, 2, 0, 0, "002", total_bytes=2048))
            chapter_dir = self._output_dir / title / chapter_label
            chapter_dir.mkdir(parents=True, exist_ok=True)
            return ChapterDownloadResult(
                chapter_dir=chapter_dir,
                total=2,
                downloaded=2,
                skipped=0,
                failed=0,
            )

    def fake_convert(chapter_dir: Path, fmt: str, *, optimize: bool, config: AppConfig) -> Path:
        assert fmt == "pdf"
        assert optimize is True
        return chapter_dir.with_suffix(".pdf")

    monkeypatch.setattr(download_usecase, "Downloader", FakeDownloader)
    monkeypatch.setattr(download_usecase, "convert", fake_convert)

    service = AsyncMock()
    service.get_chapter_images.return_value = ChapterImages(
        title="Chapter 1",
        chapter_label="Chapter 1",
        image_urls=["https://img/1", "https://img/2"],
    )

    with caplog.at_level(logging.INFO, logger="comix_dl.application.download_usecase"):
        summary = await download_usecase.download_chapters(
            browser=object(),
            service=service,
            series_title="Series A",
            chapters=[ChapterInfo(title="Chapter 1", chapter_id=101, number="1")],
            output_dir=tmp_path,
            fmt="pdf",
            config=_config(),
            optimize=True,
            on_event=events.append,
            history_repository=history,
            notifier=lambda title, body: notifications.append((title, body)),
        )

    assert summary.total_chapters == 1
    assert summary.completed == 1
    assert summary.skipped == 0
    assert summary.partial == 0
    assert summary.failed == 0
    assert summary.total_bytes == 2048
    assert summary.issues == ()
    assert [event.kind for event in events] == ["started", "planned", "progress", "progress", "converted"]
    assert history.calls == [{
        "title": "Series A",
        "chapters_count": 1,
        "fmt": "pdf",
        "total_size_bytes": 2048,
        "completed": 1,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
        "summary_text": "1 downloaded",
        "issues": [],
    }]
    assert notifications == [("comix-dl: Series A", "1 downloaded (2.0 KB)")]
    assert any(
        record.message == "chapter_download_finished"
        and record.context["chapter_id"] == 101
        and record.context["chapter_title"] == "Chapter 1"
        and record.context["status"] == "converted"
        and record.context["bytes"] == 2048
        and record.context["retry_count"] == 0
        for record in caplog.records
    )
    assert any(
        record.message == "download_batch_finished"
        and record.context["series"] == "Series A"
        and record.context["status"] == "ok"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_download_chapters_counts_skipped_partial_and_missing_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    history = RecordingHistoryRepository()
    notifications: list[tuple[str, str]] = []
    created_downloaders: list[object] = []

    class FakeDownloader:
        def __init__(self, _browser: object, output_dir: Path, config: AppConfig) -> None:
            self._output_dir = output_dir
            self._on_progress = None
            self.bytes_downloaded = 0
            self.retry_count = 0
            created_downloaders.append(self)

        def is_chapter_complete(self, _series_title: str, chapter_title: str) -> bool:
            return chapter_title == "Chapter 1"

        async def download_chapter(
            self,
            image_urls: list[str],
            title: str,
            chapter_label: str,
        ) -> ChapterDownloadResult:
            self.bytes_downloaded = 1000
            if self._on_progress is not None:
                self._on_progress(DownloadProgress(1, 2, 0, 0, "001", total_bytes=1000))
            chapter_dir = self._output_dir / title / chapter_label
            chapter_dir.mkdir(parents=True, exist_ok=True)
            return ChapterDownloadResult(
                chapter_dir=chapter_dir,
                total=2,
                downloaded=1,
                skipped=0,
                failed=1,
                failed_files=("002",),
            )

    def fail_if_converted(*args: object, **kwargs: object) -> Path:
        raise AssertionError("partial download must not be converted")

    monkeypatch.setattr(download_usecase, "Downloader", FakeDownloader)
    monkeypatch.setattr(download_usecase, "convert", fail_if_converted)

    service = AsyncMock()

    async def get_chapter_images(chapter_id: int) -> ChapterImages | None:
        if chapter_id == 2:
            return ChapterImages(
                title="Chapter 2",
                chapter_label="Chapter 2",
                image_urls=["https://img/1", "https://img/2"],
            )
        return None

    service.get_chapter_images.side_effect = get_chapter_images

    chapters = [
        ChapterInfo(title="Chapter 1", chapter_id=1, number="1"),
        ChapterInfo(title="Chapter 2", chapter_id=2, number="2"),
        ChapterInfo(title="Chapter 3", chapter_id=3, number="3"),
    ]

    summary = await download_usecase.download_chapters(
        browser=object(),
        service=service,
        series_title="Series B",
        chapters=chapters,
        output_dir=tmp_path,
        fmt="pdf",
        config=_config(),
        optimize=True,
        history_repository=history,
        notifier=lambda title, body: notifications.append((title, body)),
    )

    assert summary.total_chapters == 3
    assert summary.completed == 0
    assert summary.skipped == 1
    assert summary.partial == 1
    assert summary.failed == 1
    assert summary.total_bytes == 1000
    assert [issue.message for issue in summary.issues] == [
        "Chapter 2 is incomplete: 1/2 pages failed.",
        "no images available from remote API",
    ]
    assert len(created_downloaders) == 3
    assert history.calls == [{
        "title": "Series B",
        "chapters_count": 3,
        "fmt": "pdf",
        "total_size_bytes": 1000,
        "completed": 0,
        "partial": 1,
        "failed": 1,
        "skipped": 1,
        "summary_text": "1 skipped, 1 partial, 1 failed",
        "issues": [
            "Chapter 2: Chapter 2 is incomplete: 1/2 pages failed.",
            "Chapter 3: no images available from remote API",
        ],
    }]
    assert notifications == [
        (
            "comix-dl: Series B",
            "1 skipped, 1 partial, 1 failed (1000.0 B) | "
            "Chapter 2: Chapter 2 is incomplete: 1/2 pages failed. | +1 more issue(s)",
        )
    ]
