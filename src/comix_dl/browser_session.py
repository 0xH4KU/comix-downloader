"""Browser session lifecycle management for Chrome CDP access."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from comix_dl.config import AppConfig
from comix_dl.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from io import TextIOWrapper

    from playwright.async_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)
T = TypeVar("T")

_POOL_UNAVAILABLE_MESSAGE = (
    "Browser page pool is unavailable; pooled download requests cannot proceed. "
    "This usually means pooled page creation failed during browser startup."
)

# Module-level reference for current-process atexit cleanup
_active_chrome: subprocess.Popen[bytes] | None = None
_active_instance_lock: TextIOWrapper | None = None


def _lock_file_handle(fileobj: TextIOWrapper) -> None:
    """Acquire a non-blocking exclusive file lock."""
    if os.name == "nt":
        import msvcrt

        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    fcntl.flock(fileobj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file_handle(fileobj: TextIOWrapper) -> None:
    """Release a previously acquired file lock."""
    if os.name == "nt":
        import msvcrt

        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    fcntl.flock(fileobj.fileno(), fcntl.LOCK_UN)


def _atexit_kill_chrome() -> None:
    """Last-resort cleanup for Chrome started by this Python process only."""
    global _active_chrome
    if _active_chrome is not None:
        try:
            _active_chrome.terminate()
            _active_chrome.wait(timeout=3)
        except Exception:
            with contextlib.suppress(Exception):
                _active_chrome.kill()
        _active_chrome = None


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
            "/opt/homebrew/bin/chromium",
        ]
        for candidate_path in candidates:
            if Path(candidate_path).exists():
                return candidate_path
        return candidates[0]

    if system == "Linux":
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                return found
        return "google-chrome"

    env_candidates: list[Path] = []
    for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if base:
            env_candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")

    for env_candidate in env_candidates:
        if env_candidate.exists():
            return str(env_candidate)

    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        return found
    return "chrome.exe"


class BrowserSessionManager:
    """Own Chrome lifecycle, CDP connection, and the pooled Playwright pages."""

    def __init__(self, *, max_pages: int | None = None, config: AppConfig | None = None) -> None:
        self._config = config if config is not None else AppConfig()
        resolved_max_pages = (
            max_pages if max_pages is not None else self._config.download.max_concurrent_images
        )
        if resolved_max_pages < 1:
            raise ConfigurationError("Browser page pool size must be at least 1.")

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._chrome_process: subprocess.Popen[bytes] | None = None
        self._started = False
        self._user_data_dir = self._config.browser.cookie_dir / "chrome-profile"
        self._lock_file = self._config.browser.cookie_dir / "browser.lock"
        self._cdp_port: int = 0
        self._max_pages = resolved_max_pages
        self._page_pool: asyncio.Queue[Page] = asyncio.Queue()
        self._all_pages: list[Page] = []
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._instance_lock_handle: TextIOWrapper | None = None

    async def start(self) -> None:
        """Launch Chrome and connect via CDP."""
        if self._started:
            return

        try:
            self._config.browser.cookie_dir.mkdir(parents=True, exist_ok=True)
            self._acquire_instance_lock()
            self._user_data_dir.mkdir(parents=True, exist_ok=True)
            self._launch_chrome()

            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()

            browser = await self._connect_over_cdp_with_timeout()
            contexts = browser.contexts
            self._context = (
                contexts[0]
                if contexts
                else await self._new_context_with_timeout(browser, action="Creating a browser context")
            )

            pages = self._context.pages
            self._page = (
                pages[0]
                if pages
                else await self._new_page_with_timeout(action="Creating the main browser page")
            )
            self._started = True

            for _ in range(self._max_pages):
                try:
                    page = await self._new_page_with_timeout(action="Creating a pooled browser page")
                    self._all_pages.append(page)
                    self._page_pool.put_nowait(page)
                except Exception:
                    break
            if not self._all_pages:
                raise RuntimeError("Failed to create any pooled browser pages for downloads.")
        except Exception:
            await self.close()
            raise

        logger.info(
            "Connected to Chrome via CDP (port %d, %d pool pages)",
            self._cdp_port,
            self._page_pool.qsize(),
        )

    def _acquire_instance_lock(self) -> None:
        """Acquire the single-instance lock for browser sessions."""
        global _active_instance_lock
        if self._instance_lock_handle is not None:
            return
        if _active_instance_lock is not None:
            raise RuntimeError(
                f"Another comix-dl browser session is already running "
                f"(lock file: {self._lock_file}).",
            )

        handle = self._lock_file.open("a+", encoding="utf-8")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\n")
                handle.flush()
            _lock_file_handle(handle)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
        except Exception:
            handle.close()
            raise RuntimeError(
                f"Another comix-dl browser session is already running "
                f"(lock file: {self._lock_file}).",
            ) from None

        self._instance_lock_handle = handle
        _active_instance_lock = handle

    def _release_instance_lock(self) -> None:
        """Release the single-instance lock if held by this browser."""
        global _active_instance_lock
        handle = self._instance_lock_handle
        if handle is None:
            return

        with contextlib.suppress(Exception):
            _unlock_file_handle(handle)
        handle.close()
        with contextlib.suppress(OSError):
            self._lock_file.unlink()
        self._instance_lock_handle = None
        if _active_instance_lock is handle:
            _active_instance_lock = None

    def _launch_chrome(self) -> None:
        """Launch Chrome subprocess with remote debugging enabled."""
        global _active_chrome
        import platform

        system = platform.system()
        chrome_path = self._config.browser.chrome_path or _find_chrome(system)

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

    async def close(self) -> None:
        """Disconnect from Chrome and close the subprocess."""
        global _active_chrome

        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        for page in self._all_pages:
            with contextlib.suppress(Exception):
                await page.close()
        self._all_pages.clear()

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
        self._release_instance_lock()
        logger.info("Browser session closed")

    async def __aenter__(self) -> BrowserSessionManager:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def acquire_page(self) -> Page:
        """Get a page from the pool, waiting if all pooled pages are busy."""
        while True:
            if not self._all_pages:
                raise RuntimeError(_POOL_UNAVAILABLE_MESSAGE)
            page = await self._page_pool.get()
            if self._page_is_healthy(page):
                return page
            logger.warning("Discarded unhealthy pooled page before reuse")
            await self._replace_dead_page(page)

    def release_page(self, page: Page) -> None:
        """Return a page to the pool."""
        if page not in self._all_pages:
            return
        if not self._page_is_healthy(page):
            logger.warning("Not returning unhealthy page to pool; scheduling replacement")
            with contextlib.suppress(RuntimeError):
                task = asyncio.create_task(self._replace_dead_page(page))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            return
        with contextlib.suppress(asyncio.QueueFull):
            self._page_pool.put_nowait(page)

    def _page_is_healthy(self, page: Page) -> bool:
        """Return whether a pooled page still looks reusable."""
        with contextlib.suppress(Exception):
            return not page.is_closed()
        return False

    async def _replace_dead_page(self, dead_page: Page) -> None:
        """Remove a crashed page from the pool and try to create a replacement."""
        if dead_page in self._all_pages:
            self._all_pages.remove(dead_page)
            logger.warning("Removed dead page from pool (%d remaining)", len(self._all_pages))

        with contextlib.suppress(Exception):
            await dead_page.close()

        if self._context is not None:
            try:
                new_page = await self._new_page_with_timeout(action="Creating a replacement browser page")
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
        """Navigate all pool pages to *url* so they share the correct origin."""

        async def _nav(page: Page) -> None:
            with contextlib.suppress(Exception):
                await self._goto_with_timeout(page, url, action="Initializing pooled browser page")

        pages: list[Page] = []
        while not self._page_pool.empty():
            try:
                pages.append(self._page_pool.get_nowait())
            except asyncio.QueueEmpty:
                break

        if pages:
            await asyncio.gather(*[_nav(page) for page in pages])
            for page in pages:
                self._page_pool.put_nowait(page)
            logger.debug("Initialized %d pool pages at %s", len(pages), url)

    async def _ensure_page(self) -> Page:
        if not self._started:
            await self.start()
        assert self._page is not None
        return self._page
