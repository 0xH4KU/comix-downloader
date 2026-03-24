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
import signal
import sys

from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from comix_dl import __version__
from comix_dl.cli.display import console
from comix_dl.cli.flows import (
    flow_clean,
    flow_info,
    flow_list,
    flow_noninteractive_download,
    flow_search,
    flow_url_download,
)
from comix_dl.cli.interactive import flow_history, flow_settings, parse_chapter_selection, run_doctor
from comix_dl.settings import load_settings

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
    parser = argparse.ArgumentParser(
        prog="comix-dl",
        description="Download manga from comix.to",
    )
    parser.add_argument("-V", "--version", action="version", version=f"comix-dl {__version__}")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Minimal output")

    sub = parser.add_subparsers(dest="command")

    # search
    sp = sub.add_parser("search", help="Search for manga")
    sp.add_argument("query", help="Search query")

    # download
    sp = sub.add_parser("download", help="Download manga by URL or slug")
    sp.add_argument("url", help="Manga URL or slug")
    sp.add_argument("-c", "--chapters", default="all", help="Chapter selection (e.g. 1-5, all)")
    sp.add_argument("-f", "--format", choices=["pdf", "cbz", "both"], default=None)
    sp.add_argument("-o", "--output", default=None, help="Output directory")
    sp.add_argument("--no-optimize", action="store_true", help="Skip image optimization")

    # info
    sp = sub.add_parser("info", help="Show manga metadata")
    sp.add_argument("url", help="Manga URL or slug")

    # list
    sub.add_parser("list", help="List downloaded manga")

    # clean
    sp = sub.add_parser("clean", help="Remove raw image directories")
    sp.add_argument("--force", action="store_true", help="Skip confirmation")

    # history
    sp = sub.add_parser("history", help="Show download history")
    sp.add_argument("action", nargs="?", choices=["clear"], help="Optional action")

    # doctor
    sub.add_parser("doctor", help="Run environment diagnostics")

    # settings
    sub.add_parser("settings", help="View / edit settings")

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
        return _run_async(flow_search(sys.argv[1]))

    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s:%(name)s:%(message)s")

    # Quiet mode
    if getattr(args, "quiet", False):
        console.quiet = True

    if args.command == "search":
        return _run_async(flow_search(args.query))

    if args.command == "download":
        settings = load_settings()
        fmt = args.format or settings.default_format
        output = args.output or settings.output_dir
        optimize = settings.optimize_images and not args.no_optimize
        return _run_async(flow_noninteractive_download(args.url, args.chapters, fmt, output, optimize=optimize))

    if args.command == "info":
        return _run_async(flow_info(args.url))

    if args.command == "list":
        return flow_list()

    if args.command == "clean":
        return flow_clean(force=args.force)

    if args.command == "history":
        return flow_history(action=args.action)

    if args.command == "doctor":
        return run_doctor()

    if args.command == "settings":
        flow_settings()
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
                _run_async(flow_search(query.strip()))

        elif choice == "2":
            url = Prompt.ask("[bold]Manga URL or slug[/bold]")
            if url.strip():
                _run_async(flow_url_download(url.strip()))

        elif choice == "3":
            flow_list()

        elif choice == "4":
            flow_history()

        elif choice == "5":
            flow_settings()

        elif choice == "6":
            run_doctor()


# -- Backward-compatible re-exports for tests ---------------------------------
# Tests import _build_parser and _parse_chapter_selection from comix_dl.cli
_parse_chapter_selection = parse_chapter_selection
