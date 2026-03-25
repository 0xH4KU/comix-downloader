"""Shared fixtures for comix-downloader tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


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
