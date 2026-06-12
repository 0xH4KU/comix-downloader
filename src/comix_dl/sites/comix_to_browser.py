"""Browser hooks for comix.to-specific Cloudflare probes and image rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from playwright.async_api import Page

    from comix_dl.core.engines.cdp_browser import CdpBrowser


async def probe_service_access(engine: CdpBrowser, page: Page) -> bool:
    """Verify that the browser context can fetch the comix.to JSON API."""
    base_url = getattr(engine, "_base_url", None)
    if base_url is None:
        return False
    probe_url = f"{base_url}/api/v1/manga?keyword=test&limit=1"
    try:
        result = await engine._evaluate_with_timeout(
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
            timeout_ms=engine._config.browser.timeout_ms,
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


async def render_scrambled_image(
    engine: CdpBrowser,
    page: Page,
    url: str,
    *,
    width: int | None = None,
    height: int | None = None,
    referer: str | None = None,
    reader_url: str | None = None,
    page_index: int | None = None,
) -> bytes:
    """Render a comix.to scrambled image through the reader DOM when possible."""
    if not reader_url or page_index is None:
        raise RuntimeError(f"Scrambled image {url} is missing reader context.")

    try:
        return await capture_reader_dom_image(
            page,
            engine=engine,
            url=url,
            reader_url=reader_url,
            page_index=page_index,
        )
    except Exception:
        return await render_scrambled_image_via_legacy_renderer(
            engine,
            page,
            url,
            width=width,
            height=height,
            referer=referer,
        )


async def capture_reader_dom_image(
    page: Page,
    *,
    engine: CdpBrowser,
    url: str,
    reader_url: str,
    page_index: int,
) -> bytes:
    """Capture the hydrated reader DOM element for one scrambled page."""
    del page

    async with engine._main_page_lock:
        reader_page = await _navigate_reader_page(engine, reader_url)
        result = await engine._evaluate_with_timeout(
            reader_page,
            """async ([pageIndex, timeoutMs]) => {
                const deadline = Date.now() + timeoutMs;
                const selector = `.rpage-page:nth-of-type(${pageIndex + 1}) .rpage-page__img`;

                while (Date.now() < deadline) {
                    if (document.body.classList.contains('rpage-body')) {
                        const pages = Array.from(document.querySelectorAll('.rpage-page'));
                        const target = pages[pageIndex];
                        if (target) {
                            target.scrollIntoView({ block: 'center' });
                            const image = target.querySelector('.rpage-page__img');
                            if (image instanceof HTMLImageElement) {
                                if (image.complete && image.naturalWidth > 0 && image.naturalHeight > 0) {
                                    return { ok: true, selector };
                                }
                            } else if (image instanceof HTMLCanvasElement) {
                                if (image.width > 0 && image.height > 0) {
                                    return { ok: true, selector };
                                }
                            } else if (image) {
                                return { ok: true, selector };
                            }
                        }
                    }
                    await new Promise((resolve) => setTimeout(resolve, 100));
                }

                throw new Error(`Reader DOM target was not found for page index ${pageIndex}.`);
            }""",
            [page_index, engine._config.download.read_timeout_ms],
            timeout_ms=engine._config.download.read_timeout_ms,
            action=f"Selecting reader DOM target for {reader_url}",
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError(
                f"Reader DOM target selection returned an invalid result for {reader_url}.",
            )

        selector = str(result.get("selector", "") or "")
        target = await reader_page.query_selector(selector)
        if target is None:
            raise RuntimeError(f"Reader DOM target was not found for {reader_url} page {page_index + 1}.")

        return await engine._run_with_timeout(
            target.screenshot(
                type="png",
                timeout=engine._config.download.read_timeout_ms,
                style=None,
            ),
            timeout_ms=engine._config.download.read_timeout_ms,
            action=f"Capturing reader DOM image for {url}",
        )


async def _navigate_reader_page(engine: CdpBrowser, reader_url: str) -> Page:
    """Navigate the shared main page to the hydrated reader view."""
    for attempt in range(2):
        reader_page = await engine._ensure_page()
        await engine._goto_with_timeout(reader_page, reader_url, action="Navigating reader page")
        if await engine._is_cf_challenge(reader_page):
            if attempt == 0:
                await engine._refresh_cf_clearance(
                    reason=f"Cloudflare challenge detected while loading reader page {reader_url}.",
                )
                continue
            raise RuntimeError(
                f"Cloudflare challenge persisted after clearance refresh for reader page {reader_url}.",
            )
        return reader_page
    raise AssertionError("Reader page navigation retry loop exited unexpectedly")


async def render_scrambled_image_via_legacy_renderer(
    engine: CdpBrowser,
    page: Page,
    url: str,
    *,
    width: int | None = None,
    height: int | None = None,
    referer: str | None = None,
) -> bytes:
    """Render a comix.to scrambled image through the legacy imported renderer."""
    try:
        await render_scrambled_image_on_page(page, engine=engine, url=url, width=width, height=height, referer=referer)
        return await screenshot_scrambled_canvas(page, engine=engine, url=url)
    except Exception:
        retry_url = cache_busted_url(url)
        if retry_url == url or not engine._page_is_healthy(page):
            raise
        await render_scrambled_image_on_page(
            page,
            engine=engine,
            url=retry_url,
            width=width,
            height=height,
            referer=referer,
        )
        return await screenshot_scrambled_canvas(page, engine=engine, url=retry_url)


async def render_scrambled_image_on_page(
    page: Page,
    *,
    engine: CdpBrowser,
    url: str,
    width: int | None,
    height: int | None,
    referer: str | None,
) -> dict[str, object]:
    """Render a scrambled image URL into a reusable DOM canvas on *page*."""
    result = await engine._evaluate_with_timeout(
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
                function isInstance(value, ctorName) {
                    const ctor = globalThis[ctorName];
                    return typeof ctor === 'function' && value instanceof ctor;
                }

                function loadImage(src) {
                    return new Promise((resolve, reject) => {
                        const image = new Image();
                        image.onload = () => resolve(image);
                        image.onerror = () => reject(new Error('Could not load rendered image source.'));
                        image.src = src;
                    });
                }

                async function toDrawableImage(source) {
                    if (source == null) return source;
                    if (isInstance(source, 'Response')) return await toDrawableImage(await source.blob());
                    if (isInstance(source, 'Blob')) {
                        if (typeof createImageBitmap === 'function') return await createImageBitmap(source);
                        const objectUrl = URL.createObjectURL(source);
                        try {
                            return await loadImage(objectUrl);
                        } finally {
                            URL.revokeObjectURL(objectUrl);
                        }
                    }
                    if (isInstance(source, 'ArrayBuffer') || ArrayBuffer.isView(source)) {
                        return await toDrawableImage(new Blob([source]));
                    }
                    if (typeof source === 'string') return await loadImage(source);
                    if (
                        isInstance(source, 'HTMLCanvasElement')
                        || isInstance(source, 'HTMLImageElement')
                        || isInstance(source, 'SVGImageElement')
                        || isInstance(source, 'ImageBitmap')
                        || isInstance(source, 'ImageData')
                        || isInstance(source, 'OffscreenCanvas')
                    ) {
                        if (isInstance(source, 'HTMLImageElement') && !source.complete) {
                            await new Promise((resolve, reject) => {
                                source.onload = () => resolve();
                                source.onerror = () => reject(new Error('Rendered image did not finish loading.'));
                            });
                        }
                        return source;
                    }
                    if (typeof source === 'object') {
                        const nested = source.canvas
                            || source.image
                            || source.bitmap
                            || source.blob
                            || source.url
                            || source.src
                            || source.data;
                        if (nested && nested !== source) return await toDrawableImage(nested);
                    }
                    throw new Error('Secure image renderer returned an unsupported result.');
                }

                function drawableSize(drawable) {
                    return {
                        width: drawable.naturalWidth || drawable.videoWidth || drawable.width || 0,
                        height: drawable.naturalHeight || drawable.videoHeight || drawable.height || 0,
                    };
                }

                async function drawRenderedImage(source, targetCanvas) {
                    if (source == null || source === targetCanvas) return;
                    const drawable = await toDrawableImage(source);
                    if (drawable == null || drawable === targetCanvas) return;
                    const context = targetCanvas.getContext('2d');
                    if (!context) throw new Error('Could not create scrambled image canvas context.');

                    if (isInstance(drawable, 'ImageData')) {
                        if (!targetCanvas.width) targetCanvas.width = drawable.width;
                        if (!targetCanvas.height) targetCanvas.height = drawable.height;
                        context.putImageData(drawable, 0, 0);
                        return;
                    }

                    const size = drawableSize(drawable);
                    if ((!Number.isFinite(imageWidth) || imageWidth <= 0) && size.width > 0) {
                        targetCanvas.width = size.width;
                    }
                    if ((!Number.isFinite(imageHeight) || imageHeight <= 0) && size.height > 0) {
                        targetCanvas.height = size.height;
                    }
                    if (targetCanvas.width <= 0 || targetCanvas.height <= 0) {
                        throw new Error('Secure image renderer returned a drawable without dimensions.');
                    }
                    context.clearRect(0, 0, targetCanvas.width, targetCanvas.height);
                    context.drawImage(drawable, 0, 0, targetCanvas.width, targetCanvas.height);
                    if (drawable && typeof drawable.close === 'function') drawable.close();
                }

                function shouldTryLegacyCanvasRenderer(error) {
                    const message = String((error && (error.stack || error.message)) || error || '');
                    return /canvas|HTMLCanvasElement|getContext|drawImage|putImageData/i.test(message);
                }

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
                try {
                    const controller = new AbortController();
                    const rendered = await renderer(imageUrl, controller.signal);
                    await drawRenderedImage(rendered, canvas);
                } catch (error) {
                    if (!shouldTryLegacyCanvasRenderer(error)) throw error;
                    try {
                        await renderer(imageUrl, canvas);
                    } catch (_legacyError) {
                        throw error;
                    }
                }
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
        timeout_ms=engine._config.download.read_timeout_ms,
        action=f"Rendering scrambled image from {url}",
    )
    if not isinstance(result, dict) or result.get("selector") != "#__comix_scrambled_canvas":
        raise RuntimeError(f"Scrambled image renderer returned an invalid result for {url}.")
    return cast("dict[str, object]", result)


async def screenshot_scrambled_canvas(page: Page, *, engine: CdpBrowser, url: str) -> bytes:
    """Capture the rendered scrambled canvas as PNG bytes."""
    element = await page.query_selector("#__comix_scrambled_canvas")
    if element is None:
        raise RuntimeError(f"Scrambled image canvas was not found after rendering {url}.")
    return await engine._run_with_timeout(
        element.screenshot(
            type="png",
            timeout=engine._config.download.read_timeout_ms,
            style=None,
        ),
        timeout_ms=engine._config.download.read_timeout_ms,
        action=f"Capturing rendered scrambled image from {url}",
    )


def cache_busted_url(url: str) -> str:
    """Append a lightweight cache buster used for decode retry."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}r=1"


__all__ = [
    "cache_busted_url",
    "capture_reader_dom_image",
    "probe_service_access",
    "render_scrambled_image",
    "render_scrambled_image_on_page",
    "render_scrambled_image_via_legacy_renderer",
    "screenshot_scrambled_canvas",
]
