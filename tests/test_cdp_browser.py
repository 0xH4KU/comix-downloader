"""Tests for comix_dl.cdp_browser utilities and timeout wiring."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from comix_dl.cdp_browser import CdpBrowser, _find_free_port, _is_port_in_use
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
    async def test_connect_over_cdp_uses_connect_timeout(self, monkeypatch: pytest.MonkeyPatch):
        config = AppConfig()
        config.download.connect_timeout_ms = 1234

        browser = CdpBrowser(config=config)
        browser._cdp_port = 9444

        captured: dict[str, float] = {}

        async def fake_wait_for(awaitable: object, timeout: float) -> object:
            captured["timeout"] = timeout
            return await awaitable

        async def connect(endpoint: str) -> object:
            assert endpoint == "http://127.0.0.1:9444"
            return {"ok": True}

        monkeypatch.setattr("comix_dl.cdp_browser.asyncio.wait_for", fake_wait_for)
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

        browser = CdpBrowser(config=config)
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

        monkeypatch.setattr("comix_dl.cdp_browser.time.monotonic", clock.monotonic)
        monkeypatch.setattr("comix_dl.cdp_browser.time.sleep", clock.sleep)
        monkeypatch.setattr("comix_dl.cdp_browser.socket.create_connection", fail_connect)

        with pytest.raises(
            RuntimeError,
            match=r"Chrome CDP port 9222 did not become ready within 600ms\.",
        ):
            browser._wait_for_cdp_ready()
