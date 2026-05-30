"""Tests for comix_dl.core.engines.cdp_browser utilities and timeout wiring."""

from __future__ import annotations

import asyncio
import base64
import signal
import socket
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from comix_dl.core.config import AppConfig, BrowserConfig, DownloadConfig
from comix_dl.core.engines.browser_session import BrowserSessionManager
from comix_dl.core.engines.cdp_browser import CdpBrowser
from comix_dl.core.engines.chrome_process import _find_free_port, _is_port_in_use
from comix_dl.core.errors import (
    BrowserTimeoutError,
    CloudflareChallengeError,
    ConfigurationError,
    Http403Error,
)


def _make_config(
    *,
    browser: BrowserConfig | None = None,
    download: DownloadConfig | None = None,
) -> AppConfig:
    return AppConfig(
        browser=browser or BrowserConfig(),
        download=download or DownloadConfig(),
    )


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

    async def test_get_json_timeout_returns_healthy_page_to_pool(self):
        config = _make_config(download=DownloadConfig(read_timeout_ms=20))

        browser = CdpBrowser(config=config)
        browser._started = True
        browser.ensure_cf_clearance = AsyncMock()
        browser.release_page = MagicMock()
        browser._replace_dead_page = AsyncMock()

        page = MagicMock()
        page.is_closed.return_value = False
        page.evaluate = AsyncMock(side_effect=_hang)
        browser.acquire_page = AsyncMock(return_value=page)

        with pytest.raises(
            RuntimeError,
            match=r"Fetching JSON from https://api\.example\.com/data timed out after 20ms\.",
        ):
            await browser.get_json("https://api.example.com/data")

        browser.release_page.assert_called_once_with(page)
        browser._replace_dead_page.assert_not_awaited()

    async def test_get_json_timeout_replaces_closed_page(self):
        config = _make_config(download=DownloadConfig(read_timeout_ms=20))

        browser = CdpBrowser(config=config)
        browser._started = True
        browser.ensure_cf_clearance = AsyncMock()
        browser.release_page = MagicMock()
        browser._replace_dead_page = AsyncMock()

        page = MagicMock()
        page.is_closed.return_value = True
        page.evaluate = AsyncMock(side_effect=_hang)
        browser.acquire_page = AsyncMock(return_value=page)

        with pytest.raises(
            RuntimeError,
            match=r"Fetching JSON from https://api\.example\.com/data timed out after 20ms\.",
        ):
            await browser.get_json("https://api.example.com/data")

        browser._replace_dead_page.assert_awaited_once_with(page)
        browser.release_page.assert_not_called()

    async def test_get_json_request_error_returns_healthy_page_to_pool(self):
        browser = CdpBrowser(config=AppConfig())
        browser._started = True
        browser.ensure_cf_clearance = AsyncMock()
        browser.release_page = MagicMock()
        browser._replace_dead_page = AsyncMock()

        page = MagicMock()
        page.is_closed.return_value = False
        browser.acquire_page = AsyncMock(return_value=page)
        browser._evaluate_with_timeout = AsyncMock(side_effect=RuntimeError("HTTP 500"))

        with pytest.raises(RuntimeError, match=r"HTTP 500"):
            await browser.get_json("https://api.example.com/data")

        browser.release_page.assert_called_once_with(page)
        browser._replace_dead_page.assert_not_awaited()

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
            CloudflareChallengeError,
            match=(
                r"Cloudflare clearance refresh did not recover browser access to "
                r"https://api\.example\.com/data after HTTP 403\."
            ),
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


