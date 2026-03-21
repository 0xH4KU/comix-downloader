"""Playwright-based browser session with Cloudflare bypass via persistent Chrome profile."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from comix_dl.config import CONFIG

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

# Cloudflare challenge indicators
_CF_CHALLENGE_TITLES = frozenset({"Just a moment...", "Attention Required!", "Verify you are human"})
_CF_CHALLENGE_SELECTORS = [
    "#challenge-running",
    "#cf-challenge-running",
    ".cf-browser-verification",
    "input[name='cf-turnstile-response']",
    "#turnstile-wrapper",
    "iframe[src*='challenges.cloudflare.com']",
]


class BrowserSession:
    """Manage a Playwright browser with Cloudflare bypass using a persistent Chrome profile.

    The key insight: instead of injecting stealth scripts to *hide* automation
    markers, we use ``launch_persistent_context`` with a **real Chrome user data
    directory**.  This makes the browser indistinguishable from a normal Chrome
    instance — cookies, localStorage, history, and browser fingerprints all
    persist naturally.  Cloudflare trusts returning visitors with established
    profiles.

    Usage::

        async with BrowserSession() as session:
            html = await session.fetch_page("https://comix.to/...")
            data = await session.fetch_bytes("https://comix.to/image.webp")
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._started = False
        self._cf_cleared = False
        # Profile dir — all cookies/localStorage persist here automatically
        self._user_data_dir = CONFIG.browser.cookie_dir / "chrome-profile"

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Launch a persistent Chrome instance."""
        if self._started:
            return

        from playwright.async_api import async_playwright

        self._user_data_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()

        headless = CONFIG.browser.headless
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
        ]

        launch_kwargs: dict[str, object] = {
            "user_data_dir": str(self._user_data_dir),
            "headless": headless,
            "args": launch_args,
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
        }

        if not headless:
            launch_kwargs["channel"] = "chrome"
            logger.info("Headed mode: using system Chrome with persistent profile")
        else:
            launch_kwargs["user_agent"] = CONFIG.browser.user_agent

        # launch_persistent_context returns a BrowserContext directly
        self._context = await self._playwright.chromium.launch_persistent_context(
            **launch_kwargs,  # type: ignore[arg-type]
        )
        self._context.set_default_timeout(CONFIG.browser.timeout_ms)

        # Reuse existing page or create new one
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        self._started = True
        logger.info("Browser session started (profile: %s)", self._user_data_dir)

    async def close(self) -> None:
        """Shut down the browser.  Profile data persists on disk automatically."""
        if not self._started:
            return

        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

        self._page = None
        self._context = None
        self._playwright = None
        self._started = False
        self._cf_cleared = False
        logger.info("Browser session closed")

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # -- public API -----------------------------------------------------------

    async def ensure_cf_clearance(self, base_url: str | None = None) -> None:
        """Visit the site homepage to obtain Cloudflare clearance cookies.

        With a persistent profile, this usually passes automatically after
        the first manual solve because the browser is already "trusted".
        """
        if self._cf_cleared:
            return

        url = base_url or CONFIG.service.base_url
        logger.info("Checking CF clearance at %s", url)
        page = await self._ensure_page()

        await page.goto(url, wait_until="domcontentloaded")

        if await self._is_cf_challenge(page):
            logger.info("Cloudflare challenge detected, waiting for resolution…")
            await self._wait_for_cf_clearance(page)

        self._cf_cleared = True
        logger.info("CF clearance confirmed")

    async def fetch_page(self, url: str) -> str:
        """Navigate to *url*, handle CF challenges, and return the HTML content."""
        page = await self._ensure_page()

        logger.debug("Navigating to %s", url)
        response = await page.goto(url, wait_until="domcontentloaded")

        # Check for Cloudflare challenge
        if await self._is_cf_challenge(page):
            logger.info("Cloudflare challenge detected, waiting for resolution…")
            await self._wait_for_cf_clearance(page)

        status = response.status if response else 0
        if status >= 400:
            if await self._is_cf_challenge(page):
                raise RuntimeError(f"Cloudflare challenge could not be resolved for {url}")
            logger.warning("HTTP %d for %s but page content available, proceeding", status, url)

        html = await page.content()
        logger.debug("Fetched %d chars from %s", len(html), url)
        return html

    async def fetch_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        """Download binary content using the browser context's cookies."""
        if not self._context:
            await self.start()
        assert self._context is not None
        await self.ensure_cf_clearance()

        page = await self._ensure_page()

        # Use page.evaluate(fetch()) so the request comes from the page context
        # with all CF cookies, CSRF tokens, and headers automatically included.
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
        """POST JSON to *url* from within the page context.

        Using ``page.evaluate(fetch(...))`` ensures all Cloudflare cookies
        and CSRF tokens are automatically included.
        """
        if not self._context:
            await self.start()
        assert self._context is not None
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

    # -- Cloudflare handling --------------------------------------------------

    async def _is_cf_challenge(self, page: Page) -> bool:
        """Detect whether the current page is a Cloudflare challenge."""
        try:
            title = await page.title()
        except Exception:
            # Context destroyed by navigation = CF challenge just resolved
            return False

        if title in _CF_CHALLENGE_TITLES:
            return True

        for selector in _CF_CHALLENGE_SELECTORS:
            try:
                if await page.query_selector(selector):
                    return True
            except Exception:
                return False

        return False

    async def _wait_for_cf_clearance(self, page: Page) -> None:
        """Wait for the Cloudflare challenge to resolve (auto or manual)."""
        import asyncio

        deadline = time.monotonic() + CONFIG.browser.cf_wait_seconds
        poll_interval = 1.0

        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)

            try:
                still_challenge = await self._is_cf_challenge(page)
            except Exception:
                logger.info("Cloudflare challenge resolved (page navigated)")
                return

            if not still_challenge:
                logger.info("Cloudflare challenge resolved")
                await asyncio.sleep(1.0)
                return

            remaining = deadline - time.monotonic()
            logger.debug("Still waiting for CF clearance (%.1fs remaining)", remaining)

        raise RuntimeError(
            f"Cloudflare challenge did not resolve within {CONFIG.browser.cf_wait_seconds}s. "
            "Try running with --headed to solve manually."
        )

    # -- helpers --------------------------------------------------------------

    async def _ensure_page(self) -> Page:
        if not self._started:
            await self.start()
        assert self._page is not None
        return self._page
