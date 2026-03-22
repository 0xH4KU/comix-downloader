"""Command-line interface for comix-downloader.

Supports both interactive (menu) and non-interactive (CLI flags) modes.

Usage::

    comix-dl                    # Interactive main menu
    comix-dl "query"            # Quick search shortcut
    comix-dl search "query"     # Search with subcommand
    comix-dl download URL       # Non-interactive download
    comix-dl info URL           # Show manga metadata
    comix-dl list               # List downloaded manga
    comix-dl clean              # Remove raw image dirs
    comix-dl history            # Show download history
    comix-dl doctor             # Diagnostics
    comix-dl settings           # View / edit settings
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import re
import signal
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from comix_dl import __version__
from comix_dl.cdp_browser import CdpBrowser
from comix_dl.comix_service import ChapterInfo, ComixService, SearchResult, SeriesInfo
from comix_dl.config import CONFIG
from comix_dl.downloader import Downloader, DownloadProgress
from comix_dl.settings import Settings, load_settings, save_settings

console = Console()
_shutdown_requested = False

BANNER = r"""
  ██████╗ ██████╗ ███╗   ███╗██╗██╗  ██╗
 ██╔════╝██╔═══██╗████╗ ████║██║╚██╗██╔╝
 ██║     ██║   ██║██╔████╔██║██║ ╚███╔╝
 ██║     ██║   ██║██║╚██╔╝██║██║ ██╔██╗
 ╚██████╗╚██████╔╝██║ ╚═╝ ██║██║██╔╝ ██╗
  ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝╚═╝  ╚═╝
