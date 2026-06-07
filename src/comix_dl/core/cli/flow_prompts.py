"""Prompt and metadata rendering helpers for download flows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.prompt import Prompt

from comix_dl.core.cli.display import console
from comix_dl.core.cli.interactive import filter_chapters_interactive, parse_chapter_selection

if TYPE_CHECKING:
    from comix_dl.core.errors import RemoteApiError
    from comix_dl.core.models import ChapterInfo, SeriesInfo


def render_series_info_panel(info: SeriesInfo) -> None:
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


def render_remote_api_error(exc: RemoteApiError) -> None:
    """Render one user-meaningful API failure at the CLI boundary."""
    console.print(f"[red]{exc}[/red]")


def prompt_chapter_selection(chapters: list[ChapterInfo]) -> list[ChapterInfo] | None:
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


__all__ = [
    "prompt_chapter_selection",
    "render_remote_api_error",
    "render_series_info_panel",
]
