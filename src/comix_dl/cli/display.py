"""Display helpers — search tables, series headers, chapter lists, formatters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from comix_dl.comix_service import ChapterInfo, SearchResult, SeriesInfo

console = Console()


def format_bytes(n: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n = int(n / 1024)
    return f"{n:.1f} TB"


def print_search_table(results: list[SearchResult], query: str) -> None:
    """Print search results table."""
    table = Table(title=f"Search results for '{query}'", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="bold")
    table.add_column("URL", style="dim cyan")

    for i, r in enumerate(results, 1):
        table.add_row(str(i), r.title, r.url)

    console.print(table)


def print_series_header(info: SeriesInfo) -> None:
    """Print series metadata."""
    console.print(f"\n[bold]{info.title}[/bold]")
    if info.description:
        desc = info.description[:200]
        if len(info.description) > 200:
            desc += "…"
        console.print(f"[dim]{desc}[/dim]")
    console.print(f"\n[bold]{len(info.chapters)} chapters available[/bold]\n")


def print_chapters_table(chapters: list[ChapterInfo]) -> None:
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
