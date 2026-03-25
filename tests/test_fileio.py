"""Tests for filesystem helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from comix_dl.fileio import atomic_write_text

if TYPE_CHECKING:
    from pathlib import Path


class TestAtomicWriteText:
    def test_creates_file(self, tmp_path: Path):
        target = tmp_path / "state.json"

        atomic_write_text(target, '{"ok": true}\n')

        assert target.read_text(encoding="utf-8") == '{"ok": true}\n'

    def test_overwrites_existing_file(self, tmp_path: Path):
        target = tmp_path / "state.json"
        target.write_text("old\n", encoding="utf-8")

        atomic_write_text(target, "new\n")

        assert target.read_text(encoding="utf-8") == "new\n"
        assert not list(tmp_path.glob("*.tmp"))
