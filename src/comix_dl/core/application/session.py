"""Application runtime/session helpers used by the CLI adapter."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from comix_dl import sites
from comix_dl.core.application.download_usecase import (
    DownloadEventHandler,
    DownloadSummary,
    ShutdownCheck,
    download_chapters,
)
from comix_dl.core.application.query_usecase import (
    load_series,
    resolve_series_from_input,
    search_series,
)
from comix_dl.core.engines.cdp_browser import CdpBrowser
from comix_dl.core.history import HistoryRepository
from comix_dl.core.mirror_resolver import (
    MirrorStateRepository,
    record_probe_outcome,
    select_initial_mirror,
)
from comix_dl.core.notify import send_notification
from comix_dl.core.settings import Settings, SettingsRepository, build_runtime_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from comix_dl.core.application.query_usecase import SeriesLookupResult
    from comix_dl.core.config import AppConfig
    from comix_dl.core.models import ChapterInfo, SearchResult, SeriesInfo
    from comix_dl.sites.base import SiteAdapter


def _adapter_for_session(adapter: SiteAdapter) -> SiteAdapter:
    """Return a per-session adapter when the registered adapter supports it."""
    factory = getattr(adapter, "new_session", None)
    if callable(factory):
        return cast("Callable[[], SiteAdapter]", factory)()
    return adapter


def _configure_engine_for_adapter(adapter: SiteAdapter, engine: CdpBrowser) -> None:
    """Let adapters install pre-clearance browser hooks when supported."""
    configure = getattr(adapter, "configure_engine", None)
    if callable(configure):
        configure(engine)


@dataclass(frozen=True)
class RuntimeContext:
    """Resolved runtime state used by CLI presentation flows."""

    settings: Settings
    config: AppConfig
    output_dir: Path


@dataclass(frozen=True)
class ApplicationSession:
    """A browser-backed application session exposed to the CLI layer."""

    settings: Settings
    config: AppConfig
    output_dir: Path
    browser: CdpBrowser
    adapter: SiteAdapter

    async def search(self, query: str, *, limit: int = 20) -> list[SearchResult]:
        """Search for series results via the active site adapter."""
        return await search_series(self.adapter, self.browser, query, limit=limit)

    async def resolve_series(self, url_or_slug: str) -> SeriesLookupResult:
        """Resolve a series from a user-facing URL or slug."""
        return await resolve_series_from_input(self.adapter, self.browser, url_or_slug)

    async def load_series(self, identifier: str) -> SeriesInfo:
        """Load one fully-hydrated series by canonical identifier."""
        return await load_series(self.adapter, self.browser, identifier)

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
        """Run the shared application download use case."""
        return await download_chapters(
            self.browser,
            self.adapter,
            series_title=series_title,
            chapters=chapters,
            output_dir=self.output_dir,
            fmt=fmt,
            config=self.config,
            optimize=optimize,
            on_event=on_event,
            is_shutdown=is_shutdown,
            history_repository=HistoryRepository(),
            notifier=send_notification,
        )


def load_runtime(
    *,
    settings: Settings | None = None,
    config: AppConfig | None = None,
    output: str | Path | None = None,
) -> RuntimeContext:
    """Resolve normalized runtime settings/config/output for one CLI invocation."""
    resolved_settings = settings or SettingsRepository().load()
    runtime_config = config or build_runtime_config(resolved_settings)
    output_dir = Path(output) if output is not None else runtime_config.download.default_output_dir
    return RuntimeContext(
        settings=resolved_settings,
        config=runtime_config,
        output_dir=output_dir,
    )


@asynccontextmanager
async def open_application_session(
    *,
    settings: Settings | None = None,
    config: AppConfig | None = None,
    output: str | Path | None = None,
    mirror_override: str | None = None,
    mirror_repository: MirrorStateRepository | None = None,
) -> AsyncIterator[ApplicationSession]:
    """Open one browser-backed application session for CLI flows.

    Resolves the active mirror via :mod:`comix_dl.core.mirror_resolver`:
    the explicit override wins, otherwise the most recently
    successful mirror from disk, otherwise the adapter's first
    declared mirror. Probe outcomes are persisted so subsequent runs
    converge quickly.
    """
    runtime = load_runtime(settings=settings, config=config, output=output)
    adapter = _adapter_for_session(sites.get_active())
    repository = mirror_repository or MirrorStateRepository()
    base_url = select_initial_mirror(
        adapter, override=mirror_override, repository=repository,
    )
    async with CdpBrowser(config=runtime.config, base_url=base_url) as browser:
        _configure_engine_for_adapter(adapter, browser)

        async def _on_ready_with_probe(engine: CdpBrowser) -> None:
            await adapter.on_engine_ready(engine)
            await record_probe_outcome(
                adapter,
                engine,
                base_url,
                repository=repository,
                skipped=mirror_override is not None,
            )

        browser.register_on_engine_ready(_on_ready_with_probe)
        yield ApplicationSession(
            settings=runtime.settings,
            config=runtime.config,
            output_dir=runtime.output_dir,
            browser=browser,
            adapter=adapter,
        )
