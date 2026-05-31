"""Tests for CdpBrowser request, Cloudflare, and extension-hook behavior."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from comix_dl.core.config import AppConfig
from comix_dl.core.engines.browser_session import BrowserSessionManager
from comix_dl.core.engines.cdp_browser import CdpBrowser
from comix_dl.core.errors import CloudflareChallengeError


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

        result = await browser.get_bytes("https://cdn.example.com/img", referer="https://ref.example.com")

        assert result == b"hello"
        call_kwargs = browser._evaluate_request_with_cf_retry.await_args.kwargs
        assert call_kwargs["use_page_pool"] is True
        assert call_kwargs["arg"] == [
            "https://cdn.example.com/img",
            {"Referer": "https://ref.example.com"},
        ]

    async def test_post_json_delegates_without_using_page_pool(self):
        browser = CdpBrowser(config=AppConfig())
        browser._evaluate_request_with_cf_retry = AsyncMock(return_value={"ok": True})

        result = await browser.post_json("https://api.example.com/post", {"name": "value"})

        assert result == {"ok": True}
        call_kwargs = browser._evaluate_request_with_cf_retry.await_args.kwargs
        assert call_kwargs["use_page_pool"] is False
        assert call_kwargs["arg"] == ["https://api.example.com/post", {"name": "value"}]

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
            return_value="<script src='/cdn-cgi/challenge-platform/h/g/orchestrate'></script>",
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
