"""Browser session lifecycle management for Chrome CDP access."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import socket
import subprocess
import time
from typing import TYPE_CHECKING, TypeVar

from comix_dl.core.config import AppConfig, resolve_config
from comix_dl.core.engines.chrome_process import (
    _LIVE_SESSIONS,
    _cleanup_stale_profile_chrome,
    _find_chrome,
    _find_free_port,
    _lock_file_handle,
    _remove_pid_file,
    _unlock_file_handle,
    _write_pid_file,
)
from comix_dl.core.errors import (
    BrowserTimeoutError,
    ConfigurationError,
    PagePoolUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from io import TextIOWrapper

    from playwright.async_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)
T = TypeVar("T")

_POOL_UNAVAILABLE_MESSAGE = (
    "Browser page pool is unavailable; pooled download requests cannot proceed. "
    "This usually means pooled page creation failed or the browser context is unavailable."
)


class BrowserSessionManager:
    """Own Chrome lifecycle, CDP connection, and the pooled Playwright pages."""

    def __init__(
        self,
        *,
        max_pages: int | None = None,
        config: AppConfig | None = None,
        base_url: str | None = None,
    ) -> None:
        self._config = resolve_config(config)
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
        self._pid_file = self._config.browser.cookie_dir / "chrome.pid"
        self._cdp_port: int = 0
        self._max_pages = resolved_max_pages
        self._page_pool: asyncio.Queue[Page] = asyncio.Queue()
        self._all_pages: list[Page] = []
        self._page_creation_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._instance_lock_handle: TextIOWrapper | None = None
        self._closing = False
        # Site-specific origin used for page-pool warm-up navigation and
        # caller-driven Cloudflare clearance. Optional at construction
        # time so unit tests can build a manager without committing to a
        # site, but required before any pool page is warmed.
        self._base_url = base_url
        # Register self with the live-session weakset so the atexit
        # handler can find us at process exit. Membership is dropped
        # automatically when this object is garbage-collected.
        _LIVE_SESSIONS.add(self)

    def _atexit_cleanup(self) -> None:
        """Last-resort cleanup invoked by :func:`_atexit_cleanup_all`.

        Only touches resources this instance owns; other live
        ``BrowserSessionManager`` instances are responsible for their
        own Chrome subprocesses and PID files.
        """
        if self._chrome_process is not None:
            try:
                self._chrome_process.terminate()
                self._chrome_process.wait(timeout=3)
            except Exception:
                with contextlib.suppress(Exception):
                    self._chrome_process.kill()
            self._chrome_process = None
        _remove_pid_file(self._pid_file)

    async def start(self) -> None:
        """Launch Chrome and connect via CDP."""
        if self._started:
            return

        self._closing = False
        try:
            self._config.browser.cookie_dir.mkdir(parents=True, exist_ok=True)
            self._acquire_instance_lock()
            _cleanup_stale_profile_chrome(self._pid_file, self._user_data_dir)
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
            self._page = await self._prepare_main_page()
            self._started = True
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
        if self._instance_lock_handle is not None:
            return
        # POSIX advisory locks are merged within a single process, so
        # we can't rely on fcntl/msvcrt alone to detect a sibling
        # session in the same Python process. Walk the live-session
        # weakset to enforce the same-process constraint explicitly.
        # The weakset is typed against the minimal _AtExitCleanup
        # protocol so we use getattr with a default rather than
        # tightening that protocol with lock-management attributes.
        for other in _LIVE_SESSIONS:
            if other is self:
                continue
            other_handle = getattr(other, "_instance_lock_handle", None)
            other_lock_file = getattr(other, "_lock_file", None)
            if other_handle is not None and other_lock_file == self._lock_file:
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

    def _release_instance_lock(self) -> None:
        """Release the single-instance lock if held by this browser."""
        handle = self._instance_lock_handle
        if handle is None:
            return

        with contextlib.suppress(Exception):
            _unlock_file_handle(handle)
        handle.close()
        with contextlib.suppress(OSError):
            self._lock_file.unlink()
        self._instance_lock_handle = None

    def _launch_chrome(self) -> None:
        """Launch Chrome subprocess with remote debugging enabled."""

        system = platform.system()
        chrome_path = self._config.browser.chrome_path or _find_chrome(system)

        self._cdp_port = _find_free_port()
        logger.debug("Using CDP port %d", self._cdp_port)

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
            _write_pid_file(self._pid_file, self._chrome_process.pid)
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
            raise BrowserTimeoutError(f"{action} timed out after {timeout_ms}ms.") from exc

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

    async def _close_page_quietly(self, page: Page) -> None:
        """Close one page and suppress browser-side cleanup errors."""
        with contextlib.suppress(Exception):
            await page.close()

    async def _prepare_main_page(self) -> Page:
        """Reuse one healthy page as the primary tab and close any extras."""
        assert self._context is not None
        main_page: Page | None = None
        extra_pages: list[Page] = []

        for page in list(self._context.pages):
            if not self._page_is_healthy(page):
                extra_pages.append(page)
                continue
            if main_page is None:
                main_page = page
                continue
            extra_pages.append(page)

        if extra_pages:
            await asyncio.gather(*[self._close_page_quietly(page) for page in extra_pages])
            logger.info("Closed %d stray browser tab(s) before starting session", len(extra_pages))

        if main_page is not None:
            return main_page
        return await self._new_page_with_timeout(action="Creating the main browser page")

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
        if self._closing:
            return

        self._closing = True

        try:
            for task in list(self._background_tasks):
                task.cancel()
            if self._background_tasks:
                with contextlib.suppress(Exception):
                    await asyncio.gather(*self._background_tasks, return_exceptions=True)
                self._background_tasks.clear()

            if self._page is not None and self._page not in self._all_pages:
                await self._close_page_quietly(self._page)

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
        finally:
            self._page = None
            self._context = None
            self._playwright = None
            self._chrome_process = None
            _remove_pid_file(self._pid_file)
            self._started = False
            self._release_instance_lock()
            self._closing = False
            logger.info("Browser session closed")

    async def __aenter__(self) -> BrowserSessionManager:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def acquire_page(self) -> Page:
        """Get a page from the pool, waiting if all pooled pages are busy."""
        if self._closing:
            raise PagePoolUnavailableError(_POOL_UNAVAILABLE_MESSAGE)

        while True:
            try:
                page = self._page_pool.get_nowait()
            except asyncio.QueueEmpty:
                page = None

            if page is not None:
                if self._page_is_healthy(page):
                    return page
                logger.warning("Discarded unhealthy pooled page before reuse")
                await self._replace_dead_page(page)
                continue

            if len(self._all_pages) < self._max_pages and self._context is not None:
                async with self._page_creation_lock:
                    try:
                        page = self._page_pool.get_nowait()
                    except asyncio.QueueEmpty:
                        page = None
                    if page is not None:
                        if self._page_is_healthy(page):
                            return page
                        logger.warning("Discarded unhealthy pooled page before reuse")
                        await self._replace_dead_page(page)
                        continue

                    if len(self._all_pages) < self._max_pages:
                        return await self._create_pooled_page(
                            action="Creating a pooled browser page",
                            navigate_to_base=True,
                        )

            if not self._all_pages:
                raise PagePoolUnavailableError(_POOL_UNAVAILABLE_MESSAGE)

            page = await self._page_pool.get()
            if self._page_is_healthy(page):
                return page
            logger.warning("Discarded unhealthy pooled page before reuse")
            await self._replace_dead_page(page)

    def release_page(self, page: Page) -> None:
        """Return a page to the pool."""
        if self._closing:
            return
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

        if self._closing or self._context is None or not self._started:
            return

        if self._context is not None:
            try:
                new_page = await self._create_pooled_page(
                    action="Creating a replacement browser page",
                    navigate_to_base=True,
                )
                self.release_page(new_page)
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

    async def _create_pooled_page(self, *, action: str, navigate_to_base: bool) -> Page:
        """Create one pooled page lazily and optionally warm it on the service origin."""
        new_page = await self._new_page_with_timeout(action=action)
        try:
            if navigate_to_base:
                if self._base_url is None:
                    raise ConfigurationError(
                        "Cannot warm pool page: no base_url configured on the engine.",
                    )
                await self._goto_with_timeout(
                    new_page,
                    self._base_url,
                    action="Initializing pooled browser page",
                )
        except Exception:
            with contextlib.suppress(Exception):
                await new_page.close()
            raise

        self._all_pages.append(new_page)
        return new_page
