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
) -> bytes:
    """Render a comix.to scrambled image through the site's reader canvas."""
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
    "probe_service_access",
    "render_scrambled_image",
    "render_scrambled_image_on_page",
    "screenshot_scrambled_canvas",
]
