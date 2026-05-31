"""Tests for browser session timeout and page-pool behavior."""

from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from comix_dl.core.config import AppConfig, BrowserConfig, DownloadConfig
from comix_dl.core.engines.browser_session import BrowserSessionManager
from comix_dl.core.engines.cdp_browser import CdpBrowser
from comix_dl.core.errors import ConfigurationError
from tests.cdp_browser_helpers import _hang, _make_config


class TestBrowserTimeouts:
    def test_default_pool_size_uses_configured_image_concurrency(self):
        config = _make_config(download=DownloadConfig(max_concurrent_images=6))

        browser = BrowserSessionManager(config=config)

        assert browser._max_pages == 6

    def test_rejects_zero_page_pool_size(self):
        config = _make_config(download=DownloadConfig(max_concurrent_images=0))

        with pytest.raises(ConfigurationError, match=r"Browser page pool size must be at least 1\."):
            BrowserSessionManager(config=config)

    async def test_connect_over_cdp_uses_connect_timeout(self, monkeypatch: pytest.MonkeyPatch):
        config = _make_config(download=DownloadConfig(connect_timeout_ms=1234))

        browser = BrowserSessionManager(config=config)
        browser._cdp_port = 9444

        captured: dict[str, float] = {}

        async def fake_wait_for(awaitable: object, timeout: float) -> object:
            captured["timeout"] = timeout
            return await awaitable

        async def connect(endpoint: str) -> object:
            assert endpoint == "http://127.0.0.1:9444"
            return {"ok": True}

        monkeypatch.setattr("comix_dl.core.engines.browser_session.asyncio.wait_for", fake_wait_for)
        browser._playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

        result = await browser._connect_over_cdp_with_timeout()

        assert result == {"ok": True}
        assert captured["timeout"] == pytest.approx(1.234)

    async def test_fetch_page_timeout_uses_browser_timeout(self):
        config = _make_config(browser=BrowserConfig(timeout_ms=20))

        browser = CdpBrowser(config=config)
        browser._started = True
        page = MagicMock()
        page.goto = AsyncMock(side_effect=_hang)
        browser._page = page
        browser.ensure_cf_clearance = AsyncMock()
        browser._is_cf_challenge = AsyncMock(return_value=False)

        with pytest.raises(
            RuntimeError,
            match=r"Navigating browser page to https://example\.com timed out after 20ms\.",
        ):
            await browser.fetch_page("https://example.com")

    async def test_get_json_timeout_replaces_dead_page(self):
        config = _make_config(download=DownloadConfig(read_timeout_ms=20))

        browser = CdpBrowser(config=config)
        browser._started = True
        browser.ensure_cf_clearance = AsyncMock()
        browser.release_page = MagicMock()
        browser._replace_dead_page = AsyncMock()

        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=_hang)
        browser.acquire_page = AsyncMock(return_value=page)

        with pytest.raises(
            RuntimeError,
            match=r"Fetching JSON from https://api\.example\.com/data timed out after 20ms\.",
        ):
            await browser.get_json("https://api.example.com/data")

        browser._replace_dead_page.assert_awaited_once_with(page)
        browser.release_page.assert_not_called()

    def test_wait_for_cdp_ready_uses_configured_timeout(self, monkeypatch: pytest.MonkeyPatch):
        config = _make_config(download=DownloadConfig(connect_timeout_ms=600))

        browser = BrowserSessionManager(config=config)
        browser._cdp_port = 9222

        class _Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.now += seconds

        clock = _Clock()

        def fail_connect(*_args: object, **_kwargs: object) -> None:
            raise ConnectionRefusedError()

        monkeypatch.setattr("comix_dl.core.engines.browser_session.time.monotonic", clock.monotonic)
        monkeypatch.setattr("comix_dl.core.engines.browser_session.time.sleep", clock.sleep)
        monkeypatch.setattr("comix_dl.core.engines.browser_session.socket.create_connection", fail_connect)

        with pytest.raises(
            RuntimeError,
            match=r"Chrome CDP port 9222 did not become ready within 600ms\.",
        ):
            browser._wait_for_cdp_ready()

    async def test_acquire_page_waits_for_released_pool_page(self):
        browser = BrowserSessionManager(config=AppConfig())
        page = MagicMock()
        page.is_closed.return_value = False
        browser._all_pages = [page]
        browser._ensure_page = AsyncMock(side_effect=AssertionError("must not fall back to main page"))

        acquire_task = asyncio.create_task(browser.acquire_page())
        await asyncio.sleep(0)

        assert not acquire_task.done()

        browser.release_page(page)

        assert await acquire_task is page
        browser._ensure_page.assert_not_called()

    async def test_acquire_page_raises_when_pool_is_empty(self):
        browser = BrowserSessionManager(config=AppConfig())

        with pytest.raises(
            RuntimeError,
            match=r"Browser page pool is unavailable; pooled download requests cannot proceed\.",
        ):
            await browser.acquire_page()

    async def test_acquire_page_creates_pooled_page_lazily(self):
        browser = BrowserSessionManager(config=AppConfig(), base_url="https://example.test")
        page = MagicMock()
        page.is_closed.return_value = False
        browser._context = MagicMock()
        browser._new_page_with_timeout = AsyncMock(return_value=page)
        browser._goto_with_timeout = AsyncMock()

        result = await browser.acquire_page()

        assert result is page
        assert browser._all_pages == [page]
        browser._new_page_with_timeout.assert_awaited_once_with(action="Creating a pooled browser page")
        browser._goto_with_timeout.assert_awaited_once_with(
            page,
            "https://example.test",
            action="Initializing pooled browser page",
        )
        assert browser._page_pool.empty()

    async def test_release_page_skips_closed_page_and_replaces_it(self):
        browser = BrowserSessionManager(config=AppConfig())
        page = MagicMock()
        page.is_closed.return_value = True
        browser._all_pages = [page]
        browser._replace_dead_page = AsyncMock()

        browser.release_page(page)
        await asyncio.sleep(0)

        assert browser._page_pool.empty()
        browser._replace_dead_page.assert_awaited_once_with(page)

    async def test_release_page_does_not_replace_pages_while_session_is_closing(self):
        browser = BrowserSessionManager(config=AppConfig())
        page = MagicMock()
        page.is_closed.return_value = True
        browser._all_pages = [page]
        browser._replace_dead_page = AsyncMock()
        browser._closing = True

        browser.release_page(page)
        await asyncio.sleep(0)

        assert browser._page_pool.empty()
        browser._replace_dead_page.assert_not_awaited()

    async def test_acquire_page_discards_closed_page_from_queue(self):
        browser = BrowserSessionManager(config=AppConfig())
        dead_page = MagicMock()
        dead_page.is_closed.return_value = True
        healthy_page = MagicMock()
        healthy_page.is_closed.return_value = False
        browser._all_pages = [dead_page, healthy_page]
        browser._replace_dead_page = AsyncMock()
        browser._page_pool.put_nowait(dead_page)
        browser._page_pool.put_nowait(healthy_page)

        result = await browser.acquire_page()

        assert result is healthy_page
        browser._replace_dead_page.assert_awaited_once_with(dead_page)

    async def test_start_reuses_one_primary_page_and_closes_stray_tabs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        browser = BrowserSessionManager(config=AppConfig())
        browser._acquire_instance_lock = MagicMock()
        browser._release_instance_lock = MagicMock()
        browser._launch_chrome = MagicMock()

        primary_page = MagicMock()
        primary_page.is_closed.return_value = False
        primary_page.close = AsyncMock()
        extra_page = MagicMock()
        extra_page.is_closed.return_value = False
        extra_page.close = AsyncMock()

        context = SimpleNamespace(pages=[primary_page, extra_page])
        browser_object = SimpleNamespace(contexts=[context])
        browser._connect_over_cdp_with_timeout = AsyncMock(return_value=browser_object)

        playwright = SimpleNamespace(chromium=SimpleNamespace(), stop=AsyncMock())
        monkeypatch.setattr(
            "playwright.async_api.async_playwright",
            MagicMock(return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))),
        )

        await browser.start()

        assert browser._page is primary_page
        extra_page.close.assert_awaited_once()

        await browser.close()
        primary_page.close.assert_awaited_once()

    async def test_replace_dead_page_enqueues_replacement_page(self):
        browser = BrowserSessionManager(config=AppConfig(), base_url="https://example.test")
        dead_page = MagicMock()
        dead_page.is_closed.return_value = True
        new_page = MagicMock()
        new_page.is_closed.return_value = False
        browser._all_pages = [dead_page]
        browser._context = MagicMock()
        browser._started = True
        browser._context.new_page = AsyncMock(return_value=new_page)
        browser._goto_with_timeout = AsyncMock()

        await browser._replace_dead_page(dead_page)

        assert browser._all_pages == [new_page]
        assert await browser.acquire_page() is new_page
        browser._goto_with_timeout.assert_awaited_once()

    async def test_replace_dead_page_skips_replacement_while_session_is_closing(self):
        browser = BrowserSessionManager(config=AppConfig())
        dead_page = MagicMock()
        dead_page.is_closed.return_value = True
        dead_page.close = AsyncMock()
        browser._all_pages = [dead_page]
        browser._context = MagicMock()
        browser._started = True
        browser._closing = True
        browser._create_pooled_page = AsyncMock()

        await browser._replace_dead_page(dead_page)

        assert browser._all_pages == []
        browser._create_pooled_page.assert_not_awaited()

    def test_atexit_cleanup_only_targets_current_process_chrome(self):
        # Each manager owns its own Chrome subprocess. Verify the
        # instance-level ``_atexit_cleanup`` terminates the right
        # process and clears the reference, without touching any
        # global state.
        manager = BrowserSessionManager(config=AppConfig())
        process = MagicMock()
        manager._chrome_process = process

        manager._atexit_cleanup()

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)
        assert manager._chrome_process is None

    def test_cleanup_stale_profile_chrome_terminates_matching_process(self, tmp_path, monkeypatch):
        from comix_dl.core.engines import chrome_process

        pid_file = tmp_path / "chrome.pid"
        pid_file.write_text("4242\n", encoding="utf-8")
        user_data_dir = tmp_path / "chrome-profile"
        alive = True

        def fake_kill(pid: int, sig: int) -> None:
            nonlocal alive
            assert pid == 4242
            if sig == 0:
                if alive:
                    return
                raise ProcessLookupError
            if sig == signal.SIGTERM:
                alive = False
                return
            raise AssertionError(f"unexpected signal {sig}")

        # Patch the chrome_process module — _cleanup_stale_profile_chrome
        # and the helpers it calls all live there now.
        monkeypatch.setattr(chrome_process.os, "kill", fake_kill)
        monkeypatch.setattr(chrome_process.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(chrome_process, "_pid_matches_profile_chrome", lambda pid, path: True)

        chrome_process._cleanup_stale_profile_chrome(pid_file, user_data_dir)

        assert not pid_file.exists()

    def test_single_instance_lock_rejects_second_browser(self, tmp_path):
        config = _make_config(browser=BrowserConfig(cookie_dir=tmp_path))

        first = BrowserSessionManager(config=config)
        second = BrowserSessionManager(config=config)

        first._acquire_instance_lock()
        try:
            with pytest.raises(
                RuntimeError,
                match=r"Another comix-dl browser session is already running",
            ):
                second._acquire_instance_lock()
        finally:
            first._release_instance_lock()

    def test_releasing_instance_lock_allows_next_browser(self, tmp_path):
        config = _make_config(browser=BrowserConfig(cookie_dir=tmp_path))

        first = BrowserSessionManager(config=config)
        second = BrowserSessionManager(config=config)

        first._acquire_instance_lock()
        assert first._lock_file.exists()

        first._release_instance_lock()
        second._acquire_instance_lock()

        try:
            assert second._instance_lock_handle is not None
        finally:
            second._release_instance_lock()
