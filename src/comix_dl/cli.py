"""Command-line interface for comix-downloader."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from comix_dl import __version__
from comix_dl.settings import Settings, load_settings, save_settings

console = Console()

BANNER = r"""
  ██████╗ ██████╗ ███╗   ███╗██╗██╗  ██╗
 ██╔════╝██╔═══██╗████╗ ████║██║╚██╗██╔╝
 ██║     ██║   ██║██╔████╔██║██║ ╚███╔╝
 ██║     ██║   ██║██║╚██╔╝██║██║ ██╔██╗
 ╚██████╗╚██████╔╝██║ ╚═╝ ██║██║██╔╝ ██╗
  ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝╚═╝  ╚═╝
"""


def main() -> int:
    """CLI entry point."""
    args = sys.argv[1:]

    # Quick shortcut: `comix-dl "omori"` → direct search
    if args and not args[0].startswith("-"):
        logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
        return asyncio.run(_flow_search(args[0]))

    # Flags
    if "--version" in args or "-v" in args:
        console.print(f"comix-dl v{__version__}")
        return 0

    if "--doctor" in args:
        return _run_doctor()

    # Set log level
    if "--debug" in args:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s:%(name)s:%(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    # Main menu loop
    return _main_menu()


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
        console.print("  [cyan]3[/cyan]  Settings")
        console.print("  [cyan]4[/cyan]  Doctor (diagnostics)")
        console.print("  [cyan]q[/cyan]  Exit")

        choice = Prompt.ask(
            "\n[bold]Choose[/bold]",
            choices=["1", "2", "3", "4", "q"],
            default="1",
            show_choices=False,
        )

        if choice == "q":
            console.print("[dim]Bye![/dim]")
            return 0

        if choice == "1":
            query = Prompt.ask("[bold]Search query[/bold]")
            if query.strip():
                asyncio.run(_flow_search(query.strip()))

        elif choice == "2":
            url = Prompt.ask("[bold]Manga URL[/bold]")
            if url.strip():
                asyncio.run(_flow_url_download(url.strip()))

        elif choice == "3":
            _flow_settings()

        elif choice == "4":
            _run_doctor()


# -- Flow: Search & Download --------------------------------------------------


async def _flow_search(query: str) -> int:
    """Interactive search → select → download."""
    from comix_dl.cdp_browser import CdpBrowser
    from comix_dl.comix_service import ComixService

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

        # 2. Select series
        choice = Prompt.ask(
            "\n[bold]Select manga[/bold] [dim](number, or q to quit)[/dim]",
            default="1",
        )
        if choice.lower() in ("q", "quit", "exit"):
            return 0

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(results):
                console.print("[red]Invalid selection.[/red]")
                return 1
        except ValueError:
            console.print("[red]Invalid selection.[/red]")
            return 1

        selected = results[idx]
        console.print(f"\n[bold cyan]→ {selected.title}[/bold cyan]")

        # 3. Load chapters
        with console.status("[bold cyan]Loading chapters…"):
            info = await service.get_series(selected.hash_id)

        if not info.chapters:
            console.print("[yellow]No chapters found.[/yellow]")
            return 0

        if info.description:
            console.print(f"\n[dim]{info.description[:200]}[/dim]")

        console.print(f"\n[bold]{len(info.chapters)} chapters available[/bold]\n")
        _print_chapters_table(info.chapters)

        # 4. Select chapters
        console.print()
        console.print("[dim]Examples: 1  ·  1-5  ·  1,3,5  ·  all  ·  q to quit[/dim]")
        ch_choice = Prompt.ask(
            "[bold]Select chapters[/bold]",
            default="all",
        )
        if ch_choice.lower() in ("q", "quit", "exit"):
            return 0

        to_download = _parse_chapter_selection(ch_choice, info.chapters)
        if not to_download:
            console.print("[red]No valid chapters selected.[/red]")
            return 1

        # 5. Format
        fmt = Prompt.ask(
            "[bold]Output format[/bold]",
            choices=["pdf", "cbz", "both"],
            default=settings.default_format,
        )

        console.print(
            f"\n[bold green]Downloading {len(to_download)} chapter(s)…[/bold green]\n"
        )

        # 6. Parallel download & convert
        await _download_chapters(
            browser, service, info.title, to_download, output_dir, fmt, settings,
        )

        console.print(f"\n[bold green]✓ Done! Files saved to {output_dir}[/bold green]")

    return 0


async def _flow_url_download(url: str) -> int:
    """Download from a manga URL."""
    from comix_dl.cdp_browser import CdpBrowser
    from comix_dl.comix_service import ComixService

    settings = load_settings()
    output_dir = Path(settings.output_dir)

    # Extract hash_id from the URL or try as search term
    slug = url.rstrip("/").split("/")[-1]

    async with CdpBrowser() as browser:
        service = ComixService(browser)

        # Try to get series info
        with console.status("[bold cyan]Fetching series info…"):
            try:
                # First search for the slug to get hash_id
                results = await service.search(slug, limit=5)
                matched = next((r for r in results if r.slug == slug), None)
                if matched:
                    info = await service.get_series(matched.hash_id)
                else:
                    console.print("[yellow]Could not find manga. Try using search instead.[/yellow]")
                    return 1
            except RuntimeError as exc:
                console.print(f"[red]{exc}[/red]")
                return 1

        console.print(f"\n[bold]{info.title}[/bold] — {len(info.chapters)} chapters\n")
        _print_chapters_table(info.chapters)

        console.print()
        console.print("[dim]Examples: 1  ·  1-5  ·  1,3,5  ·  all  ·  q to quit[/dim]")
        ch_choice = Prompt.ask("[bold]Select chapters[/bold]", default="all")
        if ch_choice.lower() in ("q", "quit", "exit"):
            return 0

        to_download = _parse_chapter_selection(ch_choice, info.chapters)
        if not to_download:
            console.print("[red]No valid chapters selected.[/red]")
            return 1

        fmt = Prompt.ask(
            "[bold]Output format[/bold]",
            choices=["pdf", "cbz", "both"],
            default=settings.default_format,
        )

        console.print(
            f"\n[bold green]Downloading {len(to_download)} chapter(s)…[/bold green]\n"
        )

        await _download_chapters(
            browser, service, info.title, to_download, output_dir, fmt, settings,
        )

        console.print(f"\n[bold green]✓ Done! Files saved to {output_dir}[/bold green]")

    return 0


# -- Flow: Settings -----------------------------------------------------------


def _flow_settings() -> None:
    """Interactive settings editor."""
    settings = load_settings()

    while True:
        console.print()
        console.print(Panel("[bold]Settings[/bold]", border_style="cyan"))
        console.print(f"  [cyan]1[/cyan]  Download directory:    [bold]{settings.output_dir}[/bold]")
        console.print(f"  [cyan]2[/cyan]  Default format:        [bold]{settings.default_format}[/bold]")
        console.print(f"  [cyan]3[/cyan]  Concurrent chapters:   [bold]{settings.concurrent_chapters}[/bold]")
        console.print(f"  [cyan]4[/cyan]  Concurrent images:     [bold]{settings.concurrent_images}[/bold]")
        console.print(f"  [cyan]5[/cyan]  Max retries:           [bold]{settings.max_retries}[/bold]")
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
            settings.concurrent_chapters = IntPrompt.ask(
                "  Concurrent chapters (1-5)",
                default=settings.concurrent_chapters,
            )

        elif choice == "4":
            settings.concurrent_images = IntPrompt.ask(
                "  Concurrent images (1-16)",
                default=settings.concurrent_images,
            )

        elif choice == "5":
            settings.max_retries = IntPrompt.ask(
                "  Max retries (0-10)",
                default=settings.max_retries,
            )


# -- Download engine ----------------------------------------------------------


async def _download_chapters(
    browser: object,
    service: object,
    series_title: str,
    chapters: list[object],
    output_dir: Path,
    fmt: str,
    settings: Settings,
) -> None:
    """Download and convert multiple chapters in parallel."""
    from comix_dl.converters import convert
    from comix_dl.downloader import Downloader, DownloadProgress

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    )

    sem = asyncio.Semaphore(settings.concurrent_chapters)

    async def _one(ch: object) -> None:
        async with sem:
            ch_info = ch  # type: ignore[assignment]
            task_id = progress.add_task(f"  {ch_info.title}", total=None)

            chapter_data = await service.get_chapter_images(ch_info.chapter_id)  # type: ignore[union-attr]
            if chapter_data is None:
                progress.update(task_id, description=f"  [red]✗ {ch_info.title}[/red]")
                return

            total = len(chapter_data.image_urls)
            progress.update(task_id, total=total, completed=0)

            def on_img(p: DownloadProgress, _tid: int = task_id) -> None:
                progress.update(_tid, completed=p.completed)

            downloader = Downloader(browser, output_dir=output_dir, on_progress=on_img)  # type: ignore[arg-type]
            try:
                image_dir = await downloader.download_chapter(
                    chapter_data.image_urls,
                    series_title,
                    chapter_data.chapter_label,
                )
            except RuntimeError:
                progress.update(task_id, description=f"  [red]✗ {ch_info.title}[/red]")
                return

            try:
                out = convert(image_dir, fmt)
                progress.update(task_id, description=f"  [green]✓ {out.name}[/green]")
            except RuntimeError:
                progress.update(task_id, description=f"  [yellow]⚠ {ch_info.title}[/yellow]")

    with progress:
        tasks = [_one(ch) for ch in chapters]
        await asyncio.gather(*tasks)


# -- UI Helpers ---------------------------------------------------------------


def _print_search_table(results: list[object], query: str) -> None:
    """Print search results table."""
    table = Table(title=f"Search results for '{query}'", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="bold")
    table.add_column("URL", style="dim cyan")

    for i, r in enumerate(results, 1):
        table.add_row(str(i), r.title, r.url)  # type: ignore[union-attr]

    console.print(table)


def _print_chapters_table(chapters: list[object]) -> None:
    """Print chapter list table."""
    table = Table(show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Chapter", style="bold")

    for i, ch in enumerate(chapters, 1):
        table.add_row(str(i), ch.title)  # type: ignore[union-attr]

    console.print(table)


def _parse_chapter_selection(selection: str, chapters: list[object]) -> list[object]:
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

    console.print("[bold]comix-downloader — Diagnostics[/bold]")
    console.print("=" * 40)
    all_ok = True

    v = sys.version_info
    ok = v >= (3, 11)
    sym = "[green]✓[/green]" if ok else "[red]✗[/red]"
    console.print(f"  {sym} Python {v.major}.{v.minor}.{v.micro}")
    all_ok &= ok

    for module, name in [
        ("playwright", "playwright"),
        ("bs4", "beautifulsoup4"),
        ("PIL", "Pillow"),
        ("rich", "rich"),
    ]:
        try:
            __import__(module)
            console.print(f"  [green]✓[/green] {name}")
        except ImportError:
            console.print(f"  [red]✗[/red] {name}")
            all_ok = False

    chrome = shutil.which("google-chrome") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(chrome).exists():
        console.print("  [green]✓[/green] Chrome")
    else:
        console.print("  [red]✗[/red] Chrome not found")
        all_ok = False

    settings = load_settings()
    out = Path(settings.output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        console.print(f"  [green]✓[/green] Output: {out}")
    except OSError:
        console.print(f"  [red]✗[/red] Output: {out}")
        all_ok = False

    console.print("=" * 40)
    if all_ok:
        console.print("[bold green]✓ All OK[/bold green]")
    else:
        console.print("[bold red]✗ Issues found[/bold red]")
    return 0 if all_ok else 1
