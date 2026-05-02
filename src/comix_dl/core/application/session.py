"""Application runtime/session helpers used by the CLI adapter."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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
from comix_dl.core.settings import Settings, SettingsRepository, build_runtime_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from comix_dl.core.application.query_usecase import SeriesLookupResult
    from comix_dl.core.config import AppConfig
    from comix_dl.core.models import ChapterInfo, SearchResult, SeriesInfo
    from comix_dl.sites.base import SiteAdapter


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
) -> AsyncIterator[ApplicationSession]:
    """Open one browser-backed application session for CLI flows.

    Selects the active site adapter from the registry, takes its first
    mirror as the engine base URL (F-5 will probe the mirror list and
    pick the first reachable one), and registers the adapter's
    on_engine_ready hook so site-specific bootstrap (URL signing,
    cookies, etc.) runs once after CF clearance.
    """
    runtime = load_runtime(settings=settings, config=config, output=output)
    adapter = sites.get_active()
    base_url = adapter.mirrors[0]
    async with CdpBrowser(config=runtime.config, base_url=base_url) as browser:
        browser.register_on_engine_ready(adapter.on_engine_ready)
        yield ApplicationSession(
            settings=runtime.settings,
            config=runtime.config,
            output_dir=runtime.output_dir,
            browser=browser,
            adapter=adapter,
        )
