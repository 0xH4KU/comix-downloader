"""Tests for the Textual-independent TUI controller."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

from comix_dl.core.application.download_usecase import DownloadChapterEvent, DownloadSummary, ShutdownCheck
from comix_dl.core.history import HistoryEntry
from comix_dl.core.models import ChapterInfo, SearchResult, SeriesInfo
from comix_dl.core.settings import Settings
from comix_dl.core.tui.controller import ApplicationSessionLike, TuiController
from comix_dl.core.tui.state import DownloadRequest
from tests.flow_helpers import _make_series, _make_summary, _write_valid_pdf

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path


class _FakeSession:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(output_dir=str(tmp_path), default_format="pdf", optimize_images=True)
        self.output_dir = tmp_path
        self.search_mock = AsyncMock(
            return_value=[
                SearchResult(
                    title="Series A",
                    url="https://comix.to/manga/series-a",
                    slug="series-a",
                    hash_id="a",
                )
            ]
        )
        self.load_series_mock = AsyncMock(return_value=_make_series())
        self.download_mock = AsyncMock(return_value=_make_summary(completed=1))

    async def search(self, query: str) -> list[SearchResult]:
        results = await self.search_mock(query)
        return cast("list[SearchResult]", results)

    async def load_series(self, identifier: str) -> SeriesInfo:
        series = await self.load_series_mock(identifier)
        return cast("SeriesInfo", series)

    async def download(
        self,
        *,
        series_title: str,
        chapters: list[ChapterInfo],
        fmt: str,
        optimize: bool,
        on_event: Callable[[DownloadChapterEvent], None] | None = None,
        is_shutdown: ShutdownCheck | None = None,
    ) -> DownloadSummary:
        summary = await self.download_mock(
            series_title=series_title,
            chapters=chapters,
            fmt=fmt,
            optimize=optimize,
            on_event=on_event,
            is_shutdown=is_shutdown,
        )
        return cast("DownloadSummary", summary)


class _TrackingSessionContext:
    def __init__(self, session: ApplicationSessionLike) -> None:
        self._session = session
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> ApplicationSessionLike:
        self.entered = True
        return self._session

    async def __aexit__(self, *_: object) -> None:
        self.exited = True


class _FailingSessionContext:
    def __init__(self) -> None:
        self.exited = False

    async def __aenter__(self) -> ApplicationSessionLike:
        raise RuntimeError("cannot enter")

    async def __aexit__(self, *_: object) -> None:
        self.exited = True


class _FakeSessionFactory:
    def __init__(self, *contexts: AbstractAsyncContextManager[ApplicationSessionLike]) -> None:
        self._contexts = list(contexts)
        self.kwargs_seen: list[dict[str, str | None]] = []

    def __call__(
        self,
        *,
        mirror_override: str | None = None,
    ) -> AbstractAsyncContextManager[ApplicationSessionLike]:
        self.kwargs_seen.append({"mirror_override": mirror_override})
        return self._contexts.pop(0)


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


def _session(tmp_path: Path) -> _FakeSession:
    return _FakeSession(tmp_path)


@pytest.mark.asyncio
async def test_controller_opens_and_closes_session(tmp_path: Path) -> None:
    session = _session(tmp_path)
    context = _TrackingSessionContext(session)
    factory = _FakeSessionFactory(context)
    controller = TuiController(session_factory=factory, mirror="https://comix.cc")

    await controller.open()

    assert controller.is_open is True
    assert context.entered is True
    assert factory.kwargs_seen == [{"mirror_override": "https://comix.cc"}]

    await controller.close()

    assert controller.is_open is False
    assert context.exited is True


@pytest.mark.asyncio
async def test_controller_failed_open_does_not_leave_context_to_close(tmp_path: Path) -> None:
    failing_context = _FailingSessionContext()
    good_context = _TrackingSessionContext(_session(tmp_path))
    factory = _FakeSessionFactory(failing_context, good_context)
    controller = TuiController(session_factory=factory)

    with pytest.raises(RuntimeError, match="cannot enter"):
        await controller.open()

    assert controller.is_open is False

    await controller.close()

    assert failing_context.exited is False

    await controller.open()

    assert controller.is_open is True
    assert good_context.entered is True


@pytest.mark.asyncio
async def test_controller_searches_with_open_session(tmp_path: Path) -> None:
    session = _session(tmp_path)
    controller = TuiController(session_factory=_FakeSessionFactory(_TrackingSessionContext(session)))

    await controller.open()
    results = await controller.search("series")

    assert results[0].title == "Series A"
    session.search_mock.assert_awaited_once_with("series")


@pytest.mark.asyncio
async def test_controller_loads_series_with_open_session(tmp_path: Path) -> None:
    session = _session(tmp_path)
    controller = TuiController(session_factory=_FakeSessionFactory(_TrackingSessionContext(session)))

    await controller.open()
    series = await controller.load_series("series-a")

    assert series.title == "Series A"
    session.load_series_mock.assert_awaited_once_with("series-a")


@pytest.mark.asyncio
async def test_controller_download_forwards_events_and_shutdown(tmp_path: Path) -> None:
    session = _session(tmp_path)
    events: list[DownloadChapterEvent] = []

    async def fake_download(**kwargs: Any) -> object:
        kwargs["on_event"](DownloadChapterEvent(chapter_id=1, chapter_title="Chapter 1", kind="started"))
        assert callable(kwargs["is_shutdown"])
        assert kwargs["is_shutdown"]() is False
        return _make_summary(completed=1)

    session.download_mock.side_effect = fake_download
    controller = TuiController(session_factory=_FakeSessionFactory(_TrackingSessionContext(session)))
    await controller.open()

    chapter = ChapterInfo(title="Chapter 1", chapter_id=1, number="1")
    request = DownloadRequest(series_title="Series A", chapters=(chapter,), fmt="cbz", optimize=False)
    summary = await controller.download(request, on_event=events.append)

    assert summary.completed == 1
    assert events[0].kind == "started"
    session.download_mock.assert_awaited_once()
    await_args = session.download_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["series_title"] == "Series A"
    assert await_args.kwargs["chapters"] == [chapter]
    assert await_args.kwargs["fmt"] == "cbz"
    assert await_args.kwargs["optimize"] is False


@pytest.mark.asyncio
async def test_controller_shutdown_flag_is_reset_on_open(tmp_path: Path) -> None:
    controller = TuiController(session_factory=_FakeSessionFactory(_TrackingSessionContext(_session(tmp_path))))

    controller.request_shutdown()
    assert controller.is_shutdown_requested() is True

    await controller.open()

    assert controller.is_shutdown_requested() is False


@pytest.mark.asyncio
async def test_controller_rejects_use_before_open(tmp_path: Path) -> None:
    controller = TuiController(session_factory=_FakeSessionFactory(_TrackingSessionContext(_session(tmp_path))))

    with pytest.raises(RuntimeError, match="TUI session is not open"):
        await controller.search("series")


@pytest.mark.asyncio
async def test_settings_and_output_dir_use_session_when_open(tmp_path: Path) -> None:
    session = _session(tmp_path / "session-output")
    fallback_settings = Settings(output_dir=str(tmp_path / "fallback-output"), default_format="cbz")
    settings_repository = _FakeSettingsRepository(fallback_settings)
    controller = TuiController(
        session_factory=_FakeSessionFactory(_TrackingSessionContext(session)),
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
