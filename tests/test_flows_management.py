"""Tests for CLI flow info, list, and cleanup paths."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from comix_dl.core.application.cleanup_usecase import (
    CleanupCandidate,
    CleanupPlan,
    CleanupResult,
    DownloadedSeries,
)
from comix_dl.core.application.query_usecase import SeriesLookupResult
from comix_dl.core.application.session import RuntimeContext
from comix_dl.core.cli import flows
from comix_dl.core.config import AppConfig
from comix_dl.core.errors import RemoteApiError
from comix_dl.core.settings import Settings
from tests.flow_helpers import _make_series, _make_session, _SessionContext, _write_valid_pdf


@pytest.mark.asyncio
async def test_flow_info_handles_missing_and_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_session = _make_session(tmp_path)
    missing_session.resolve_series.return_value = SeriesLookupResult(slug="missing", series=None, suggestions=[])

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(missing_session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())

    assert await flows.flow_info("missing") == 1

    success_session = _make_session(tmp_path)
    success_session.resolve_series.return_value = SeriesLookupResult(
        slug="series-a",
        series=_make_series(),
        suggestions=[],
    )
    render_panel = MagicMock()
    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(success_session))
    monkeypatch.setattr(flows, "_render_series_info_panel", render_panel)

    assert await flows.flow_info("series-a") == 0
    render_panel.assert_called_once()


@pytest.mark.asyncio
async def test_flow_info_surfaces_remote_api_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path)
    session.resolve_series.side_effect = RemoteApiError("info failed")

    monkeypatch.setattr(flows, "open_application_session", lambda **_kwargs: _SessionContext(session))
    monkeypatch.setattr(flows.console, "status", lambda *_args, **_kwargs: nullcontext())

    with flows.console.capture() as capture:
        result = await flows.flow_info("series-a")

    assert result == 1
    assert "info failed" in capture.get()


def test_flow_list_handles_missing_output_dir(tmp_path: Path) -> None:
    missing_dir = tmp_path / "missing"

    with patch.object(
        flows,
        "load_runtime",
        return_value=RuntimeContext(
            settings=Settings(output_dir=str(missing_dir)),
            config=AppConfig(),
            output_dir=missing_dir,
        ),
    ):
        assert flows.flow_list() == 0


def test_flow_list_renders_downloaded_series_table(tmp_path: Path) -> None:
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()

    with (
        patch.object(
            flows,
            "load_runtime",
            return_value=RuntimeContext(
                settings=Settings(output_dir=str(output_dir)),
                config=AppConfig(),
                output_dir=output_dir,
            ),
        ),
        patch.object(
            flows,
            "list_downloaded_series",
            return_value=[
                DownloadedSeries(
                    name="Series A",
                    path=output_dir / "Series A",
                    completed_chapters=2,
                    total_size_bytes=2048,
                )
            ],
        ),
        flows.console.capture() as capture,
    ):
        result = flows.flow_list()

    assert result == 0
    output = capture.get()
    assert "Downloaded Manga" in output
    assert "Series A" in output
    assert "2.0 KB" in output


def test_flow_clean_auto_confirm_skips_prompt_and_removes_candidates(tmp_path: Path) -> None:
    chapter_dir = tmp_path / "Series A" / "Chapter 1"
    chapter_dir.mkdir(parents=True)
    (chapter_dir / ".complete").touch()
    _write_valid_pdf(chapter_dir.parent / "Chapter 1.pdf")
    (chapter_dir / "001.jpg").write_bytes(b"image")

    with (
        patch.object(
            flows,
            "load_runtime",
            return_value=RuntimeContext(
                settings=Settings(output_dir=str(tmp_path)),
                config=AppConfig(),
                output_dir=tmp_path,
            ),
        ),
        patch.object(flows.Prompt, "ask", side_effect=AssertionError("prompt should not be used")),
    ):
        result = flows.flow_clean(auto_confirm=True)

    assert result == 0
    assert not chapter_dir.exists()


def test_flow_clean_handles_cancel_and_failure_reporting(tmp_path: Path) -> None:
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    candidate = CleanupCandidate(
        path=output_dir / "Series A" / "Chapter 1",
        relative_path=Path("Series A/Chapter 1"),
        size_bytes=1024,
    )
    plan = CleanupPlan(output_dir=output_dir, candidates=[candidate], total_size_bytes=1024)

    with (
        patch.object(
            flows,
            "load_runtime",
            return_value=RuntimeContext(
                settings=Settings(output_dir=str(output_dir)),
                config=AppConfig(),
                output_dir=output_dir,
            ),
        ),
        patch.object(flows, "build_cleanup_plan", return_value=plan),
        patch.object(flows.Prompt, "ask", return_value="n"),
        patch.object(flows, "apply_cleanup_plan", side_effect=AssertionError("cleanup should not run")),
    ):
        assert flows.flow_clean() == 0

    with (
        patch.object(
            flows,
            "load_runtime",
            return_value=RuntimeContext(
                settings=Settings(output_dir=str(output_dir)),
                config=AppConfig(),
                output_dir=output_dir,
            ),
        ),
        patch.object(flows, "build_cleanup_plan", return_value=plan),
        patch.object(
            flows,
            "apply_cleanup_plan",
            return_value=CleanupResult(
                removed_count=0,
                failed=[(candidate.path, "permission denied")],
            ),
        ),
        flows.console.capture() as capture,
    ):
        result = flows.flow_clean(force=True)

    assert result == 0
    output = capture.get()
    assert "Failed to remove Chapter 1: permission denied" in output
    assert "freed 0.0 B" in output


def test_auto_cleanup_prompt_respects_decline_and_auto_confirm(tmp_path: Path) -> None:
    output_dir = tmp_path / "downloads"
    candidate = CleanupCandidate(
        path=output_dir / "Series A" / "Chapter 1",
        relative_path=Path("Series A/Chapter 1"),
        size_bytes=2048,
    )
    plan = CleanupPlan(output_dir=output_dir, candidates=[candidate], total_size_bytes=2048)

    with (
        patch.object(flows, "build_cleanup_plan", return_value=plan),
        patch.object(flows.Prompt, "ask", return_value="n"),
        patch.object(flows, "apply_cleanup_plan", side_effect=AssertionError("cleanup should not run")),
    ):
        flows._auto_cleanup_prompt(output_dir, "Series A", auto_confirm=False)

    with (
        patch.object(flows, "build_cleanup_plan", return_value=plan),
        patch.object(
            flows,
            "apply_cleanup_plan",
            return_value=CleanupResult(removed_count=1, failed=[]),
        ),
        flows.console.capture() as capture,
    ):
        flows._auto_cleanup_prompt(output_dir, "Series A", auto_confirm=True)

    assert "Cleaned 1 dir(s), freed 2.0 KB" in capture.get()
