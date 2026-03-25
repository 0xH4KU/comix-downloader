"""Tests for application runtime/session helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from comix_dl.application import session as app_session
from comix_dl.settings import Settings

if TYPE_CHECKING:
    from comix_dl.config import AppConfig


def test_load_runtime_uses_explicit_output_override() -> None:
    settings = Settings(output_dir="/tmp/default-output", default_format="cbz")

    runtime = app_session.load_runtime(settings=settings, output="/tmp/custom-output")

    assert runtime.settings is settings
    assert runtime.config.convert.default_format == "cbz"
    assert runtime.output_dir == Path("/tmp/custom-output")


@pytest.mark.asyncio
async def test_open_application_session_builds_browser_and_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    @dataclass
    class FakeBrowser:
        config: AppConfig

    class FakeBrowserContext:
        def __init__(self, *, config: AppConfig) -> None:
            captured["browser_config"] = config
            self._browser = FakeBrowser(config=config)

        async def __aenter__(self) -> FakeBrowser:
            return self._browser

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class FakeService:
        def __init__(self, browser: FakeBrowser, *, config: AppConfig) -> None:
            captured["service_browser"] = browser
            captured["service_config"] = config

    monkeypatch.setattr(app_session, "CdpBrowser", FakeBrowserContext)
    monkeypatch.setattr(app_session, "ComixService", FakeService)

    settings = Settings(output_dir="/tmp/default-output")

    async with app_session.open_application_session(settings=settings, output="/tmp/custom-output") as session:
        assert session.settings is settings
        assert session.output_dir == Path("/tmp/custom-output")
        assert captured["browser_config"] is session.config
        assert captured["service_config"] is session.config
        assert captured["service_browser"] is session.browser

