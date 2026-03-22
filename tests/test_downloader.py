"""Tests for comix_dl.downloader — sanitization, extension guessing, resume, and download logic."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from comix_dl.downloader import DownloadProgress, Downloader, sanitize_dirname


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

        assert result == chapter_dir
        assert len(progress_snapshots) == 1
        assert progress_snapshots[0].skipped == 2

    async def test_all_failures_raises(self, tmp_path: Path, mock_browser: AsyncMock):
        """If every image download fails, RuntimeError should be raised."""
        mock_browser.get_bytes.side_effect = Exception("Network error")
        dl = Downloader(mock_browser, output_dir=tmp_path)

        # Disable retry delays for test speed
        with patch.object(dl, "_download_image", return_value=False):
            with pytest.raises(RuntimeError, match="All .* downloads failed"):
                await dl.download_chapter(
                    ["https://cdn.com/1.jpg"],
                    "Test Manga",
                    "Chapter 1",
                )

    async def test_successful_download_creates_marker(self, tmp_path: Path, mock_browser: AsyncMock):
        """Successful download should create .complete marker."""
        # Make get_bytes return valid JPEG data
        mock_browser.get_bytes.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100

        dl = Downloader(mock_browser, output_dir=tmp_path)

        # Minimal config for fast test
        with (
            patch("comix_dl.downloader.CONFIG.download.image_delay", 0),
            patch("comix_dl.downloader.CONFIG.download.max_retries", 0),
        ):
            result_dir = await dl.download_chapter(
                ["https://cdn.com/1.jpg"],
                "Test Manga",
                "Chapter 1",
            )

        assert (result_dir / ".complete").exists()
        # Should have one image file
        images = [f for f in result_dir.iterdir() if f.suffix == ".jpg"]
        assert len(images) == 1

    async def test_resume_skips_existing_images(self, tmp_path: Path, mock_browser: AsyncMock):
        """If image file already exists, it should be skipped."""
        mock_browser.get_bytes.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100

        dl = Downloader(mock_browser, output_dir=tmp_path)
        chapter_dir = tmp_path / "Test Manga" / "Chapter 1"
        chapter_dir.mkdir(parents=True)

        # Pre-create first image
        (chapter_dir / "001.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 50)

        with (
            patch("comix_dl.downloader.CONFIG.download.image_delay", 0),
            patch("comix_dl.downloader.CONFIG.download.max_retries", 0),
        ):
            await dl.download_chapter(
                ["https://cdn.com/1.jpg", "https://cdn.com/2.jpg"],
                "Test Manga",
                "Chapter 1",
            )

        # get_bytes should only be called once (for image 2)
        assert mock_browser.get_bytes.call_count == 1


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

        dl = Downloader(mock_browser, output_dir=tmp_path)

        with patch("comix_dl.downloader.CONFIG.download.retry_delay", 0):
            success = await dl._download_image(
                "https://cdn.com/test.jpg",
                tmp_path,
                "001",
            )

        assert success is True
        assert call_count == 3

    async def test_max_retries_exhausted(self, tmp_path: Path, mock_browser: AsyncMock):
        """Should return False after exhausting retries."""
        mock_browser.get_bytes.side_effect = Exception("Persistent failure")

        dl = Downloader(mock_browser, output_dir=tmp_path)

        with (
            patch("comix_dl.downloader.CONFIG.download.max_retries", 2),
            patch("comix_dl.downloader.CONFIG.download.retry_delay", 0),
        ):
            success = await dl._download_image(
                "https://cdn.com/test.jpg",
                tmp_path,
                "001",
            )

        assert success is False
        # 1 initial + 2 retries = 3 calls
        assert mock_browser.get_bytes.call_count == 3


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
