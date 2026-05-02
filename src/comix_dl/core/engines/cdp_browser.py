"""Browser client using Chrome DevTools Protocol (CDP).

Connects to a user-launched Chrome instance via CDP. Since Chrome is
launched by us (not Playwright), there are no ``--enable-automation`` flags
and no "Chrome is being controlled by automated test software" banner.

This prevents Cloudflare from detecting automation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from typing import TYPE_CHECKING, cast

from comix_dl.core.engines.browser_session import (
    BrowserSessionManager,
)
from comix_dl.core.errors import (
    BrowserTimeoutError,
    CloudflareChallengeError,
    ComixError,
    Http403Error,
    PagePoolUnavailableError,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from comix_dl.core.config import AppConfig

logger = logging.getLogger(__name__)

_CF_CONTENT_MARKERS = (
    "/cdn-cgi/challenge-platform/",
    "__cf_chl_",
    "cf-browser-verification",
    "cf-chl-widget",
    "checking your browser",
    "verify you are human",
)

__all__ = [
    "BrowserSessionManager",
    "CdpBrowser",
]


def _translate_browser_error(exc: Exception) -> Exception:
    """Translate raw browser-layer exceptions into typed domain errors.

    Playwright and other low-level layers raise generic ``RuntimeError``
    or ``Exception`` instances whose meaning is encoded in the message
    string. This function inspects the message once at the browser
    boundary and re-emits a typed :mod:`comix_dl.core.errors` subclass so that
    upstream callers can rely on ``isinstance`` checks instead of fragile
    string matching.

    Already-typed :class:`ComixError` instances pass through untouched.
    Unknown errors are returned as-is so the caller can re-raise them.
    """
    if isinstance(exc, ComixError):
        return exc

    message = str(exc)
    if "HTTP 403" in message or "403 Forbidden" in message:
        return Http403Error(message)
    if "timed out" in message:
        return BrowserTimeoutError(message)
    if "page pool" in message.lower():
        return PagePoolUnavailableError(message)
    return exc


class CdpBrowser(BrowserSessionManager):
    """Cloudflare-aware browser client built on BrowserSessionManager."""

    def __init__(self, *, max_pages: int | None = None, config: AppConfig | None = None) -> None:
        super().__init__(max_pages=max_pages, config=config)
        self._cf_cleared = False
        self._cf_lock = asyncio.Lock()
        self._main_page_lock = asyncio.Lock()
        self._signing_installed = False

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            self._cf_cleared = False
            self._signing_installed = False

    async def __aenter__(self) -> CdpBrowser:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _is_cf_access_error(self, exc: Exception) -> bool:
        """Return whether an exception indicates expired Cloudflare clearance.

        Accepts both already-typed :class:`Http403Error` instances and raw
        exceptions whose message indicates a 403; the boundary translation
        helper normalises them before the ``isinstance`` check.
        """
        return isinstance(_translate_browser_error(exc), Http403Error)

    def _release_page_if_pooled(self, page: Page) -> None:
        """Return a healthy pooled page so clearance refresh can reinitialize it."""
        if page in self._all_pages:
            self.release_page(page)

    async def _refresh_cf_clearance(self, *, reason: str) -> None:
        """Drop cached clearance state and reacquire it once."""
        logger.warning("%s Resetting Cloudflare clearance and retrying once.", reason)
        self._cf_cleared = False
        await self.ensure_cf_clearance()

    async def _evaluate_request_with_cf_retry(
        self,
        *,
        url: str,
        expression: str,
        arg: object,
        action: str,
        use_page_pool: bool,
    ) -> object:
        """Evaluate a browser request, refreshing CF clearance once on HTTP 403."""
        if not self._started:
            await self.start()
        await self.ensure_cf_clearance()

        for attempt in range(2):
            if use_page_pool:
                should_retry, result = await self._evaluate_request_attempt(
                    url=url,
                    expression=expression,
                    arg=arg,
                    action=action,
                    attempt=attempt,
                    use_page_pool=True,
                )
            else:
                async with self._main_page_lock:
                    should_retry, result = await self._evaluate_request_attempt(
                        url=url,
                        expression=expression,
                        arg=arg,
                        action=action,
                        attempt=attempt,
                        use_page_pool=False,
                    )
            if should_retry:
                continue
            return result

        raise AssertionError("CF retry loop exited unexpectedly")

    async def _evaluate_request_attempt(
        self,
        *,
        url: str,
        expression: str,
        arg: object,
        action: str,
        attempt: int,
        use_page_pool: bool,
    ) -> tuple[bool, object]:
        """Run one browser request attempt and report whether the caller should retry."""
        page = await self.acquire_page() if use_page_pool else await self._ensure_page()
        try:
            result = await self._evaluate_with_timeout(
                page,
                expression,
                arg,
                timeout_ms=self._config.download.read_timeout_ms,
                action=action,
            )
        except Exception as exc:
            typed = _translate_browser_error(exc)
            if isinstance(typed, Http403Error):
                self._release_page_if_pooled(page)
                if attempt == 0:
                    await self._refresh_cf_clearance(
                        reason=f"{action} received HTTP 403 from {url}.",
                    )
                    return True, {}
                raise CloudflareChallengeError(
                    "Cloudflare clearance refresh did not recover browser access "
                    f"to {url} after HTTP 403.",
                ) from typed
            if use_page_pool:
                await self._replace_dead_page(page)
            if typed is exc:
                raise
            raise typed from exc
        else:
            if use_page_pool:
                self.release_page(page)
            return False, result

    async def _bring_page_to_front(self, page: Page) -> None:
        """Try to focus the tab the user must solve Cloudflare in."""
        with contextlib.suppress(Exception):
            await self._run_with_timeout(
                page.bring_to_front(),
                timeout_ms=self._config.browser.timeout_ms,
                action="Bringing Cloudflare challenge tab to front",
            )

    async def _probe_service_access(self, page: Page) -> bool:
        """Verify that the current browser context can reach the service origin."""
        probe_url = f"{self._config.service.base_url}/api/v2/manga?keyword=test&limit=1"
        try:
            result = await self._evaluate_with_timeout(
                page,
                """async (url) => {
                    const resp = await fetch(url, { redirect: 'follow' });
                    return {
                        ok: resp.ok,
                        url: resp.url,
                        contentType: resp.headers.get('content-type') || '',
                    };
                }""",
                probe_url,
                timeout_ms=self._config.browser.timeout_ms,
                action=f"Probing browser access to {probe_url}",
            )
        except Exception:
            return False

        if not isinstance(result, dict):
            return False
        if result.get("ok") is not True:
            return False

        final_url = str(result.get("url", "")).lower()
        if "/cdn-cgi/challenge-platform/" in final_url or "__cf_chl_" in final_url:
            return False
        return "json" in str(result.get("contentType", "")).lower()

    async def ensure_cf_clearance(self) -> None:
        """Navigate to comix.to to pass CF challenge if needed."""
        if self._cf_cleared:
            return

        async with self._cf_lock:
            if self._cf_cleared:
                return

            url = self._config.service.base_url
            logger.info("Checking CF clearance at %s", url)
            page = await self._ensure_page()

            await self._goto_with_timeout(page, url, action="Checking Cloudflare clearance")

            if await self._is_cf_challenge(page):
                logger.info("CF challenge detected - bringing Chrome to front for manual solve")
                await self._bring_page_to_front(page)
                with contextlib.suppress(Exception):
                    await self._evaluate_with_timeout(
                        page,
                        """() => {
                            window.moveTo(100, 100);
                            window.resizeTo(800, 600);
                        }""",
                        None,
                        timeout_ms=self._config.browser.timeout_ms,
                        action="Moving Chrome window for Cloudflare challenge",
                    )
                await self._wait_for_cf_clearance(page)
            elif not await self._has_cf_clearance_cookie(page):
                logger.warning(
                    "Cloudflare clearance check completed without a cf_clearance cookie; "
                    "subsequent API requests may still be challenged.",
                )

            self._cf_cleared = True
            logger.info("CF clearance confirmed")
            await self._install_api_signing(page)
            await self._init_pool_pages(url)

    async def fetch_page(self, url: str) -> str:
        """Navigate to *url* and return HTML."""
        if not self._started:
            await self.start()
        await self.ensure_cf_clearance()

        for attempt in range(2):
            page = await self._ensure_page()
            await self._goto_with_timeout(page, url, action="Navigating browser page")

            if await self._is_cf_challenge(page):
                if attempt == 0:
                    await self._refresh_cf_clearance(
                        reason=f"Cloudflare challenge detected while loading {url}.",
                    )
                    continue
                raise CloudflareChallengeError(
                    f"Cloudflare challenge persisted after clearance refresh for {url}.",
                )

            return cast(
                "str",
                await self._run_with_timeout(
                    page.content(),
                    timeout_ms=self._config.browser.timeout_ms,
                    action=f"Reading page content from {url}",
                ),
            )

        raise AssertionError("CF retry loop exited unexpectedly")

    async def get_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        """Download binary content via page.evaluate(fetch()) with base64 encoding."""
        result = await self._evaluate_request_with_cf_retry(
            url=url,
            expression="""async ([url, headers]) => {
                const resp = await fetch(url, { headers: headers || {} });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                const chunkSize = 8192;
                for (let i = 0; i < bytes.length; i += chunkSize) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
                }
                return btoa(binary);
            }""",
            arg=[url, {"Referer": referer} if referer else {}],
            action=f"Fetching binary response from {url}",
            use_page_pool=True,
        )
        return base64.b64decode(cast("str", result))

    async def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        """POST JSON via page.evaluate(fetch())."""
        result = await self._evaluate_request_with_cf_retry(
            url=url,
            expression="""async ([url, body]) => {
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                return await resp.json();
            }""",
            arg=[url, payload],
            action=f"Posting JSON to {url}",
            use_page_pool=False,
        )
        return cast("dict[str, object]", result)

    async def get_json(self, url: str, *, use_page_pool: bool = True) -> dict[str, object]:
        """GET JSON via page.evaluate(fetch()).

        Applies API request signing automatically for endpoints that
        require it (e.g. ``/manga/*/chapters``).
        """
        result = await self._evaluate_request_with_cf_retry(
            url=url,
            expression="""async (url) => {
                if (url.includes('/chapters') && typeof window.__comixSign === 'function') {
                    const u = new URL(url);
                    const path = u.pathname.replace(new RegExp("^/api/v2"), '');
                    const queryObj = {};
                    u.searchParams.forEach((v, k) => { queryObj[k] = isNaN(v) ? v : Number(v); });
                    const signed = window.__comixSign('GET', path, { query: queryObj });
                    const newUrl = new URL(u.origin + u.pathname);
                    for (const [k, v] of Object.entries(signed.query)) {
                        newUrl.searchParams.set(k, String(v));
                    }
                    url = newUrl.toString();
                }
                const resp = await fetch(url);
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                return await resp.json();
            }""",
            arg=url,
            action=f"Fetching JSON from {url}",
            use_page_pool=use_page_pool,
        )
        return cast("dict[str, object]", result)

    async def _install_api_signing(self, page: Page) -> None:
        """Extract the API request-signing function from the site's JS and install it.

        The site uses an obfuscated HMAC-like function to sign requests to
        certain endpoints (e.g. ``/chapters``).  Rather than reimplementing
        the algorithm, we:

        1. Find the Next.js chunk containing the API client definition.
        2. Extract the signing IIFE from the chunk source text.
        3. ``eval`` it in each browser page so ``window.__comixSign`` is
           available for ``get_json`` to call transparently.
        """
        if self._signing_installed:
            return

        install_js = """
        async () => {
            const scripts = document.querySelectorAll('script[src*="_next/static/chunks"]');
            for (const script of scripts) {
                try {
                    const resp = await fetch(script.src);
                    const text = await resp.text();
                    if (!text.includes('baseUrl:"https://comix.to/api/v2/"')) continue;
                    const classIdx = text.indexOf('class n extends Error{response');
                    if (classIdx === -1) continue;
                    const iifeStart = text.lastIndexOf('let i=', classIdx);
                    if (iifeStart === -1) continue;
                    const iifeEndSearch = text.substring(iifeStart, classIdx);
                    const iifeEndIdx = iifeEndSearch.lastIndexOf('}();');
                    if (iifeEndIdx === -1) continue;
                    const iife = text.substring(iifeStart, iifeStart + iifeEndIdx + 4);
                    eval('window.__comixSign = ' + iife.substring('let i='.length));
                    return typeof window.__comixSign === 'function';
                } catch (e) { continue; }
            }
            return false;
        }
        """

        try:
            result = await self._evaluate_with_timeout(
                page,
                install_js,
                None,
                timeout_ms=self._config.browser.timeout_ms,
                action="Installing API request signing function",
            )
            if result:
                logger.info("API request signing installed on main page")
                self._signing_installed = True
            else:
                logger.warning(
                    "Could not extract API signing function; "
                    "chapter listing requests may fail with HTTP 403."
                )
        except Exception as exc:
            logger.warning("Failed to install API signing: %s", exc)

    async def _install_signing_on_page(self, page: Page) -> None:
        """Copy ``window.__comixSign`` from the main page to a pooled page."""
        if not self._signing_installed:
            return

        copy_js = """
        async () => {
            if (typeof window.__comixSign === 'function') return true;
            const scripts = document.querySelectorAll('script[src*="_next/static/chunks"]');
            for (const script of scripts) {
                try {
                    const resp = await fetch(script.src);
                    const text = await resp.text();
                    if (!text.includes('baseUrl:"https://comix.to/api/v2/"')) continue;
                    const classIdx = text.indexOf('class n extends Error{response');
                    if (classIdx === -1) continue;
                    const iifeStart = text.lastIndexOf('let i=', classIdx);
                    if (iifeStart === -1) continue;
                    const iifeEndSearch = text.substring(iifeStart, classIdx);
                    const iifeEndIdx = iifeEndSearch.lastIndexOf('}();');
                    if (iifeEndIdx === -1) continue;
                    const iife = text.substring(iifeStart, iifeStart + iifeEndIdx + 4);
                    eval('window.__comixSign = ' + iife.substring('let i='.length));
                    return typeof window.__comixSign === 'function';
                } catch (e) { continue; }
            }
            return false;
        }
        """
        try:
            await self._evaluate_with_timeout(
                page,
                copy_js,
                None,
                timeout_ms=self._config.browser.timeout_ms,
                action="Installing API signing on pooled page",
            )
        except Exception as exc:
            logger.debug("Failed to install signing on pooled page: %s", exc)

    async def _create_pooled_page(self, *, action: str, navigate_to_base: bool) -> Page:
        """Create a pooled page and install API signing if warmed on the base origin."""
        page = await super()._create_pooled_page(action=action, navigate_to_base=navigate_to_base)
        if navigate_to_base and self._signing_installed:
            await self._install_signing_on_page(page)
        return page

    async def _init_pool_pages(self, url: str) -> None:
        """Navigate pool pages to *url* and install API signing on each."""
        await super()._init_pool_pages(url)
        if not self._signing_installed:
            return

        pages: list[Page] = []
        while not self._page_pool.empty():
            try:
                pages.append(self._page_pool.get_nowait())
            except asyncio.QueueEmpty:
                break

        if pages:
            await asyncio.gather(*[self._install_signing_on_page(p) for p in pages])
            for page in pages:
                self._page_pool.put_nowait(page)

    async def _has_cf_clearance_cookie(self, page: Page) -> bool:
        """Return whether the current browser context has a cf_clearance cookie."""
        try:
            cookies = await page.context.cookies()
        except Exception:
            return False
        return any(cookie.get("name") == "cf_clearance" for cookie in cookies)

    async def _is_cf_challenge(self, page: Page) -> bool:
        try:
            title = await page.title()
        except Exception:
            title = ""

        if title in self._config.browser.cf_titles:
            return True

        page_url = ""
        with contextlib.suppress(Exception):
            page_url = str(page.url).lower()
        if any(marker in page_url for marker in ("/cdn-cgi/challenge-platform/", "__cf_chl_")):
            return True

        for selector in self._config.browser.cf_selectors:
            try:
                if await page.query_selector(selector):
                    return True
            except Exception:
                break

        with contextlib.suppress(Exception):
            content = cast(
                "str",
                await self._run_with_timeout(
                    page.content(),
                    timeout_ms=self._config.browser.timeout_ms,
                    action="Inspecting Cloudflare challenge state",
                ),
            ).lower()
            if any(marker in content for marker in _CF_CONTENT_MARKERS):
                return True

        return False

    async def _wait_for_cf_clearance(self, page: Page) -> None:
        deadline = time.monotonic() + self._config.browser.cf_wait_seconds

        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)

            if await self._has_cf_clearance_cookie(page) and await self._probe_service_access(page):
                logger.info("CF clearance cookie is present and service access probe succeeded")
                return

            try:
                still = await self._is_cf_challenge(page)
            except Exception:
                logger.info("CF resolved (page navigated)")
                return

            if not still:
                logger.info("CF challenge resolved")
                await asyncio.sleep(1.0)
                return

        raise CloudflareChallengeError(
            f"CF challenge did not resolve within {self._config.browser.cf_wait_seconds}s."
        )