"""


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="comix-dl",
        description="A focused comix.to manga downloader with Cloudflare bypass.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"comix-dl v{__version__}")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress output (errors only)")

    sub = parser.add_subparsers(dest="command")

    # search
    p_search = sub.add_parser("search", help="Search for manga")
    p_search.add_argument("query", help="Search query")

    # download
    p_dl = sub.add_parser("download", help="Download manga by URL or slug")
    p_dl.add_argument("url", help="Manga URL or slug")
    p_dl.add_argument("-c", "--chapters", default="all", help="Chapter selection: all, 1-5, 1,3,5 (default: all)")
    p_dl.add_argument("-f", "--format", choices=["pdf", "cbz", "both"], default=None, help="Output format")
    p_dl.add_argument("-o", "--output", default=None, help="Output directory")
    p_dl.add_argument("--no-optimize", action="store_true", help="Disable image optimization")

    # info
    p_info = sub.add_parser("info", help="Show manga metadata without downloading")
    p_info.add_argument("url", help="Manga URL or slug")

    # list
    sub.add_parser("list", help="List downloaded manga and chapters")

    # clean
    p_clean = sub.add_parser("clean", help="Remove raw image directories after conversion")
    p_clean.add_argument("--force", action="store_true", help="Skip confirmation")

    # history
    p_hist = sub.add_parser("history", help="Show download history")
    p_hist.add_argument("action", nargs="?", choices=["clear"], help="clear: delete all history")

    # doctor
    sub.add_parser("doctor", help="Run environment diagnostics")

    # settings
    sub.add_parser("settings", help="View and edit settings")

    return parser


def main() -> int:
    """CLI entry point."""
    parser = _build_parser()

    # Special case: bare `comix-dl "query"` (no subcommand, positional arg)
    if (
        len(sys.argv) == 2
        and not sys.argv[1].startswith("-")
        and sys.argv[1] not in ("search", "download", "info", "list", "clean", "history", "doctor", "settings")
    ):
        logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
        return _run_async(_flow_search(sys.argv[1]))

    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s:%(name)s:%(message)s")

    # Quiet mode
    if getattr(args, "quiet", False):
        console.quiet = True

    if args.command == "search":
        return _run_async(_flow_search(args.query))

    if args.command == "download":
        settings = load_settings()
        fmt = args.format or settings.default_format
        output = args.output or settings.output_dir
        optimize = settings.optimize_images and not args.no_optimize
        return _run_async(_flow_noninteractive_download(args.url, args.chapters, fmt, output, optimize=optimize))

    if args.command == "info":
        return _run_async(_flow_info(args.url))

    if args.command == "list":
        return _flow_list()

    if args.command == "clean":
        return _flow_clean(force=args.force)

    if args.command == "history":
        return _flow_history(action=args.action)

    if args.command == "doctor":
        return _run_doctor()

    if args.command == "settings":
        _flow_settings()
        return 0

    # No subcommand → interactive main menu
    return _main_menu()


def _run_async(coro: object) -> int:
    """Run an async coroutine with Ctrl+C handling."""
    global _shutdown_requested
    _shutdown_requested = False

    loop = asyncio.new_event_loop()

    def _on_sigint(*_: object) -> None:
        global _shutdown_requested
        _shutdown_requested = True
        console.print("\n[yellow]⚠ Ctrl+C — finishing current downloads then stopping…[/yellow]")

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        return loop.run_until_complete(coro)  # type: ignore[arg-type]
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return 130
    finally:
        loop.close()
        signal.signal(signal.SIGINT, signal.SIG_DFL)


# -- Main Menu ----------------------------------------------------------------


def _main_menu() -> int:
    """Interactive main menu."""
    settings = load_settings()

    console.print(Text(BANNER, style="bold cyan"), highlight=False)
    console.print(
        Panel(
            f"[dim]v{__version__}[/dim]  ·  "
            f"Output: [cyan]{settings.output_dir}[/cyan]  ·  "
            f"Format: [cyan]{settings.default_format}[/cyan]",
            title="[bold]comix-downloader[/bold]",
            border_style="cyan",
        )
    )

    while True:
        console.print()
        console.print("[bold]What would you like to do?[/bold]\n")
        console.print("  [cyan]1[/cyan]  Search manga")
        console.print("  [cyan]2[/cyan]  Download by URL")
        console.print("  [cyan]3[/cyan]  My downloads")
        console.print("  [cyan]4[/cyan]  Download history")
        console.print("  [cyan]5[/cyan]  Settings")
        console.print("  [cyan]6[/cyan]  Doctor (diagnostics)")
        console.print("  [cyan]q[/cyan]  Exit")

        choice = Prompt.ask(
            "\n[bold]Choose[/bold]",
            choices=["1", "2", "3", "4", "5", "6", "q"],
            default="1",
            show_choices=False,
        )

        if choice == "q":
            console.print("[dim]Bye![/dim]")
            return 0

        if choice == "1":
            query = Prompt.ask("[bold]Search query[/bold]")
            if query.strip():
                _run_async(_flow_search(query.strip()))

        elif choice == "2":
            url = Prompt.ask("[bold]Manga URL or slug[/bold]")
            if url.strip():
                _run_async(_flow_url_download(url.strip()))

        elif choice == "3":
            _flow_list()

        elif choice == "4":
            _flow_history()

        elif choice == "5":
            _flow_settings()

        elif choice == "6":
            _run_doctor()


# -- Flow: Search & Download --------------------------------------------------


async def _flow_search(query: str) -> int:
    """Interactive search → select → download."""
    settings = load_settings()
    output_dir = Path(settings.output_dir)

    async with CdpBrowser() as browser:
        service = ComixService(browser)

        # 1. Search
        with console.status("[bold cyan]Searching…"):
            results = await service.search(query)

        if not results:
            console.print("[yellow]No results found.[/yellow]")
            return 0

        _print_search_table(results, query)

        # 2. Select series — loop allows re-selecting after viewing info
        info = None
        while True:
            choice = Prompt.ask(
                "\n[bold]Select manga[/bold] [dim](number, or 1i for info, q to quit)[/dim]",
                default="1",
            )
            if choice.lower() in ("q", "quit", "exit"):
                return 0

            # Parse info mode: "3i" → idx=2, show_info=True
            show_info = False
            raw = choice.strip().lower()
            if raw.endswith("i"):
                show_info = True
                raw = raw[:-1]

            try:
                idx = int(raw) - 1
                if idx < 0 or idx >= len(results):
                    console.print("[red]Invalid selection.[/red]")
                    continue
            except ValueError:
                console.print("[red]Invalid selection.[/red]")
                continue

            selected = results[idx]
            console.print(f"\n[bold cyan]→ {selected.title}[/bold cyan]")

            # Optional: show info panel when user typed e.g. "1i"
            if show_info:
                with console.status("[bold cyan]Loading info…"):
                    info = await service.get_series(selected.hash_id)

                meta_lines = [f"[bold]{info.title}[/bold]"]
                if info.description:
                    desc = info.description[:300]
                    if len(info.description) > 300:
                        desc += "…"
                    meta_lines.append(f"[dim]{desc}[/dim]")
                meta_lines.append("")
                meta_lines.append(f"[cyan]URL:[/cyan]       {info.url}")
                meta_lines.append(f"[cyan]Chapters:[/cyan]  {len(info.chapters)}")
                if info.authors:
                    meta_lines.append(f"[cyan]Authors:[/cyan]   {', '.join(info.authors)}")
                if info.genres:
                    meta_lines.append(f"[cyan]Genres:[/cyan]    {', '.join(info.genres)}")

                console.print(Panel(
                    "\n".join(meta_lines),
                    title="[bold]Manga Info[/bold]",
                    border_style="cyan",
                ))

                cont = Prompt.ask("[bold]Fetch chapters?[/bold] [dim](Y/n)[/dim]", default="y")
                if cont.lower() not in ("y", "yes", ""):
                    # Re-show search results and let user pick again
                    _print_search_table(results, query)
                    info = None
                    continue

                # info already loaded, break to chapter selection
                break
            else:
                # Direct selection — load chapters and break
                break

        # 3. Load chapters (if not already loaded by info display)
        if info is None:
            with console.status("[bold cyan]Loading chapters…"):
                info = await service.get_series(selected.hash_id)

        if not info.chapters:
            console.print("[yellow]No chapters found.[/yellow]")
            return 0

        _print_series_header(info)
        _print_chapters_table(info.chapters)

        # 4. Filter chapters (optional)
        filtered = _filter_chapters_interactive(info.chapters)

        # 5. Select chapters
        console.print()
        console.print("[dim]Examples: 1  ·  1-5  ·  1,3,5  ·  all  ·  q to quit[/dim]")
        ch_choice = Prompt.ask(
            "[bold]Select chapters[/bold]",
            default="all",
        )
        if ch_choice.lower() in ("q", "quit", "exit"):
            return 0

        to_download = _parse_chapter_selection(ch_choice, filtered)
        if not to_download:
            console.print("[red]No valid chapters selected.[/red]")
            return 1

        # Confirm selection
        console.print(
            f"\n[bold]Selected {len(to_download)} chapter(s):[/bold]"
        )
        for ch in to_download[:10]:
            console.print(f"  • {ch.title}")
        if len(to_download) > 10:
            console.print(f"  [dim]… and {len(to_download) - 10} more[/dim]")

        # 5. Format
        fmt = Prompt.ask(
            "[bold]Output format[/bold]",
            choices=["pdf", "cbz", "both"],
            default=settings.default_format,
        )

        # 6. Download
        await _download_chapters(browser, service, info.title, to_download, output_dir, fmt, settings)

    return 0


async def _flow_url_download(url: str) -> int:
    """Download from a manga URL (interactive mode)."""
    settings = load_settings()
    output_dir = Path(settings.output_dir)

    # Extract slug from the URL
    slug = url.rstrip("/").split("/")[-1]

    async with CdpBrowser() as browser:
        service = ComixService(browser)

        with console.status("[bold cyan]Fetching series info…"):
            try:
                results = await service.search(slug, limit=10)
                # Try exact slug match first, then partial title match
                matched = next((r for r in results if r.slug == slug), None)
                if not matched and results:
                    console.print(f"[yellow]Exact match not found for '{slug}'. Did you mean:[/yellow]\n")
                    _print_search_table(results, slug)
                    choice = Prompt.ask(
                        "\n[bold]Select manga[/bold] [dim](number, or q to quit)[/dim]",
                        default="1",
                    )
                    if choice.lower() in ("q", "quit", "exit"):
                        return 0
                    try:
                        matched = results[int(choice) - 1]
                    except (ValueError, IndexError):
                        console.print("[red]Invalid selection.[/red]")
                        return 1

                if not matched:
                    console.print("[yellow]Could not find manga. Try using search instead.[/yellow]")
                    return 1

                info = await service.get_series(matched.hash_id)
            except RuntimeError as exc:
                console.print(f"[red]{exc}[/red]")
                return 1

        _print_series_header(info)
        _print_chapters_table(info.chapters)

        filtered = _filter_chapters_interactive(info.chapters)

        console.print()
        console.print("[dim]Examples: 1  ·  1-5  ·  1,3,5  ·  all  ·  q to quit[/dim]")
        ch_choice = Prompt.ask("[bold]Select chapters[/bold]", default="all")
        if ch_choice.lower() in ("q", "quit", "exit"):
            return 0

        to_download = _parse_chapter_selection(ch_choice, filtered)
        if not to_download:
            console.print("[red]No valid chapters selected.[/red]")
            return 1

        console.print(
            f"\n[bold]Selected {len(to_download)} chapter(s):[/bold]"
        )
        for ch in to_download[:10]:
            console.print(f"  • {ch.title}")
        if len(to_download) > 10:
            console.print(f"  [dim]… and {len(to_download) - 10} more[/dim]")

        fmt = Prompt.ask(
            "[bold]Output format[/bold]",
            choices=["pdf", "cbz", "both"],
            default=settings.default_format,
        )

        await _download_chapters(browser, service, info.title, to_download, output_dir, fmt, settings)

    return 0


async def _flow_noninteractive_download(
    url: str, chapters_sel: str, fmt: str, output: str, *, optimize: bool = True,
) -> int:
    """Fully non-interactive download flow."""
    output_dir = Path(output)
    slug = url.rstrip("/").split("/")[-1]

    async with CdpBrowser() as browser:
        service = ComixService(browser)

        console.print(f"[bold]Searching for '{slug}'…[/bold]")
        results = await service.search(slug, limit=10)
        matched = next((r for r in results if r.slug == slug), results[0] if results else None)

        if not matched:
            console.print("[red]Manga not found.[/red]")
            return 1

        console.print(f"[bold cyan]→ {matched.title}[/bold cyan]")
        info = await service.get_series(matched.hash_id)

        if not info.chapters:
            console.print("[yellow]No chapters found.[/yellow]")
            return 0

        to_download = _parse_chapter_selection(chapters_sel, info.chapters)
        if not to_download:
            console.print("[red]No valid chapters selected.[/red]")
            return 1

        console.print(f"[bold]Downloading {len(to_download)} chapter(s) as {fmt.upper()}…[/bold]\n")
        settings = load_settings()
        await _download_chapters(
            browser, service, info.title, to_download, output_dir, fmt, settings,
            optimize=optimize,
        )

    return 0


# -- Flow: Settings -----------------------------------------------------------


def _flow_settings() -> None:
    """Interactive settings editor."""
    settings = load_settings()

    while True:
        console.print()
        console.print(Panel("[bold]Settings[/bold]", border_style="cyan"))
        delay_status = "[green]on[/green]" if settings.download_delay else "[red]off[/red]"
        optimize_status = "[green]on[/green]" if settings.optimize_images else "[red]off[/red]"
        console.print(f"  [cyan]1[/cyan]  Download directory:    [bold]{settings.output_dir}[/bold]")
        console.print(f"  [cyan]2[/cyan]  Default format:        [bold]{settings.default_format}[/bold]")
        console.print(f"  [cyan]3[/cyan]  Concurrent chapters:   [bold]{settings.concurrent_chapters}[/bold]")
        console.print(f"  [cyan]4[/cyan]  Concurrent images:     [bold]{settings.concurrent_images}[/bold]")
        console.print(f"  [cyan]5[/cyan]  Max retries:           [bold]{settings.max_retries}[/bold]")
        console.print(f"  [cyan]6[/cyan]  Download delay:        {delay_status}")
        console.print(f"  [cyan]7[/cyan]  Optimize images:       {optimize_status}")
        console.print("  [cyan]s[/cyan]  Save & return")
        console.print("  [cyan]q[/cyan]  Discard & return")

        choice = Prompt.ask("\n[bold]Edit setting[/bold]", default="s")

        if choice == "q":
            return

        if choice == "s":
            save_settings(settings)
            console.print("[green]✓ Settings saved![/green]")
            return

        if choice == "1":
            new_dir = Prompt.ask("  Download directory", default=settings.output_dir)
            settings.output_dir = new_dir

        elif choice == "2":
            settings.default_format = Prompt.ask(
                "  Default format",
                choices=["pdf", "cbz", "both"],
                default=settings.default_format,
            )

        elif choice == "3":
            val = IntPrompt.ask("  Concurrent chapters (1-5)", default=settings.concurrent_chapters)
            settings.concurrent_chapters = max(1, min(5, val))

        elif choice == "4":
            val = IntPrompt.ask("  Concurrent images (1-16)", default=settings.concurrent_images)
            settings.concurrent_images = max(1, min(16, val))

        elif choice == "5":
            val = IntPrompt.ask("  Max retries (0-10)", default=settings.max_retries)
            settings.max_retries = max(0, min(10, val))

        elif choice == "6":
            settings.download_delay = not settings.download_delay
            state = "enabled" if settings.download_delay else "disabled"
            console.print(f"  [bold]Download delay {state}[/bold]")

        elif choice == "7":
            settings.optimize_images = not settings.optimize_images
            state = "enabled" if settings.optimize_images else "disabled"
            console.print(f"  [bold]Image optimization {state}[/bold]")


# -- Download engine ----------------------------------------------------------


async def _download_chapters(
    browser: CdpBrowser,
    service: ComixService,
    series_title: str,
    chapters: list[ChapterInfo],
    output_dir: Path,
    fmt: str,
    settings: Settings,
    *,
    optimize: bool | None = None,
) -> None:
    """Download and convert multiple chapters in parallel."""
    from comix_dl.converters import convert
    from comix_dl.history import record_download
    from comix_dl.notify import send_notification

    if optimize is None:
        optimize = settings.optimize_images

    start_time = time.monotonic()
    total_chapters = len(chapters)
    completed_ok = 0
    skipped_count = 0
    failed_count = 0
    total_bytes = 0

    console.print(
        f"\n[bold green]Downloading {total_chapters} chapter(s) → {output_dir}[/bold green]\n"
    )

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    sem = asyncio.Semaphore(settings.concurrent_chapters)

    async def _one(ch: ChapterInfo) -> None:
        nonlocal completed_ok, skipped_count, failed_count, total_bytes

        if _shutdown_requested:
            return

        async with sem:
            if _shutdown_requested:
                return

            downloader = Downloader(browser, output_dir=output_dir)

            # Check if already downloaded (resume)
            if downloader.is_chapter_complete(series_title, ch.title):
                task_id = progress.add_task(f"  [dim]↳ {ch.title} (skipped)[/dim]", total=1, completed=1)
                skipped_count += 1
                return

            task_id = progress.add_task(f"  {ch.title}", total=None)

            chapter_data = await service.get_chapter_images(ch.chapter_id)
            if chapter_data is None:
                progress.update(task_id, description=f"  [red]✗ {ch.title} (no images)[/red]")
                failed_count += 1
                return

            total = len(chapter_data.image_urls)
            progress.update(task_id, total=total, completed=0)

            def on_img(p: DownloadProgress, _tid: object = task_id) -> None:
                progress.update(_tid, completed=p.completed)  # type: ignore[arg-type]

            downloader._on_progress = on_img
            try:
                image_dir = await downloader.download_chapter(
                    chapter_data.image_urls,
                    series_title,
                    chapter_data.chapter_label,
                )
            except RuntimeError:
                progress.update(task_id, description=f"  [red]✗ {ch.title}[/red]")
                failed_count += 1
                return

            total_bytes += downloader.bytes_downloaded

            try:
                out = convert(image_dir, fmt, optimize=optimize)
                progress.update(task_id, description=f"  [green]✓ {out.name}[/green]")
                completed_ok += 1
            except RuntimeError:
                progress.update(task_id, description=f"  [yellow]⚠ {ch.title} (convert failed)[/yellow]")
                failed_count += 1

            # Delay between chapters to avoid rate limits
            ch_delay = CONFIG.download.chapter_delay
            if ch_delay > 0:
                await asyncio.sleep(random.uniform(ch_delay * 0.5, ch_delay * 1.5))

    with progress:
        tasks = [_one(ch) for ch in chapters]
        await asyncio.gather(*tasks)

    # Summary
    elapsed = time.monotonic() - start_time
    console.print()
    parts = []
    if completed_ok:
        parts.append(f"[green]{completed_ok} downloaded[/green]")
    if skipped_count:
        parts.append(f"[dim]{skipped_count} skipped[/dim]")
    if failed_count:
        parts.append(f"[red]{failed_count} failed[/red]")

    # Speed statistics
    size_str = _format_bytes(total_bytes)
    speed = total_bytes / elapsed if elapsed > 0 else 0
    speed_str = _format_bytes(int(speed)) + "/s"

    summary = " · ".join(parts) if parts else "[green]Nothing to do[/green]"
    summary_line = (
        f"{summary}  ·  {size_str}  ·  {speed_str}  ·  "
        f"[dim]{elapsed:.1f}s elapsed[/dim]  ·  {output_dir}"
    )
    console.print(Panel(
        summary_line,
        title="[bold]Download Summary[/bold]",
        border_style="green" if failed_count == 0 else "yellow",
    ))

    # Record to history
    record_download(
        title=series_title,
        chapters_count=total_chapters,
        fmt=fmt,
        total_size_bytes=total_bytes,
        completed=completed_ok,
        failed=failed_count,
        skipped=skipped_count,
    )

    # Desktop notification (only for substantial downloads)
    if total_chapters > 0:
        notify_body = f"{completed_ok} downloaded"
        if skipped_count:
            notify_body += f", {skipped_count} skipped"
        if failed_count:
            notify_body += f", {failed_count} failed"
        notify_body += f" ({size_str})"
        send_notification(f"comix-dl: {series_title}", notify_body)

    # Auto-cleanup prompt: offer to remove raw image dirs after conversion
    if completed_ok > 0 and fmt != "none":
        _auto_cleanup_prompt(output_dir, series_title)


def _auto_cleanup_prompt(output_dir: Path, series_title: str) -> None:
    """After conversion, offer to remove raw image directories."""
    import shutil

    from comix_dl.downloader import sanitize_dirname

    manga_dir = output_dir / sanitize_dirname(series_title)
    if not manga_dir.exists():
        return

    dirs_to_remove: list[Path] = []
    for chapter_dir in sorted(manga_dir.iterdir()):
        if not chapter_dir.is_dir():
            continue
        has_output = (
            (chapter_dir.parent / (chapter_dir.name + ".pdf")).exists()
            or (chapter_dir.parent / (chapter_dir.name + ".cbz")).exists()
        )
        if has_output and (chapter_dir / ".complete").exists():
            dirs_to_remove.append(chapter_dir)

    if not dirs_to_remove:
        return

    total_size = sum(
        f.stat().st_size for d in dirs_to_remove for f in d.rglob("*") if f.is_file()
    )

    # In quiet mode, auto-clean (default is Y)
    if console.quiet:
        do_clean = True
    else:
        console.print(
            f"\n[bold]{len(dirs_to_remove)} raw image dir(s) can be removed "
            f"({_format_bytes(total_size)})[/bold]"
        )
        ans = Prompt.ask("[bold]Clean up raw images?[/bold] [dim](Y/n)[/dim]", default="y")
        do_clean = ans.lower() in ("y", "yes", "")

    if do_clean:
        removed = 0
        for d in dirs_to_remove:
            try:
                shutil.rmtree(d)
                removed += 1
            except OSError:
                pass
        console.print(f"[green]✓ Cleaned {removed} dir(s), freed {_format_bytes(total_size)}[/green]")


def _format_bytes(n: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n = int(n / 1024)
    return f"{n:.1f} TB"


# -- Flow: Info ---------------------------------------------------------------


async def _flow_info(url: str) -> int:
    """Show manga metadata without downloading."""
    slug = url.rstrip("/").split("/")[-1]

    async with CdpBrowser() as browser:
        service = ComixService(browser)

        with console.status("[bold cyan]Fetching info…"):
            results = await service.search(slug, limit=10)
            matched = next((r for r in results if r.slug == slug), results[0] if results else None)

        if not matched:
            console.print("[red]Manga not found.[/red]")
            return 1

        with console.status("[bold cyan]Loading details…"):
            info = await service.get_series(matched.hash_id)

        # Metadata panel
        meta_lines = [f"[bold]{info.title}[/bold]"]
        if info.description:
            desc = info.description[:300]
            if len(info.description) > 300:
                desc += "…"
            meta_lines.append(f"[dim]{desc}[/dim]")
        meta_lines.append("")
        meta_lines.append(f"[cyan]URL:[/cyan]       {info.url}")
        meta_lines.append(f"[cyan]Chapters:[/cyan]  {len(info.chapters)}")
        if info.authors:
            meta_lines.append(f"[cyan]Authors:[/cyan]   {', '.join(info.authors)}")
        if info.genres:
            meta_lines.append(f"[cyan]Genres:[/cyan]    {', '.join(info.genres)}")

        console.print(Panel(
            "\n".join(meta_lines),
            title="[bold]Manga Info[/bold]",
            border_style="cyan",
        ))

    return 0


# -- Flow: List ---------------------------------------------------------------


def _flow_list() -> int:
    """List downloaded manga and chapters."""
    settings = load_settings()
    output_dir = Path(settings.output_dir)

    if not output_dir.exists():
        console.print("[yellow]Output directory does not exist.[/yellow]")
        return 0

    table = Table(title="Downloaded Manga", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Manga", style="bold")
    table.add_column("Chapters", style="cyan", justify="right")
    table.add_column("Size", style="dim", justify="right")

    row_num = 0
    for manga_dir in sorted(output_dir.iterdir()):
        if not manga_dir.is_dir():
            continue

        # Count completed chapters
        complete_count = 0
        total_size = 0
        for sub in manga_dir.iterdir():
            if sub.is_dir() and (sub / ".complete").exists():
                complete_count += 1
            if sub.is_file():
                total_size += sub.stat().st_size

        if complete_count == 0 and total_size == 0:
            continue

        row_num += 1
        table.add_row(
            str(row_num),
            manga_dir.name,
            str(complete_count),
            _format_bytes(total_size),
        )

    if row_num == 0:
        console.print("[dim]No downloaded manga found.[/dim]")
        return 0

    console.print(table)
    console.print(f"\n[dim]Output directory: {output_dir}[/dim]")
    return 0


# -- Flow: Clean --------------------------------------------------------------


def _flow_clean(*, force: bool = False) -> int:
    """Remove raw image directories that have corresponding PDF/CBZ files."""
    settings = load_settings()
    output_dir = Path(settings.output_dir)

    if not output_dir.exists():
        console.print("[yellow]Output directory does not exist.[/yellow]")
        return 0

    dirs_to_remove: list[Path] = []

    for manga_dir in sorted(output_dir.iterdir()):
        if not manga_dir.is_dir():
            continue

        for chapter_dir in sorted(manga_dir.iterdir()):
            if not chapter_dir.is_dir():
                continue
            # Check if there's a corresponding PDF or CBZ
            has_output = (
                (chapter_dir.parent / (chapter_dir.name + ".pdf")).exists()
                or (chapter_dir.parent / (chapter_dir.name + ".cbz")).exists()
            )
            if has_output and (chapter_dir / ".complete").exists():
                dirs_to_remove.append(chapter_dir)

    if not dirs_to_remove:
        console.print("[dim]Nothing to clean — no raw image directories with converted output found.[/dim]")
        return 0

    total_size = sum(
        f.stat().st_size
        for d in dirs_to_remove
        for f in d.rglob("*")
        if f.is_file()
    )

    console.print(f"\n[bold]Found {len(dirs_to_remove)} directory(ies) to clean ({_format_bytes(total_size)}):[/bold]")
    for d in dirs_to_remove[:10]:
        console.print(f"  • {d.relative_to(output_dir)}")
    if len(dirs_to_remove) > 10:
        console.print(f"  [dim]… and {len(dirs_to_remove) - 10} more[/dim]")

    if not force:
        confirm = Prompt.ask("\n[bold]Remove these directories?[/bold] [dim](y/N)[/dim]", default="n")
        if confirm.lower() not in ("y", "yes"):
            console.print("[dim]Cancelled.[/dim]")
            return 0

    import shutil
    removed = 0
    for d in dirs_to_remove:
        try:
            shutil.rmtree(d)
            removed += 1
        except OSError as exc:
            console.print(f"[red]Failed to remove {d.name}: {exc}[/red]")

    console.print(f"[green]✓ Removed {removed} directory(ies), freed {_format_bytes(total_size)}[/green]")
    return 0


# -- Flow: History ------------------------------------------------------------


def _flow_history(*, action: str | None = None) -> int:
    """Show or clear download history."""
    from comix_dl.history import clear_history, list_history

    if action == "clear":
        clear_history()
        console.print("[green]✓ History cleared[/green]")
        return 0

    entries = list_history()
    if not entries:
        console.print("[dim]No download history.[/dim]")
        return 0

    table = Table(title="Download History", show_lines=False)
    table.add_column("Date", style="dim", width=12)
    table.add_column("Title", style="bold")
    table.add_column("Ch", style="cyan", justify="right", width=4)
    table.add_column("Format", style="dim", width=6)
    table.add_column("Size", style="dim", justify="right", width=10)
    table.add_column("Status", width=20)

    for entry in entries[:50]:  # Show last 50
        # Parse timestamp
        try:
            dt = entry.timestamp[:10]  # Just the date
        except Exception:
            dt = "?"

        # Status
        parts = []
        if entry.completed:
            parts.append(f"[green]{entry.completed} ok[/green]")
        if entry.skipped:
            parts.append(f"[dim]{entry.skipped} skip[/dim]")
        if entry.failed:
            parts.append(f"[red]{entry.failed} fail[/red]")
        status = " ".join(parts)

        table.add_row(
            dt,
            entry.title,
            str(entry.chapters_count),
            entry.format.upper(),
            _format_bytes(entry.total_size_bytes),
            status,
        )

    console.print(table)
    console.print(f"\n[dim]{len(entries)} total entries · comix-dl history clear to purge[/dim]")
    return 0




def _print_search_table(results: list[SearchResult], query: str) -> None:
    """Print search results table."""
    table = Table(title=f"Search results for '{query}'", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="bold")
    table.add_column("URL", style="dim cyan")

    for i, r in enumerate(results, 1):
        table.add_row(str(i), r.title, r.url)

    console.print(table)


def _print_series_header(info: SeriesInfo) -> None:
    """Print series metadata."""
    console.print(f"\n[bold]{info.title}[/bold]")
    if info.description:
        desc = info.description[:200]
        if len(info.description) > 200:
            desc += "…"
        console.print(f"[dim]{desc}[/dim]")
    console.print(f"\n[bold]{len(info.chapters)} chapters available[/bold]\n")


def _print_chapters_table(chapters: list[ChapterInfo]) -> None:
    """Print chapter list table."""
    table = Table(show_lines=False, show_header=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Chapter", style="bold")
    table.add_column("Pages", style="cyan", justify="right", width=6)
    table.add_column("Lang", style="dim", width=5)

    for i, ch in enumerate(chapters, 1):
        pages = str(ch.image_count) if ch.image_count > 0 else "—"
        table.add_row(str(i), ch.title, pages, ch.language)

    console.print(table)


def _filter_chapters_interactive(chapters: list[ChapterInfo]) -> list[ChapterInfo]:
    """Let the user filter the chapter list by keyword before selection.

    Syntax (case-insensitive, multiple tokens per line):
        +key1 +key2   keep chapters matching ANY of the keywords (OR)
        -key1 -key2   remove chapters matching ANY of the keywords
        u             undo last filter
        r             reset to original list
        (empty)       done filtering, continue to selection
    """
    filtered = list(chapters)
    history: list[list[ChapterInfo]] = []  # undo stack

    console.print()
    console.print(
        "[dim]Filter:  +key (keep)  ·  -key (exclude)  ·"
        "  multi: +key1 +key2  ·  u=undo  ·  r=reset  ·  Enter=skip[/dim]"
    )

    while True:
        raw = Prompt.ask("[bold]Filter[/bold]", default="")
        if not raw:
            break

        cmd = raw.strip()
        if not cmd:
            break

        # Undo
        if cmd.lower() == "u":
            if history:
                filtered = history.pop()
                console.print(f"  [cyan]Undone. {len(filtered)} chapter(s)[/cyan]")
                _print_chapters_table(filtered)
            else:
                console.print("  [dim]Nothing to undo[/dim]")
            continue

        # Reset
        if cmd.lower() == "r":
            history.append(filtered)
            filtered = list(chapters)
            console.print(f"  [cyan]Reset. {len(filtered)} chapter(s)[/cyan]")
            _print_chapters_table(filtered)
            continue

        # Parse tokens: split by space, group by +/-
        tokens = re.findall(r'[+\-]?\S+', cmd)
        keep_words: list[str] = []
        remove_words: list[str] = []

        for tok in tokens:
            if tok.startswith("+"):
                w = tok[1:].strip().lower()
                if w:
                    keep_words.append(w)
            elif tok.startswith("-"):
                w = tok[1:].strip().lower()
                if w:
                    remove_words.append(w)
            else:
                # bare word → keep
                keep_words.append(tok.lower())

        if not keep_words and not remove_words:
            continue

        # Save state for undo
        history.append(filtered)
        before = len(filtered)

        if keep_words:
            # Keep chapters matching ANY of the keywords (OR)
            filtered = [
                ch for ch in filtered
                if any(w in ch.title.lower() for w in keep_words)
            ]
            label = ", ".join(keep_words)
            kept = len(filtered)
            console.print(
                f"  [green]Kept {kept} chapter(s) matching"
                f" '{label}' (removed {before - kept})[/green]"
            )

        if remove_words:
            before2 = len(filtered)
            # Remove chapters matching ANY of the keywords
            filtered = [
                ch for ch in filtered
                if not any(w in ch.title.lower() for w in remove_words)
            ]
            label = ", ".join(remove_words)
            removed = before2 - len(filtered)
            console.print(
                f"  [yellow]Removed {removed} chapter(s) matching"
                f" '{label}' ({len(filtered)} remaining)[/yellow]"
            )

        if not filtered:
            console.print("[red]No chapters left! Resetting.[/red]")
            filtered = history.pop()
            continue

        # Show updated list
        console.print(f"\n[bold]{len(filtered)} chapters after filtering:[/bold]")
        _print_chapters_table(filtered)

    return filtered


def _parse_chapter_selection(selection: str, chapters: list[ChapterInfo]) -> list[ChapterInfo]:
    """Parse chapter selection: ``all``, ``1``, ``1-5``, ``1,3,5``."""
    if selection.strip().lower() == "all":
        return list(chapters)

    indices: set[int] = set()
    for part in selection.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                for i in range(int(a.strip()), int(b.strip()) + 1):
                    indices.add(i)
            except ValueError:
                continue
        else:
            try:
                indices.add(int(part))
            except ValueError:
                continue

    return [chapters[i - 1] for i in sorted(indices) if 1 <= i <= len(chapters)]


# -- Doctor -------------------------------------------------------------------


def _run_doctor() -> int:
    """Run environment diagnostics."""
    import shutil

    console.print()
    console.print(Panel("[bold]comix-downloader — Diagnostics[/bold]", border_style="cyan"))
    all_ok = True

    v = sys.version_info
    ok = v >= (3, 11)
    sym = "[green]✓[/green]" if ok else "[red]✗[/red]"
    console.print(f"  {sym} Python {v.major}.{v.minor}.{v.micro}")
    all_ok &= ok

    for module, name in [
        ("playwright", "playwright"),
        ("PIL", "Pillow"),
        ("rich", "rich"),
    ]:
        try:
            __import__(module)
            console.print(f"  [green]✓[/green] {name}")
        except ImportError:
            console.print(f"  [red]✗[/red] {name} — install with: pip install {name}")
            all_ok = False

    import platform
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
    if chrome and Path(chrome).exists():
        console.print(f"  [green]✓[/green] Chrome ({chrome})")
    else:
        console.print("  [red]✗[/red] Chrome not found — install Google Chrome")
        all_ok = False

    settings = load_settings()
    out = Path(settings.output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        console.print(f"  [green]✓[/green] Output: {out}")
    except OSError:
        console.print(f"  [red]✗[/red] Output: {out} (cannot create)")
        all_ok = False

    console.print()
    if all_ok:
        console.print("[bold green]✓ All OK — ready to download![/bold green]")
    else:
        console.print("[bold red]✗ Issues found — fix the above before continuing[/bold red]")
    return 0 if all_ok else 1
