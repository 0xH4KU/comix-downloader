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


def test_package_includes_javascript_assets() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    config = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    package_data = config["tool"]["setuptools"]["package-data"]
    assert "*.js" in package_data["comix_dl.sites.assets"]
