"""Shared fixtures for comix-downloader tests."""

from __future__ import annotations

import copy
from dataclasses import fields
from typing import Any
from unittest.mock import AsyncMock

import pytest

from comix_dl.config import CONFIG, AppConfig


@pytest.fixture(autouse=True)
def _isolate_config():
    """Save and restore CONFIG state around every test.

    This prevents tests from polluting each other through the global
    CONFIG singleton.
    """
    originals: dict[str, Any] = {}
    for f in fields(AppConfig):
        originals[f.name] = copy.deepcopy(getattr(CONFIG, f.name))

    yield

    for name, value in originals.items():
        setattr(CONFIG, name, value)


@pytest.fixture()
def mock_browser() -> AsyncMock:
    """Return a mock CdpBrowser with async methods pre-configured."""
    browser = AsyncMock()
    browser.get_json = AsyncMock(return_value={})
    browser.get_bytes = AsyncMock(return_value=b"")
    browser.ensure_cf_clearance = AsyncMock()
    browser.acquire_page = AsyncMock()
    browser.release_page = AsyncMock()
    return browser
