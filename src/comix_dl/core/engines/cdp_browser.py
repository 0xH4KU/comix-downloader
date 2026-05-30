"""Browser client using Chrome DevTools Protocol (CDP).

Connects to a user-launched Chrome instance via CDP. Since Chrome is
launched by us (not Playwright), there are no ``--enable-automation`` flags
and no "Chrome is being controlled by automated test software" banner.

This prevents Cloudflare from detecting automation.

Site-specific hooks
-------------------

Although the public surface (``get_json`` / ``get_bytes`` / ``post_json``
/ ``fetch_page``) is intentionally site-agnostic, two extension points
let a :class:`~comix_dl.sites.base.SiteAdapter` inject behaviour:

* :meth:`CdpBrowser.register_url_transformer` accepts a JS IIFE that
  pushes a function onto ``window.__comixUrlTransformers``. The fetch
  helpers run that array in registration order on every outbound
  request, allowing adapters to add request signing, auth headers,
  etc. The IIFE is replayed against every page in the pool — main
  page, every existing pooled page, and every lazily-created page.
* :meth:`CdpBrowser.register_on_engine_ready` accepts a coroutine to
  run exactly once after Cloudflare clearance has been confirmed and
  before any caller-visible request runs. Adapters use it to install
  signing functions, prime cookies, etc.

Both registries are populated outside this module — typically by the
application session bootstrap calling
``adapter.on_engine_ready(engine)`` after wiring the engine up.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from comix_dl.core.engines.browser_session import (
    BrowserSessionManager,
)
from comix_dl.core.errors import (
    BrowserTimeoutError,
    CloudflareChallengeError,
    ComixError,
    ConfigurationError,
    Http403Error,
    PagePoolUnavailableError,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from comix_dl.core.config import AppConfig

logger = logging.getLogger(__name__)

_CF_CONTENT_MARKERS = (
    "__cf_chl_",
    "cf-browser-verification",
    "cf-chl-widget",
    "checking your browser",
    "verify you are human",
)

# A coroutine invoked once after the engine completes Cloudflare
# clearance. The engine passes itself in so the hook can run setup
# work (install signing, prime caches, etc.) using the public engine
# API.
OnEngineReadyHook = Callable[["CdpBrowser"], Awaitable[None]]

# JS snippet appended to the start of every outbound fetch helper.
# It runs registered URL transformers in order. Errors in any single
# transformer are swallowed so a misbehaving adapter does not break
# unrelated requests.
_URL_TRANSFORMER_JS_PRELUDE = """
    if (Array.isArray(window.__comixUrlTransformers)) {
        for (const t of window.__comixUrlTransformers) {
            try {
                const transformed = t(__comixMethod, url);
                if (typeof transformed === 'string' && transformed) {
                    url = transformed;
                }
            } catch (e) { /* per-transformer errors do not block the fetch */ }
        }
    }
"""

_JSON_REQUESTER_JS_PRELUDE = """
    if (typeof window.__comixJsonRequest === 'function') {
        const handled = await window.__comixJsonRequest(__comixMethod, url, __comixBody);
        if (handled && handled.__handled) {
            return handled.data;
        }
    }
