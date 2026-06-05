"""Optional live smoke tests for the comix.to remote contract.

Run with:

    COMIX_DL_LIVE=1 pytest tests/test_live_smoke.py -q

These tests intentionally touch the real browser-backed session and
remote site. They are skipped by default so normal CI stays deterministic.
"""

from __future__ import annotations

import os

import pytest

from comix_dl.core.application.session import open_application_session

pytestmark = pytest.mark.skipif(
    os.environ.get("COMIX_DL_LIVE") != "1",
    reason="set COMIX_DL_LIVE=1 to run browser-backed live smoke tests",
)


@pytest.mark.asyncio
async def test_live_remote_contract_search_series_chapter_and_image() -> None:
    query = os.environ.get("COMIX_DL_LIVE_QUERY", "one piece")

    async with open_application_session() as session:
        results = await session.search(query, limit=1)
        assert results, f"live search returned no results for {query!r}"

        series = await session.load_series(results[0].hash_id)
        assert series.title
        assert series.chapters

        chapter = series.chapters[0]
        chapter_images = await session.adapter.get_chapter_images(session.browser, chapter.chapter_id)
        assert chapter_images is not None
        assert chapter_images.pages

        first_page = chapter_images.pages[0]
        if first_page.scrambled:
            payload = await session.browser.get_scrambled_image_bytes(
                first_page.url,
                width=first_page.width,
                height=first_page.height,
                referer=series.url,
            )
        else:
            payload = await session.browser.get_bytes(first_page.url, referer=series.url)

        assert payload, "live sample image fetch returned an empty payload"
