"""Browser client using Chrome DevTools Protocol (CDP).

Connects to a user-launched Chrome instance via CDP.  Since Chrome is
launched by US (not Playwright) there are no ``--enable-automation`` flags
and no "Chrome is being controlled by automated test software" banner.

This prevents Cloudflare from detecting automation.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import TYPE_CHECKING

from comix_dl.config import CONFIG

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

_CDP_PORT = 9222


class CdpBrowser:
    """Connect to a user-launched Chrome via CDP for Cloudflare bypass.

    Flow:
    1. We launch Chrome ourselves (subprocess) with ``--remote-debugging-port``.
    2. Playwright connects via CDP — Chrome has NO automation banner.
    3. Requests made through the page context carry real browser fingerprint.

    Usage::

        async with CdpBrowser() as browser:
            html = await browser.fetch_page("https://comix.to/chapter/...")
            data = await browser.post_json("https://comix.to/apo/", {...})
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._chrome_process: subprocess.Popen[bytes] | None = None
        self._started = False
        self._cf_cleared = False
        self._user_data_dir = CONFIG.browser.cookie_dir / "chrome-profile"

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Launch Chrome and connect via CDP."""
        if self._started:
            return

        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        self._launch_chrome()

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        # Connect to the Chrome we just launched
        browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{_CDP_PORT}",
        )
        # Get the default context (which is Chrome's real context)
        contexts = browser.contexts
        self._context = contexts[0] if contexts else await browser.new_context()

        # Get existing page or create new one
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._started = True
        logger.info("Connected to Chrome via CDP (port %d)", _CDP_PORT)

    def _launch_chrome(self) -> None:
        """Launch Chrome subprocess with remote debugging enabled."""
        import platform
        import shutil

        system = platform.system()

        if system == "Darwin":
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        elif system == "Linux":
            chrome_path = shutil.which("google-chrome") or shutil.which("chromium-browser") or "google-chrome"
        else:
            chrome_path = shutil.which("chrome") or "chrome"

        args = [
            chrome_path,
            f"--remote-debugging-port={_CDP_PORT}",
            f"--user-data-dir={self._user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            # Always launch visible — headless Chrome is still detected by CF.
            # The window opens briefly and closes after work is done.
        ]

        logger.debug("Launching Chrome: %s", " ".join(args))
        try:
            self._chrome_process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait for Chrome to start and open the debug port
            self._wait_for_cdp_ready()
        except FileNotFoundError:
            raise RuntimeError(
                f"Chrome not found at {chrome_path}. "
                "Install Google Chrome to use comix-dl."
            ) from None

    def _wait_for_cdp_ready(self, timeout: float = 10.0) -> None:
        """Wait until Chrome's CDP port is accepting connections."""
        import socket

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", _CDP_PORT), timeout=1):
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.3)

        raise RuntimeError(
            f"Chrome did not start within {timeout}s. "
            "Check if another Chrome instance is using port 9222."
        )

    async def close(self) -> None:
        """Disconnect from Chrome and close the subprocess."""
        if self._playwright:
            await self._playwright.stop()

        if self._chrome_process:
            self._chrome_process.terminate()
            self._chrome_process.wait(timeout=5)
            logger.debug("Chrome process terminated")

        self._page = None
        self._context = None
        self._playwright = None
        self._chrome_process = None
        self._started = False
        self._cf_cleared = False
        logger.info("Browser session closed")

    async def __aenter__(self) -> CdpBrowser:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # -- CF clearance ---------------------------------------------------------

    async def ensure_cf_clearance(self) -> None:
        """Navigate to comix.to to pass CF challenge if needed."""
        if self._cf_cleared:
            return

        url = CONFIG.service.base_url
        logger.info("Checking CF clearance at %s", url)
        page = await self._ensure_page()

        await page.goto(url, wait_until="domcontentloaded")

        if await self._is_cf_challenge(page):
            logger.info("CF challenge detected, waiting for resolution…")
            await self._wait_for_cf_clearance(page)

        self._cf_cleared = True
        logger.info("CF clearance confirmed")

    # -- public API -----------------------------------------------------------

    async def fetch_page(self, url: str) -> str:
        """Navigate to *url* and return HTML."""
        page = await self._ensure_page()
        await page.goto(url, wait_until="domcontentloaded")

        if await self._is_cf_challenge(page):
            await self._wait_for_cf_clearance(page)

        return await page.content()

    async def get_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        """Download binary content via page.evaluate(fetch())."""
        if not self._started:
            await self.start()
        await self.ensure_cf_clearance()
        page = await self._ensure_page()

        result = await page.evaluate(
            """async ([url, headers]) => {
                const resp = await fetch(url, { headers: headers || {} });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const buf = await resp.arrayBuffer();
                return Array.from(new Uint8Array(buf));
            }""",
            [url, {"Referer": referer} if referer else {}],
        )
        return bytes(result)

    async def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        """POST JSON via page.evaluate(fetch())."""
        if not self._started:
            await self.start()
        await self.ensure_cf_clearance()
        page = await self._ensure_page()

        result = await page.evaluate(
            """async ([url, body]) => {
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                return await resp.json();
            }""",
            [url, payload],
        )
        return result  # type: ignore[no-any-return]

    async def get_json(self, url: str) -> dict[str, object]:
        """GET JSON via page.evaluate(fetch())."""
        if not self._started:
            await self.start()
        await self.ensure_cf_clearance()
        page = await self._ensure_page()

        result = await page.evaluate(
            """async (url) => {
                const resp = await fetch(url);
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                return await resp.json();
            }""",
            url,
        )
        return result  # type: ignore[no-any-return]

    # -- CF detection ---------------------------------------------------------

    async def _is_cf_challenge(self, page: Page) -> bool:
        try:
            title = await page.title()
        except Exception:
            return False

        cf_titles = {"Just a moment...", "Attention Required!", "Verify you are human"}
        if title in cf_titles:
            return True

        cf_selectors = [
            "#challenge-running",
            "#cf-challenge-running",
            "iframe[src*='challenges.cloudflare.com']",
        ]
        for selector in cf_selectors:
            try:
                if await page.query_selector(selector):
                    return True
            except Exception:
                return False

        return False

    async def _wait_for_cf_clearance(self, page: Page) -> None:
        import asyncio

        deadline = time.monotonic() + CONFIG.browser.cf_wait_seconds

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
            f"CF challenge did not resolve within {CONFIG.browser.cf_wait_seconds}s."
        )

    async def _ensure_page(self) -> Page:
        if not self._started:
            await self.start()
        assert self._page is not None
        return self._page
