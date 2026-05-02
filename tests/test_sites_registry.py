"""Tests for the site adapter registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from comix_dl import sites
from comix_dl.core.errors import ConfigurationError
from comix_dl.sites.base import SiteAdapter

if TYPE_CHECKING:
    from collections.abc import Generator

    from comix_dl.core.models import (
        ChapterImages,
        ChapterInfo,
        DedupDecision,
        SearchResult,
        SeriesInfo,
    )
    from comix_dl.sites.base import Engine


@dataclass
class _StubAdapter:
    """Minimal SiteAdapter implementation used as a registry test fixture."""

    name: str
    mirrors: list[str] = field(default_factory=list)
    needs_browser: bool = True
    matched_urls: tuple[str, ...] = ()

    def matches_url(self, url: str) -> bool:
        return url in self.matched_urls

    def parse_identifier(self, url_or_slug: str) -> str | None:
        return url_or_slug or None

    async def on_engine_ready(self, engine: Engine) -> None:
        return None

    async def probe_alive(self, engine: Engine) -> bool:
        return True

    async def search(
        self, engine: Engine, query: str, *, limit: int = 20,
    ) -> list[SearchResult]:
        return []

    async def get_series(self, engine: Engine, identifier: str) -> SeriesInfo:
        raise NotImplementedError

    async def get_chapter_images(self, engine: Engine, chapter_id: int) -> ChapterImages:
        raise NotImplementedError

    def deduplicate(
        self, chapters: list[ChapterInfo],
    ) -> tuple[list[ChapterInfo], list[DedupDecision]]:
        return chapters, []


@pytest.fixture(autouse=True)
def _isolate_registry() -> Generator[None, None, None]:
    """Reset the registry between tests to avoid cross-test leakage."""
    sites.clear()
    yield
    sites.clear()


class TestRegistryBasics:
    def test_stub_adapter_satisfies_protocol(self) -> None:
        adapter = _StubAdapter(name="stub")
        assert isinstance(adapter, SiteAdapter)

    def test_register_and_get_by_name(self) -> None:
        adapter = _StubAdapter(name="stub")
        sites.register(adapter)
        assert sites.get_by_name("stub") is adapter

    def test_get_by_name_unknown_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="Unknown site adapter"):
            sites.get_by_name("missing")

    def test_unregister_is_idempotent(self) -> None:
        adapter = _StubAdapter(name="stub")
        sites.register(adapter)
        sites.unregister("stub")
        sites.unregister("stub")
        with pytest.raises(ConfigurationError):
            sites.get_by_name("stub")

    def test_register_overrides_same_name(self) -> None:
        first = _StubAdapter(name="dup")
        second = _StubAdapter(name="dup")
        sites.register(first)
        sites.register(second)
        assert sites.get_by_name("dup") is second
        assert sites.all_adapters() == (second,)


class TestGetForUrl:
    def test_returns_adapter_that_matches(self) -> None:
        a = _StubAdapter(name="a", matched_urls=("https://a.example/x",))
        b = _StubAdapter(name="b", matched_urls=("https://b.example/y",))
        sites.register(a)
        sites.register(b)
        assert sites.get_for_url("https://b.example/y") is b

    def test_returns_none_when_no_adapter_claims(self) -> None:
        sites.register(_StubAdapter(name="a"))
        assert sites.get_for_url("https://nowhere.example/") is None


class TestGetActive:
    def test_raises_when_registry_empty(self) -> None:
        with pytest.raises(ConfigurationError, match="No site adapter is registered"):
            sites.get_active()

    def test_returns_only_adapter(self) -> None:
        adapter = _StubAdapter(name="solo")
        sites.register(adapter)
        assert sites.get_active() is adapter

    def test_raises_when_multiple_adapters_present(self) -> None:
        sites.register(_StubAdapter(name="a"))
        sites.register(_StubAdapter(name="b"))
        with pytest.raises(ConfigurationError, match="Multiple site adapters registered"):
            sites.get_active()
