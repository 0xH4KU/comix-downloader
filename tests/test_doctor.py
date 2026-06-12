"""Tests for basic and deep diagnostics."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from comix_dl.core.errors import RemoteApiError
from comix_dl.core.models import ChapterImages, ChapterInfo, ChapterPage, SearchResult
from tests.flow_helpers import _make_series, _SessionContext


@pytest.mark.asyncio
async def test_run_deep_doctor_reports_remote_chain_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from comix_dl.core.cli import doctor

    session = SimpleNamespace(
        search=AsyncMock(return_value=[
            SearchResult(
                title="Series A",
                url="https://comix.to/manga/series-a",
                slug="series-a",
                hash_id="series-a",
            )
        ]),
        load_series=AsyncMock(return_value=_make_series(
            chapters=[ChapterInfo(title="Chapter 1", chapter_id=101, number="1")],
        )),
        adapter=SimpleNamespace(
            get_chapter_images=AsyncMock(return_value=ChapterImages(
                title="Chapter 1",
                chapter_label="Chapter 1",
                image_urls=["https://img.example/001.webp"],
                pages=[ChapterPage(url="https://img.example/001.webp")],
            )),
        ),
        browser=SimpleNamespace(
            get_bytes=AsyncMock(return_value=b"abc"),
        ),
    )

    monkeypatch.setattr(doctor, "open_application_session", lambda **_kwargs: _SessionContext(session))
    monkeypatch.setattr(doctor.console, "status", lambda *_args, **_kwargs: nullcontext())

    with doctor.console.capture() as capture:
        result = await doctor.run_deep_doctor(mirror="https://comix.to", query="series")

    assert result == 0
    output = capture.get()
    assert "Chrome/CDP session" in output
    assert "Search API" in output
    assert "Series metadata" in output
    assert "Chapter images" in output
    assert "Sample image fetch" in output
    assert "3 bytes" in output
    session.search.assert_awaited_once_with("series", limit=1)
    session.load_series.assert_awaited_once_with("series-a")
    session.adapter.get_chapter_images.assert_awaited_once_with(session.browser, 101)
    session.browser.get_bytes.assert_awaited_once_with(
        "https://img.example/001.webp",
        referer="https://comix.to/manga/series-a",
    )


@pytest.mark.asyncio
async def test_run_deep_doctor_passes_reader_context_for_scrambled_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comix_dl.core.cli import doctor

    page = ChapterPage(
        url="https://img.example/002.webp",
        width=970,
        height=1458,
        scrambled=True,
        reader_url="https://comix.to/title/series-a/101-chapter-1",
        page_index=1,
    )
    session = SimpleNamespace(
        search=AsyncMock(return_value=[
            SearchResult(
                title="Series A",
                url="https://comix.to/manga/series-a",
                slug="series-a",
                hash_id="series-a",
            )
        ]),
        load_series=AsyncMock(return_value=_make_series(
            chapters=[ChapterInfo(title="Chapter 1", chapter_id=101, number="1")],
        )),
        adapter=SimpleNamespace(
            get_chapter_images=AsyncMock(return_value=ChapterImages(
                title="Chapter 1",
                chapter_label="Chapter 1",
                image_urls=[page.url],
                pages=[page],
            )),
        ),
        browser=SimpleNamespace(
            get_scrambled_image_bytes=AsyncMock(return_value=b"png"),
        ),
    )

    monkeypatch.setattr(doctor, "open_application_session", lambda **_kwargs: _SessionContext(session))
    monkeypatch.setattr(doctor.console, "status", lambda *_args, **_kwargs: nullcontext())

    result = await doctor.run_deep_doctor(query="series")

    assert result == 0
    session.browser.get_scrambled_image_bytes.assert_awaited_once_with(
        "https://img.example/002.webp",
        width=970,
        height=1458,
        referer="https://comix.to/manga/series-a",
        reader_url="https://comix.to/title/series-a/101-chapter-1",
        page_index=1,
    )


@pytest.mark.asyncio
async def test_run_deep_doctor_reports_failing_step(monkeypatch: pytest.MonkeyPatch) -> None:
    from comix_dl.core.cli import doctor

    session = SimpleNamespace(
        search=AsyncMock(side_effect=RemoteApiError("request signing failed")),
    )

    monkeypatch.setattr(doctor, "open_application_session", lambda **_kwargs: _SessionContext(session))
    monkeypatch.setattr(doctor.console, "status", lambda *_args, **_kwargs: nullcontext())

    with doctor.console.capture() as capture:
        result = await doctor.run_deep_doctor(query="series")

    assert result == 1
    output = capture.get()
    assert "Search API" in output
    assert "request signing failed" in output


@pytest.mark.asyncio
async def test_run_deep_doctor_reports_series_metadata_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from comix_dl.core.cli import doctor

    session = SimpleNamespace(
        search=AsyncMock(return_value=[
            SearchResult(
                title="Series A",
                url="https://comix.to/manga/series-a",
                slug="series-a",
                hash_id="series-a",
            )
        ]),
        load_series=AsyncMock(side_effect=RemoteApiError("Could not find manga with identifier 'series-a'")),
    )

    monkeypatch.setattr(doctor, "open_application_session", lambda **_kwargs: _SessionContext(session))
    monkeypatch.setattr(doctor.console, "status", lambda *_args, **_kwargs: nullcontext())

    with doctor.console.capture() as capture:
        result = await doctor.run_deep_doctor(query="series")

    assert result == 1
    output = capture.get()
    assert "Series metadata" in output
    assert "Could not find manga" in output


@pytest.mark.asyncio
async def test_run_deep_doctor_reports_sample_image_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from comix_dl.core.cli import doctor

    pages = [
        ChapterPage(url="https://img-a.example/001.webp"),
        ChapterPage(url="https://img-a.example/002.webp"),
        ChapterPage(url="https://img-b.example/003.webp"),
        ChapterPage(url="https://img-b.example/004.webp"),
    ]
    session = SimpleNamespace(
        search=AsyncMock(return_value=[
            SearchResult(
                title="Series A",
                url="https://comix.to/manga/series-a",
                slug="series-a",
                hash_id="series-a",
            )
        ]),
        load_series=AsyncMock(return_value=_make_series(
            chapters=[ChapterInfo(title="Chapter 1", chapter_id=101, number="1")],
        )),
        adapter=SimpleNamespace(
            get_chapter_images=AsyncMock(return_value=ChapterImages(
                title="Chapter 1",
                chapter_label="Chapter 1",
                image_urls=[page.url for page in pages],
                pages=pages,
            )),
        ),
        browser=SimpleNamespace(
            get_bytes=AsyncMock(side_effect=[
                b"page-1",
                RuntimeError("HTTP 503"),
                b"page-3",
                RuntimeError("HTTP 520"),
            ]),
        ),
    )

    monkeypatch.setattr(doctor, "open_application_session", lambda **_kwargs: _SessionContext(session))
    monkeypatch.setattr(doctor.console, "status", lambda *_args, **_kwargs: nullcontext())

    with doctor.console.capture() as capture:
        result = await doctor.run_deep_doctor(query="series")

    assert result == 1
    output = capture.get()
    assert "Sample image fetch" in output
    assert "2/4 sampled image(s) fetched" in output
    assert "img-a.example" in output
    assert "img-b.example" in output
    assert "002 HTTP 503" in output
    assert "004 HTTP 520" in output
    assert session.browser.get_bytes.await_count == 4
