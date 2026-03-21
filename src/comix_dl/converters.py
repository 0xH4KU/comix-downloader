"""Convert downloaded images to PDF or CBZ archives."""

from __future__ import annotations

import logging
import zipfile
from typing import TYPE_CHECKING

from comix_dl.config import CONFIG

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = frozenset(CONFIG.convert.supported_image_formats)


def collect_images(directory: Path) -> list[Path]:
    """Return sorted image files in *directory*."""
    files = [
        f for f in sorted(directory.iterdir())
        if f.is_file() and f.suffix.lstrip(".").lower() in _IMAGE_EXTENSIONS
    ]
    return files


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

    out = output_path or image_dir.with_suffix(".cbz")
    out.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as zf:
        for img in images:
            zf.write(img, img.name)

    logger.info("Created CBZ: %s (%d images)", out.name, len(images))
    return out


def to_pdf(image_dir: Path, output_path: Path | None = None) -> Path:
    """Create a PDF from images in *image_dir*.

    Args:
        image_dir: Directory containing image files.
        output_path: Where to write the PDF. Defaults to ``image_dir.with_suffix('.pdf')``.

    Returns:
        Path to the created PDF file.

    Raises:
        RuntimeError: If no images are found.
    """
    from PIL import Image

    images = collect_images(image_dir)
    if not images:
        raise RuntimeError(f"No images found in {image_dir}")

    out = output_path or image_dir.with_suffix(".pdf")
    out.parent.mkdir(parents=True, exist_ok=True)

    pil_images: list[Image.Image] = []
    for img_path in images:
        try:
            img = Image.open(img_path)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            pil_images.append(img)
        except Exception as exc:
            logger.warning("Skipping %s: %s", img_path.name, exc)

    if not pil_images:
        raise RuntimeError(f"No valid images could be loaded from {image_dir}")

    first, *rest = pil_images
    first.save(
        out,
        "PDF",
        resolution=CONFIG.convert.pdf_dpi,
        save_all=True,
        append_images=rest,
    )

    # Clean up
    for img in pil_images:
        img.close()

    logger.info("Created PDF: %s (%d pages)", out.name, len(pil_images))
    return out


def convert(image_dir: Path, fmt: str = "cbz") -> Path:
    """Convert images using the specified format.

    Args:
        image_dir: Directory containing image files.
        fmt: One of ``"cbz"``, ``"pdf"``, or ``"both"``.

    Returns:
        Path to the last created output file.
    """
    fmt = fmt.lower().strip()

    if fmt == "both":
        to_cbz(image_dir)
        return to_pdf(image_dir)

    if fmt == "pdf":
        return to_pdf(image_dir)

    return to_cbz(image_dir)
