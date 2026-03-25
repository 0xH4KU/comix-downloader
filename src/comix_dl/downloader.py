"""Concurrent image downloader with progress tracking and resume support."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from comix_dl.config import AppConfig
from comix_dl.fileio import atomic_write_bytes, atomic_write_text

if TYPE_CHECKING:
    from pathlib import Path

    from comix_dl.cdp_browser import CdpBrowser

logger = logging.getLogger(__name__)

_COMPLETE_MARKER = ".complete"
_STATE_FILE = "chapter.state.json"


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
    failed_files: tuple[str, ...] = ()

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


@dataclass
class _PageDownloadResult:
    """Per-page outcome used to build chapter state."""

    filename: str
    url: str
    status: str
    error: str | None = None


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
        self._config = config if config is not None else AppConfig()
        self._output_dir = output_dir or self._config.download.default_output_dir
        self._on_progress = on_progress
        self.bytes_downloaded: int = 0

    @staticmethod
    def _describe_download_error(url: str, filename: str, exc: Exception) -> str:
        """Return a clearer error message for common image-download failures."""
        message = str(exc)
        if "timed out" in message:
            return f"Image request timed out for {filename} from {url}: {message}"
        if "HTTP 403" in message or "403 Forbidden" in message:
            return (
                f"Image request was blocked by HTTP 403 for {filename} from {url}; "
                "Cloudflare clearance may have expired."
            )
        if "page pool" in message.lower():
            return f"Browser page pool is unavailable while downloading {filename} from {url}: {message}"
        return message

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
        existing_files = self._index_existing_downloads(chapter_dir)

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

        async def fetch_one(index: int, url: str) -> _PageDownloadResult:
            nonlocal _progress_done
            async with semaphore:
                # Random delay to avoid rate limits
                delay = self._config.download.image_delay
                if delay > 0:
                    await asyncio.sleep(random.uniform(delay * 0.3, delay * 1.7))

                filename = f"{index + 1:03d}"

                # Resume: only trust existing files that still look like valid images.
                existing = existing_files.pop(filename, [])
                if existing and any(self._is_valid_image_file(f) for f in existing):
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
                    return _PageDownloadResult(filename=filename, url=url, status="skip")
                if existing:
                    for stale in existing:
                        with contextlib.suppress(OSError):
                            stale.unlink()

                success, error = await self._download_image(url, chapter_dir, filename, referer=referer)
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
                return _PageDownloadResult(
                    filename=filename,
                    url=url,
                    status="ok" if success else "fail",
                    error=error,
                )

        tasks = [fetch_one(i, url) for i, url in enumerate(image_urls)]
        results = await asyncio.gather(*tasks)

        completed = sum(1 for r in results if r.status == "ok")
        skipped = sum(1 for r in results if r.status == "skip")
        failed_results = [r for r in results if r.status == "fail"]
        failed = len(failed_results)
        result = ChapterDownloadResult(
            chapter_dir=chapter_dir,
            total=total,
            downloaded=completed,
            skipped=skipped,
            failed=failed,
            failed_files=tuple(r.filename for r in failed_results),
        )

        # Mark as complete (only if no failures)
        if failed == 0:
            (chapter_dir / _COMPLETE_MARKER).touch()
            self._remove_state_file(chapter_dir)
        else:
            self._write_state_file(chapter_dir, title, chapter, result, failed_results)

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
    ) -> tuple[bool, str | None]:
        """Download a single image with retry.

        Returns:
            ``(True, None)`` on success, otherwise ``(False, last_error)``.
        """
        max_retries = self._config.download.max_retries
        retry_delay = self._config.download.retry_delay
        last_error: str | None = None

        for attempt in range(max_retries + 1):
            try:
                data = await self._client.get_bytes(url, referer=referer)

                # Determine extension from URL or content
                ext = self._guess_extension(url, data)
                filepath = output_dir / f"{filename}{ext}"
                atomic_write_bytes(filepath, data)
                self.bytes_downloaded += len(data)
                return True, None

            except Exception as exc:
                last_error = self._describe_download_error(url, filename, exc)
                if attempt < max_retries:
                    wait = retry_delay * (2 ** attempt)
                    logger.debug(
                        "Retry %d/%d for %s after %.1fs: %s",
                        attempt + 1, max_retries, filename, wait, last_error,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.warning(
                        "Failed to download %s after %d attempts: %s",
                        url,
                        max_retries + 1,
                        last_error,
                    )

        return False, last_error

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
        if data[:2] == b"BM":
            return ".bmp"

        return ".jpg"  # default fallback

    @staticmethod
    def _is_valid_image_file(path: Path) -> bool:
        """Best-effort validation for a previously downloaded image file."""
        try:
            with path.open("rb") as fh:
                header = fh.read(16)
        except OSError:
            return False

        if not header:
            return False

        suffix = path.suffix.lower()
        if suffix == ".webp":
            return header[:4] == b"RIFF" and header[8:12] == b"WEBP"
        if suffix == ".png":
            return header[:8] == b"\x89PNG\r\n\x1a\n"
        if suffix in {".jpg", ".jpeg"}:
            return header[:2] == b"\xff\xd8"
        if suffix == ".gif":
            return header[:4] == b"GIF8"
        if suffix == ".bmp":
            return header[:2] == b"BM"
        if suffix == ".avif":
            return len(header) >= 12 and header[4:12] == b"ftypavif"
        return False

    @staticmethod
    def _remove_state_file(chapter_dir: Path) -> None:
        with contextlib.suppress(OSError):
            (chapter_dir / _STATE_FILE).unlink()

    @staticmethod
    def _write_state_file(
        chapter_dir: Path,
        title: str,
        chapter: str,
        result: ChapterDownloadResult,
        failed_results: list[_PageDownloadResult],
    ) -> None:
        payload = {
            "updated_at": datetime.now(UTC).isoformat(),
            "title": title,
            "chapter": chapter,
            "status": result.status,
            "total": result.total,
            "downloaded": result.downloaded,
            "skipped": result.skipped,
            "failed": result.failed,
            "failed_pages": [
                {
                    "filename": item.filename,
                    "url": item.url,
                    "error": item.error or "unknown error",
                }
                for item in failed_results
            ],
        }
        atomic_write_text(
            chapter_dir / _STATE_FILE,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )

    @staticmethod
    def _index_existing_downloads(chapter_dir: Path) -> dict[str, list[Path]]:
        """Index existing page files once so resume checks avoid repeated glob() calls."""
        indexed: dict[str, list[Path]] = {}
        for entry in chapter_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.name in {_COMPLETE_MARKER, _STATE_FILE}:
                continue
            if entry.name.endswith(".part") or (entry.name.startswith(".") and entry.name.endswith(".tmp")):
                with contextlib.suppress(OSError):
                    entry.unlink()
                continue
            indexed.setdefault(entry.stem, []).append(entry)
        return indexed
