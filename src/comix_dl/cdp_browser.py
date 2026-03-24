"""Browser client using Chrome DevTools Protocol (CDP).

Connects to a user-launched Chrome instance via CDP.  Since Chrome is
launched by US (not Playwright) there are no ``--enable-automation`` flags
and no "Chrome is being controlled by automated test software" banner.

This prevents Cloudflare from detecting automation.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import contextlib
import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from comix_dl.config import CONFIG

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Playwright

    from comix_dl.config import AppConfig

logger = logging.getLogger(__name__)

# Module-level reference for atexit cleanup
_active_chrome: subprocess.Popen[bytes] | None = None
_PID_FILE: Path = Path.home() / ".config" / "comix-dl" / "chrome.pid"


def _write_pid(pid: int) -> None:
    """Write Chrome PID to disk for crash recovery."""
    with contextlib.suppress(OSError):
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(pid))


def _remove_pid() -> None:
    """Remove the PID file."""
    with contextlib.suppress(OSError):
        _PID_FILE.unlink(missing_ok=True)


def _cleanup_stale_chrome() -> None:
    """Kill orphaned Chrome from a previous crash (SIGKILL / OOM).

    Reads the PID file left behind when Python couldn't run atexit,
    checks if the process is still alive, and terminates it.
    """
    if not _PID_FILE.exists():
        return
    try:
        pid = int(_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        _remove_pid()
        return

    try:
        # Check if process is alive (signal 0 = no-op probe)
        os.kill(pid, 0)
    except ProcessLookupError:
        # Already dead — just clean up the stale PID file
        _remove_pid()
        return
    except PermissionError:
        # Process exists but we can't signal it — leave it alone
        _remove_pid()
        return

    # Process is alive — terminate it
    logger.warning("Found orphaned Chrome (PID %d) from a previous crash, terminating", pid)
    try:
        os.kill(pid, signal.SIGTERM)
        # Give it a moment to exit gracefully
        for _ in range(10):
            time.sleep(0.3)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            # Still alive — force kill
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    except Exception as exc:
        logger.debug("Failed to clean up stale Chrome PID %d: %s", pid, exc)
    finally:
        _remove_pid()


def _atexit_kill_chrome() -> None:
    """Last-resort cleanup: kill Chrome if still running."""
    global _active_chrome
    if _active_chrome is not None:
        try:
            _active_chrome.terminate()
            _active_chrome.wait(timeout=3)
        except Exception:
            with contextlib.suppress(Exception):
                _active_chrome.kill()
        _active_chrome = None
    _remove_pid()


atexit.register(_atexit_kill_chrome)


def _find_free_port() -> int:
    """Find an available port for CDP."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _is_port_in_use(port: int) -> bool:
    """Check whether a TCP port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


class CdpBrowser:
    """Connect to a user-launched Chrome via CDP for Cloudflare bypass.

    Features:
    - Base64 binary transfer (3-4x faster than JSON array)
    - Page pool for parallel downloads
    - Dynamic CDP port (no conflicts)
    - Graceful shutdown with atexit fallback

    Usage::

        async with CdpBrowser() as browser:
            data = await browser.get_bytes("https://example.com/image.jpg")
            json = await browser.get_json("https://example.com/api/data")
    """

    def __init__(self, *, max_pages: int = 4, config: AppConfig | None = None) -> None:
        self._config = config or CONFIG
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._chrome_process: subprocess.Popen[bytes] | None = None
        self._started = False
        self._cf_cleared = False
        self._cf_lock = asyncio.Lock()
        self._user_data_dir = self._config.browser.cookie_dir / "chrome-profile"
        self._cdp_port: int = 0
        self._max_pages = max_pages
        self._page_pool: asyncio.Queue[Page] = asyncio.Queue()
        self._all_pages: list[Page] = []

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Launch Chrome and connect via CDP."""
        if self._started:
            return

        # Clean up any orphaned Chrome from a previous crash
        _cleanup_stale_chrome()

        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        self._launch_chrome()

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        # Connect to the Chrome we just launched
        browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self._cdp_port}",
        )
        # Get the default context (which is Chrome's real context)
        contexts = browser.contexts
        self._context = contexts[0] if contexts else await browser.new_context()

        # Get existing page or create new one
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._started = True

        # Initialise page pool with additional pages
        for _ in range(self._max_pages):
            try:
                page = await self._context.new_page()
                self._all_pages.append(page)
                self._page_pool.put_nowait(page)
            except Exception:
                break

        logger.info("Connected to Chrome via CDP (port %d, %d pool pages)",
                     self._cdp_port, self._page_pool.qsize())

    def _launch_chrome(self) -> None:
        """Launch Chrome subprocess with remote debugging enabled."""
        global _active_chrome
        import platform
        import shutil

        system = platform.system()

        if system == "Darwin":
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        elif system == "Linux":
            chrome_path = shutil.which("google-chrome") or shutil.which("chromium-browser") or "google-chrome"
        else:
            chrome_path = shutil.which("chrome") or "chrome"

        # Pick port — use 9222 if free, otherwise find a random one
        if _is_port_in_use(9222):
            self._cdp_port = _find_free_port()
            logger.info("Port 9222 in use, using %d instead", self._cdp_port)
        else:
            self._cdp_port = 9222

        args = [
            chrome_path,
            f"--remote-debugging-port={self._cdp_port}",
            f"--user-data-dir={self._user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            # Hide window off-screen — only show if CF challenge needs manual solve
            "--window-position=-32000,-32000",
            "--window-size=1,1",
        ]

        logger.debug("Launching Chrome: %s", " ".join(args))
        try:
            self._chrome_process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _active_chrome = self._chrome_process
            _write_pid(self._chrome_process.pid)
            self._wait_for_cdp_ready()
        except FileNotFoundError:
            raise RuntimeError(
                f"Chrome not found at {chrome_path}. "
                "Install Google Chrome to use comix-dl."
            ) from None

    def _wait_for_cdp_ready(self, timeout: float = 10.0) -> None:
        """Wait until Chrome's CDP port is accepting connections."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self._cdp_port), timeout=1):
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.3)

        raise RuntimeError(
            f"Chrome did not start within {timeout}s. "
            f"Check if another process is using port {self._cdp_port}."
        )

    async def close(self) -> None:
        """Disconnect from Chrome and close the subprocess."""
        global _active_chrome

        # Close pool pages
        for page in self._all_pages:
            with contextlib.suppress(Exception):
                await page.close()
        self._all_pages.clear()
        # Drain the queue
        while not self._page_pool.empty():
            try:
                self._page_pool.get_nowait()
            except asyncio.QueueEmpty:
                break

        if self._playwright:
            with contextlib.suppress(Exception):
                await self._playwright.stop()

        if self._chrome_process:
            try:
                self._chrome_process.terminate()
                self._chrome_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._chrome_process.kill()
                self._chrome_process.wait(timeout=3)
            except Exception:
                pass
            logger.debug("Chrome process terminated")

        self._page = None
        self._context = None
        self._playwright = None
        self._chrome_process = None
        _active_chrome = None
        self._started = False
        self._cf_cleared = False
        _remove_pid()
        logger.info("Browser session closed")

    async def __aenter__(self) -> CdpBrowser:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # -- page pool ------------------------------------------------------------

    async def acquire_page(self) -> Page:
        """Get a page from the pool (blocks if none available)."""
        try:
            return self._page_pool.get_nowait()
        except asyncio.QueueEmpty:
            # All pool pages in use — fall back to the main page
            return await self._ensure_page()

    def release_page(self, page: Page) -> None:
        """Return a page to the pool."""
        if page in self._all_pages:
            with contextlib.suppress(asyncio.QueueFull):
                self._page_pool.put_nowait(page)

    async def _replace_dead_page(self, dead_page: Page) -> None:
        """Remove a crashed page from the pool and try to create a replacement."""
        if dead_page in self._all_pages:
            self._all_pages.remove(dead_page)
            logger.warning("Removed dead page from pool (%d remaining)", len(self._all_pages))

        with contextlib.suppress(Exception):
            await dead_page.close()

        # Try to create a replacement
        if self._context is not None:
            try:
                new_page = await self._context.new_page()
                # Navigate to the correct origin so fetch() works
                base = self._config.service.base_url
                with contextlib.suppress(Exception):
                    await new_page.goto(base, wait_until="domcontentloaded")
                self._all_pages.append(new_page)
                self._page_pool.put_nowait(new_page)
                logger.info("Replaced dead page with new one (%d pool pages)", self._page_pool.qsize())
            except Exception as exc:
                logger.warning("Failed to create replacement page: %s", exc)

    async def _init_pool_pages(self, url: str) -> None:
        """Navigate all pool pages to *url* so they share the correct origin.

        Without this, pool pages are on about:blank and fetch() to
        comix.to fails with CORS/origin errors.
        """
        async def _nav(page: Page) -> None:
            with contextlib.suppress(Exception):
                await page.goto(url, wait_until="domcontentloaded")

        # Drain pool, navigate all pages, put them back
        pages: list[Page] = []
        while not self._page_pool.empty():
            try:
                pages.append(self._page_pool.get_nowait())
            except asyncio.QueueEmpty:
                break

        if pages:
            await asyncio.gather(*[_nav(p) for p in pages])
            for p in pages:
                self._page_pool.put_nowait(p)
            logger.debug("Initialized %d pool pages at %s", len(pages), url)

    # -- CF clearance ---------------------------------------------------------

    async def ensure_cf_clearance(self) -> None:
        """Navigate to comix.to to pass CF challenge if needed.

        Uses a lock so only one concurrent task performs the clearance check.
        """
        if self._cf_cleared:
            return

        async with self._cf_lock:
            # Double-check after acquiring lock
            if self._cf_cleared:
                return

            url = self._config.service.base_url
            logger.info("Checking CF clearance at %s", url)
            page = await self._ensure_page()

            await page.goto(url, wait_until="domcontentloaded")

            if await self._is_cf_challenge(page):
                logger.info("CF challenge detected — bringing Chrome to front for manual solve")
                with contextlib.suppress(Exception):
                    await page.evaluate("""() => {
                        window.moveTo(100, 100);
                        window.resizeTo(800, 600);
                    }""")
                await self._wait_for_cf_clearance(page)

            self._cf_cleared = True
            logger.info("CF clearance confirmed")

            # Navigate pool pages to the same origin so fetch() works on them
            await self._init_pool_pages(url)

    # -- public API -----------------------------------------------------------

    async def fetch_page(self, url: str) -> str:
        """Navigate to *url* and return HTML."""
        page = await self._ensure_page()
        await page.goto(url, wait_until="domcontentloaded")

        if await self._is_cf_challenge(page):
            await self._wait_for_cf_clearance(page)

        return await page.content()

    async def get_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        """Download binary content via page.evaluate(fetch()) with base64 encoding.

        Uses base64 instead of JSON array for ~3-4x less overhead.
        If the page crashes during evaluation, it is removed from the pool
        and a replacement is created.
        """
        if not self._started:
            await self.start()
        await self.ensure_cf_clearance()

        page = await self.acquire_page()
        try:
            result = await page.evaluate(
                """async ([url, headers]) => {
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
                [url, {"Referer": referer} if referer else {}],
            )
        except Exception:
            # Page may be corrupted — don't return it to the pool
            await self._replace_dead_page(page)
            raise
        else:
            self.release_page(page)
        return base64.b64decode(result)

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
        """GET JSON via page.evaluate(fetch()).

        Uses page pool for parallel requests.  If the page crashes,
        it is replaced rather than returned to the pool.
        """
        if not self._started:
            await self.start()
        await self.ensure_cf_clearance()

        page = await self.acquire_page()
        try:
            result = await page.evaluate(
                """async (url) => {
                    const resp = await fetch(url);
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    return await resp.json();
                }""",
                url,
            )
        except Exception:
            await self._replace_dead_page(page)
            raise
        else:
            self.release_page(page)
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

    async def _ensure_page(self) -> Page:
        if not self._started:
            await self.start()
        assert self._page is not None
        return self._page
