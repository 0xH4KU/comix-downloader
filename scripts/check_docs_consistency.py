#!/usr/bin/env python3
"""Check version strings that user-facing docs depend on."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _extract(pattern: str, text: str, *, label: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"Could not find {label}.")
    return match.group(1)


def main() -> int:
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package_text = (ROOT / "src/comix_dl/__init__.py").read_text(encoding="utf-8")
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")

    pyproject_version = _extract(
        r'^version = "([0-9]+\.[0-9]+\.[0-9]+)"$',
        pyproject_text,
        label="pyproject version",
    )
    package_version = _extract(
        r'__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"',
        package_text,
        label="package fallback version",
    )
    readme_badge_version = _extract(
        r"version-([0-9]+\.[0-9]+\.[0-9]+)-blue",
        readme_text,
        label="README version badge",
    )

    mismatches: list[str] = []
    if package_version != pyproject_version:
        mismatches.append(
            f"src/comix_dl/__init__.py fallback version is {package_version}, expected {pyproject_version}.",
        )
    if readme_badge_version != pyproject_version:
        mismatches.append(
            f"README badge version is {readme_badge_version}, expected {pyproject_version}.",
        )

    if mismatches:
        print("Docs consistency check failed:", file=sys.stderr)
        for item in mismatches:
            print(f"- {item}", file=sys.stderr)
        return 1

    print(f"Docs consistency OK: version {pyproject_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
