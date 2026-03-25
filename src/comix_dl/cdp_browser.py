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
from typing import TYPE_CHECKING, TypeVar, cast

from comix_dl.config import CONFIG

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from playwright.async_api import Browser, BrowserContext, Page, Playwright

    from comix_dl.config import AppConfig

logger = logging.getLogger(__name__)
T = TypeVar("T")

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


def _find_chrome(system: str) -> str:
    """Auto-detect Chrome executable path for the current platform."""
    import shutil

    if system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            # Homebrew cask
            "/opt/homebrew/bin/chromium",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
        return candidates[0]  # Fallback to standard path for error message

    if system == "Linux":
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                return found
        return "google-chrome"

    # Windows
    env_candidates: list[Path] = []
    for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if base:
            env_candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")

    for candidate in env_candidates:
        if candidate.exists():
            return str(candidate)

    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        return found
    return "chrome.exe"


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

        try:
            self._user_data_dir.mkdir(parents=True, exist_ok=True)
            self._launch_chrome()

            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()

            # Connect to the Chrome we just launched
            browser = await self._connect_over_cdp_with_timeout()
            # Get the default context (which is Chrome's real context)
            contexts = browser.contexts
            self._context = (
                contexts[0]
                if contexts
                else await self._new_context_with_timeout(browser, action="Creating a browser context")
            )

            # Get existing page or create new one
            pages = self._context.pages
            self._page = (
                pages[0]
                if pages
                else await self._new_page_with_timeout(action="Creating the main browser page")
            )
            self._started = True

            # Initialise page pool with additional pages
            for _ in range(self._max_pages):
                try:
                    page = await self._new_page_with_timeout(action="Creating a pooled browser page")
                    self._all_pages.append(page)
                    self._page_pool.put_nowait(page)
                except Exception:
                    break
        except Exception:
            await self.close()
            raise

        logger.info("Connected to Chrome via CDP (port %d, %d pool pages)",
                     self._cdp_port, self._page_pool.qsize())

    def _launch_chrome(self) -> None:
        """Launch Chrome subprocess with remote debugging enabled."""
        global _active_chrome
        import platform

        system = platform.system()

        # User override from config takes priority
        chrome_path = self._config.browser.chrome_path
        if not chrome_path:
            chrome_path = _find_chrome(system)

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

    def _wait_for_cdp_ready(self, timeout: float | None = None) -> None:
        """Wait until Chrome's CDP port is accepting connections."""
        actual_timeout = timeout or (self._config.download.connect_timeout_ms / 1000)
        deadline = time.monotonic() + actual_timeout
        while time.monotonic() < deadline:
            if self._chrome_process is not None and self._chrome_process.poll() is not None:
                raise RuntimeError(
                    f"Chrome exited before CDP port {self._cdp_port} became ready."
                )
            try:
                connect_timeout = min(1.0, max(deadline - time.monotonic(), 0.1))
                with socket.create_connection(("127.0.0.1", self._cdp_port), timeout=connect_timeout):
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.3)

        raise RuntimeError(
            f"Chrome CDP port {self._cdp_port} did not become ready "
            f"within {int(actual_timeout * 1000)}ms."
        )

    async def _run_with_timeout(self, awaitable: Awaitable[T], *, timeout_ms: int, action: str) -> T:
        """Run an awaitable with a clear timeout boundary."""
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout_ms / 1000)
        except TimeoutError as exc:
            raise RuntimeError(f"{action} timed out after {timeout_ms}ms.") from exc

    async def _connect_over_cdp_with_timeout(self) -> Browser:
        """Connect Playwright to Chrome's CDP endpoint with a bounded timeout."""
        assert self._playwright is not None
        endpoint = f"http://127.0.0.1:{self._cdp_port}"
        return await self._run_with_timeout(
            self._playwright.chromium.connect_over_cdp(endpoint),
            timeout_ms=self._config.download.connect_timeout_ms,
            action=f"Connecting to Chrome CDP at {endpoint}",
        )

    async def _new_context_with_timeout(self, browser: Browser, *, action: str) -> BrowserContext:
        """Create a browser context with an explicit timeout."""
        return await self._run_with_timeout(
            browser.new_context(),
            timeout_ms=self._config.browser.timeout_ms,
            action=action,
        )

    async def _new_page_with_timeout(self, *, action: str) -> Page:
        """Create a new page with an explicit timeout."""
        assert self._context is not None
        return await self._run_with_timeout(
            self._context.new_page(),
            timeout_ms=self._config.browser.timeout_ms,
            action=action,
        )

    async def _goto_with_timeout(self, page: Page, url: str, *, action: str) -> None:
        """Navigate with an explicit timeout."""
        await self._run_with_timeout(
            page.goto(url, wait_until="domcontentloaded"),
            timeout_ms=self._config.browser.timeout_ms,
            action=f"{action} to {url}",
        )

    async def _evaluate_with_timeout(
        self,
        page: Page,
        expression: str,
        arg: object,
        *,
        timeout_ms: int,
        action: str,
    ) -> object:
        """Evaluate browser-side JavaScript with an explicit timeout."""
        return await self._run_with_timeout(
            page.evaluate(expression, arg),
            timeout_ms=timeout_ms,
            action=action,
        )

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
                    raise RuntimeError(
                        f"Cloudflare clearance refresh did not recover access to {url}.",
                    ) from exc
                if use_page_pool:
                    await self._replace_dead_page(page)
                raise
            else:
                if use_page_pool:
                    self.release_page(page)
                return result

        raise AssertionError("CF retry loop exited unexpectedly")

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
                new_page = await self._new_page_with_timeout(action="Creating a replacement browser page")
                # Navigate to the correct origin so fetch() works
                base = self._config.service.base_url
                with contextlib.suppress(Exception):
                    await self._goto_with_timeout(
                        new_page,
                        base,
                        action="Navigating replacement browser page",
                    )
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
                await self._goto_with_timeout(page, url, action="Initializing pooled browser page")

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

            await self._goto_with_timeout(page, url, action="Checking Cloudflare clearance")

            if await self._is_cf_challenge(page):
                logger.info("CF challenge detected — bringing Chrome to front for manual solve")
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

            # Navigate pool pages to the same origin so fetch() works on them
            await self._init_pool_pages(url)

    # -- public API -----------------------------------------------------------

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
                raise RuntimeError(
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
        """Download binary content via page.evaluate(fetch()) with base64 encoding.

        Uses base64 instead of JSON array for ~3-4x less overhead.
        If the page crashes during evaluation, it is removed from the pool
        and a replacement is created.
        """
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
        """GET JSON via page.evaluate(fetch()).

        Uses page pool for parallel requests.  If the page crashes,
        it is replaced rather than returned to the pool.
        """
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

    # -- CF detection ---------------------------------------------------------

    async def _is_cf_challenge(self, page: Page) -> bool:
        # Primary signal: if cf_clearance cookie is set, we're through
        try:
            cookies = await page.context.cookies()
            if any(c.get("name") == "cf_clearance" for c in cookies):
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

    async def _ensure_page(self) -> Page:
        if not self._started:
            await self.start()
        assert self._page is not None
        return self._page
