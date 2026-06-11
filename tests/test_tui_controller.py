"""Tests for the Textual-independent TUI controller."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from comix_dl.core.application.download_usecase import DownloadChapterEvent
from comix_dl.core.history import HistoryEntry
from comix_dl.core.models import ChapterInfo, SearchResult
from comix_dl.core.settings import Settings
from comix_dl.core.tui.controller import TuiController
from comix_dl.core.tui.state import DownloadRequest
from tests.flow_helpers import (
    _make_series,
    _make_summary,
    _SessionContext,
    _write_valid_pdf,
)

if TYPE_CHECKING:
    from pathlib import Path


class _TrackingSessionContext(_SessionContext):
    def __init__(self, session: object) -> None:
        super().__init__(session)
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> object:
        self.entered = True
        return await super().__aenter__()

    async def __aexit__(self, *_: object) -> None:
        self.exited = True
        await super().__aexit__(*_)


class _FakeSettingsRepository:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.saved: list[Settings] = []

    def load(self) -> Settings:
        return self._settings

    def save(self, settings: Settings) -> None:
        self.saved.append(settings)
        self._settings = settings


class _FakeHistoryRepository:
    def __init__(self, entries: list[HistoryEntry]) -> None:
        self._entries = entries

    def list_entries(self) -> list[HistoryEntry]:
        return self._entries


def _session(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        settings=Settings(output_dir=str(tmp_path), default_format="pdf", optimize_images=True),
        output_dir=tmp_path,
        search=AsyncMock(
            return_value=[
                SearchResult(
                    title="Series A",
                    url="https://comix.to/manga/series-a",
                    slug="series-a",
                    hash_id="a",
                )
            ]
        ),
        load_series=AsyncMock(return_value=_make_series()),
        download=AsyncMock(return_value=_make_summary(completed=1)),
    )


@pytest.mark.asyncio
async def test_controller_opens_and_closes_session(tmp_path: Path) -> None:
    session = _session(tmp_path)
    context = _TrackingSessionContext(session)
    kwargs_seen: list[dict[str, object]] = []
    controller = TuiController(
        session_factory=lambda **kwargs: kwargs_seen.append(kwargs) or context,
        mirror="https://comix.cc",
    )

    await controller.open()

    assert controller.is_open is True
    assert context.entered is True
    assert kwargs_seen == [{"mirror_override": "https://comix.cc"}]

    await controller.close()

    assert controller.is_open is False
    assert context.exited is True


@pytest.mark.asyncio
async def test_controller_searches_with_open_session(tmp_path: Path) -> None:
    session = _session(tmp_path)
    controller = TuiController(session_factory=lambda **_kwargs: _SessionContext(session))

    await controller.open()
    results = await controller.search("series")

    assert results[0].title == "Series A"
    session.search.assert_awaited_once_with("series")


@pytest.mark.asyncio
async def test_controller_loads_series_with_open_session(tmp_path: Path) -> None:
    session = _session(tmp_path)
    controller = TuiController(session_factory=lambda **_kwargs: _SessionContext(session))

    await controller.open()
    series = await controller.load_series("series-a")

    assert series.title == "Series A"
    session.load_series.assert_awaited_once_with("series-a")


@pytest.mark.asyncio
async def test_controller_download_forwards_events_and_shutdown(tmp_path: Path) -> None:
    session = _session(tmp_path)
    events: list[DownloadChapterEvent] = []

    async def fake_download(**kwargs: Any) -> object:
        kwargs["on_event"](DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="started"))
        assert callable(kwargs["is_shutdown"])
        assert kwargs["is_shutdown"]() is False
        return _make_summary(completed=1)

    session.download.side_effect = fake_download
    controller = TuiController(session_factory=lambda **_kwargs: _SessionContext(session))
    await controller.open()

    chapter = ChapterInfo(title="Chapter 1", chapter_id=1, number="1")
    request = DownloadRequest(series_title="Series A", chapters=(chapter,), fmt="cbz", optimize=False)
    summary = await controller.download(request, on_event=events.append)

    assert summary.completed == 1
    assert events[0].kind == "started"
    session.download.assert_awaited_once()
    assert session.download.await_args.kwargs["series_title"] == "Series A"
    assert session.download.await_args.kwargs["chapters"] == [chapter]
    assert session.download.await_args.kwargs["fmt"] == "cbz"
    assert session.download.await_args.kwargs["optimize"] is False


@pytest.mark.asyncio
async def test_controller_shutdown_flag_is_reset_on_open(tmp_path: Path) -> None:
    controller = TuiController(session_factory=lambda **_kwargs: _SessionContext(_session(tmp_path)))

    controller.request_shutdown()
    assert controller.is_shutdown_requested() is True

    await controller.open()

    assert controller.is_shutdown_requested() is False


@pytest.mark.asyncio
async def test_controller_rejects_use_before_open(tmp_path: Path) -> None:
    controller = TuiController(session_factory=lambda **_kwargs: _SessionContext(_session(tmp_path)))

    with pytest.raises(RuntimeError, match="TUI session is not open"):
        await controller.search("series")


@pytest.mark.asyncio
async def test_settings_and_output_dir_use_session_when_open(tmp_path: Path) -> None:
    session = _session(tmp_path / "session-output")
    fallback_settings = Settings(output_dir=str(tmp_path / "fallback-output"), default_format="cbz")
    settings_repository = _FakeSettingsRepository(fallback_settings)
    controller = TuiController(
        session_factory=lambda **_kwargs: _SessionContext(session),
        settings_repository=settings_repository,
    )

    assert controller.settings is fallback_settings
    assert controller.output_dir == tmp_path / "fallback-output"

    await controller.open()

    assert controller.settings is session.settings
    assert controller.output_dir == tmp_path / "session-output"


def test_settings_and_history_repository_delegation(tmp_path: Path) -> None:
    entry = HistoryEntry(timestamp="2026-06-11T00:00:00+00:00", title="Series A", chapters_count=1, format="pdf")
    settings = Settings(output_dir=str(tmp_path), default_format="pdf")
    settings_repository = _FakeSettingsRepository(settings)
    history_repository = _FakeHistoryRepository([entry])
    controller = TuiController(settings_repository=settings_repository, history_repository=history_repository)

    updated = Settings(output_dir=str(tmp_path / "updated"), default_format="cbz")
    controller.save_settings(updated)

    assert controller.load_settings() is updated
    assert controller.history_entries() == [entry]


def test_download_listing_and_cleanup_use_output_dir(tmp_path: Path) -> None:
    series_dir = tmp_path / "Series A"
    chapter_dir = series_dir / "Chapter 1"
    chapter_dir.mkdir(parents=True)
    (chapter_dir / ".complete").touch()
    (chapter_dir / "001.jpg").write_bytes(b"image")
    _write_valid_pdf(series_dir / "Chapter 1.pdf")
    settings_repository = _FakeSettingsRepository(Settings(output_dir=str(tmp_path)))
    controller = TuiController(settings_repository=settings_repository)

    downloads = controller.list_downloads()
    plan = controller.cleanup_plan(series_title="Series A")
    result = controller.apply_cleanup(plan)

    assert [download.name for download in downloads] == ["Series A"]
    assert [candidate.relative_path.as_posix() for candidate in plan.candidates] == ["Series A/Chapter 1"]
    assert result.removed_count == 1
    assert not chapter_dir.exists()
