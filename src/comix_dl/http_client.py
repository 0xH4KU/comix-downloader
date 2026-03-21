"""HTTP client with cookie management — reads cookies from the user's real browser.

Two cookie sources:
1. **Automatic** — ``browser_cookie3`` reads cookies from Chrome/Firefox.
2. **Manual** — user provides a ``cookies.json`` file.

All HTTP requests use ``httpx`` with the extracted cookies.  No Playwright,
no automation markers, no Cloudflare detection.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import httpx

from comix_dl.config import CONFIG

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_COOKIE_JSON_PATH = CONFIG.browser.cookie_dir / CONFIG.browser.cookie_file


class HttpClient:
    """HTTP client that uses cookies from the user's real browser.

    Usage::

        async with HttpClient() as client:
            html = await client.get_text("https://comix.to/...")
            data = await client.post_json("https://comix.to/apo/", {...})
            raw  = await client.get_bytes("https://comix.to/image.webp")
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Create the HTTP client with cookies from the user's browser."""
        cookies = load_cookies()
        user_agent = _detect_chrome_ua() or CONFIG.browser.user_agent

        self._client = httpx.AsyncClient(
            cookies=cookies,
            headers={
                "User-Agent": user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Origin": CONFIG.service.base_url,
                "Referer": CONFIG.service.base_url + "/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "sec-ch-ua-platform": '"macOS"',
            },
            follow_redirects=True,
            timeout=httpx.Timeout(
                connect=CONFIG.download.connect_timeout_ms / 1000,
                read=CONFIG.download.read_timeout_ms / 1000,
                write=30.0,
                pool=30.0,
            ),
        )
        logger.info("HTTP client started with %d cookies (UA: %s)", len(cookies), user_agent[:60])

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> HttpClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # -- public API -----------------------------------------------------------

    async def get_text(self, url: str) -> str:
        """GET a URL and return the response text."""
        client = self._ensure_client()
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    async def get_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        """GET binary content."""
        client = self._ensure_client()
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.content

    async def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        """POST JSON and return the parsed response."""
        client = self._ensure_client()
        resp = await client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    def _ensure_client(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("HttpClient not started. Use 'async with HttpClient() as client:'")
        return self._client


# -- cookie loading (public for CLI login command) ----------------------------


def load_cookies() -> dict[str, str]:
    """Load cookies from the best available source.

    Priority:
    1. Manual ``cookies.json`` file in config dir.
    2. Automatic extraction from Chrome via ``browser_cookie3``.
    """
    # 1. Try manual cookie file
    cookies = _load_from_json(_COOKIE_JSON_PATH)
    if cookies:
        logger.info("Loaded %d cookies from %s", len(cookies), _COOKIE_JSON_PATH)
        return cookies

    # 2. Try browser_cookie3 (reads from user's real browser)
    cookies = _load_from_browser()
    if cookies:
        logger.info("Loaded %d cookies from browser", len(cookies))
        # Cache them for next run
        save_cookies_json(cookies)
        return cookies

    logger.warning(
        "No cookies found. Run 'comix-dl login' to set up cookies, "
        "or place a cookies.json in %s",
        CONFIG.browser.cookie_dir,
    )
    return {}


def save_cookies_json(cookies: dict[str, str]) -> None:
    """Cache cookies to the JSON file."""
    path = _COOKIE_JSON_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(cookies, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.debug("Saved %d cookies to %s", len(cookies), path)
    except OSError as exc:
        logger.warning("Failed to save cookies: %s", exc)


def _load_from_json(path: Path) -> dict[str, str]:
    """Load cookies from a JSON file (list-of-dicts or flat dict format)."""
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read cookie file %s: %s", path, exc)
        return {}

    # Flat dict format: {"name": "value", ...}
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items()}

    # List-of-dicts format (from browser extensions / EditThisCookie)
    if isinstance(raw, list):
        cookies: dict[str, str] = {}
        for entry in raw:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("Name", "")
                value = entry.get("value") or entry.get("Value", "")
                domain = entry.get("domain") or entry.get("Domain", "")
                if name and value and "comix" in str(domain):
                    cookies[name] = value
        return cookies

    return {}


def _load_from_browser() -> dict[str, str]:
    """Read comix.to cookies from the user's Chrome using browser_cookie3."""
    try:
        import browser_cookie3
    except ImportError:
        logger.debug("browser_cookie3 not installed, cannot read browser cookies")
        return {}

    # Try Chrome
    try:
        cj = browser_cookie3.chrome(domain_name=".comix.to")
        cookies = {c.name: c.value for c in cj if c.value}
        if cookies:
            return cookies
    except Exception as exc:
        logger.debug("Failed to read Chrome cookies: %s", exc)

    # Try Firefox
    try:
        cj = browser_cookie3.firefox(domain_name=".comix.to")
        cookies_ff = {c.name: c.value for c in cj if c.value}
        if cookies_ff:
            return cookies_ff
    except Exception as exc:
        logger.debug("Failed to read Firefox cookies: %s", exc)

    return {}


def _detect_chrome_ua() -> str | None:
    """Detect the real Chrome User-Agent from the system.

    CF ties ``cf_clearance`` to the UA that solved the challenge.  If our
    UA doesn't match, CF returns 403 even with a valid cookie.
    """
    import platform
    import re
    import subprocess

    system = platform.system()
    chrome_version: str | None = None

    try:
        if system == "Darwin":
            result = subprocess.run(
                [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "--version",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            match = re.search(r"([\d.]+)", result.stdout)
            if match:
                chrome_version = match.group(1)
        elif system == "Linux":
            result = subprocess.run(
                ["google-chrome", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            match = re.search(r"([\d.]+)", result.stdout)
            if match:
                chrome_version = match.group(1)
    except Exception:
        pass

    if not chrome_version:
        return None

    ua = (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36"
    )
    logger.debug("Detected Chrome UA: %s", ua)
    return ua

