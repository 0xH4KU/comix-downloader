"""Focused tests for CLI flow adapter behaviors."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from comix_dl.application.session import RuntimeContext
from comix_dl.cli import flows
from comix_dl.config import AppConfig
from comix_dl.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


def test_flow_clean_auto_confirm_skips_prompt_and_removes_candidates(tmp_path: Path) -> None:
    chapter_dir = tmp_path / "Series A" / "Chapter 1"
    chapter_dir.mkdir(parents=True)
    (chapter_dir / ".complete").touch()
    (chapter_dir.parent / "Chapter 1.pdf").write_bytes(b"pdf")
    (chapter_dir / "001.jpg").write_bytes(b"image")

    with (
        patch.object(
            flows,
            "load_runtime",
            return_value=RuntimeContext(
                settings=Settings(output_dir=str(tmp_path)),
                config=AppConfig(),
                output_dir=tmp_path,
            ),
        ),
        patch.object(flows.Prompt, "ask", side_effect=AssertionError("prompt should not be used")),
    ):
        result = flows.flow_clean(auto_confirm=True)

    assert result == 0
    assert not chapter_dir.exists()
