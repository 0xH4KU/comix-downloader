"""Full-screen Textual interface for comix-downloader."""

from __future__ import annotations


def run_tui(*, mirror: str | None = None) -> int:
    """Launch the optional full-screen TUI."""
    try:
        from comix_dl.core.tui.app import ComixTuiApp
    except ImportError as exc:
        from comix_dl.core.cli.display import console

        console.print(f"[red]Unable to start TUI: {exc}[/red]")
        return 1

    app = ComixTuiApp(mirror=mirror)
    result = app.run()
    return int(result or 0)


__all__ = ["run_tui"]
