"""Tests for application runtime/session helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from comix_dl import sites
from comix_dl.core.application import session as app_session
from comix_dl.core.models import ChapterInfo
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
    tmp_path: Path,
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

    # Avoid touching the user's real ~/.config when the session writes
    # mirror state on the probe outcome.
    mirror_repo = app_session.MirrorStateRepository(state_file=tmp_path / "mirror_state.json")

    settings = Settings(output_dir="/tmp/default-output")

    try:
        async with app_session.open_application_session(
            settings=settings,
            output="/tmp/custom-output",
            mirror_repository=mirror_repo,
        ) as session:
            assert session.settings is settings
            assert session.output_dir == Path("/tmp/custom-output")
            assert session.adapter is fake_adapter
            assert captured["browser_base_url"] == "https://fake.example"
            assert captured["browser_config"] is session.config
            # The session must register exactly one on_engine_ready hook
            # so site setup runs at the right moment. The hook wraps
            # adapter.on_engine_ready so it can also probe the mirror;
            # we only assert one was registered, not its identity.
            assert len(session.browser.ready_hooks) == 1
    finally:
        # Restore the real registry by re-importing the comix_to module.
        sites.clear()
        import comix_dl.sites.comix_to  # noqa: F401 — side-effect import re-registers


@pytest.mark.asyncio
async def test_open_application_session_uses_session_scoped_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    @dataclass
    class FakeBrowser:
        config: AppConfig
        base_url: str
        ready_hooks: list[object]

        def register_on_engine_ready(self, hook: object) -> None:
            self.ready_hooks.append(hook)

    class FakeBrowserContext:
        def __init__(self, *, config: AppConfig, base_url: str) -> None:
            self._browser = FakeBrowser(config=config, base_url=base_url, ready_hooks=[])

        async def __aenter__(self) -> FakeBrowser:
            return self._browser

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class FakeAdapter:
        name = "fake-session-scoped"
        needs_browser = True

        def __init__(self, label: str) -> None:
            self.label = label
            self.mirrors = ["https://fake.example"]
            self.cache: dict[str, str] = {}

        def new_session(self) -> FakeAdapter:
            return FakeAdapter(f"{self.label}-session")

        async def on_engine_ready(self, _engine: object) -> None:
            return None

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

    registry_adapter = FakeAdapter("registry")
    sites.clear()
    sites.register(registry_adapter)
    monkeypatch.setattr(app_session, "CdpBrowser", FakeBrowserContext)
    mirror_repo = app_session.MirrorStateRepository(state_file=tmp_path / "mirror_state.json")

    try:
        async with app_session.open_application_session(
            settings=Settings(output_dir="/tmp/default-output"),
            mirror_repository=mirror_repo,
        ) as first:
            first_adapter = first.adapter

        async with app_session.open_application_session(
            settings=Settings(output_dir="/tmp/default-output"),
            mirror_repository=mirror_repo,
        ) as second:
            second_adapter = second.adapter

        assert first_adapter is not registry_adapter
        assert second_adapter is not registry_adapter
        assert first_adapter is not second_adapter
        assert first_adapter.label == "registry-session"
        assert second_adapter.label == "registry-session"
        assert first_adapter.cache is not second_adapter.cache
    finally:
        sites.clear()
        import comix_dl.sites.comix_to  # noqa: F401 — side-effect import re-registers


@pytest.mark.asyncio
async def test_application_session_download_injects_default_history_and_notifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakeHistoryRepository:
        pass

    def fake_notifier(title: str, body: str) -> None:
        captured["notification"] = (title, body)

    sentinel = object()

    async def fake_download_chapters(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(app_session, "HistoryRepository", FakeHistoryRepository)
    monkeypatch.setattr(app_session, "send_notification", fake_notifier)
    monkeypatch.setattr(app_session, "download_chapters", fake_download_chapters)

    browser = object()
    adapter = object()
    session = app_session.ApplicationSession(
        settings=Settings(output_dir=str(tmp_path)),
        config=app_session.build_runtime_config(Settings(output_dir=str(tmp_path))),
        output_dir=tmp_path,
        browser=browser,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
    )
    chapters = [ChapterInfo(title="Chapter 1", chapter_id=1, number="1")]

    result = await session.download(
        series_title="Series A",
        chapters=chapters,
        fmt="pdf",
        optimize=True,
    )

    assert result is sentinel
    assert isinstance(captured["kwargs"]["history_repository"], FakeHistoryRepository)
    assert captured["kwargs"]["notifier"] is fake_notifier
    assert captured["kwargs"]["chapters"] == chapters