class TestBrowserHelpers:
    async def test_close_resets_cf_flag_even_if_parent_close_fails(self):
        browser = CdpBrowser(config=AppConfig())
        browser._cf_cleared = True

        with (
            patch.object(BrowserSessionManager, "close", AsyncMock(side_effect=RuntimeError("boom"))),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await browser.close()

        assert browser._cf_cleared is False

    async def test_context_manager_starts_and_closes_browser(self):
        browser = CdpBrowser(config=AppConfig())
        browser.start = AsyncMock()
        browser.close = AsyncMock()

        async with browser as current:
            assert current is browser

        browser.start.assert_awaited_once()
        browser.close.assert_awaited_once()

    def test_cf_access_error_and_release_helpers(self):
        browser = CdpBrowser(config=AppConfig())
        page = MagicMock()
        browser.release_page = MagicMock()
        browser._all_pages = [page]

        assert browser._is_cf_access_error(RuntimeError("HTTP 403 Forbidden")) is True
        assert browser._is_cf_access_error(RuntimeError("timeout")) is False

        browser._release_page_if_pooled(page)
        browser._release_page_if_pooled(MagicMock())

        browser.release_page.assert_called_once_with(page)

    async def test_refresh_cf_clearance_resets_and_rechecks(self):
        browser = CdpBrowser(config=AppConfig())
        browser._cf_cleared = True

        async def ensure() -> None:
            browser._cf_cleared = True

        browser.ensure_cf_clearance = AsyncMock(side_effect=ensure)

        await browser._refresh_cf_clearance(reason="retry")

        assert browser._cf_cleared is True
        browser.ensure_cf_clearance.assert_awaited_once()

    async def test_evaluate_request_with_cf_retry_uses_primary_page_for_non_pooled_calls(self):
        browser = CdpBrowser(config=AppConfig())
        browser._started = False
        browser.start = AsyncMock()
        browser.ensure_cf_clearance = AsyncMock()
        browser.acquire_page = AsyncMock(side_effect=AssertionError("pool should not be used"))
        page = MagicMock()
        browser._ensure_page = AsyncMock(return_value=page)
        browser._evaluate_with_timeout = AsyncMock(return_value={"ok": True})

        result = await browser._evaluate_request_with_cf_retry(
            url="https://api.example.com/data",
            expression="() => ({ ok: true })",
            arg=None,
            action="Posting JSON to https://api.example.com/data",
            use_page_pool=False,
        )

        assert result == {"ok": True}
        browser.start.assert_awaited_once()
        browser.ensure_cf_clearance.assert_awaited_once()
        browser._ensure_page.assert_awaited_once()
        browser._evaluate_with_timeout.assert_awaited_once()

    async def test_fetch_page_raises_when_challenge_persists_after_refresh(self):
        browser = CdpBrowser(config=AppConfig())
        browser._started = True
        browser.ensure_cf_clearance = AsyncMock()
        browser._refresh_cf_clearance = AsyncMock()
        page = MagicMock()
        browser._ensure_page = AsyncMock(return_value=page)
        browser._goto_with_timeout = AsyncMock()
        browser._is_cf_challenge = AsyncMock(side_effect=[True, True])

        with pytest.raises(
            CloudflareChallengeError,
            match=r"Cloudflare challenge persisted after clearance refresh for https://example\.com\.",
        ):
            await browser.fetch_page("https://example.com")

        browser._refresh_cf_clearance.assert_awaited_once()

    async def test_get_bytes_decodes_base64_payload_and_passes_referer(self):
        browser = CdpBrowser(config=AppConfig())
        browser._evaluate_request_with_cf_retry = AsyncMock(
            return_value=base64.b64encode(b"hello").decode("ascii"),
        )
        browser._get_bytes_direct = AsyncMock()

        result = await browser.get_bytes("https://cdn.example.com/img", referer="https://ref.example.com")

        assert result == b"hello"
        browser._get_bytes_direct.assert_not_awaited()
        call_kwargs = browser._evaluate_request_with_cf_retry.await_args.kwargs
        assert call_kwargs["use_page_pool"] is True
        assert "AbortController" in call_kwargs["expression"]
        assert "signal: controller.signal" in call_kwargs["expression"]
        assert call_kwargs["arg"] == [
            "https://cdn.example.com/img",
            {"Referer": "https://ref.example.com"},
            29_000,
        ]

    async def test_get_bytes_falls_back_to_direct_http_after_browser_fetch_failure(self):
        browser = CdpBrowser(config=AppConfig())
        browser._evaluate_request_with_cf_retry = AsyncMock(
            side_effect=RuntimeError("Page.evaluate: TypeError: Failed to fetch"),
        )
        browser._get_bytes_direct = AsyncMock(return_value=b"image")

        result = await browser.get_bytes("https://cdn.example.com/img.webp", referer="https://ref.example.com")

        assert result == b"image"
        browser._get_bytes_direct.assert_awaited_once_with(
            "https://cdn.example.com/img.webp",
            referer="https://ref.example.com",
        )

    async def test_get_bytes_falls_back_to_direct_http_after_browser_timeout(self):
        browser = CdpBrowser(config=AppConfig())
        browser._evaluate_request_with_cf_retry = AsyncMock(
            side_effect=BrowserTimeoutError(
                "Fetching binary response from https://cdn.example.com/img.webp timed out after 30000ms.",
            ),
        )
        browser._get_bytes_direct = AsyncMock(return_value=b"image")

        result = await browser.get_bytes("https://cdn.example.com/img.webp")

        assert result == b"image"
        browser._get_bytes_direct.assert_awaited_once_with(
            "https://cdn.example.com/img.webp",
            referer=None,
        )

    async def test_get_bytes_does_not_fallback_after_http_403(self):
        browser = CdpBrowser(config=AppConfig())
        browser._evaluate_request_with_cf_retry = AsyncMock(side_effect=Http403Error("HTTP 403 Forbidden"))
        browser._get_bytes_direct = AsyncMock()

        with pytest.raises(Http403Error, match="HTTP 403 Forbidden"):
            await browser.get_bytes("https://cdn.example.com/img.webp")

        browser._get_bytes_direct.assert_not_awaited()

    async def test_get_scrambled_image_bytes_renders_canvas_and_screenshots_element(self):
        browser = CdpBrowser(config=AppConfig())
        browser._started = True
        browser.ensure_cf_clearance = AsyncMock()
        page = MagicMock()
        page.is_closed.return_value = False
        element = MagicMock()
        element.screenshot = AsyncMock(return_value=b"unscrambled-png")
        page.query_selector = AsyncMock(return_value=element)
        browser.acquire_page = AsyncMock(return_value=page)
        browser.release_page = MagicMock()
        browser._evaluate_with_timeout = AsyncMock(return_value={"ok": True, "selector": "#__comix_scrambled_canvas"})

        result = await browser.get_scrambled_image_bytes(
            "https://static.comix.to/c0db/si/b/page-009.webp",
            width=968,
            height=1378,
            referer="https://comix.to/title/lzdj-omori",
        )

        assert result == b"unscrambled-png"
        browser.ensure_cf_clearance.assert_awaited_once()
        browser.acquire_page.assert_awaited_once()
        browser.release_page.assert_called_once_with(page)
        element.screenshot.assert_awaited_once_with(
            type="png",
            timeout=30_000,
            style=None,
        )
        expression = browser._evaluate_with_timeout.await_args.args[1]
        arg = browser._evaluate_with_timeout.await_args.args[2]
        assert arg == [
            "https://static.comix.to/c0db/si/b/page-009.webp",
            968,
            1378,
            {"Referer": "https://comix.to/title/lzdj-omori"},
        ]
        assert "window.__comixRenderScrambledImage" in expression
        assert "secure-" in expression
        assert "document.body.appendChild" in expression

    async def test_get_scrambled_image_bytes_retries_with_cache_bust_after_render_failure(self):
        browser = CdpBrowser(config=AppConfig())
        browser._started = True
        browser.ensure_cf_clearance = AsyncMock()
        page = MagicMock()
        page.is_closed.return_value = False
        element = MagicMock()
        element.screenshot = AsyncMock(return_value=b"retry-png")
        page.query_selector = AsyncMock(return_value=element)
        browser.acquire_page = AsyncMock(return_value=page)
        browser.release_page = MagicMock()
        browser._evaluate_with_timeout = AsyncMock(
            side_effect=[
                RuntimeError("Page.evaluate: InvalidStateError: The source image could not be decoded."),
                {"ok": True, "selector": "#__comix_scrambled_canvas"},
            ],
        )

        result = await browser.get_scrambled_image_bytes("https://cdn.example.com/img.webp")

        assert result == b"retry-png"
        assert browser._evaluate_with_timeout.await_count == 2
        first_arg = browser._evaluate_with_timeout.await_args_list[0].args[2]
        second_arg = browser._evaluate_with_timeout.await_args_list[1].args[2]
        assert first_arg[0] == "https://cdn.example.com/img.webp"
        assert second_arg[0] == "https://cdn.example.com/img.webp?r=1"
        browser.release_page.assert_called_once_with(page)

    def test_get_bytes_direct_sync_sends_browser_like_headers_and_timeout(self):
        config = _make_config(download=DownloadConfig(read_timeout_ms=1234))
        browser = CdpBrowser(config=config)
        captured: dict[str, object] = {}

        class Response:
            status = 200

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"image"

        def fake_urlopen(request: object, *, timeout: float) -> Response:
            captured["headers"] = dict(request.header_items())  # type: ignore[attr-defined]
            captured["timeout"] = timeout
            return Response()

        with patch("comix_dl.core.engines.cdp_browser.urlopen", fake_urlopen):
            result = browser._get_bytes_direct_sync(
                "https://cdn.example.com/img.webp",
                referer="https://ref.example.com",
            )

        headers = {str(key).lower(): value for key, value in cast("dict[str, object]", captured["headers"]).items()}
        assert result == b"image"
        assert captured["timeout"] == pytest.approx(1.234)
        assert headers["user-agent"].startswith("Mozilla/5.0")
        assert "image/webp" in str(headers["accept"])
        assert headers["referer"] == "https://ref.example.com"

    def test_get_bytes_direct_sync_translates_http_403(self):
        browser = CdpBrowser(config=AppConfig())

        def fake_urlopen(*_args: object, **_kwargs: object) -> object:
            raise HTTPError("https://cdn.example.com/img.webp", 403, "Forbidden", {}, None)

        with (
            patch("comix_dl.core.engines.cdp_browser.urlopen", fake_urlopen),
            pytest.raises(Http403Error, match="HTTP 403"),
        ):
            browser._get_bytes_direct_sync("https://cdn.example.com/img.webp")

    def test_get_bytes_direct_sync_translates_wrapped_timeout(self):
        browser = CdpBrowser(config=AppConfig())

        def fake_urlopen(*_args: object, **_kwargs: object) -> object:
            raise URLError(TimeoutError("timed out"))

        with (
            patch("comix_dl.core.engines.cdp_browser.urlopen", fake_urlopen),
            pytest.raises(BrowserTimeoutError, match="Direct binary response"),
        ):
            browser._get_bytes_direct_sync("https://cdn.example.com/img.webp")

    async def test_post_json_delegates_without_using_page_pool(self):
        browser = CdpBrowser(config=AppConfig())
        browser._evaluate_request_with_cf_retry = AsyncMock(return_value={"ok": True})

        result = await browser.post_json("https://api.example.com/post", {"name": "value"})

        assert result == {"ok": True}
        call_kwargs = browser._evaluate_request_with_cf_retry.await_args.kwargs
        assert call_kwargs["use_page_pool"] is False
        assert call_kwargs["arg"] == ["https://api.example.com/post", {"name": "value"}]

    async def test_get_json_expression_allows_json_requester_override(self):
        browser = CdpBrowser(config=AppConfig())
        browser._evaluate_request_with_cf_retry = AsyncMock(return_value={"ok": True})

        await browser.get_json("https://api.example.com/data")

        expression = browser._evaluate_request_with_cf_retry.await_args.kwargs["expression"]
        assert "window.__comixJsonRequest" in expression
        assert "const __comixBody = undefined" in expression
        assert "return handled.data" in expression

    async def test_post_json_expression_allows_json_requester_override(self):
        browser = CdpBrowser(config=AppConfig())
        browser._evaluate_request_with_cf_retry = AsyncMock(return_value={"ok": True})

        await browser.post_json("https://api.example.com/post", {"name": "value"})

        expression = browser._evaluate_request_with_cf_retry.await_args.kwargs["expression"]
        assert "window.__comixJsonRequest" in expression
        assert "const __comixBody = body" in expression
        assert "return handled.data" in expression

    async def test_probe_service_access_uses_current_v1_manga_endpoint(self):
        browser = CdpBrowser(config=AppConfig(), base_url="https://example.test")
        browser._evaluate_with_timeout = AsyncMock(
            return_value={
                "ok": True,
                "url": "https://example.test/api/v1/manga?keyword=test&limit=1",
                "contentType": "application/json",
            },
        )
        page = MagicMock()

        assert await browser._probe_service_access(page) is True

        probe_url = browser._evaluate_with_timeout.await_args.args[2]
        assert probe_url == "https://example.test/api/v1/manga?keyword=test&limit=1"

    async def test_ensure_cf_clearance_brings_challenge_tab_to_front(self):
        browser = CdpBrowser(config=AppConfig(), base_url="https://example.test")
        browser._started = True
        page = MagicMock()
        page.bring_to_front = AsyncMock()
        browser._page = page
        browser._ensure_page = AsyncMock(return_value=page)
        browser._goto_with_timeout = AsyncMock()
        browser._evaluate_with_timeout = AsyncMock(return_value=None)
        browser._is_cf_challenge = AsyncMock(return_value=True)
        browser._wait_for_cf_clearance = AsyncMock()
        browser._init_pool_pages = AsyncMock()

        await browser.ensure_cf_clearance()

        page.bring_to_front.assert_awaited_once()
        browser._wait_for_cf_clearance.assert_awaited_once_with(page)

    async def test_is_cf_challenge_returns_false_when_clearance_cookie_exists(self):
        browser = CdpBrowser(config=AppConfig())
        page = MagicMock()
        page.context.cookies = AsyncMock(return_value=[{"name": "cf_clearance"}])
        page.title = AsyncMock(return_value="regular page")
        page.query_selector = AsyncMock(return_value=None)
        page.content = AsyncMock(return_value="<html>ok</html>")

        assert await browser._is_cf_challenge(page) is False

    async def test_is_cf_challenge_detects_title_and_selector_signals(self):
        browser = CdpBrowser(config=AppConfig())
        page = MagicMock()
        page.context.cookies = AsyncMock(return_value=[])
        page.title = AsyncMock(return_value=browser._config.browser.cf_titles[0])

        assert await browser._is_cf_challenge(page) is True

        page = MagicMock()
        page.context.cookies = AsyncMock(side_effect=RuntimeError("no cookies"))
        page.title = AsyncMock(return_value="regular page")
        page.query_selector = AsyncMock(side_effect=[None, object()])

        assert await browser._is_cf_challenge(page) is True

    async def test_is_cf_challenge_prefers_live_challenge_signal_over_stale_cookie(self):
        browser = CdpBrowser(config=AppConfig())
        page = MagicMock()
        page.context.cookies = AsyncMock(return_value=[{"name": "cf_clearance"}])
        page.title = AsyncMock(return_value=browser._config.browser.cf_titles[0])

        assert await browser._is_cf_challenge(page) is True

    async def test_is_cf_challenge_detects_challenge_content_markers(self):
        browser = CdpBrowser(config=AppConfig())
        page = MagicMock()
        page.context.cookies = AsyncMock(return_value=[])
        page.title = AsyncMock(return_value="regular page")
        page.query_selector = AsyncMock(return_value=None)
        page.content = AsyncMock(
            return_value="<html><body>Checking your browser <script>var __cf_chl_opt = {}</script></body></html>",
        )

        assert await browser._is_cf_challenge(page) is True

    async def test_wait_for_cf_clearance_returns_when_challenge_resolves(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        browser = CdpBrowser(config=AppConfig())
        browser._config.browser.cf_wait_seconds = 5
        browser._is_cf_challenge = AsyncMock(return_value=False)
        page = MagicMock()

        class _Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

            async def sleep(self, seconds: float) -> None:
                self.now += seconds

        clock = _Clock()

        monkeypatch.setattr("comix_dl.core.engines.cdp_browser.time.monotonic", clock.monotonic)
        monkeypatch.setattr("comix_dl.core.engines.cdp_browser.asyncio.sleep", AsyncMock(side_effect=clock.sleep))

        await browser._wait_for_cf_clearance(page)

        browser._is_cf_challenge.assert_awaited_once_with(page)

    async def test_wait_for_cf_clearance_accepts_probe_success_before_dom_fully_clears(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        browser = CdpBrowser(config=AppConfig())
        browser._config.browser.cf_wait_seconds = 5
        browser._has_cf_clearance_cookie = AsyncMock(return_value=True)
        browser._probe_service_access = AsyncMock(return_value=True)
        browser._is_cf_challenge = AsyncMock(return_value=True)
        page = MagicMock()

        class _Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

            async def sleep(self, seconds: float) -> None:
                self.now += seconds

        clock = _Clock()

        monkeypatch.setattr("comix_dl.core.engines.cdp_browser.time.monotonic", clock.monotonic)
        monkeypatch.setattr("comix_dl.core.engines.cdp_browser.asyncio.sleep", AsyncMock(side_effect=clock.sleep))

        await browser._wait_for_cf_clearance(page)

        browser._probe_service_access.assert_awaited_once_with(page)
        browser._is_cf_challenge.assert_not_awaited()

    async def test_ensure_cf_clearance_warns_when_cookie_is_missing(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        browser = CdpBrowser(config=AppConfig(), base_url="https://example.test")
        browser._started = True
        page = MagicMock()
        browser._page = page
        browser._ensure_page = AsyncMock(return_value=page)
        browser._goto_with_timeout = AsyncMock()
        browser._is_cf_challenge = AsyncMock(return_value=False)
        browser._has_cf_clearance_cookie = AsyncMock(return_value=False)
        browser._init_pool_pages = AsyncMock()

        with caplog.at_level("WARNING"):
            await browser.ensure_cf_clearance()

        assert "without a cf_clearance cookie" in caplog.text
        browser._init_pool_pages.assert_awaited_once_with("https://example.test")


class TestExtensionHooks:
    """F-4: register_url_transformer / register_on_engine_ready wiring."""

    def test_register_url_transformer_appends_iife(self) -> None:
        browser = CdpBrowser(config=AppConfig())
        assert browser._url_transformer_iifes == []

        browser.register_url_transformer("(function() {})()")
        browser.register_url_transformer("(function() { other; })()")

        assert browser._url_transformer_iifes == [
            "(function() {})()",
            "(function() { other; })()",
        ]

    def test_register_on_engine_ready_appends_hook(self) -> None:
        browser = CdpBrowser(config=AppConfig())
        assert browser._on_ready_hooks == []

        async def hook_a(_engine: CdpBrowser) -> None: ...
        async def hook_b(_engine: CdpBrowser) -> None: ...

        browser.register_on_engine_ready(hook_a)
        browser.register_on_engine_ready(hook_b)

        assert browser._on_ready_hooks == [hook_a, hook_b]

    async def test_install_url_transformers_on_page_evaluates_each_iife(self) -> None:
        browser = CdpBrowser(config=AppConfig())
        browser.register_url_transformer("(function() { a; })()")
        browser.register_url_transformer("(function() { b; })()")
        browser._evaluate_with_timeout = AsyncMock()

        page = MagicMock()
        await browser._install_url_transformers_on_page(page)

        assert browser._evaluate_with_timeout.await_count == 2
        first_iife = browser._evaluate_with_timeout.await_args_list[0].args[1]
        second_iife = browser._evaluate_with_timeout.await_args_list[1].args[1]
        assert first_iife == "(function() { a; })()"
        assert second_iife == "(function() { b; })()"

    async def test_install_url_transformers_logs_when_one_iife_fails(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        browser = CdpBrowser(config=AppConfig())
        browser.register_url_transformer("ok-iife")
        browser.register_url_transformer("bad-iife")
        browser.register_url_transformer("ok-iife-2")

        async def evaluate_side_effect(*args: object, **_kwargs: object) -> object:
            if args[1] == "bad-iife":
                raise RuntimeError("boom")
            return None

        browser._evaluate_with_timeout = AsyncMock(side_effect=evaluate_side_effect)
        page = MagicMock()

        with caplog.at_level("WARNING"):
            await browser._install_url_transformers_on_page(page)

        assert browser._evaluate_with_timeout.await_count == 3
        assert "Failed to install a URL transformer" in caplog.text

    async def test_run_on_engine_ready_hooks_runs_each_once(self) -> None:
        browser = CdpBrowser(config=AppConfig())
        calls: list[str] = []

        async def hook_a(_engine: CdpBrowser) -> None:
            calls.append("a")

        async def hook_b(_engine: CdpBrowser) -> None:
            calls.append("b")

        browser.register_on_engine_ready(hook_a)
        browser.register_on_engine_ready(hook_b)

        await browser._run_on_engine_ready_hooks()
        assert calls == ["a", "b"]

        # Calling again is a no-op so adapter setup never runs twice
        # (even if ensure_cf_clearance is somehow re-entered).
        await browser._run_on_engine_ready_hooks()
        assert calls == ["a", "b"]

    async def test_ensure_cf_clearance_runs_hooks_after_clearance(self) -> None:
        browser = CdpBrowser(config=AppConfig(), base_url="https://example.test")
        browser._started = True
        page = MagicMock()
        browser._page = page
        browser._ensure_page = AsyncMock(return_value=page)
        browser._goto_with_timeout = AsyncMock()
        browser._is_cf_challenge = AsyncMock(return_value=False)
        browser._has_cf_clearance_cookie = AsyncMock(return_value=True)
        browser._init_pool_pages = AsyncMock()
        browser._install_url_transformers_on_all_pages = AsyncMock()

        ran: list[str] = []

        async def hook(_engine: CdpBrowser) -> None:
            ran.append("hook")

        browser.register_on_engine_ready(hook)

        await browser.ensure_cf_clearance()

        assert ran == ["hook"]
        browser._install_url_transformers_on_all_pages.assert_awaited_once()

    async def test_create_pooled_page_installs_transformers_when_registered(self) -> None:
        browser = CdpBrowser(config=AppConfig())
        browser.register_url_transformer("(function() {})()")
        browser._install_url_transformers_on_page = AsyncMock()

        new_page = MagicMock()
        with patch.object(
            BrowserSessionManager,
            "_create_pooled_page",
            AsyncMock(return_value=new_page),
        ):
            page = await browser._create_pooled_page(action="creating", navigate_to_base=True)

        assert page is new_page
        browser._install_url_transformers_on_page.assert_awaited_once_with(new_page)

    async def test_create_pooled_page_skips_transformer_install_when_no_navigation(self) -> None:
        browser = CdpBrowser(config=AppConfig())
        browser.register_url_transformer("(function() {})()")
        browser._install_url_transformers_on_page = AsyncMock()

        new_page = MagicMock()
        with patch.object(
            BrowserSessionManager,
            "_create_pooled_page",
            AsyncMock(return_value=new_page),
        ):
            await browser._create_pooled_page(action="creating", navigate_to_base=False)

        browser._install_url_transformers_on_page.assert_not_awaited()
