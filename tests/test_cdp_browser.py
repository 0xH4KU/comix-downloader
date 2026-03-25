"""Tests for comix_dl.cdp_browser utilities and timeout wiring."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import comix_dl.browser_session as browser_session_module
from comix_dl.browser_session import BrowserSessionManager
from comix_dl.cdp_browser import CdpBrowser, _atexit_kill_chrome, _find_free_port, _is_port_in_use
from comix_dl.config import AppConfig


def _can_bind_localhost() -> bool:
    """Return whether this environment allows binding localhost TCP sockets."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", 0))
        except OSError:
            return False
    return True


LOCALHOST_SKIP = pytest.mark.skipif(
    not _can_bind_localhost(),
    reason="Environment blocks binding localhost TCP sockets",
)


async def _hang(*_args: object, **_kwargs: object) -> object:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


@LOCALHOST_SKIP
class TestFindFreePort:
    def test_returns_valid_port(self):
        port = _find_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_returned_port_is_available(self):
        port = _find_free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))

    def test_returns_different_ports(self):
        ports = {_find_free_port() for _ in range(5)}
        assert len(ports) >= 2


@LOCALHOST_SKIP
class TestIsPortInUse:
    def test_unused_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        assert _is_port_in_use(port) is False

    def test_used_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            assert _is_port_in_use(port) is True


class TestBrowserTimeouts:
    def test_default_pool_size_uses_configured_image_concurrency(self):
        config = AppConfig()
        config.download.max_concurrent_images = 6

        browser = BrowserSessionManager(config=config)

        assert browser._max_pages == 6

    def test_rejects_zero_page_pool_size(self):
        config = AppConfig()
        config.download.max_concurrent_images = 0

        with pytest.raises(ValueError, match=r"Browser page pool size must be at least 1\."):
            BrowserSessionManager(config=config)

    async def test_connect_over_cdp_uses_connect_timeout(self, monkeypatch: pytest.MonkeyPatch):
        config = AppConfig()
        config.download.connect_timeout_ms = 1234

        browser = BrowserSessionManager(config=config)
        browser._cdp_port = 9444

        captured: dict[str, float] = {}

        async def fake_wait_for(awaitable: object, timeout: float) -> object:
            captured["timeout"] = timeout
            return await awaitable

        async def connect(endpoint: str) -> object:
            assert endpoint == "http://127.0.0.1:9444"
            return {"ok": True}

        monkeypatch.setattr("comix_dl.browser_session.asyncio.wait_for", fake_wait_for)
        browser._playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

        result = await browser._connect_over_cdp_with_timeout()

        assert result == {"ok": True}
        assert captured["timeout"] == pytest.approx(1.234)

    async def test_fetch_page_timeout_uses_browser_timeout(self):
        config = AppConfig()
        config.browser.timeout_ms = 20

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
        config = AppConfig()
        config.download.read_timeout_ms = 20

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
        config = AppConfig()
        config.download.connect_timeout_ms = 600

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

        monkeypatch.setattr("comix_dl.browser_session.time.monotonic", clock.monotonic)
        monkeypatch.setattr("comix_dl.browser_session.time.sleep", clock.sleep)
        monkeypatch.setattr("comix_dl.browser_session.socket.create_connection", fail_connect)

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
            match=r"Browser page pool is empty; cannot perform pooled requests\.",
        ):
            await browser.acquire_page()

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

    async def test_replace_dead_page_enqueues_replacement_page(self):
        browser = BrowserSessionManager(config=AppConfig())
        dead_page = MagicMock()
        dead_page.is_closed.return_value = True
        new_page = MagicMock()
        new_page.is_closed.return_value = False
        browser._all_pages = [dead_page]
        browser._context = MagicMock()
        browser._context.new_page = AsyncMock(return_value=new_page)
        browser._goto_with_timeout = AsyncMock()

        await browser._replace_dead_page(dead_page)

        assert browser._all_pages == [new_page]
        assert await browser.acquire_page() is new_page
        browser._goto_with_timeout.assert_awaited_once()

    def test_atexit_cleanup_only_targets_current_process_chrome(self):
        process = MagicMock()
        browser_session_module._active_chrome = process

        _atexit_kill_chrome()

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)
        assert browser_session_module._active_chrome is None

    def test_single_instance_lock_rejects_second_browser(self, tmp_path):
        config = AppConfig()
        config.browser.cookie_dir = tmp_path

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
        config = AppConfig()
        config.browser.cookie_dir = tmp_path

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


class TestCloudflareRecovery:
    async def test_get_json_retries_once_after_http_403(self):
        config = AppConfig()
        browser = CdpBrowser(config=config)
        browser._started = True
        browser._cf_cleared = True
        browser.release_page = MagicMock()
        browser._replace_dead_page = AsyncMock()

        async def ensure() -> None:
            browser._cf_cleared = True

        browser.ensure_cf_clearance = AsyncMock(side_effect=ensure)

        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=[RuntimeError("HTTP 403 Forbidden"), {"ok": True}])
        browser.acquire_page = AsyncMock(return_value=page)
        browser._all_pages = [page]

        result = await browser.get_json("https://api.example.com/data")

        assert result == {"ok": True}
        assert browser.ensure_cf_clearance.await_count == 2
        assert browser.release_page.call_count == 2
        browser._replace_dead_page.assert_not_awaited()

    async def test_get_json_raises_clear_error_after_second_http_403(self):
        config = AppConfig()
        browser = CdpBrowser(config=config)
        browser._started = True
        browser._cf_cleared = True
        browser.release_page = MagicMock()
        browser._replace_dead_page = AsyncMock()

        async def ensure() -> None:
            browser._cf_cleared = True

        browser.ensure_cf_clearance = AsyncMock(side_effect=ensure)

        page = MagicMock()
        page.evaluate = AsyncMock(
            side_effect=[RuntimeError("HTTP 403 Forbidden"), RuntimeError("HTTP 403 Forbidden")],
        )
        browser.acquire_page = AsyncMock(return_value=page)
        browser._all_pages = [page]

        with pytest.raises(
            RuntimeError,
            match=r"Cloudflare clearance refresh did not recover access to https://api\.example\.com/data\.",
        ):
            await browser.get_json("https://api.example.com/data")

        assert browser.ensure_cf_clearance.await_count == 2
        assert browser.release_page.call_count == 2
        browser._replace_dead_page.assert_not_awaited()

    async def test_fetch_page_retries_after_cloudflare_challenge(self):
        config = AppConfig()
        browser = CdpBrowser(config=config)
        browser._started = True
        browser._cf_cleared = True

        async def ensure() -> None:
            browser._cf_cleared = True

        browser.ensure_cf_clearance = AsyncMock(side_effect=ensure)

        page = MagicMock()
        page.goto = AsyncMock(return_value=None)
        page.content = AsyncMock(return_value="<html>ok</html>")
        browser._page = page
        browser._is_cf_challenge = AsyncMock(side_effect=[True, False])

        result = await browser.fetch_page("https://example.com")

        assert result == "<html>ok</html>"
        assert browser.ensure_cf_clearance.await_count == 2
        assert page.goto.await_count == 2
