"""Download progress pane for the TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol, cast

from textual import on
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Static

from comix_dl.core.tui.state import DownloadRowsState, format_summary_line

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from comix_dl.core.application.cleanup_usecase import CleanupPlan, CleanupResult
    from comix_dl.core.application.download_usecase import DownloadChapterEvent, DownloadEventHandler, DownloadSummary
    from comix_dl.core.tui.state import DownloadRequest


class DownloadController(Protocol):
    """Controller surface used by the live download pane."""

    async def download(
        self,
        request: DownloadRequest,
        *,
        on_event: DownloadEventHandler | None = None,
    ) -> DownloadSummary:
        """Download chapters and emit live progress events."""

    def request_shutdown(self) -> None:
        """Request shutdown/cancellation of active download work."""

    def cleanup_plan(self, series_title: str | None = None) -> CleanupPlan:
        """Return raw download directories eligible for cleanup."""

    def apply_cleanup(self, plan: CleanupPlan) -> CleanupResult:
        """Apply a cleanup plan."""


class DownloadApp(Protocol):
    """App shell surface used by the download pane."""

    async def action_show_search(self) -> None:
        """Return to the search pane."""

    def set_active_view(self, active: str) -> None:
        """Set the active shell navigation destination."""

    def set_status(self, message: str) -> None:
        """Set the shared shell status text."""


class DownloadTitle(Static):
    """Static title with a stable renderable test surface."""

    @property
    def renderable(self) -> object:
        return self.content


class DownloadStatus(Static):
    """Static status line with a stable renderable test surface."""

    @property
    def renderable(self) -> object:
        return self.content


class DownloadPane(Widget):
    """Live download progress pane."""

    BINDINGS: ClassVar = [
        ("c", "cancel", "Cancel"),
        ("b", "back_to_search", "Back"),
    ]

    def __init__(self, controller: object, request: DownloadRequest) -> None:
        super().__init__()
        self.controller = cast("DownloadController", controller)
        self.request = request
        self.rows = DownloadRowsState.from_chapters(list(request.chapters))
        self._cleanup_plan: CleanupPlan | None = None

    @property
    def shell(self) -> DownloadApp:
        return cast("DownloadApp", self.app)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield DownloadTitle(
                "Download",
                id="download-title",
                classes="pane-title",
            )
            yield Static(self._batch_summary("Preparing"), id="download-summary", classes="muted")
            yield DownloadStatus("Preparing download...", id="download-status", classes="muted")
            with Horizontal(id="download-actions"):
                yield Button("Cancel", id="cancel-button", variant="warning")
                yield Button("Cleanup", id="cleanup-button", disabled=True)
            table: DataTable[object] = DataTable(id="download-table")
            table.cursor_type = "row"
            yield table

    def on_mount(self) -> None:
        table = self.query_one("#download-table", DataTable)
        table.add_columns("Chapter", "Status", "Progress", "Detail")
        self._refresh_table()
        table.focus()
        self.shell.set_active_view("Download")
        self.shell.set_status(f"Downloading {len(self.request.chapters)} chapter(s)")
        self.run_worker(
            self._run_download(),
            name="download",
            group="download",
            exclusive=True,
            exit_on_error=False,
        )

    def _refresh_table(self) -> None:
        table = self.query_one("#download-table", DataTable)
        table.clear()
        for row in self.rows.rows.values():
            table.add_row(row.title, row.status, row.progress_text, row.detail, key=str(row.chapter_id))

    def _set_status(self, message: str) -> None:
        self.query_one("#download-status", Static).update(message)

    def _batch_summary(self, state: str) -> str:
        chapter_count = len(self.request.chapters)
        return f"{self.request.series_title} · {chapter_count} chapter(s) · {self.request.fmt.upper()} · {state}"

    def _set_summary(self, state: str) -> None:
        self.query_one("#download-summary", Static).update(self._batch_summary(state))

    def _handle_event(self, event: DownloadChapterEvent) -> None:
        self.rows.apply(event)
        self._refresh_table()

    async def _run_download(self) -> None:
        self._set_summary("Running")
        self._set_status(f"Downloading {len(self.request.chapters)} chapter(s)...")
        self.shell.set_status(f"Downloading {len(self.request.chapters)} chapter(s)")
        try:
            summary = await self.controller.download(self.request, on_event=self._handle_event)
        except Exception as exc:
            self._set_summary("Failed")
            self._set_status(f"Download failed: {exc}")
            self.shell.set_status("Download failed")
            self.query_one("#cancel-button", Button).disabled = True
            return

        self.query_one("#cancel-button", Button).disabled = True
        self._set_summary("Complete")
        self.shell.set_status("Download complete")
        summary_line = format_summary_line(summary)
        status = (
            f"Download complete: {summary_line}. "
            "Next: cleanup raw folders, return to Search, or inspect Library."
        )
        try:
            self._cleanup_plan = self.controller.cleanup_plan(series_title=self.request.series_title)
        except Exception as exc:
            self._cleanup_plan = None
            self.query_one("#cleanup-button", Button).disabled = True
            self._set_status(f"{status}. Cleanup check failed: {exc}")
            return

        cleanup_button = self.query_one("#cleanup-button", Button)
        if self._cleanup_plan.candidates:
            cleanup_button.disabled = False
            self._set_status(
                f"{status}. Cleanup available for {len(self._cleanup_plan.candidates)} raw folder(s)."
            )
            return

        cleanup_button.disabled = True
        self._set_status(status)

    @on(Button.Pressed, "#cancel-button")
    def _button_cancel(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-button":
            self.action_cancel()

    @on(Button.Pressed, "#cleanup-button")
    def _button_cleanup(self, event: Button.Pressed) -> None:
        if event.button.id == "cleanup-button":
            self.action_cleanup()

    def action_cancel(self) -> None:
        self.controller.request_shutdown()
        self._set_summary("Cancelling")
        self._set_status("Cancellation requested. Waiting for active chapter work to stop...")
        self.shell.set_status("Cancelling download")
        self.query_one("#cancel-button", Button).disabled = True

    async def action_back_to_search(self) -> None:
        await self.shell.action_show_search()

    def action_cleanup(self) -> None:
        plan = self._cleanup_plan
        if plan is None:
            plan = self.controller.cleanup_plan(series_title=self.request.series_title)
        self._cleanup_plan = plan
        if not plan.candidates:
            self.query_one("#cleanup-button", Button).disabled = True
            self._set_status("No cleanup candidates found.")
            return

        result = self.controller.apply_cleanup(plan)
        self.query_one("#cleanup-button", Button).disabled = True
        if result.failed:
            self._set_status(
                f"Cleanup removed {result.removed_count} raw folder(s); {len(result.failed)} failed."
            )
            return
        self._set_status(f"Cleanup removed {result.removed_count} raw folder(s).")
