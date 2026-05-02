"""Tests for application runtime/session helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from comix_dl import sites
from comix_dl.core.application import session as app_session
from comix_dl.core.settings import Settings

if TYPE_CHECKING:
    from comix_dl.core.config import AppConfig


def test_load_runtime_uses_explicit_output_override() -> None:
    settings = Settings(output_dir="/tmp/default-output", default_format="cbz")

    runtime = app_session.load_runtime(settings=settings, output="/tmp/custom-output")

    assert runtime.settings is settings
    assert runtime.config.convert.default_format == "cbz"
    assert runtime.output_dir == Path("/tmp/custom-output")


@pytest.mark.asyncio
async def test_open_application_session_wires_active_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    @dataclass
    class FakeBrowser:
        config: AppConfig
        base_url: str
        ready_hooks: list[object]

        def register_on_engine_ready(self, hook: object) -> None:
            self.ready_hooks.append(hook)

    class FakeBrowserContext:
        def __init__(self, *, config: AppConfig, base_url: str) -> None:
            captured["browser_config"] = config
            captured["browser_base_url"] = base_url
            self._browser = FakeBrowser(config=config, base_url=base_url, ready_hooks=[])

        async def __aenter__(self) -> FakeBrowser:
            return self._browser

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class FakeAdapter:
        name = "fake"
        needs_browser = True

        def __init__(self) -> None:
            self.mirrors = ["https://fake.example"]

        async def on_engine_ready(self, _engine: object) -> None:
            return None

        # Other SiteAdapter methods are not exercised in this test.
        def matches_url(self, url: str) -> bool:
            return False

        def parse_identifier(self, _: str) -> str | None:
            return None

        async def probe_alive(self, _engine: object) -> bool:
            return True

        async def search(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

        async def get_series(self, *_args: object, **_kwargs: object) -> object:
            raise NotImplementedError

        async def get_chapter_images(self, *_args: object, **_kwargs: object) -> object:
            return None

        def deduplicate(self, chapters: list[object]) -> tuple[list[object], list[object]]:
            return chapters, []

    fake_adapter = FakeAdapter()

    # Replace the live registry with our fake for the duration of the test.
    sites.clear()
    sites.register(fake_adapter)
    monkeypatch.setattr(app_session, "CdpBrowser", FakeBrowserContext)

    settings = Settings(output_dir="/tmp/default-output")

    try:
        async with app_session.open_application_session(
            settings=settings, output="/tmp/custom-output",
        ) as session:
            assert session.settings is settings
            assert session.output_dir == Path("/tmp/custom-output")
            assert session.adapter is fake_adapter
            assert captured["browser_base_url"] == "https://fake.example"
            assert captured["browser_config"] is session.config
            # The session must register the adapter's on_engine_ready hook
            # with the engine so site setup runs at the right moment.
            assert session.browser.ready_hooks == [fake_adapter.on_engine_ready]
    finally:
        # Restore the real registry by re-importing the comix_to module.
        sites.clear()
        import comix_dl.sites.comix_to  # noqa: F401 — side-effect import re-registers
