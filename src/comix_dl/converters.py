"""Convert downloaded images to PDF or CBZ archives."""

from __future__ import annotations

import logging
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from comix_dl.config import AppConfig
from comix_dl.errors import ConversionError

logger = logging.getLogger(__name__)


def _resolve_config(config: AppConfig | None = None) -> AppConfig:
    """Return an explicit runtime config or a fresh default config."""
    return config if config is not None else AppConfig()


def _get_image_extensions(config: AppConfig | None = None) -> frozenset[str]:
    """Return supported image extensions (lazy, respects runtime config)."""
    cfg = _resolve_config(config)
    return frozenset(cfg.convert.supported_image_formats)


def _get_pdf_batch_size(config: AppConfig | None = None) -> int:
    """Return a safe PDF batch size from config."""
    cfg = _resolve_config(config)
    return max(1, cfg.convert.pdf_batch_size)


def collect_images(directory: Path, config: AppConfig | None = None) -> list[Path]:
    """Return sorted image files in *directory*."""
    extensions = _get_image_extensions(config)
    return [
        f for f in sorted(directory.iterdir())
        if f.is_file() and f.suffix.lstrip(".").lower() in extensions
    ]


def to_cbz(image_dir: Path, output_path: Path | None = None, config: AppConfig | None = None) -> Path:
    """Create a CBZ archive from images in *image_dir*.

    Args:
        image_dir: Directory containing image files.
        output_path: Where to write the CBZ. Defaults to ``image_dir.with_suffix('.cbz')``.

    Returns:
        Path to the created CBZ file.

    Raises:
        ConversionError: If no images are found.
    """
    images = collect_images(image_dir, config=config)
    if not images:
        raise ConversionError(f"No images found in {image_dir}")

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
        ConversionError: If no images are found.
    """
    cfg = _resolve_config(config)
    images = collect_images(image_dir, config=cfg)
    if not images:
        raise ConversionError(f"No images found in {image_dir}")

    out = output_path or (image_dir.parent / (image_dir.name + ".pdf"))
    out.parent.mkdir(parents=True, exist_ok=True)

    dpi = cfg.convert.pdf_dpi
    _build_pdf_batched(images, out, dpi, batch_size=_get_pdf_batch_size(cfg))

    logger.info("Created PDF: %s (%d pages)", out.name, len(images))
    return out


def _build_pdf_batched(
    image_paths: list[Path],
    output: Path,
    dpi: float,
    *,
    batch_size: int = 20,
) -> None:
    """Build PDF by loading images in batches to limit memory usage.

    Only ``batch_size`` images are held in memory at any time.
    The first batch creates the PDF; subsequent batches are written to
    temporary PDFs and merged into the final output.

    For multi-batch PDFs, a merge backend (``pikepdf`` or ``pypdf``) is
    required. If none is available, this function fails fast rather than
    silently creating an incomplete PDF.
    """
    from PIL import Image

    def _load_batch(paths: list[Path]) -> list[Image.Image]:
        imgs: list[Image.Image] = []
        for p in paths:
            try:
                img: Image.Image = Image.open(p)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                imgs.append(img)
            except Exception as exc:
                logger.warning("Skipping %s: %s", p.name, exc)
        return imgs

    # For small sets (≤ batch_size), single-pass is fine
    if len(image_paths) <= batch_size:
        loaded = _load_batch(image_paths)
        if not loaded:
            raise ConversionError("No valid images to create PDF")
        first, *rest = loaded
        first.save(output, "PDF", resolution=dpi, save_all=True, append_images=rest)
        for img in loaded:
            img.close()
        return

    # For large sets, build in true batches inside an isolated temp workspace
    with tempfile.TemporaryDirectory(prefix="comix-dl-pdf-") as temp_dir:
        temp_root = Path(temp_dir)
        temp_pdfs: list[Path] = []
        for i in range(0, len(image_paths), batch_size):
            batch_paths = image_paths[i : i + batch_size]
            batch_imgs = _load_batch(batch_paths)
            if not batch_imgs:
                continue

            # Each batch becomes one temp PDF inside a dedicated workspace that
            # is removed automatically after merge or failure.
            tmp_path = temp_root / f"batch-{(i // batch_size) + 1:04d}.pdf"

            first, *rest = batch_imgs
            first.save(tmp_path, "PDF", resolution=dpi, save_all=True, append_images=rest)
            for img in batch_imgs:
                img.close()

            temp_pdfs.append(tmp_path)

        if not temp_pdfs:
            raise ConversionError("No valid images to create PDF")

        # Merge all batch PDFs into the final output
        _merge_pdfs(temp_pdfs, output)


def _merge_pdfs(pdf_paths: list[Path], output: Path) -> None:
    """Merge multiple PDF files into one.

    Uses pikepdf if available for efficient merging and falls back to pypdf.
    If neither backend is available, raises instead of emitting a truncated PDF.
    """
    if len(pdf_paths) == 1:
        import shutil
        shutil.copy2(pdf_paths[0], output)
        return

    try:
        import pikepdf  # type: ignore[import-not-found]
        with pikepdf.open(pdf_paths[0]) as dest:
            for src_path in pdf_paths[1:]:
                with pikepdf.open(src_path) as src:
                    dest.pages.extend(src.pages)
            dest.save(output)
        return
    except ImportError:
        pass

    # Fallback: if no pikepdf, use pypdf
    try:
        from pypdf import PdfWriter
        writer = PdfWriter()
        for p in pdf_paths:
            writer.append(str(p))
        writer.write(str(output))
        writer.close()
        return
    except ImportError:
        pass

    raise ConversionError(
        "Large PDF conversion requires a PDF merge backend (`pypdf` or `pikepdf`). "
        "Install one of them and retry; refusing to create an incomplete PDF."
    )


def convert(
    image_dir: Path,
    fmt: str = "cbz",
    *,
    optimize: bool = False,
    config: AppConfig | None = None,
) -> Path:
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
        optimize_images(image_dir, config=config)

    if fmt == "both":
        to_cbz(image_dir, config=config)
        return to_pdf(image_dir, config=config)

    if fmt == "pdf":
        return to_pdf(image_dir, config=config)

    return to_cbz(image_dir, config=config)


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


def optimize_images(image_dir: Path, *, quality: int = 85, config: AppConfig | None = None) -> OptimizeResult:
    """Convert PNG/JPG/JPEG images in *image_dir* to WebP for smaller size.

    Already-WebP images are skipped.  The original files are replaced.

    Args:
        image_dir: Directory containing image files.
        quality: WebP quality (0-100).  Default 85 balances size vs quality.

    Returns:
        OptimizeResult with size savings info.
    """
    from PIL import Image

    images = collect_images(image_dir, config=config)
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
