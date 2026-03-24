"""Convert downloaded images to PDF or CBZ archives."""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

from comix_dl.config import CONFIG, AppConfig

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _get_image_extensions(config: AppConfig | None = None) -> frozenset[str]:
    """Return supported image extensions (lazy, respects runtime config)."""
    cfg = config or CONFIG
    return frozenset(cfg.convert.supported_image_formats)


def collect_images(directory: Path, config: AppConfig | None = None) -> list[Path]:
    """Return sorted image files in *directory*."""
    extensions = _get_image_extensions(config)
    return [
        f for f in sorted(directory.iterdir())
        if f.is_file() and f.suffix.lstrip(".").lower() in extensions
    ]


def to_cbz(image_dir: Path, output_path: Path | None = None) -> Path:
    """Create a CBZ archive from images in *image_dir*.

    Args:
        image_dir: Directory containing image files.
        output_path: Where to write the CBZ. Defaults to ``image_dir.with_suffix('.cbz')``.

    Returns:
        Path to the created CBZ file.

    Raises:
        RuntimeError: If no images are found.
    """
    images = collect_images(image_dir)
    if not images:
        raise RuntimeError(f"No images found in {image_dir}")

    out = output_path or (image_dir.parent / (image_dir.name + ".cbz"))
    out.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as zf:
        for img in images:
            zf.write(img, img.name)

    logger.info("Created CBZ: %s (%d images)", out.name, len(images))
    return out


def to_pdf(image_dir: Path, output_path: Path | None = None, config: AppConfig | None = None) -> Path:
    """Create a PDF from images in *image_dir*.

    Images are processed in batches to limit memory usage.

    Args:
        image_dir: Directory containing image files.
        output_path: Where to write the PDF. Defaults to ``image_dir.with_suffix('.pdf')``.
        config: Optional AppConfig for DPI setting.

    Returns:
        Path to the created PDF file.

    Raises:
        RuntimeError: If no images are found.
    """
    cfg = config or CONFIG
    images = collect_images(image_dir, config=cfg)
    if not images:
        raise RuntimeError(f"No images found in {image_dir}")

    out = output_path or (image_dir.parent / (image_dir.name + ".pdf"))
    out.parent.mkdir(parents=True, exist_ok=True)

    dpi = cfg.convert.pdf_dpi
    _build_pdf_batched(images, out, dpi, batch_size=20)

    logger.info("Created PDF: %s (%d pages)", out.name, len(images))
    return out


def _build_pdf_batched(
    image_paths: list[Path],
    output: Path,
    dpi: float,
    *,
    batch_size: int = 20,
) -> None:
    """Build PDF by loading images in batches to limit memory usage."""
    from PIL import Image

    loaded: list[Image.Image] = []
    for img_path in image_paths:
        try:
            img: Image.Image = Image.open(img_path)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            loaded.append(img)
        except Exception as exc:
            logger.warning("Skipping %s: %s", img_path.name, exc)

    if not loaded:
        raise RuntimeError("No valid images to create PDF")

    first, *rest = loaded
    first.save(
        output,
        "PDF",
        resolution=dpi,
        save_all=True,
        append_images=rest,
    )

    for img in loaded:
        img.close()


def convert(image_dir: Path, fmt: str = "cbz", *, optimize: bool = False) -> Path:
    """Convert images using the specified format.

    Args:
        image_dir: Directory containing image files.
        fmt: One of ``"cbz"``, ``"pdf"``, or ``"both"``.
        optimize: If True, convert images to WebP before packaging.

    Returns:
        Path to the last created output file.
    """
    fmt = fmt.lower().strip()

    if optimize:
        optimize_images(image_dir)

    if fmt == "both":
        to_cbz(image_dir)
        return to_pdf(image_dir)

    if fmt == "pdf":
        return to_pdf(image_dir)

    return to_cbz(image_dir)


@dataclass
class OptimizeResult:
    """Result of image optimization."""

    original_bytes: int
    optimized_bytes: int
    converted_count: int
    skipped_count: int

    @property
    def saved_bytes(self) -> int:
        return self.original_bytes - self.optimized_bytes

    @property
    def savings_pct(self) -> float:
        if self.original_bytes == 0:
            return 0.0
        return (self.saved_bytes / self.original_bytes) * 100


def optimize_images(image_dir: Path, *, quality: int = 85) -> OptimizeResult:
    """Convert PNG/JPG/JPEG images in *image_dir* to WebP for smaller size.

    Already-WebP images are skipped.  The original files are replaced.

    Args:
        image_dir: Directory containing image files.
        quality: WebP quality (0-100).  Default 85 balances size vs quality.

    Returns:
        OptimizeResult with size savings info.
    """
    from PIL import Image

    images = collect_images(image_dir)
    original_bytes = 0
    optimized_bytes = 0
    converted = 0
    skipped = 0

    for img_path in images:
        original_bytes += img_path.stat().st_size

        # Skip if already WebP
        if img_path.suffix.lower() == ".webp":
            optimized_bytes += img_path.stat().st_size
            skipped += 1
            continue

        try:
            img: Image.Image = Image.open(img_path)
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")

            webp_path = img_path.with_suffix(".webp")
            img.save(webp_path, "WEBP", quality=quality)
            img.close()

            optimized_bytes += webp_path.stat().st_size

            # Remove original file
            if img_path != webp_path:
                img_path.unlink()

            converted += 1
        except Exception as exc:
            logger.warning("Optimize skip %s: %s", img_path.name, exc)
            optimized_bytes += img_path.stat().st_size
            skipped += 1

    result = OptimizeResult(
        original_bytes=original_bytes,
        optimized_bytes=optimized_bytes,
        converted_count=converted,
        skipped_count=skipped,
    )

    if converted > 0:
        logger.info(
            "Optimized %d images: %.1f MB → %.1f MB (%.0f%% saved)",
            converted,
            result.original_bytes / 1_048_576,
            result.optimized_bytes / 1_048_576,
            result.savings_pct,
        )

    return result
