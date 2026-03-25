"""Download workflow flows — search, URL download, non-interactive, info, list, clean."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Prompt

from comix_dl.application.cleanup_usecase import apply_cleanup_plan, build_cleanup_plan, list_downloaded_series
from comix_dl.application.download_reporting import build_download_report
from comix_dl.application.session import ApplicationSession, load_runtime, open_application_session
from comix_dl.cli.display import (
    console,
    format_bytes,
    print_chapters_table,
    print_dedup_report,
    print_search_table,
    print_series_header,
)
from comix_dl.cli.interactive import filter_chapters_interactive, parse_chapter_selection

if TYPE_CHECKING:
    from pathlib import Path

    from comix_dl.application.download_usecase import DownloadChapterEvent, DownloadSummary
    from comix_dl.comix_service import ChapterInfo, SearchResult, SeriesInfo
    from comix_dl.config import AppConfig
    from comix_dl.settings import Settings


def _is_shutdown() -> bool:
    """Check the module-level shutdown flag."""
    from comix_dl.cli import _shutdown_requested

    return _shutdown_requested


def _render_series_info_panel(info: SeriesInfo) -> None:
    """Render a manga metadata panel."""
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


def _prompt_chapter_selection(chapters: list[ChapterInfo]) -> list[ChapterInfo] | None:
    """Prompt the user for which chapters to download."""
    filtered = filter_chapters_interactive(chapters)

    console.print()
    console.print("[dim]Examples: 1  ·  1-5  ·  1,3,5  ·  all  ·  q to quit[/dim]")
    choice = Prompt.ask("[bold]Select chapters[/bold]", default="all")
    if choice.lower() in ("q", "quit", "exit"):
        return None

    selected = parse_chapter_selection(choice, filtered)
    if not selected:
        console.print("[red]No valid chapters selected.[/red]")
        return []

    console.print(f"\n[bold]Selected {len(selected)} chapter(s):[/bold]")
    for chapter in selected[:10]:
        console.print(f"  • {chapter.title}")
    if len(selected) > 10:
        console.print(f"  [dim]… and {len(selected) - 10} more[/dim]")

    return selected


def _render_download_event(
    progress: Progress,
    task_ids: dict[int, TaskID],
    event: DownloadChapterEvent,
) -> None:
    """Render download progress events emitted by the application use case."""
    task_id = task_ids.get(event.chapter_id)

    if event.kind == "skipped":
        if task_id is None:
            task_id = progress.add_task(
                f"  [dim]↳ {event.chapter_title} (skipped)[/dim]",
                total=1,
                completed=1,
            )
            task_ids[event.chapter_id] = task_id
        else:
            progress.update(
                task_id,
                description=f"  [dim]↳ {event.chapter_title} (skipped)[/dim]",
                total=1,
                completed=1,
            )
        return

    if task_id is None:
        task_id = progress.add_task(f"  {event.chapter_title}", total=None)
        task_ids[event.chapter_id] = task_id

    if event.kind == "started":
        progress.update(task_id, description=f"  {event.chapter_title}", total=None)
        return

    if event.kind == "planned":
        progress.update(task_id, total=event.total or 0, completed=0)
        return

    if event.kind == "progress":
        if event.total is None:
            progress.update(task_id, completed=event.completed)
        else:
            progress.update(task_id, completed=event.completed, total=event.total)
        return

    if event.kind == "missing_images":
        progress.update(task_id, description=f"  [red]✗ {event.chapter_title} (no images)[/red]")
        return

    if event.kind == "failed":
        progress.update(task_id, description=f"  [red]✗ {event.chapter_title}[/red]")
        return

    if event.kind == "partial":
        message = event.message or f"{event.chapter_title} is incomplete"
        progress.update(task_id, description=f"  [yellow]⚠ {message}[/yellow]")
        return

    if event.kind == "converted":
        output_name = event.output_name or event.chapter_title
        progress.update(task_id, description=f"  [green]✓ {output_name}[/green]")
        return

    if event.kind == "conversion_failed":
        progress.update(
            task_id,
            description=f"  [yellow]⚠ {event.chapter_title} (convert failed)[/yellow]",
        )


def _render_download_summary(summary: DownloadSummary, output_dir: Path) -> None:
    """Print the final download summary panel."""
    console.print()

    report = build_download_report(summary)
    speed = summary.total_bytes / summary.elapsed_seconds if summary.elapsed_seconds > 0 else 0
    speed_str = format_bytes(int(speed)) + "/s"
    summary_line = (
        f"{report.summary_text}  ·  {report.size_text}  ·  {speed_str}  ·  "
        f"[dim]{summary.elapsed_seconds:.1f}s elapsed[/dim]  ·  {output_dir}"
    )
    if report.issue_lines:
        issue_block = "\n".join(f"- {line}" for line in report.preview_issue_lines())
        summary_line += f"\n\n[bold]Issues[/bold]\n{issue_block}"

    console.print(Panel(
        summary_line,
        title="[bold]Download Summary[/bold]",
        border_style="green" if summary.failed == 0 and summary.partial == 0 else "yellow",
    ))


async def _download_with_progress(
    session: ApplicationSession,
    *,
    series_title: str,
    chapters: list[ChapterInfo],
    fmt: str,
    optimize: bool,
    auto_cleanup: bool,
) -> None:
    """Run the application download use case and render progress in the CLI."""
    console.print(f"\n[bold green]Downloading {len(chapters)} chapter(s) → {session.output_dir}[/bold green]\n")

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    task_ids: dict[int, TaskID] = {}

    with progress:
        summary = await session.download(
            series_title=series_title,
            chapters=chapters,
            fmt=fmt,
            optimize=optimize,
            on_event=lambda event: _render_download_event(progress, task_ids, event),
            is_shutdown=_is_shutdown,
        )

    _render_download_summary(summary, session.output_dir)

    if summary.completed > 0:
        _auto_cleanup_prompt(session.output_dir, series_title, auto_confirm=auto_cleanup)


# -- Flow: Search & Download --------------------------------------------------


async def flow_search(query: str, *, quiet: bool = False) -> int:
    """Interactive search → select → download."""
    async with open_application_session() as session:
        with console.status("[bold cyan]Searching…"):
            results = await session.search(query)

        if not results:
            console.print("[yellow]No results found.[/yellow]")
            return 0

        print_search_table(results, query)

        info: SeriesInfo | None = None
        selected: SearchResult | None = None

        while True:
            choice = Prompt.ask(
                "\n[bold]Select manga[/bold] [dim](number, or 1i for info, q to quit)[/dim]",
                default="1",
            )
            if choice.lower() in ("q", "quit", "exit"):
                return 0

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

            if show_info:
                with console.status("[bold cyan]Loading info…"):
                    info = await session.load_series(selected.hash_id)
                _render_series_info_panel(info)

                cont = Prompt.ask("[bold]Fetch chapters?[/bold] [dim](Y/n)[/dim]", default="y")
                if cont.lower() not in ("y", "yes", ""):
                    print_search_table(results, query)
                    info = None
                    continue

            break

        if selected is None:
            return 1

        if info is None:
            with console.status("[bold cyan]Loading chapters…"):
                info = await session.load_series(selected.hash_id)

        if not info.chapters:
            console.print("[yellow]No chapters found.[/yellow]")
            return 0

        print_series_header(info)
        print_dedup_report(info.dedup_decisions)
        print_chapters_table(info.chapters)

        to_download = _prompt_chapter_selection(info.chapters)
        if to_download is None:
            return 0
        if not to_download:
            return 1

        fmt = Prompt.ask(
            "[bold]Output format[/bold]",
            choices=["pdf", "cbz", "both"],
            default=session.settings.default_format,
        )

        await _download_with_progress(
            session,
            series_title=info.title,
            chapters=to_download,
            fmt=fmt,
            optimize=session.settings.optimize_images,
            auto_cleanup=quiet,
        )

    return 0


async def flow_url_download(url: str, *, quiet: bool = False) -> int:
    """Download from a manga URL (interactive mode)."""
    async with open_application_session() as session:
        with console.status("[bold cyan]Fetching series info…"):
            lookup = await session.resolve_series(url)

        info = lookup.series
        if info is None and lookup.suggestions:
            console.print(f"[yellow]Exact match not found for '{lookup.slug}'. Did you mean:[/yellow]\n")
            print_search_table(lookup.suggestions, lookup.slug)
            choice = Prompt.ask(
                "\n[bold]Select manga[/bold] [dim](number, or q to quit)[/dim]",
                default="1",
            )
            if choice.lower() in ("q", "quit", "exit"):
                return 0
            try:
                selected = lookup.suggestions[int(choice) - 1]
            except (ValueError, IndexError):
                console.print("[red]Invalid selection.[/red]")
                return 1

            with console.status("[bold cyan]Loading chapters…"):
                info = await session.load_series(selected.hash_id)

        if info is None:
            console.print("[yellow]Could not find manga. Try using search instead.[/yellow]")
            return 1

        print_series_header(info)
        print_dedup_report(info.dedup_decisions)
        print_chapters_table(info.chapters)

        to_download = _prompt_chapter_selection(info.chapters)
        if to_download is None:
            return 0
        if not to_download:
            return 1

        fmt = Prompt.ask(
            "[bold]Output format[/bold]",
            choices=["pdf", "cbz", "both"],
            default=session.settings.default_format,
        )

        await _download_with_progress(
            session,
            series_title=info.title,
            chapters=to_download,
            fmt=fmt,
            optimize=session.settings.optimize_images,
            auto_cleanup=quiet,
        )

    return 0


async def flow_noninteractive_download(
    url: str,
    chapters_sel: str,
    fmt: str | None = None,
    output: str | None = None,
    *,
    optimize: bool | None = None,
    settings: Settings | None = None,
    config: AppConfig | None = None,
    quiet: bool = False,
) -> int:
    """Fully non-interactive download flow."""
    async with open_application_session(settings=settings, config=config, output=output) as session:
        lookup = await session.resolve_series(url)
        resolved_fmt = fmt or session.settings.default_format
        resolved_optimize = session.settings.optimize_images if optimize is None else optimize

        console.print(f"[bold]Looking up '{lookup.slug}'…[/bold]")
        info = lookup.series
        if info is None:
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

        console.print(f"[bold]Downloading {len(to_download)} chapter(s) as {resolved_fmt.upper()}…[/bold]\n")
        await _download_with_progress(
            session,
            series_title=info.title,
            chapters=to_download,
            fmt=resolved_fmt,
            optimize=resolved_optimize,
            auto_cleanup=quiet,
        )

    return 0


# -- Flow: Info ---------------------------------------------------------------


async def flow_info(url: str) -> int:
    """Show manga metadata without downloading."""
    async with open_application_session() as session:
        with console.status("[bold cyan]Fetching info…"):
            lookup = await session.resolve_series(url)

        if lookup.series is None:
            console.print("[red]Manga not found.[/red]")
            return 1

        _render_series_info_panel(lookup.series)

    return 0


# -- Flow: List ---------------------------------------------------------------


def flow_list() -> int:
    """List downloaded manga and chapters."""
    from rich.table import Table

    runtime = load_runtime()
    output_dir = runtime.output_dir

    if not output_dir.exists():
        console.print("[yellow]Output directory does not exist.[/yellow]")
        return 0

    downloaded = list_downloaded_series(output_dir)
    if not downloaded:
        console.print("[dim]No downloaded manga found.[/dim]")
        return 0

    table = Table(title="Downloaded Manga", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Manga", style="bold")
    table.add_column("Chapters", style="cyan", justify="right")
    table.add_column("Size", style="dim", justify="right")

    for index, item in enumerate(downloaded, 1):
        table.add_row(
            str(index),
            item.name,
            str(item.completed_chapters),
            format_bytes(item.total_size_bytes),
        )

    console.print(table)
    console.print(f"\n[dim]Output directory: {output_dir}[/dim]")
    return 0


# -- Flow: Clean --------------------------------------------------------------


def flow_clean(*, force: bool = False, auto_confirm: bool = False) -> int:
    """Remove raw image directories that have corresponding PDF/CBZ files."""
    runtime = load_runtime()
    output_dir = runtime.output_dir

    if not output_dir.exists():
        console.print("[yellow]Output directory does not exist.[/yellow]")
        return 0

    plan = build_cleanup_plan(output_dir)
    if not plan.candidates:
        console.print("[dim]Nothing to clean — no raw image directories with converted output found.[/dim]")
        return 0

    console.print(
        f"\n[bold]Found {len(plan.candidates)} directory(ies) to clean "
        f"({format_bytes(plan.total_size_bytes)}):[/bold]"
    )
    for candidate in plan.candidates[:10]:
        console.print(f"  • {candidate.relative_path}")
    if len(plan.candidates) > 10:
        console.print(f"  [dim]… and {len(plan.candidates) - 10} more[/dim]")

    if not (force or auto_confirm):
        confirm = Prompt.ask("\n[bold]Remove these directories?[/bold] [dim](y/N)[/dim]", default="n")
        if confirm.lower() not in ("y", "yes"):
            console.print("[dim]Cancelled.[/dim]")
            return 0

    result = apply_cleanup_plan(plan)
    failed_paths = {path for path, _ in result.failed}
    freed_bytes = sum(candidate.size_bytes for candidate in plan.candidates if candidate.path not in failed_paths)

    for path, message in result.failed:
        console.print(f"[red]Failed to remove {path.name}: {message}[/red]")

    console.print(
        f"[green]✓ Removed {result.removed_count} directory(ies), freed {format_bytes(freed_bytes)}[/green]"
    )
    return 0


def _auto_cleanup_prompt(output_dir: Path, series_title: str, *, auto_confirm: bool) -> None:
    """After conversion, offer to remove raw image directories."""
    plan = build_cleanup_plan(output_dir, series_title=series_title)
    if not plan.candidates:
        return

    if auto_confirm:
        do_clean = True
    else:
        console.print(
            f"\n[bold]{len(plan.candidates)} raw image dir(s) can be removed "
            f"({format_bytes(plan.total_size_bytes)})[/bold]"
        )
        answer = Prompt.ask("[bold]Clean up raw images?[/bold] [dim](Y/n)[/dim]", default="y")
        do_clean = answer.lower() in ("y", "yes", "")

    if not do_clean:
        return

    result = apply_cleanup_plan(plan)
    failed_paths = {path for path, _ in result.failed}
    freed_bytes = sum(candidate.size_bytes for candidate in plan.candidates if candidate.path not in failed_paths)

    for path, message in result.failed:
        console.print(f"[red]Failed to remove {path.name}: {message}[/red]")

    console.print(f"[green]✓ Cleaned {result.removed_count} dir(s), freed {format_bytes(freed_bytes)}[/green]")
