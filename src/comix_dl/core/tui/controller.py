"""Application controller for the full-screen TUI."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from comix_dl.core.application.cleanup_usecase import (
    apply_cleanup_plan,
    build_cleanup_plan,
    list_downloaded_series,
)
from comix_dl.core.application.session import open_application_session
from comix_dl.core.history import HistoryRepository
from comix_dl.core.settings import Settings, SettingsRepository

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from comix_dl.core.application.cleanup_usecase import CleanupPlan, CleanupResult, DownloadedSeries
    from comix_dl.core.application.download_usecase import DownloadEventHandler, DownloadSummary, ShutdownCheck
    from comix_dl.core.history import HistoryEntry
    from comix_dl.core.models import ChapterInfo, SearchResult, SeriesInfo
    from comix_dl.core.tui.state import DownloadRequest


class ApplicationSessionLike(Protocol):
    """Application-session surface used by the TUI controller."""

    settings: Settings
    output_dir: Path

    async def search(self, query: str) -> list[SearchResult]:
        """Search for series results."""

    async def load_series(self, identifier: str) -> SeriesInfo:
        """Load a fully hydrated series."""

    async def download(
        self,
        *,
        series_title: str,
        chapters: list[ChapterInfo],
        fmt: str,
        optimize: bool,
        on_event: DownloadEventHandler | None = None,
        is_shutdown: ShutdownCheck | None = None,
    ) -> DownloadSummary:
        """Download chapters."""


class SessionFactory(Protocol):
    """Factory for async application-session contexts."""

    def __call__(
        self,
        *,
        mirror_override: str | None = None,
    ) -> AbstractAsyncContextManager[ApplicationSessionLike]:
        """Create an async context manager yielding an application session."""


class SettingsStore(Protocol):
    """Settings persistence used by the TUI controller."""

    def load(self) -> Settings:
        """Load persisted settings."""

    def save(self, settings: Settings) -> None:
        """Persist settings."""


class HistoryStore(Protocol):
    """History persistence used by the TUI controller."""

    def list_entries(self) -> list[HistoryEntry]:
        """Return persisted download history entries."""


def _default_session_factory(
    *,
    mirror_override: str | None = None,
) -> AbstractAsyncContextManager[ApplicationSessionLike]:
    return cast(
        "AbstractAsyncContextManager[ApplicationSessionLike]",
        open_application_session(mirror_override=mirror_override),
    )


class TuiController:
    """Textual-independent coordinator for application use cases."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = _default_session_factory,
        settings_repository: SettingsStore | None = None,
        history_repository: HistoryStore | None = None,
        mirror: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings_repository = settings_repository or SettingsRepository()
        self._history_repository = history_repository or HistoryRepository()
        self._mirror = mirror
        self._context: AbstractAsyncContextManager[ApplicationSessionLike] | None = None
        self._session: ApplicationSessionLike | None = None
        self._shutdown_requested = False

    @property
    def is_open(self) -> bool:
        """Return whether an application session is currently open."""
        return self._session is not None

    @property
    def settings(self) -> Settings:
        """Return active session settings, or persisted settings before open."""
        if self._session is not None:
            return self._session.settings
        return self._settings_repository.load()

    @property
    def output_dir(self) -> Path:
        """Return active session output directory, or persisted settings output."""
        if self._session is not None:
            return self._session.output_dir
        return Path(self.settings.output_dir)

    async def open(self) -> None:
        """Open one async application session if needed."""
        if self._session is not None:
            return
        self._shutdown_requested = False
        context = self._session_factory(mirror_override=self._mirror)
        session = await context.__aenter__()
        self._context = context
        self._session = session

    async def close(self) -> None:
        """Close the current async application session if present."""
        context = self._context
        self._context = None
        self._session = None
        if context is not None:
            await context.__aexit__(None, None, None)

    def _require_session(self) -> ApplicationSessionLike:
        if self._session is None:
            raise RuntimeError("TUI session is not open")
        return self._session

    async def search(self, query: str) -> list[SearchResult]:
        """Search for series via the open application session."""
        return await self._require_session().search(query)

    async def load_series(self, identifier: str) -> SeriesInfo:
        """Load a series via the open application session."""
        return await self._require_session().load_series(identifier)

    async def download(
        self,
        request: DownloadRequest,
        *,
        on_event: DownloadEventHandler | None = None,
    ) -> DownloadSummary:
        """Download the requested chapters via the open application session."""
        self._shutdown_requested = False
        try:
            return await self._require_session().download(
                series_title=request.series_title,
                chapters=list(request.chapters),
                fmt=request.fmt,
                optimize=request.optimize,
                on_event=on_event,
                is_shutdown=self.is_shutdown_requested,
            )
        finally:
            self._shutdown_requested = False

    def request_shutdown(self) -> None:
        """Request cancellation for work that polls the shutdown callable."""
        self._shutdown_requested = True

    def is_shutdown_requested(self) -> bool:
        """Return whether shutdown has been requested."""
        return self._shutdown_requested

    def list_downloads(self) -> list[DownloadedSeries]:
        """List downloaded series in the active output directory."""
        return list_downloaded_series(self.output_dir)

    def history_entries(self) -> list[HistoryEntry]:
        """Return persisted download history entries."""
        return self._history_repository.list_entries()

    def load_settings(self) -> Settings:
        """Load persisted settings."""
        return self._settings_repository.load()

    def save_settings(self, settings: Settings) -> None:
        """Persist settings."""
        self._settings_repository.save(settings)

    def cleanup_plan(self, series_title: str | None = None) -> CleanupPlan:
        """Build a cleanup plan for the active output directory."""
        return build_cleanup_plan(self.output_dir, series_title=series_title)

    def apply_cleanup(self, plan: CleanupPlan) -> CleanupResult:
        """Apply a cleanup plan."""
        return apply_cleanup_plan(plan)
