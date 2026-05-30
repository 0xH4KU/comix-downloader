"""Concurrent image downloader with progress tracking and resume support."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import TYPE_CHECKING, TypeAlias

from PIL import Image, UnidentifiedImageError

from comix_dl.core.config import AppConfig, resolve_config
from comix_dl.core.errors import (
    BrowserTimeoutError,
    Http403Error,
    PagePoolUnavailableError,
    PartialDownloadError,
)
from comix_dl.core.fileio import atomic_write_bytes, atomic_write_text
from comix_dl.core.logging_utils import log_context

if TYPE_CHECKING:
    from pathlib import Path

    from comix_dl.core.engines.cdp_browser import CdpBrowser
    from comix_dl.core.models import ChapterPage

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
ImageSource: TypeAlias = "str | ChapterPage"


@dataclass
class _PageDownloadResult:
    """Per-page outcome used to build chapter state."""

    filename: str
    url: str
    status: str
    error: str | None = None


def ensure_complete_download(result: ChapterDownloadResult, *, chapter_title: str) -> None:
    """Raise a domain error when a chapter result is incomplete."""
    if result.status != "partial":
        return
    raise PartialDownloadError(
        f"{chapter_title} is incomplete: {result.failed}/{result.total} pages failed.",
    )


def sanitize_dirname(name: str) -> str:
    """Return a filesystem-safe directory name."""
    name = name.replace(":", " - ")
    name = re.sub(r'[\\/*?"<>|]', " ", name)
    name = name.replace("..", "")
    name = re.sub(r"\s+", " ", name)
    return name.strip(" .") or "download"


def _validate_within_base(path: Path, base: Path) -> None:
    """Raise if *path* escapes *base* after symlink resolution."""
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(
            f"Path traversal detected: {path} escapes base directory {base}"
        )


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
        self._config = resolve_config(config)
        self._output_dir = output_dir or self._config.download.default_output_dir
        self._on_progress = on_progress
        self.bytes_downloaded: int = 0
        self.retry_count: int = 0

    @staticmethod
    def _describe_download_error(url: str, filename: str, exc: Exception) -> str:
        """Return a clearer error message for common image-download failures.

        Dispatches by exception type rather than parsing message strings.
        Raw exceptions that escape the browser-boundary translation fall
        through to a generic representation so the caller still gets a
        message rather than a silent failure.
        """
        if isinstance(exc, BrowserTimeoutError):
            return f"Image request timed out for {filename} from {url}: {exc}"
        if isinstance(exc, Http403Error):
            return (
                f"Image request was blocked by HTTP 403 for {filename} from {url}; "
                "Cloudflare clearance may have expired."
            )
        if isinstance(exc, PagePoolUnavailableError):
            return (
                f"Browser page pool is unavailable while downloading {filename} "
                f"from {url}: {exc}"
            )
        return str(exc)

    def is_chapter_complete(self, title: str, chapter: str) -> bool:
        """Check whether a chapter has already been downloaded."""
        chapter_dir = self._output_dir / sanitize_dirname(title) / sanitize_dirname(chapter)
        _validate_within_base(chapter_dir, self._output_dir)
        return (chapter_dir / _COMPLETE_MARKER).exists()

    async def download_chapter(
        self,
        image_urls: Sequence[ImageSource],
        title: str,
        chapter: str,
        *,
        referer: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> ChapterDownloadResult:
        """Download all images for a chapter.

        Supports resume — if individual images already exist on disk they are
        skipped.  A ``.complete`` marker is written after all images succeed.

        Args:
            image_urls: List of image URLs or page metadata to download.
            title: Series title (used for directory naming).
            chapter: Chapter label (used for directory naming).
            referer: Referer header for image requests.
            on_progress: Per-call progress override. When ``None`` (the
                default) the callback supplied at ``__init__`` is used,
                so callers that share one downloader across chapters but
                want per-chapter callbacks can pass it here without
                touching ``self._on_progress``.

        Returns:
            Final download result for the chapter.
        """
        progress_cb = on_progress if on_progress is not None else self._on_progress
        chapter_dir = self._output_dir / sanitize_dirname(title) / sanitize_dirname(chapter)
        _validate_within_base(chapter_dir, self._output_dir)
        chapter_dir.mkdir(parents=True, exist_ok=True)
        existing_files = self._index_existing_downloads(chapter_dir)

        # Already fully downloaded?
        if (chapter_dir / _COMPLETE_MARKER).exists():
            logger.info("%s - %s: already downloaded, skipping", title, chapter)
            if progress_cb:
                progress_cb(DownloadProgress(
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
        progress_lock = asyncio.Lock()
        progress_done = 0

        async def _advance_progress(filename: str) -> None:
            """Serialize progress updates so callbacks see monotonic counts."""
            nonlocal progress_done
            async with progress_lock:
                progress_done += 1
                if progress_cb:
                    progress_cb(DownloadProgress(
                        completed=progress_done,
                        total=total,
                        failed=0,
                        skipped=0,
                        current_file=filename,
                        total_bytes=self.bytes_downloaded,
                    ))

        async def fetch_one(index: int, image: ImageSource) -> _PageDownloadResult:
            async with semaphore:
                # Random delay to avoid rate limits
                delay = self._config.download.image_delay
                if delay > 0:
                    await asyncio.sleep(random.uniform(delay * 0.3, delay * 1.7))

                filename = f"{index + 1:03d}"
                url = image.url if not isinstance(image, str) else image

                # Resume: only trust existing files that still look like valid images.
                existing = existing_files.pop(filename, [])
                if existing and any(self._is_valid_image_file(f) for f in existing):
                    await _advance_progress(filename)
                    return _PageDownloadResult(filename=filename, url=url, status="skip")
                if existing:
                    for stale in existing:
                        with contextlib.suppress(OSError):
                            stale.unlink()

                success, error = await self._download_image(image, chapter_dir, filename, referer=referer)
                await _advance_progress(filename)
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
        image: ImageSource,
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
        url = image.url if not isinstance(image, str) else image

        for attempt in range(max_retries + 1):
            try:
                data = await self._fetch_image_bytes(image, referer=referer)

                # Determine extension from URL or content
                ext = self._guess_extension(url, data)
                self._validate_image_bytes(data, filename=f"{filename}{ext}")
                filepath = output_dir / f"{filename}{ext}"
                atomic_write_bytes(filepath, data, sync=False)
                self.bytes_downloaded += len(data)
                return True, None

            except Exception as exc:
                last_error = self._describe_download_error(url, filename, exc)
                if attempt < max_retries:
                    wait = retry_delay * (2 ** attempt)
                    self.retry_count += 1
                    logger.debug(
                        "image_download_retry",
                        extra=log_context(
                            filename=filename,
                            retry_count=self.retry_count,
                            attempt=attempt + 1,
                            max_retries=max_retries,
                            wait_seconds=wait,
                            error=last_error,
                        ),
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.warning(
                        "image_download_failed",
                        extra=log_context(
                            filename=filename,
                            retry_count=self.retry_count,
                            attempts=max_retries + 1,
                            url=url,
                            error=last_error,
                        ),
                    )

        return False, last_error

    async def _fetch_image_bytes(self, image: ImageSource, *, referer: str | None) -> bytes:
        url = image.url if not isinstance(image, str) else image
        if not isinstance(image, str) and image.scrambled:
            return await self._client.get_scrambled_image_bytes(
                url,
                width=image.width,
                height=image.height,
                referer=referer,
            )
        return await self._client.get_bytes(url, referer=referer)

    @staticmethod
    def _guess_extension(url: str, data: bytes) -> str:
        """Determine image file extension from URL or magic bytes."""
        # Try URL first
        url_lower = url.lower().split("?")[0]
        url_ext = next(
            (
                ext
                for ext in (".webp", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".avif")
                if url_lower.endswith(ext)
            ),
            None,
        )

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

        return url_ext or ".jpg"  # default fallback

    @staticmethod
    def _is_valid_image_file(path: Path) -> bool:
        """Best-effort validation for a previously downloaded image file."""
        try:
            data = path.read_bytes()
        except OSError:
            return False

        return Downloader._image_bytes_are_valid(data)

    @staticmethod
    def _validate_image_bytes(data: bytes, *, filename: str) -> None:
        """Raise when downloaded bytes are not a fully decodable image."""
        if not Downloader._image_bytes_are_valid(data):
            raise ValueError(f"Downloaded image {filename} is incomplete or corrupt.")

    @staticmethod
    def _image_bytes_are_valid(data: bytes) -> bool:
        if not data:
            return False
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
        except (OSError, UnidentifiedImageError, ValueError):
            return False
        return True

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
