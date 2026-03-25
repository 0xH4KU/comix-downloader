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

from comix_dl.browser_session import (
    BrowserSessionManager,
    _atexit_kill_chrome,
    _find_free_port,
    _is_port_in_use,
)
from comix_dl.errors import CloudflareChallengeError

if TYPE_CHECKING:
    from playwright.async_api import Page

    from comix_dl.config import AppConfig

logger = logging.getLogger(__name__)

__all__ = [
    "BrowserSessionManager",
    "CdpBrowser",
    "_atexit_kill_chrome",
    "_find_free_port",
    "_is_port_in_use",
]


class CdpBrowser(BrowserSessionManager):
    """Cloudflare-aware browser client built on BrowserSessionManager."""

    def __init__(self, *, max_pages: int | None = None, config: AppConfig | None = None) -> None:
        super().__init__(max_pages=max_pages, config=config)
        self._cf_cleared = False
        self._cf_lock = asyncio.Lock()

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            self._cf_cleared = False

    async def __aenter__(self) -> CdpBrowser:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _is_cf_access_error(self, exc: Exception) -> bool:
        """Return whether an exception indicates expired Cloudflare clearance."""
        message = str(exc)
        return "HTTP 403" in message or "403 Forbidden" in message

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
                if self._is_cf_access_error(exc):
                    self._release_page_if_pooled(page)
                    if attempt == 0:
                        await self._refresh_cf_clearance(
                            reason=f"{action} received HTTP 403 from {url}.",
                        )
                        continue
                    raise CloudflareChallengeError(
                        "Cloudflare clearance refresh did not recover browser access "
                        f"to {url} after HTTP 403.",
                    ) from exc
                if use_page_pool:
                    await self._replace_dead_page(page)
                raise
            else:
                if use_page_pool:
                    self.release_page(page)
                return result

        raise AssertionError("CF retry loop exited unexpectedly")

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

            self._cf_cleared = True
            logger.info("CF clearance confirmed")
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

    async def get_json(self, url: str) -> dict[str, object]:
        """GET JSON via page.evaluate(fetch())."""
        result = await self._evaluate_request_with_cf_retry(
            url=url,
            expression="""async (url) => {
                const resp = await fetch(url);
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                return await resp.json();
            }""",
            arg=url,
            action=f"Fetching JSON from {url}",
            use_page_pool=True,
        )
        return cast("dict[str, object]", result)

    async def _is_cf_challenge(self, page: Page) -> bool:
        try:
            cookies = await page.context.cookies()
            if any(cookie.get("name") == "cf_clearance" for cookie in cookies):
                return False
        except Exception:
            pass

        try:
            title = await page.title()
        except Exception:
            return False

        if title in self._config.browser.cf_titles:
            return True

        for selector in self._config.browser.cf_selectors:
            try:
                if await page.query_selector(selector):
                    return True
            except Exception:
                return False

        return False

    async def _wait_for_cf_clearance(self, page: Page) -> None:
        deadline = time.monotonic() + self._config.browser.cf_wait_seconds

        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)

            try:
                still = await self._is_cf_challenge(page)
            except Exception:
                logger.info("CF resolved (page navigated)")
                return

            if not still:
                logger.info("CF challenge resolved")
                await asyncio.sleep(1.0)
                return

        raise RuntimeError(
            f"CF challenge did not resolve within {self._config.browser.cf_wait_seconds}s."
        )
