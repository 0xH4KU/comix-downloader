"""Tests for comix_dl.downloader — sanitization, extension guessing, resume, and download logic."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from comix_dl.config import AppConfig
from comix_dl.downloader import (
    ChapterDownloadResult,
    Downloader,
    DownloadProgress,
    ensure_complete_download,
    sanitize_dirname,
)
from comix_dl.errors import PartialDownloadError

if TYPE_CHECKING:
    from pathlib import Path


def _make_downloader(mock_browser: AsyncMock, output_dir: Path, **download_overrides: object) -> Downloader:
    config = AppConfig()
    for name, value in download_overrides.items():
        setattr(config.download, name, value)
    return Downloader(mock_browser, output_dir=output_dir, config=config)


# ---------------------------------------------------------------------------
# sanitize_dirname
# ---------------------------------------------------------------------------

class TestSanitizeDirname:
    def test_normal_name(self):
        assert sanitize_dirname("My Manga") == "My Manga"

    def test_colons_replaced(self):
        assert sanitize_dirname("Chapter 1: The Beginning") == "Chapter 1 - The Beginning"

    def test_special_chars_removed(self):
        result = sanitize_dirname('File/Name*With?"Bad<Chars>|')
        assert "/" not in result
        assert "*" not in result
        assert "?" not in result
        assert '"' not in result
        assert "<" not in result
        assert ">" not in result
        assert "|" not in result

    def test_whitespace_collapsed(self):
        assert sanitize_dirname("Too   Many    Spaces") == "Too Many Spaces"

    def test_leading_trailing_dots_stripped(self):
        assert sanitize_dirname("...hidden...") == "hidden"

    def test_empty_string_returns_download(self):
        assert sanitize_dirname("") == "download"

    def test_only_special_chars_returns_download(self):
        assert sanitize_dirname("***") == "download"

    def test_unicode_preserved(self):
        assert sanitize_dirname("漫画タイトル") == "漫画タイトル"


# ---------------------------------------------------------------------------
# _guess_extension
# ---------------------------------------------------------------------------

class TestGuessExtension:
    def test_webp_from_url(self):
        assert Downloader._guess_extension("https://cdn.com/img.webp", b"") == ".webp"

    def test_png_from_url(self):
        assert Downloader._guess_extension("https://cdn.com/img.png", b"") == ".png"

    def test_jpg_from_url(self):
        assert Downloader._guess_extension("https://cdn.com/img.jpg", b"") == ".jpg"

    def test_avif_from_url(self):
        assert Downloader._guess_extension("https://cdn.com/img.avif", b"") == ".avif"

    def test_url_with_query_params(self):
        assert Downloader._guess_extension("https://cdn.com/img.png?token=abc", b"") == ".png"

    def test_webp_from_magic_bytes(self):
        data = b"RIFF\x00\x00\x00\x00WEBP"
        assert Downloader._guess_extension("https://cdn.com/unknown", data) == ".webp"

    def test_png_from_magic_bytes(self):
        data = b"\x89PNG\r\n\x1a\n"
        assert Downloader._guess_extension("https://cdn.com/unknown", data) == ".png"

    def test_jpeg_from_magic_bytes(self):
        data = b"\xff\xd8\xff\xe0"
        assert Downloader._guess_extension("https://cdn.com/unknown", data) == ".jpg"

    def test_gif_from_magic_bytes(self):
        data = b"GIF89a"
        assert Downloader._guess_extension("https://cdn.com/unknown", data) == ".gif"

    def test_avif_from_magic_bytes(self):
        data = b"\x00\x00\x00\x00ftypavif"
        assert Downloader._guess_extension("https://cdn.com/unknown", data) == ".avif"

    def test_unknown_defaults_to_jpg(self):
        assert Downloader._guess_extension("https://cdn.com/unknown", b"\x00\x00") == ".jpg"

    def test_case_insensitive_url(self):
        assert Downloader._guess_extension("https://cdn.com/IMG.PNG", b"") == ".png"


class TestImageValidation:
    def test_valid_jpeg_detected(self, tmp_path: Path):
        path = tmp_path / "001.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10)
        assert Downloader._is_valid_image_file(path) is True

    def test_corrupt_jpeg_rejected(self, tmp_path: Path):
        path = tmp_path / "001.jpg"
        path.write_bytes(b"not-a-jpeg")
        assert Downloader._is_valid_image_file(path) is False


class TestExistingDownloadIndex:
    def test_indexes_page_files_once(self, tmp_path: Path):
        chapter_dir = tmp_path / "chapter"
        chapter_dir.mkdir()
        (chapter_dir / "001.jpg").write_bytes(b"\xff\xd8")
        (chapter_dir / "001.webp").write_bytes(b"RIFFxxxxWEBP")
        (chapter_dir / "002.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (chapter_dir / ".complete").touch()
        (chapter_dir / "chapter.state.json").write_text("{}")

        indexed = Downloader._index_existing_downloads(chapter_dir)

        assert sorted(indexed) == ["001", "002"]
        assert sorted(p.name for p in indexed["001"]) == ["001.jpg", "001.webp"]


# ---------------------------------------------------------------------------
# is_chapter_complete
# ---------------------------------------------------------------------------

class TestIsChapterComplete:
    def test_complete_marker_exists(self, tmp_path: Path, mock_browser: AsyncMock):
        dl = Downloader(mock_browser, output_dir=tmp_path)
        chapter_dir = tmp_path / "Test Manga" / "Chapter 1"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / ".complete").touch()
        assert dl.is_chapter_complete("Test Manga", "Chapter 1") is True

    def test_complete_marker_missing(self, tmp_path: Path, mock_browser: AsyncMock):
        dl = Downloader(mock_browser, output_dir=tmp_path)
        assert dl.is_chapter_complete("Test Manga", "Chapter 1") is False

    def test_dir_exists_but_no_marker(self, tmp_path: Path, mock_browser: AsyncMock):
        dl = Downloader(mock_browser, output_dir=tmp_path)
        chapter_dir = tmp_path / "Test Manga" / "Chapter 1"
        chapter_dir.mkdir(parents=True)
        assert dl.is_chapter_complete("Test Manga", "Chapter 1") is False


# ---------------------------------------------------------------------------
# download_chapter — resume and error handling
# ---------------------------------------------------------------------------

class TestDownloadChapter:
    async def test_skips_already_complete(self, tmp_path: Path, mock_browser: AsyncMock):
        """If .complete marker exists, all images should be reported as skipped."""
        dl = Downloader(mock_browser, output_dir=tmp_path)
        chapter_dir = tmp_path / "Test Manga" / "Chapter 1"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / ".complete").touch()

        progress_snapshots: list[DownloadProgress] = []
        dl._on_progress = lambda p: progress_snapshots.append(p)

        urls = ["https://cdn.com/1.jpg", "https://cdn.com/2.jpg"]
        result = await dl.download_chapter(urls, "Test Manga", "Chapter 1")

        assert result == ChapterDownloadResult(
            chapter_dir=chapter_dir,
            total=2,
            downloaded=0,
            skipped=2,
            failed=0,
        )
        assert result.status == "skipped"
        assert len(progress_snapshots) == 1
        assert progress_snapshots[0].skipped == 2

    async def test_all_failures_return_failed_status(self, tmp_path: Path, mock_browser: AsyncMock):
        """If every image download fails, the chapter is marked failed."""
        mock_browser.get_bytes.side_effect = Exception("Network error")
        dl = Downloader(mock_browser, output_dir=tmp_path)

        # Disable retry delays for test speed
        with patch.object(dl, "_download_image", return_value=(False, "Network error")):
            result = await dl.download_chapter(
                ["https://cdn.com/1.jpg"],
                "Test Manga",
                "Chapter 1",
            )

        assert result.status == "failed"
        assert result.failed == 1
        assert not (result.chapter_dir / ".complete").exists()
        state = json.loads((result.chapter_dir / "chapter.state.json").read_text(encoding="utf-8"))
        assert state["status"] == "failed"
        assert state["failed_pages"][0]["filename"] == "001"

    async def test_successful_download_creates_marker(self, tmp_path: Path, mock_browser: AsyncMock):
        """Successful download should create .complete marker."""
        # Make get_bytes return valid JPEG data
        mock_browser.get_bytes.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100

        dl = _make_downloader(mock_browser, tmp_path, image_delay=0, max_retries=0)
        result = await dl.download_chapter(
            ["https://cdn.com/1.jpg"],
            "Test Manga",
            "Chapter 1",
        )

        assert result.status == "complete"
        assert (result.chapter_dir / ".complete").exists()
        assert not (result.chapter_dir / "chapter.state.json").exists()
        # Should have one image file
        images = [f for f in result.chapter_dir.iterdir() if f.suffix == ".jpg"]
        assert len(images) == 1

    async def test_resume_skips_existing_images(self, tmp_path: Path, mock_browser: AsyncMock):
        """If image file already exists, it should be skipped."""
        mock_browser.get_bytes.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100

        dl = _make_downloader(mock_browser, tmp_path, image_delay=0, max_retries=0)
        chapter_dir = tmp_path / "Test Manga" / "Chapter 1"
        chapter_dir.mkdir(parents=True)

        # Pre-create first image
        (chapter_dir / "001.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 50)

        result = await dl.download_chapter(
            ["https://cdn.com/1.jpg", "https://cdn.com/2.jpg"],
            "Test Manga",
            "Chapter 1",
        )

        assert result.status == "complete"
        # get_bytes should only be called once (for image 2)
        assert mock_browser.get_bytes.call_count == 1

    async def test_resume_redownloads_corrupt_existing_image(self, tmp_path: Path, mock_browser: AsyncMock):
        mock_browser.get_bytes.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100

        dl = _make_downloader(mock_browser, tmp_path, image_delay=0, max_retries=0)
        chapter_dir = tmp_path / "Test Manga" / "Chapter 1"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "001.jpg").write_bytes(b"not-a-jpeg")

        result = await dl.download_chapter(
            ["https://cdn.com/1.jpg"],
            "Test Manga",
            "Chapter 1",
        )

        assert result.status == "complete"
        assert mock_browser.get_bytes.call_count == 1
        assert (chapter_dir / "001.jpg").read_bytes().startswith(b"\xff\xd8")

    async def test_resume_removes_invalid_stale_extension_before_redownload(
        self, tmp_path: Path, mock_browser: AsyncMock,
    ):
        mock_browser.get_bytes.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100

        dl = _make_downloader(mock_browser, tmp_path, image_delay=0, max_retries=0)
        chapter_dir = tmp_path / "Test Manga" / "Chapter 1"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "001.png").write_bytes(b"bad-png")

        result = await dl.download_chapter(
            ["https://cdn.com/1.jpg"],
            "Test Manga",
            "Chapter 1",
        )

        assert result.status == "complete"
        assert not (chapter_dir / "001.png").exists()
        assert (chapter_dir / "001.jpg").exists()

    async def test_partial_failures_return_partial_status(self, tmp_path: Path, mock_browser: AsyncMock):
        dl = _make_downloader(mock_browser, tmp_path, max_concurrent_images=1)

        with patch.object(dl, "_download_image", side_effect=[(True, None), (False, "boom")]):
            result = await dl.download_chapter(
                ["https://cdn.com/1.jpg", "https://cdn.com/2.jpg"],
                "Test Manga",
                "Chapter 1",
            )

        assert result.status == "partial"
        assert result.downloaded == 1
        assert result.failed == 1
        assert not (result.chapter_dir / ".complete").exists()
        state = json.loads((result.chapter_dir / "chapter.state.json").read_text(encoding="utf-8"))
        assert state["status"] == "partial"
        assert state["failed_pages"][0]["filename"] == "002"
        assert result.failed_files == ("002",)

    async def test_partial_rerun_recovers_missing_page_and_clears_state(
        self, tmp_path: Path, mock_browser: AsyncMock,
    ):
        valid_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        dl = _make_downloader(
            mock_browser,
            tmp_path,
            image_delay=0,
            max_retries=0,
            max_concurrent_images=1,
        )

        mock_browser.get_bytes.side_effect = [valid_jpeg, Exception("boom")]
        first = await dl.download_chapter(
            ["https://cdn.com/1.jpg", "https://cdn.com/2.jpg"],
            "Test Manga",
            "Chapter 1",
        )

        assert first.status == "partial"
        assert (first.chapter_dir / "001.jpg").exists()
        assert (first.chapter_dir / "chapter.state.json").exists()

        mock_browser.get_bytes.reset_mock()
        mock_browser.get_bytes.side_effect = [valid_jpeg]

        second = await dl.download_chapter(
            ["https://cdn.com/1.jpg", "https://cdn.com/2.jpg"],
            "Test Manga",
            "Chapter 1",
        )

        assert second.status == "complete"
        assert mock_browser.get_bytes.call_count == 1
        assert (second.chapter_dir / ".complete").exists()
        assert not (second.chapter_dir / "chapter.state.json").exists()
        assert (second.chapter_dir / "002.jpg").exists()

    async def test_resume_recovers_from_leftover_temp_files(self, tmp_path: Path, mock_browser: AsyncMock):
        mock_browser.get_bytes.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100

        dl = _make_downloader(mock_browser, tmp_path, image_delay=0, max_retries=0)
        chapter_dir = tmp_path / "Test Manga" / "Chapter 1"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "001.jpg.part").write_bytes(b"partial-write")
        (chapter_dir / ".001.jpg.stale.tmp").write_bytes(b"tmp-write")

        result = await dl.download_chapter(
            ["https://cdn.com/1.jpg"],
            "Test Manga",
            "Chapter 1",
        )

        assert result.status == "complete"
        assert mock_browser.get_bytes.call_count == 1
        assert (chapter_dir / "001.jpg").exists()
        assert not (chapter_dir / "001.jpg.part").exists()
        assert not (chapter_dir / ".001.jpg.stale.tmp").exists()


# ---------------------------------------------------------------------------
# _download_image retry logic
# ---------------------------------------------------------------------------

class TestDownloadImageRetry:
    async def test_retry_on_failure_then_success(self, tmp_path: Path, mock_browser: AsyncMock):
        """Should retry and succeed on subsequent attempt."""
        call_count = 0

        async def mock_get_bytes(url, *, referer=None):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise Exception("Temporary failure")
            return b"\xff\xd8\xff\xe0" + b"\x00" * 100

        mock_browser.get_bytes = mock_get_bytes

        dl = _make_downloader(mock_browser, tmp_path, retry_delay=0)
        success, error = await dl._download_image(
            "https://cdn.com/test.jpg",
            tmp_path,
            "001",
        )

        assert success is True
        assert error is None
        assert call_count == 3

    async def test_max_retries_exhausted(self, tmp_path: Path, mock_browser: AsyncMock):
        """Should return False after exhausting retries."""
        mock_browser.get_bytes.side_effect = Exception("Persistent failure")

        dl = _make_downloader(mock_browser, tmp_path, max_retries=2, retry_delay=0)
        success, error = await dl._download_image(
            "https://cdn.com/test.jpg",
            tmp_path,
            "001",
        )

        assert success is False
        assert error == "Persistent failure"
        # 1 initial + 2 retries = 3 calls
        assert mock_browser.get_bytes.call_count == 3

    async def test_timeout_error_is_contextualized(self, tmp_path: Path, mock_browser: AsyncMock):
        mock_browser.get_bytes.side_effect = RuntimeError("Fetching binary response timed out after 20ms.")

        dl = _make_downloader(mock_browser, tmp_path, max_retries=0, retry_delay=0)
        success, error = await dl._download_image(
            "https://cdn.com/test.jpg",
            tmp_path,
            "001",
        )

        assert success is False
        assert error == (
            "Image request timed out for 001 from https://cdn.com/test.jpg: "
            "Fetching binary response timed out after 20ms."
        )

    async def test_page_pool_error_is_contextualized(self, tmp_path: Path, mock_browser: AsyncMock):
        mock_browser.get_bytes.side_effect = RuntimeError(
            "Browser page pool is unavailable; pooled download requests cannot proceed.",
        )

        dl = _make_downloader(mock_browser, tmp_path, max_retries=0, retry_delay=0)
        success, error = await dl._download_image(
            "https://cdn.com/test.jpg",
            tmp_path,
            "001",
        )

        assert success is False
        assert error == (
            "Browser page pool is unavailable while downloading 001 from "
            "https://cdn.com/test.jpg: Browser page pool is unavailable; "
            "pooled download requests cannot proceed."
        )


# ---------------------------------------------------------------------------
# DownloadProgress dataclass
# ---------------------------------------------------------------------------

class TestDownloadProgress:
    def test_fields(self):
        p = DownloadProgress(completed=5, total=10, failed=1, skipped=2, current_file="003")
        assert p.completed == 5
        assert p.total == 10
        assert p.failed == 1
        assert p.skipped == 2
        assert p.current_file == "003"


class TestChapterDownloadResult:
    def test_status_values(self, tmp_path: Path):
        assert ChapterDownloadResult(tmp_path, total=2, downloaded=0, skipped=2, failed=0).status == "skipped"
        assert ChapterDownloadResult(tmp_path, total=2, downloaded=2, skipped=0, failed=0).status == "complete"
        assert ChapterDownloadResult(tmp_path, total=2, downloaded=1, skipped=0, failed=1).status == "partial"
        assert ChapterDownloadResult(tmp_path, total=2, downloaded=0, skipped=0, failed=2).status == "failed"

    def test_ensure_complete_download_raises_partial_domain_error(self, tmp_path: Path):
        result = ChapterDownloadResult(tmp_path, total=3, downloaded=2, skipped=0, failed=1)

        with pytest.raises(PartialDownloadError, match=r"1/3 pages failed"):
            ensure_complete_download(result, chapter_title="Chapter 1")
