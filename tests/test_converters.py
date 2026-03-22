"""Tests for comix_dl.converters — image collection, CBZ, and PDF conversion."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import pytest

from comix_dl.converters import collect_images, convert, to_cbz, to_pdf

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_test_images(directory: Path, count: int = 3, fmt: str = "png") -> list[Path]:
    """Create minimal valid image files for testing.

    For PNG: valid 1x1 pixel PNG.
    For JPG: valid JPEG header (enough for Pillow to open).
    """
    from PIL import Image

    files = []
    for i in range(1, count + 1):
        path = directory / f"{i:03d}.{fmt}"
        img = Image.new("RGB", (10, 10), color=(i * 50, i * 30, i * 20))
        img.save(path, fmt.upper() if fmt != "jpg" else "JPEG")
        img.close()
        files.append(path)
    return files


# ---------------------------------------------------------------------------
# collect_images
# ---------------------------------------------------------------------------

class TestCollectImages:
    def test_collects_supported_formats(self, tmp_path: Path):
        (tmp_path / "001.png").write_bytes(b"\x89PNG")
        (tmp_path / "002.jpg").write_bytes(b"\xff\xd8")
        (tmp_path / "003.webp").write_bytes(b"RIFF")
        result = collect_images(tmp_path)
        assert len(result) == 3

    def test_ignores_non_image_files(self, tmp_path: Path):
        (tmp_path / "readme.txt").write_text("not an image")
        (tmp_path / ".complete").touch()
        (tmp_path / "001.png").write_bytes(b"\x89PNG")
        result = collect_images(tmp_path)
        assert len(result) == 1
        assert result[0].name == "001.png"

    def test_sorted_order(self, tmp_path: Path):
        (tmp_path / "003.png").write_bytes(b"x")
        (tmp_path / "001.png").write_bytes(b"x")
        (tmp_path / "002.png").write_bytes(b"x")
        result = collect_images(tmp_path)
        assert [f.name for f in result] == ["001.png", "002.png", "003.png"]

    def test_empty_directory(self, tmp_path: Path):
        assert collect_images(tmp_path) == []

    def test_case_insensitive_extension(self, tmp_path: Path):
        (tmp_path / "001.PNG").write_bytes(b"x")
        (tmp_path / "002.JPG").write_bytes(b"x")
        result = collect_images(tmp_path)
        assert len(result) == 2

    def test_avif_supported(self, tmp_path: Path):
        (tmp_path / "001.avif").write_bytes(b"x")
        result = collect_images(tmp_path)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# to_cbz
# ---------------------------------------------------------------------------

class TestToCbz:
    def test_creates_valid_cbz(self, tmp_path: Path):
        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        _create_test_images(img_dir, count=3)

        result = to_cbz(img_dir)
        assert result.suffix == ".cbz"
        assert result.exists()

        # CBZ is a ZIP archive
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert len(names) == 3
            assert "001.png" in names

    def test_custom_output_path(self, tmp_path: Path):
        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        _create_test_images(img_dir, count=1)

        out = tmp_path / "output" / "custom.cbz"
        result = to_cbz(img_dir, output_path=out)
        assert result == out
        assert result.exists()

    def test_no_compression(self, tmp_path: Path):
        """CBZ should use ZIP_STORED (no compression) per comic book standard."""
        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        _create_test_images(img_dir, count=1)

        result = to_cbz(img_dir)
        with zipfile.ZipFile(result) as zf:
            for info in zf.infolist():
                assert info.compress_type == zipfile.ZIP_STORED

    def test_empty_directory_raises(self, tmp_path: Path):
        img_dir = tmp_path / "empty"
        img_dir.mkdir()
        with pytest.raises(RuntimeError, match="No images found"):
            to_cbz(img_dir)


# ---------------------------------------------------------------------------
# to_pdf
# ---------------------------------------------------------------------------

class TestToPdf:
    def test_creates_valid_pdf(self, tmp_path: Path):
        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        _create_test_images(img_dir, count=3)

        result = to_pdf(img_dir)
        assert result.suffix == ".pdf"
        assert result.exists()
        # PDF files start with %PDF
        content = result.read_bytes()
        assert content[:4] == b"%PDF"

    def test_custom_output_path(self, tmp_path: Path):
        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        _create_test_images(img_dir, count=1)

        out = tmp_path / "output" / "custom.pdf"
        result = to_pdf(img_dir, output_path=out)
        assert result == out
        assert result.exists()

    def test_empty_directory_raises(self, tmp_path: Path):
        img_dir = tmp_path / "empty"
        img_dir.mkdir()
        with pytest.raises(RuntimeError, match="No images found"):
            to_pdf(img_dir)

    def test_rgba_images_converted(self, tmp_path: Path):
        """RGBA images should be converted to RGB without error."""
        from PIL import Image

        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        img = Image.new("RGBA", (10, 10), color=(255, 0, 0, 128))
        img.save(img_dir / "001.png")
        img.close()

        result = to_pdf(img_dir)
        assert result.exists()


# ---------------------------------------------------------------------------
# convert — format routing
# ---------------------------------------------------------------------------

class TestConvert:
    def test_cbz_format(self, tmp_path: Path):
        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        _create_test_images(img_dir, count=1)
        result = convert(img_dir, "cbz")
        assert result.suffix == ".cbz"

    def test_pdf_format(self, tmp_path: Path):
        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        _create_test_images(img_dir, count=1)
        result = convert(img_dir, "pdf")
        assert result.suffix == ".pdf"

    def test_both_format(self, tmp_path: Path):
        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        _create_test_images(img_dir, count=1)
        result = convert(img_dir, "both")
        # "both" returns the last created file (PDF)
        assert result.suffix == ".pdf"
        assert img_dir.with_suffix(".cbz").exists()

    def test_case_insensitive(self, tmp_path: Path):
        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        _create_test_images(img_dir, count=1)
        result = convert(img_dir, "CBZ")
        assert result.suffix == ".cbz"

    def test_default_is_cbz(self, tmp_path: Path):
        img_dir = tmp_path / "chapter"
        img_dir.mkdir()
        _create_test_images(img_dir, count=1)
        result = convert(img_dir)
        assert result.suffix == ".cbz"
