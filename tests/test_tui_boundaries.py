"""Boundary tests for the full-screen TUI package."""

from __future__ import annotations

import ast
from pathlib import Path


def test_tui_does_not_import_prompt_cli_flows() -> None:
    forbidden = {
        "comix_dl.core.cli.flows",
        "comix_dl.core.cli.flow_prompts",
        "comix_dl.core.cli.interactive",
        "comix_dl.core.cli.download_progress",
    }
    root = Path("src/comix_dl/core/tui")
    offenders: list[str] = []

    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden:
                        offenders.append(f"{path}:{alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module in forbidden:
                offenders.append(f"{path}:{node.module}")

    assert offenders == []
