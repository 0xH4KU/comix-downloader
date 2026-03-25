"""Concurrent image downloader with progress tracking and resume support."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from comix_dl.config import CONFIG, AppConfig

if TYPE_CHECKING:
    from pathlib import Path

    from comix_dl.cdp_browser import CdpBrowser

logger = logging.getLogger(__name__)

_COMPLETE_MARKER = ".complete"


@dataclass
class DownloadProgress:
    """Snapshot of download progress."""

    completed: int
    total: int
    failed: int
    skipped: int
    current_file: str
    total_bytes: int = 0


@dataclass
class ChapterDownloadResult:
    """Final status for a chapter download attempt."""

    chapter_dir: Path
    total: int
    downloaded: int
    skipped: int
    failed: int

    @property
    def success_count(self) -> int:
        return self.downloaded + self.skipped

    @property
    def status(self) -> str:
        if self.failed == self.total:
            return "failed"
        if self.failed > 0:
            return "partial"
        if self.downloaded == 0 and self.skipped == self.total:
            return "skipped"
        return "complete"


# Type alias for progress callback
ProgressCallback = Callable[[DownloadProgress], None]


def sanitize_dirname(name: str) -> str:
    """Return a filesystem-safe directory name."""
    name = name.replace(":", " - ")
    name = re.sub(r'[\\/*?"<>|]', " ", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip(" .") or "download"


class Downloader:
    """Download chapter images concurrently with retry logic and resume.

    Args:
        client: CDP browser client for fetching images.
        output_dir: Base directory for downloads.
        on_progress: Optional callback invoked after each image completes.
    """

    def __init__(
        self,
        client: CdpBrowser,
        output_dir: Path | None = None,
        on_progress: ProgressCallback | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self._client = client
        self._config = config or CONFIG
        self._output_dir = output_dir or self._config.download.default_output_dir
        self._on_progress = on_progress
        self.bytes_downloaded: int = 0

    def is_chapter_complete(self, title: str, chapter: str) -> bool:
        """Check whether a chapter has already been downloaded."""
        chapter_dir = self._output_dir / sanitize_dirname(title) / sanitize_dirname(chapter)
        return (chapter_dir / _COMPLETE_MARKER).exists()

    async def download_chapter(
        self,
        image_urls: list[str],
        title: str,
        chapter: str,
        *,
        referer: str | None = None,
    ) -> ChapterDownloadResult:
        """Download all images for a chapter.

        Supports resume — if individual images already exist on disk they are
        skipped.  A ``.complete`` marker is written after all images succeed.

        Args:
            image_urls: List of image URLs to download.
            title: Series title (used for directory naming).
            chapter: Chapter label (used for directory naming).
            referer: Referer header for image requests.

        Returns:
            Final download result for the chapter.
        """
        chapter_dir = self._output_dir / sanitize_dirname(title) / sanitize_dirname(chapter)
        chapter_dir.mkdir(parents=True, exist_ok=True)

        # Already fully downloaded?
        if (chapter_dir / _COMPLETE_MARKER).exists():
            logger.info("%s - %s: already downloaded, skipping", title, chapter)
            if self._on_progress:
                self._on_progress(DownloadProgress(
                    completed=len(image_urls),
                    total=len(image_urls),
                    failed=0,
                    skipped=len(image_urls),
                    current_file="(skipped)",
                ))
            return ChapterDownloadResult(
                chapter_dir=chapter_dir,
                total=len(image_urls),
                downloaded=0,
                skipped=len(image_urls),
                failed=0,
            )

        total = len(image_urls)
        semaphore = asyncio.Semaphore(self._config.download.max_concurrent_images)
        # Atomic-safe progress counter (incremented only inside semaphore)
        _progress_done = 0

        async def fetch_one(index: int, url: str) -> str:
            """Return 'ok', 'skip', or 'fail'."""
            nonlocal _progress_done
            async with semaphore:
                # Random delay to avoid rate limits
                delay = self._config.download.image_delay
                if delay > 0:
                    await asyncio.sleep(random.uniform(delay * 0.3, delay * 1.7))

                filename = f"{index + 1:03d}"

                # Resume: skip if image already exists
                existing = list(chapter_dir.glob(f"{filename}.*"))
                if existing and any(f.stat().st_size > 0 for f in existing):
                    _progress_done += 1
                    if self._on_progress:
                        self._on_progress(DownloadProgress(
                            completed=_progress_done,
                            total=total,
                            failed=0,
                            skipped=0,
                            current_file=filename,
                            total_bytes=self.bytes_downloaded,
                        ))
                    return "skip"

                success = await self._download_image(url, chapter_dir, filename, referer=referer)
                _progress_done += 1

                if self._on_progress:
                    self._on_progress(
                        DownloadProgress(
                            completed=_progress_done,
                            total=total,
                            failed=0,
                            skipped=0,
                            current_file=filename,
                            total_bytes=self.bytes_downloaded,
                        )
                    )
                return "ok" if success else "fail"

        tasks = [fetch_one(i, url) for i, url in enumerate(image_urls)]
        results = await asyncio.gather(*tasks)

        completed = results.count("ok")
        skipped = results.count("skip")
        failed = results.count("fail")
        result = ChapterDownloadResult(
            chapter_dir=chapter_dir,
            total=total,
            downloaded=completed,
            skipped=skipped,
            failed=failed,
        )

        # Mark as complete (only if no failures)
        if failed == 0:
            (chapter_dir / _COMPLETE_MARKER).touch()

        if result.status == "failed":
            logger.warning("%s - %s: all %d images failed", title, chapter, total)
        elif result.status == "partial":
            logger.warning(
                "%s - %s: %d downloaded, %d skipped, %d failed (not marked complete)",
                title, chapter, completed, skipped, failed,
            )
        elif skipped > 0:
            logger.info(
                "%s - %s: %d downloaded, %d skipped (resumed), %d failed",
                title, chapter, completed, skipped, failed,
            )
        elif failed > 0:
            logger.warning(
                "%s - %s: %d/%d images failed",
                title, chapter, failed, total,
            )
        else:
            logger.info(
                "%s - %s: downloaded %d images",
                title, chapter, total,
            )

        return result

    async def _download_image(
        self,
        url: str,
        output_dir: Path,
        filename: str,
        *,
        referer: str | None = None,
    ) -> bool:
        """Download a single image with retry.

        Returns:
            ``True`` on success, ``False`` on failure.
        """
        max_retries = self._config.download.max_retries
        retry_delay = self._config.download.retry_delay

        for attempt in range(max_retries + 1):
            try:
                data = await self._client.get_bytes(url, referer=referer)
                self.bytes_downloaded += len(data)

                # Determine extension from URL or content
                ext = self._guess_extension(url, data)
                filepath = output_dir / f"{filename}{ext}"
                filepath.write_bytes(data)
                return True

            except Exception as exc:
                if attempt < max_retries:
                    wait = retry_delay * (2 ** attempt)
                    logger.debug(
                        "Retry %d/%d for %s after %.1fs: %s",
                        attempt + 1, max_retries, filename, wait, exc,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.warning("Failed to download %s after %d attempts: %s", url, max_retries + 1, exc)

        return False

    @staticmethod
    def _guess_extension(url: str, data: bytes) -> str:
        """Determine image file extension from URL or magic bytes."""
        # Try URL first
        url_lower = url.lower().split("?")[0]
        for ext in (".webp", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".avif"):
            if url_lower.endswith(ext):
                return ext

        # Try magic bytes
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if data[:2] == b"\xff\xd8":
            return ".jpg"
        if data[:4] == b"GIF8":
            return ".gif"
        if len(data) >= 12 and data[4:12] == b"ftypavif":
            return ".avif"

        return ".jpg"  # default fallback