"""

_DIRECT_BYTES_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
_DIRECT_BYTES_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

__all__ = [
    "BrowserSessionManager",
    "CdpBrowser",
    "OnEngineReadyHook",
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


def _is_direct_bytes_fallback_candidate(exc: Exception) -> bool:
    """Return whether browser fetch failed in a way direct HTTP can recover."""
    typed = _translate_browser_error(exc)
    if isinstance(typed, BrowserTimeoutError):
        return True
    if isinstance(typed, Http403Error):
        return False
    return "Failed to fetch" in str(exc)


def _direct_bytes_url_supported(url: str) -> bool:
    """Return whether *url* can be fetched by the stdlib HTTP client."""
    return urlparse(url).scheme in {"http", "https"}


class CdpBrowser(BrowserSessionManager):
    """Cloudflare-aware browser client built on BrowserSessionManager."""

    def __init__(
        self,
        *,
        max_pages: int | None = None,
        config: AppConfig | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(max_pages=max_pages, config=config, base_url=base_url)
        self._cf_cleared = False
        self._cf_lock = asyncio.Lock()
        self._main_page_lock = asyncio.Lock()
        self._url_transformer_iifes: list[str] = []
        self._on_ready_hooks: list[OnEngineReadyHook] = []
        self._on_ready_hooks_done = False

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            self._cf_cleared = False
            self._on_ready_hooks_done = False

    async def __aenter__(self) -> CdpBrowser:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # -- Site-specific hook registration ---------------------------------

    def register_url_transformer(self, iife_js: str) -> None:
        """Register a JS IIFE that adds a URL transformer to every page.

        ``iife_js`` must be a self-contained immediately-invoked function
        expression. When evaluated on a page it should push a callable
        onto ``window.__comixUrlTransformers``; the callable receives
        ``(method, url)`` and returns either a (possibly modified) URL
        string or ``undefined`` to leave the URL unchanged.

        Registered transformers are replayed against the main page and
        every pooled page as they are created. Existing pages get the
        transformer asynchronously the next time they receive any
        engine-driven JS evaluation; in practice this is fine because
        adapters register transformers from inside the
        :meth:`register_on_engine_ready` hook, which fires before the
        first content request.
        """
        self._url_transformer_iifes.append(iife_js)

    def register_on_engine_ready(self, hook: OnEngineReadyHook) -> None:
        """Register a coroutine to run once after CF clearance succeeds.

        Hooks fire after the engine has completed Cloudflare clearance
        and warmed the page pool, but before any caller-visible request
        proceeds. Each hook receives this engine instance and runs to
        completion in registration order; an exception aborts the chain
        and propagates to the original ``ensure_cf_clearance`` caller.
        """
        self._on_ready_hooks.append(hook)

    async def _install_url_transformers_on_page(self, page: Page) -> None:
        """Replay every registered URL transformer IIFE on *page*."""
        for iife in self._url_transformer_iifes:
            try:
                await self._evaluate_with_timeout(
                    page,
                    iife,
                    None,
                    timeout_ms=self._config.browser.timeout_ms,
                    action="Installing URL transformer",
                )
            except Exception as exc:
                logger.warning("Failed to install a URL transformer on page: %s", exc)

    async def _run_on_engine_ready_hooks(self) -> None:
        """Invoke registered on-engine-ready hooks once."""
        if self._on_ready_hooks_done:
            return
        self._on_ready_hooks_done = True
        for hook in self._on_ready_hooks:
            await hook(self)

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
                if not self._page_is_healthy(page):
                    await self._replace_dead_page(page)
                else:
                    self.release_page(page)
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
        if self._base_url is None:
            return False
        # The lightweight probe path is comix.to-specific; F-5 will move
        # it onto the SiteAdapter so each site supplies its own probe.
        probe_url = f"{self._base_url}/api/v1/manga?keyword=test&limit=1"
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
        """Navigate to the configured base URL to pass any CF challenge."""
        if self._cf_cleared:
            return

        if self._base_url is None:
            raise ConfigurationError(
                "CdpBrowser requires base_url for Cloudflare clearance; "
                "construct the engine with base_url=<active mirror>.",
            )

        async with self._cf_lock:
            if self._cf_cleared:
                return

            url = self._base_url
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
            await self._init_pool_pages(url)
            # F-4: adapter on-engine-ready hooks fire after CF clearance
            # and pool warm-up. Hooks may call register_url_transformer
            # to inject site-specific request rewriting; we replay every
            # registered transformer across all known pages right after.
            await self._run_on_engine_ready_hooks()
            await self._install_url_transformers_on_all_pages()

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
        """Download binary content via browser fetch, with direct HTTP fallback."""
        browser_fetch_timeout_ms = max(1, self._config.download.read_timeout_ms - 1_000)
        try:
            result = await self._evaluate_request_with_cf_retry(
                url=url,
                expression="""async ([url, headers, timeoutMs]) => {
                    const __comixMethod = 'GET';
""" + _URL_TRANSFORMER_JS_PRELUDE + """
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), timeoutMs);
                    let resp;
                    try {
                        resp = await fetch(url, {
                            headers: headers || {},
                            signal: controller.signal,
                        });
                    } catch (e) {
                        if (e && e.name === 'AbortError') {
                            throw new Error(`Fetch timed out after ${timeoutMs}ms`);
                        }
                        throw e;
                    } finally {
                        clearTimeout(timer);
                    }
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
                arg=[url, {"Referer": referer} if referer else {}, browser_fetch_timeout_ms],
                action=f"Fetching binary response from {url}",
                use_page_pool=True,
            )
        except Exception as exc:
            if _is_direct_bytes_fallback_candidate(exc) and _direct_bytes_url_supported(url):
                logger.debug("Browser binary fetch failed for %s; retrying with direct HTTP: %s", url, exc)
                return await self._get_bytes_direct(url, referer=referer)
            raise
        return base64.b64decode(cast("str", result))

    async def get_scrambled_image_bytes(
        self,
        url: str,
        *,
        width: int | None = None,
        height: int | None = None,
        referer: str | None = None,
    ) -> bytes:
        """Render a comix.to scrambled image through the site's reader canvas."""
        if not self._started:
            await self.start()
        await self.ensure_cf_clearance()

        page = await self.acquire_page()
        try:
            await self._render_scrambled_image_on_page(page, url=url, width=width, height=height, referer=referer)
            screenshot = await self._screenshot_scrambled_canvas(page, url=url)
        except Exception as exc:
            retry_url = self._cache_busted_url(url)
            if retry_url != url and self._page_is_healthy(page):
                try:
                    await self._render_scrambled_image_on_page(
                        page,
                        url=retry_url,
                        width=width,
                        height=height,
                        referer=referer,
                    )
                    screenshot = await self._screenshot_scrambled_canvas(page, url=retry_url)
                except Exception as retry_exc:
                    typed = _translate_browser_error(retry_exc)
                    if self._page_is_healthy(page):
                        self.release_page(page)
                    else:
                        await self._replace_dead_page(page)
                    if typed is retry_exc:
                        raise
                    raise typed from retry_exc
                else:
                    self.release_page(page)
                    return screenshot

            typed = _translate_browser_error(exc)
            if self._page_is_healthy(page):
                self.release_page(page)
            else:
                await self._replace_dead_page(page)
            if typed is exc:
                raise
            raise typed from exc
        else:
            self.release_page(page)
            return screenshot

    async def _render_scrambled_image_on_page(
        self,
        page: Page,
        *,
        url: str,
        width: int | None,
        height: int | None,
        referer: str | None,
    ) -> dict[str, object]:
        """Render a scrambled image URL into a reusable DOM canvas on *page*."""
        result = await self._evaluate_with_timeout(
            page,
            """async ([url, width, height, headers]) => {
                window.__comixRenderScrambledImage = window.__comixRenderScrambledImage || async function(
                    imageUrl,
                    imageWidth,
                    imageHeight
                ) {
                    async function loadRenderer() {
                        if (typeof globalThis.$r === 'function') return globalThis.$r;

                        const scripts = Array.from(document.querySelectorAll('script[src]'));
                        const mainScript = scripts.find((script) => {
                            const src = script.getAttribute('src') || '';
                            return src.includes('/assets/build/')
                                && src.includes('/dist/main-')
                                && src.endsWith('.js');
                        });
                        if (!mainScript) {
                            throw new Error('Could not find comix.to main script.');
                        }

                        const mainUrl = new URL(mainScript.getAttribute('src'), window.location.href).href;
                        const mainText = await fetch(mainUrl).then((resp) => resp.text());
                        const secureMatch = mainText.match(/"([^"]*secure-[^"]+\\.js)"/)
                            || mainText.match(/from"\\.\\/(secure-[^"]+\\.js)"/);
                        if (!secureMatch) {
                            throw new Error('Could not find comix.to secure image module.');
                        }

                        const securePath = secureMatch[1];
                        const secureUrl = securePath.startsWith('./')
                            ? new URL(securePath.slice(2), mainUrl).href
                            : securePath.startsWith('/')
                                ? new URL(securePath, window.location.origin).href
                                : new URL(securePath, mainUrl).href;
                        const secureModule = await import(secureUrl);
                        const renderer = secureModule.t || globalThis.$r;
                        if (typeof renderer !== 'function') {
                            throw new Error('Could not find comix.to secure image renderer.');
                        }
                        return renderer;
                    }

                    const renderer = await loadRenderer();
                    const old = document.getElementById('__comix_scrambled_wrap');
                    if (old) old.remove();
                    const wrap = document.createElement('div');
                    wrap.id = '__comix_scrambled_wrap';
                    wrap.style.cssText = [
                        'position:absolute',
                        'left:0',
                        'top:0',
                        'z-index:2147483647',
                        'background:#fff',
                    ].join(';');
                    const canvas = document.createElement('canvas');
                    canvas.id = '__comix_scrambled_canvas';
                    canvas.className = 'rpage-page__img';
                    if (Number.isFinite(imageWidth) && imageWidth > 0) canvas.width = imageWidth;
                    if (Number.isFinite(imageHeight) && imageHeight > 0) canvas.height = imageHeight;
                    wrap.appendChild(canvas);
                    document.body.appendChild(wrap);
                    await renderer(imageUrl, canvas);
                    return {
                        ok: true,
                        selector: '#__comix_scrambled_canvas',
                        width: canvas.width,
                        height: canvas.height,
                    };
                };

                return await window.__comixRenderScrambledImage(url, width, height);
            }""",
            [url, width, height, {"Referer": referer} if referer else {}],
            timeout_ms=self._config.download.read_timeout_ms,
            action=f"Rendering scrambled image from {url}",
        )
        if not isinstance(result, dict) or result.get("selector") != "#__comix_scrambled_canvas":
            raise RuntimeError(f"Scrambled image renderer returned an invalid result for {url}.")
        return cast("dict[str, object]", result)

    async def _screenshot_scrambled_canvas(self, page: Page, *, url: str) -> bytes:
        """Capture the rendered scrambled canvas as PNG bytes."""
        element = await page.query_selector("#__comix_scrambled_canvas")
        if element is None:
            raise RuntimeError(f"Scrambled image canvas was not found after rendering {url}.")
        return await self._run_with_timeout(
            element.screenshot(
                type="png",
                timeout=self._config.download.read_timeout_ms,
                style=None,
            ),
            timeout_ms=self._config.download.read_timeout_ms,
            action=f"Capturing rendered scrambled image from {url}",
        )

    @staticmethod
    def _cache_busted_url(url: str) -> str:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}r=1"

    async def _get_bytes_direct(self, url: str, *, referer: str | None = None) -> bytes:
        """Fetch binary content with direct HTTP in a worker thread."""
        return await asyncio.to_thread(self._get_bytes_direct_sync, url, referer=referer)

    def _get_bytes_direct_sync(self, url: str, *, referer: str | None = None) -> bytes:
        """Blocking direct HTTP implementation used when in-browser fetch is flaky."""
        headers = {
            "Accept": _DIRECT_BYTES_ACCEPT,
            "User-Agent": _DIRECT_BYTES_USER_AGENT,
        }
        if referer:
            headers["Referer"] = referer

        request = Request(url, headers=headers)
        timeout = self._config.download.read_timeout_ms / 1000
        try:
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status == 403:
                    raise Http403Error(f"HTTP 403 while fetching binary response from {url}")
                if status >= 400:
                    raise RuntimeError(f"HTTP {status} while fetching binary response from {url}")
                return cast("bytes", response.read())
        except HTTPError as exc:
            if exc.code == 403:
                raise Http403Error(f"HTTP 403 while fetching binary response from {url}") from exc
            raise RuntimeError(f"HTTP {exc.code} while fetching binary response from {url}") from exc
        except TimeoutError as exc:
            raise BrowserTimeoutError(
                f"Direct binary response from {url} timed out after {self._config.download.read_timeout_ms}ms.",
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise BrowserTimeoutError(
                    f"Direct binary response from {url} timed out after {self._config.download.read_timeout_ms}ms.",
                ) from exc
            raise RuntimeError(f"Direct binary response from {url} failed: {exc.reason}") from exc

    async def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        """POST JSON via page.evaluate(fetch())."""
        result = await self._evaluate_request_with_cf_retry(
            url=url,
            expression="""async ([url, body]) => {
                const __comixMethod = 'POST';
                const __comixBody = body;
""" + _JSON_REQUESTER_JS_PRELUDE + """
""" + _URL_TRANSFORMER_JS_PRELUDE + """
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

        Outbound requests run through the registered URL transformer
        chain (see :meth:`register_url_transformer`); site-specific
        signing / auth lives in those transformers and never inside
        this engine.
        """
        result = await self._evaluate_request_with_cf_retry(
            url=url,
            expression="""async (url) => {
                const __comixMethod = 'GET';
                const __comixBody = undefined;
""" + _JSON_REQUESTER_JS_PRELUDE + """
""" + _URL_TRANSFORMER_JS_PRELUDE + """
                const resp = await fetch(url);
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                return await resp.json();
            }""",
            arg=url,
            action=f"Fetching JSON from {url}",
            use_page_pool=use_page_pool,
        )
        return cast("dict[str, object]", result)

    async def _create_pooled_page(self, *, action: str, navigate_to_base: bool) -> Page:
        """Create a pooled page and replay registered URL transformers on it."""
        page = await super()._create_pooled_page(action=action, navigate_to_base=navigate_to_base)
        if navigate_to_base and self._url_transformer_iifes:
            await self._install_url_transformers_on_page(page)
        return page

    async def _init_pool_pages(self, url: str) -> None:
        """Navigate pool pages to *url*. Adapter setup runs after this via
        :meth:`_install_url_transformers_on_all_pages`.
        """
        await super()._init_pool_pages(url)

    async def _install_url_transformers_on_all_pages(self) -> None:
        """Replay every registered URL transformer on the main page and the pool.

        Called once per session, right after the on-engine-ready hooks
        run. Hooks may register transformers; this helper makes sure
        the registrations are reflected on every page that already
        exists when the hooks finish. Pages created later go through
        :meth:`_create_pooled_page`, which installs transformers
        directly.
        """
        if not self._url_transformer_iifes:
            return

        if self._page is not None:
            await self._install_url_transformers_on_page(self._page)

        pages: list[Page] = []
        while not self._page_pool.empty():
            try:
                pages.append(self._page_pool.get_nowait())
            except asyncio.QueueEmpty:
                break

        if pages:
            await asyncio.gather(
                *[self._install_url_transformers_on_page(p) for p in pages],
            )
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
