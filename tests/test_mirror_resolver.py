"""Tests for the mirror selection / persistence layer."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from comix_dl.core.errors import ConfigurationError
from comix_dl.core.mirror_resolver import (
    MirrorProbeRecord,
    MirrorState,
    MirrorStateRepository,
    record_probe_outcome,
    select_initial_mirror,
)

if TYPE_CHECKING:
    from pathlib import Path

    from comix_dl.sites.base import Engine, SiteAdapter


class _StubAdapter:
    """Minimum SiteAdapter shape needed by mirror_resolver tests."""

    def __init__(
        self,
        *,
        name: str = "stub",
        mirrors: list[str] | None = None,
        probe_result: bool = True,
    ) -> None:
        self.name = name
        # Use ``is None`` so the caller can explicitly request an empty
        # mirror list (e.g. to verify the empty-list ConfigurationError).
        self.mirrors: list[str] = (
            ["https://primary.example"] if mirrors is None else list(mirrors)
        )
        self.needs_browser = True
        self.probe_alive = AsyncMock(return_value=probe_result)

    # The rest of SiteAdapter is irrelevant for mirror tests; the resolver
    # never touches it. Static stubs make mypy and runtime_checkable happy.
    def matches_url(self, _url: str) -> bool:  # pragma: no cover - unused
        return False

    def parse_identifier(self, _: str) -> str | None:  # pragma: no cover
        return None

    async def on_engine_ready(self, _engine: Engine) -> None:  # pragma: no cover
        return None

    async def search(self, *_args: object, **_kwargs: object) -> list[object]:
        return []

    async def get_series(self, *_args: object, **_kwargs: object) -> object:
        raise NotImplementedError

    async def get_chapter_images(self, *_args: object, **_kwargs: object) -> object:
        return None

    def deduplicate(self, chapters: list[object]) -> tuple[list[object], list[object]]:
        return chapters, []


def _typed(adapter: _StubAdapter) -> SiteAdapter:
    """mypy-friendly cast that keeps the stub in scope at runtime."""
    return adapter  # type: ignore[return-value]


class TestMirrorStateRepository:
    def test_load_returns_defaults_for_missing_file(self, tmp_path: Path) -> None:
        repo = MirrorStateRepository(state_file=tmp_path / "missing.json")
        state = repo.load("comix.to")
        assert state.active is None
        assert state.history == []

    def test_load_returns_defaults_for_corrupt_json(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        target.write_text("{not json", encoding="utf-8")
        repo = MirrorStateRepository(state_file=target)
        assert repo.load("comix.to") == MirrorState()

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        state = MirrorState(
            active="https://comix.to",
            history=[
                MirrorProbeRecord(
                    mirror="https://comix.to", succeeded=True, checked_at="2026-05-01T00:00:00+00:00",
                ),
            ],
        )
        repo.save("comix.to", state)
        loaded = repo.load("comix.to")
        assert loaded.active == "https://comix.to"
        assert len(loaded.history) == 1
        assert loaded.history[0].mirror == "https://comix.to"
        assert loaded.history[0].succeeded is True

    def test_save_preserves_other_adapters_state(self, tmp_path: Path) -> None:
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        repo.save("alpha", MirrorState(active="https://a.example"))
        repo.save("beta", MirrorState(active="https://b.example"))
        assert repo.load("alpha").active == "https://a.example"
        assert repo.load("beta").active == "https://b.example"

    def test_load_caps_history_to_recent_entries(self, tmp_path: Path) -> None:
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        records = [
            MirrorProbeRecord(mirror="https://x", succeeded=True, checked_at=f"2026-05-{i:02d}T00:00:00+00:00")
            for i in range(1, 31)
        ]
        repo.save("comix.to", MirrorState(active="https://x", history=records))
        loaded = repo.load("comix.to")
        # Implementation caps history at 20 entries (oldest discarded).
        assert len(loaded.history) == 20


class TestSelectInitialMirror:
    def test_override_wins(self, tmp_path: Path) -> None:
        adapter = _StubAdapter(mirrors=["https://primary.example", "https://backup.example"])
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        repo.save(adapter.name, MirrorState(active="https://primary.example"))
        chosen = select_initial_mirror(
            _typed(adapter), override="https://override.example", repository=repo,
        )
        assert chosen == "https://override.example"

    def test_uses_cached_active_when_listed_in_mirrors(self, tmp_path: Path) -> None:
        adapter = _StubAdapter(mirrors=["https://primary.example", "https://backup.example"])
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        repo.save(adapter.name, MirrorState(active="https://backup.example"))
        chosen = select_initial_mirror(_typed(adapter), repository=repo)
        assert chosen == "https://backup.example"

    def test_falls_back_to_first_mirror_when_cache_is_stale(self, tmp_path: Path) -> None:
        adapter = _StubAdapter(mirrors=["https://primary.example"])
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        # Cached entry is no longer in adapter.mirrors — adapter changed config.
        repo.save(adapter.name, MirrorState(active="https://retired.example"))
        chosen = select_initial_mirror(_typed(adapter), repository=repo)
        assert chosen == "https://primary.example"

    def test_raises_when_adapter_has_no_mirrors(self, tmp_path: Path) -> None:
        adapter = _StubAdapter(mirrors=[])
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        with pytest.raises(ConfigurationError, match="no mirrors configured"):
            select_initial_mirror(_typed(adapter), repository=repo)


class TestRecordProbeOutcome:
    @pytest.mark.asyncio
    async def test_success_persists_active_mirror_and_history(self, tmp_path: Path) -> None:
        adapter = _StubAdapter(probe_result=True)
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        engine = AsyncMock()

        ok = await record_probe_outcome(
            _typed(adapter), engine, "https://primary.example", repository=repo,
        )

        assert ok is True
        adapter.probe_alive.assert_awaited_once_with(engine)
        state = repo.load(adapter.name)
        assert state.active == "https://primary.example"
        assert len(state.history) == 1
        assert state.history[0].succeeded is True

    @pytest.mark.asyncio
    async def test_failure_records_history_but_does_not_clobber_active(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        adapter = _StubAdapter(probe_result=False)
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        repo.save(adapter.name, MirrorState(active="https://known-good.example"))
        engine = AsyncMock()

        with caplog.at_level("WARNING"):
            ok = await record_probe_outcome(
                _typed(adapter), engine, "https://primary.example", repository=repo,
            )

        assert ok is False
        state = repo.load(adapter.name)
        # Failed probes do not overwrite the previously-known-good mirror.
        assert state.active == "https://known-good.example"
        assert len(state.history) == 1
        assert state.history[0].succeeded is False
        assert "did not respond" in caplog.text

    @pytest.mark.asyncio
    async def test_skipped_avoids_calling_probe_alive_and_persists_choice(
        self, tmp_path: Path,
    ) -> None:
        adapter = _StubAdapter(probe_result=True)
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        engine = AsyncMock()

        ok = await record_probe_outcome(
            _typed(adapter), engine, "https://override.example",
            repository=repo, skipped=True,
        )

        assert ok is True
        adapter.probe_alive.assert_not_awaited()
        assert repo.load(adapter.name).active == "https://override.example"

    @pytest.mark.asyncio
    async def test_probe_exception_is_recorded_as_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        adapter = _StubAdapter()
        adapter.probe_alive = AsyncMock(side_effect=RuntimeError("boom"))
        repo = MirrorStateRepository(state_file=tmp_path / "state.json")
        engine = AsyncMock()

        with caplog.at_level("WARNING"):
            ok = await record_probe_outcome(
                _typed(adapter), engine, "https://primary.example", repository=repo,
            )

        assert ok is False
        assert "raised an unexpected error" in caplog.text
