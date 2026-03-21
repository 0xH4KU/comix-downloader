"""Concurrent image downloader with progress tracking."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from comix_dl.config import CONFIG

if TYPE_CHECKING:
    from pathlib import Path

    from comix_dl.cdp_browser import CdpBrowser

logger = logging.getLogger(__name__)


@dataclass
class DownloadProgress:
    """Snapshot of download progress."""

    completed: int
    total: int
    failed: int
    current_file: str


# Type alias for progress callback
ProgressCallback = Callable[[DownloadProgress], None]


def sanitize_dirname(name: str) -> str:
    """Return a filesystem-safe directory name."""
    name = name.replace(":", " - ")
    name = re.sub(r'[\\/*?"<>|]', " ", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip(" .") or "download"


class Downloader:
    """Download chapter images concurrently with retry logic.

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
    ) -> None:
        self._client = client
        self._output_dir = output_dir or CONFIG.download.default_output_dir
        self._on_progress = on_progress

    async def download_chapter(
        self,
        image_urls: list[str],
        title: str,
        chapter: str,
        *,
        referer: str | None = None,
    ) -> Path:
        """Download all images for a chapter.

        Args:
            image_urls: List of image URLs to download.
            title: Series title (used for directory naming).
            chapter: Chapter label (used for directory naming).
            referer: Referer header for image requests.

        Returns:
            Path to the directory containing downloaded images.

        Raises:
            RuntimeError: If all downloads fail.
        """
        chapter_dir = self._output_dir / sanitize_dirname(title) / sanitize_dirname(chapter)
        chapter_dir.mkdir(parents=True, exist_ok=True)

        total = len(image_urls)
        completed = 0
        failed = 0
        semaphore = asyncio.Semaphore(CONFIG.download.max_concurrent_images)

        async def fetch_one(index: int, url: str) -> bool:
            nonlocal completed, failed
            async with semaphore:
                filename = f"{index + 1:03d}"
                success = await self._download_image(url, chapter_dir, filename, referer=referer)

                if success:
                    completed += 1
                else:
                    failed += 1

                if self._on_progress:
                    self._on_progress(
                        DownloadProgress(
                            completed=completed + failed,
                            total=total,
                            failed=failed,
                            current_file=filename,
                        )
                    )
                return success

        tasks = [fetch_one(i, url) for i, url in enumerate(image_urls)]
        results = await asyncio.gather(*tasks)

        success_count = sum(1 for r in results if r)
        if success_count == 0:
            raise RuntimeError(f"All {total} image downloads failed for {title} - {chapter}")

        if failed > 0:
            logger.warning(
                "%s - %s: %d/%d images failed",
                title, chapter, failed, total,
            )
        else:
            logger.info(
                "%s - %s: downloaded %d images",
                title, chapter, total,
            )

        return chapter_dir

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
        max_retries = CONFIG.download.max_retries
        retry_delay = CONFIG.download.retry_delay

        for attempt in range(max_retries + 1):
            try:
                data = await self._client.get_bytes(url, referer=referer)

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

        return ".jpg"  # default fallback
