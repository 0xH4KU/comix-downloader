"""Application-layer abstract ports.

Use cases in :mod:`comix_dl.core.application` consume infrastructure
(history persistence, notifications, settings) through the protocols
defined here rather than through concrete classes. The framework's
default implementations (:class:`comix_dl.core.history.HistoryRepository`,
:func:`comix_dl.core.notify.send_notification`, etc.) satisfy these
protocols structurally, so production wiring is unchanged; the value
is in tests and forks that want to substitute different backends
without re-implementing the use cases themselves.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class HistoryPort(Protocol):
    """Append-only download history store consumed by download_usecase.

    The default implementation is
    :class:`comix_dl.core.history.HistoryRepository`. Tests inject
    capturing fakes; forks that want a different backend (SQLite,
    a remote endpoint) only need to satisfy this method.
    """

    def record_download(
        self,
        title: str,
        chapters_count: int,
        fmt: str,
        total_size_bytes: int = 0,
        completed: int = 0,
        partial: int = 0,
        failed: int = 0,
        skipped: int = 0,
        summary_text: str = "",
        issues: list[str] | None = None,
    ) -> None:
        """Persist one download summary record."""
        ...


__all__ = ["HistoryPort"]
