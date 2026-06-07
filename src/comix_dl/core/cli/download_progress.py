"""Download progress event rendering for CLI flows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.panel import Panel

from comix_dl.core.application.download_reporting import build_download_report
from comix_dl.core.cli.display import console, format_bytes

if TYPE_CHECKING:
    from pathlib import Path

    from rich.progress import Progress, TaskID

    from comix_dl.core.application.download_usecase import DownloadChapterEvent, DownloadSummary


def render_download_event(
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


def render_download_summary(summary: DownloadSummary, output_dir: Path) -> None:
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


__all__ = ["render_download_event", "render_download_summary"]
