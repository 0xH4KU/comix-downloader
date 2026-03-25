"""Interactive UI components — settings editor, chapter filter, history, doctor."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from comix_dl.cli.display import console, format_bytes, print_chapters_table
from comix_dl.settings import load_settings, save_settings

if TYPE_CHECKING:
    from comix_dl.comix_service import ChapterInfo


def flow_settings() -> None:
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


def flow_history(*, action: str | None = None) -> int:
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
        if entry.partial:
            parts.append(f"[yellow]{entry.partial} partial[/yellow]")
        if entry.failed:
            parts.append(f"[red]{entry.failed} fail[/red]")
        status = " ".join(parts)

        table.add_row(
            dt,
            entry.title,
            str(entry.chapters_count),
            entry.format.upper(),
            format_bytes(entry.total_size_bytes),
            status,
        )

    console.print(table)
    console.print(f"\n[dim]{len(entries)} total entries · comix-dl history clear to purge[/dim]")
    return 0


def filter_chapters_interactive(chapters: list[ChapterInfo]) -> list[ChapterInfo]:
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
                print_chapters_table(filtered)
            else:
                console.print("  [dim]Nothing to undo[/dim]")
            continue

        # Reset
        if cmd.lower() == "r":
            history.append(filtered)
            filtered = list(chapters)
            console.print(f"  [cyan]Reset. {len(filtered)} chapter(s)[/cyan]")
            print_chapters_table(filtered)
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
        print_chapters_table(filtered)

    return filtered


def parse_chapter_selection(selection: str, chapters: list[ChapterInfo]) -> list[ChapterInfo]:
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


def run_doctor() -> int:
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
