"""Download workflow flows — search, URL download, non-interactive, info, list, clean."""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt

from comix_dl.cdp_browser import CdpBrowser
from comix_dl.cli.display import console, format_bytes, print_chapters_table, print_search_table, print_series_header
from comix_dl.cli.interactive import filter_chapters_interactive, parse_chapter_selection
from comix_dl.comix_service import ComixService
from comix_dl.config import CONFIG
from comix_dl.downloader import Downloader, DownloadProgress
from comix_dl.settings import Settings, load_settings

if TYPE_CHECKING:
    from comix_dl.comix_service import ChapterInfo


def _is_shutdown() -> bool:
    """Check the module-level shutdown flag."""
    from comix_dl.cli import _shutdown_requested
    return _shutdown_requested


# -- Flow: Search & Download --------------------------------------------------


async def flow_search(query: str) -> int:
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

        print_search_table(results, query)

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
                    print_search_table(results, query)
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

        print_series_header(info)
        print_chapters_table(info.chapters)

        # 4. Filter chapters (optional)
        filtered = filter_chapters_interactive(info.chapters)

        # 5. Select chapters
        console.print()
        console.print("[dim]Examples: 1  ·  1-5  ·  1,3,5  ·  all  ·  q to quit[/dim]")
        ch_choice = Prompt.ask(
            "[bold]Select chapters[/bold]",
            default="all",
        )
        if ch_choice.lower() in ("q", "quit", "exit"):
            return 0

        to_download = parse_chapter_selection(ch_choice, filtered)
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
        await download_chapters(browser, service, info.title, to_download, output_dir, fmt, settings)

    return 0


async def flow_url_download(url: str) -> int:
    """Download from a manga URL (interactive mode)."""
    settings = load_settings()
    output_dir = Path(settings.output_dir)

    # Extract slug from the URL
    slug = url.rstrip("/").split("/")[-1]

    async with CdpBrowser() as browser:
        service = ComixService(browser)

        with console.status("[bold cyan]Fetching series info…"):
            try:
                info = await service.get_series_by_slug(slug)
            except RuntimeError:
                # Direct lookup failed — try search as fallback
                results = await service.search(slug, limit=10)
                matched = next((r for r in results if r.slug == slug), None)
                if not matched and results:
                    console.print(f"[yellow]Exact match not found for '{slug}'. Did you mean:[/yellow]\n")
                    print_search_table(results, slug)
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

        print_series_header(info)
        print_chapters_table(info.chapters)

        filtered = filter_chapters_interactive(info.chapters)

        console.print()
        console.print("[dim]Examples: 1  ·  1-5  ·  1,3,5  ·  all  ·  q to quit[/dim]")
        ch_choice = Prompt.ask("[bold]Select chapters[/bold]", default="all")
        if ch_choice.lower() in ("q", "quit", "exit"):
            return 0

        to_download = parse_chapter_selection(ch_choice, filtered)
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

        await download_chapters(browser, service, info.title, to_download, output_dir, fmt, settings)

    return 0


async def flow_noninteractive_download(
    url: str, chapters_sel: str, fmt: str, output: str, *, optimize: bool = True,
) -> int:
    """Fully non-interactive download flow."""
    output_dir = Path(output)
    slug = url.rstrip("/").split("/")[-1]

    async with CdpBrowser() as browser:
        service = ComixService(browser)

        console.print(f"[bold]Looking up '{slug}'…[/bold]")
        try:
            info = await service.get_series_by_slug(slug)
        except RuntimeError:
            console.print("[red]Manga not found.[/red]")
            return 1

        console.print(f"[bold cyan]→ {info.title}[/bold cyan]")

        if not info.chapters:
            console.print("[yellow]No chapters found.[/yellow]")
            return 0

        to_download = parse_chapter_selection(chapters_sel, info.chapters)
        if not to_download:
            console.print("[red]No valid chapters selected.[/red]")
            return 1

        console.print(f"[bold]Downloading {len(to_download)} chapter(s) as {fmt.upper()}…[/bold]\n")
        settings = load_settings()
        await download_chapters(
            browser, service, info.title, to_download, output_dir, fmt, settings,
            optimize=optimize,
        )

    return 0


# -- Flow: Info ---------------------------------------------------------------


async def flow_info(url: str) -> int:
    """Show manga metadata without downloading."""
    slug = url.rstrip("/").split("/")[-1]

    async with CdpBrowser() as browser:
        service = ComixService(browser)

        with console.status("[bold cyan]Fetching info…"):
            try:
                info = await service.get_series_by_slug(slug)
            except RuntimeError:
                console.print("[red]Manga not found.[/red]")
                return 1

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


def flow_list() -> int:
    """List downloaded manga and chapters."""
    from rich.table import Table

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
            format_bytes(total_size),
        )

    if row_num == 0:
        console.print("[dim]No downloaded manga found.[/dim]")
        return 0

    console.print(table)
    console.print(f"\n[dim]Output directory: {output_dir}[/dim]")
    return 0


# -- Flow: Clean --------------------------------------------------------------


def flow_clean(*, force: bool = False) -> int:
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

    console.print(f"\n[bold]Found {len(dirs_to_remove)} directory(ies) to clean ({format_bytes(total_size)}):[/bold]")
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

    console.print(f"[green]✓ Removed {removed} directory(ies), freed {format_bytes(total_size)}[/green]")
    return 0


# -- Download engine ----------------------------------------------------------


async def download_chapters(
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

        if _is_shutdown():
            return

        async with sem:
            if _is_shutdown():
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
    size_str = format_bytes(total_bytes)
    speed = total_bytes / elapsed if elapsed > 0 else 0
    speed_str = format_bytes(int(speed)) + "/s"

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
            f"({format_bytes(total_size)})[/bold]"
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
        console.print(f"[green]✓ Cleaned {removed} dir(s), freed {format_bytes(total_size)}[/green]")
