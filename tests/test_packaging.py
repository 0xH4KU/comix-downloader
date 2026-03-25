"""Tests for packaging metadata and runtime dependency contracts."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_runtime_dependencies_include_pypdf() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]
    dependencies = project["dependencies"]

    assert any(
        dependency.partition(">=")[0].partition("==")[0].strip().lower() == "pypdf"
        for dependency in dependencies
    )
