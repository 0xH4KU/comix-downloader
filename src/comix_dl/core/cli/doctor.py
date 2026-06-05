"""Environment and remote-chain diagnostics for the CLI."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.panel import Panel

from comix_dl.core.application.session import open_application_session
from comix_dl.core.cli.display import console, format_bytes
from comix_dl.core.settings import SettingsRepository

if TYPE_CHECKING:
    from comix_dl.core.models import ChapterInfo, SearchResult


@dataclass(frozen=True)
class _CheckResult:
    label: str
    ok: bool
    message: str


def _print_check(result: _CheckResult) -> None:
    marker = "[green]✓[/green]" if result.ok else "[red]✗[/red]"
    console.print(f"  {marker} {result.label}: {result.message}")


def run_doctor() -> int:
    """Run local environment diagnostics."""
    import platform
    import shutil

    from comix_dl import sites
    from comix_dl.core.mirror_resolver import MirrorStateRepository

    console.print()
    console.print(Panel("[bold]comix-downloader — Diagnostics[/bold]", border_style="cyan"))
    all_ok = True

    version_ok = sys.version_info >= (3, 11)
    _print_check(_CheckResult(
        "Python",
        version_ok,
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    ))
    all_ok &= version_ok

    for module, name in [
        ("playwright", "playwright"),
        ("PIL", "Pillow"),
        ("rich", "rich"),
    ]:
        try:
            __import__(module)
            _print_check(_CheckResult(name, True, "installed"))
        except ImportError:
            _print_check(_CheckResult(name, False, f"install with: pip install {name}"))
            all_ok = False

    if platform.system() == "Darwin":
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    else:
        chrome = (
            shutil.which("google-chrome")
            or shutil.which("google-chrome-stable")
            or shutil.which("chromium-browser")
            or shutil.which("chromium")
            or ""
        )
    chrome_ok = bool(chrome and Path(chrome).exists())
    _print_check(_CheckResult(
        "Chrome",
        chrome_ok,
        chrome if chrome_ok else "not found — install Google Chrome",
    ))
    all_ok &= chrome_ok

    settings = SettingsRepository().load()
    out = Path(settings.output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        _print_check(_CheckResult("Output", True, str(out)))
    except OSError:
        _print_check(_CheckResult("Output", False, f"{out} (cannot create)"))
        all_ok = False

    try:
        adapter = sites.get_active()
    except Exception as exc:
        _print_check(_CheckResult("Site adapter", False, f"not available ({exc})"))
        all_ok = False
    else:
        _print_check(_CheckResult("Site adapter", True, adapter.name))
        mirror_state = MirrorStateRepository().load(adapter.name)
        if mirror_state.active:
            console.print(f"      active mirror: [cyan]{mirror_state.active}[/cyan]")
        else:
            console.print(f"      active mirror: [dim]none cached, will use {adapter.mirrors[0]}[/dim]")
        recent = list(reversed(mirror_state.history[-3:]))
        if recent:
            console.print("      recent probes:")
            for record in recent:
                marker = "[green]ok[/green]" if record.succeeded else "[red]fail[/red]"
                console.print(f"        {marker}  {record.mirror}  ({record.checked_at})")

    console.print()
    if all_ok:
        console.print("[bold green]✓ All OK — ready to download![/bold green]")
    else:
        console.print("[bold red]✗ Issues found — fix the above before continuing[/bold red]")
    return 0 if all_ok else 1


async def run_deep_doctor(
    *,
    mirror: str | None = None,
    query: str = "one piece",
) -> int:
    """Run browser-backed diagnostics against the active remote chain."""
    console.print()
    console.print(Panel("[bold]comix-downloader — Deep Diagnostics[/bold]", border_style="cyan"))

    try:
        async with open_application_session(mirror_override=mirror) as session:
            _print_check(_CheckResult("Chrome/CDP session", True, "connected"))

            with console.status("[bold cyan]Testing search API…"):
                results = await session.search(query, limit=1)
            if not results:
                _print_check(_CheckResult("Search API", False, f"no results for '{query}'"))
                return 1
            first = results[0]
            _print_check(_CheckResult("Search API", True, _format_search_result(first)))

            with console.status("[bold cyan]Testing series metadata…"):
                series = await session.load_series(first.hash_id)
            if not series.chapters:
                _print_check(_CheckResult("Series metadata", False, f"{series.title} has no chapters"))
                return 1
            _print_check(_CheckResult(
                "Series metadata",
                True,
                f"{series.title} ({len(series.chapters)} chapter(s))",
            ))

            chapter = _choose_smoke_chapter(series.chapters)
            with console.status("[bold cyan]Testing chapter image payload…"):
                chapter_images = await session.adapter.get_chapter_images(session.browser, chapter.chapter_id)
            if chapter_images is None or not chapter_images.pages:
                _print_check(_CheckResult("Chapter images", False, f"{chapter.title} returned no images"))
                return 1
            first_page = chapter_images.pages[0]
            _print_check(_CheckResult(
                "Chapter images",
                True,
                f"{chapter.title} ({len(chapter_images.pages)} image(s))",
            ))

            with console.status("[bold cyan]Testing sample image fetch…"):
                if first_page.scrambled:
                    payload = await session.browser.get_scrambled_image_bytes(
                        first_page.url,
                        width=first_page.width,
                        height=first_page.height,
                        referer=series.url,
                    )
                else:
                    payload = await session.browser.get_bytes(first_page.url, referer=series.url)
            _print_check(_CheckResult(
                "Sample image fetch",
                bool(payload),
                _format_payload_size(payload),
            ))
            return 0 if payload else 1
    except Exception as exc:
        _print_check(_CheckResult(_classify_deep_failure(exc), False, str(exc)))
        return 1


def _format_search_result(result: SearchResult) -> str:
    return f"{result.title} ({result.hash_id})"


def _format_payload_size(payload: bytes) -> str:
    if not payload:
        return "empty response"
    return f"{len(payload)} bytes ({format_bytes(len(payload))})"


def _choose_smoke_chapter(chapters: list[ChapterInfo]) -> ChapterInfo:
    return chapters[0]


def _classify_deep_failure(exc: Exception) -> str:
    message = str(exc).lower()
    if "chrome" in message or "cdp" in message or "browser" in message:
        return "Chrome/CDP session"
    if "search" in message or "signing" in message:
        return "Search API"
    if "chapter" in message:
        return "Chapter images"
    if "image" in message or "binary" in message:
        return "Sample image fetch"
    return "Deep diagnostics"


__all__ = ["run_deep_doctor", "run_doctor"]
