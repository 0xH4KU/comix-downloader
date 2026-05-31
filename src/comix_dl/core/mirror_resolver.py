"""Mirror selection and persistence for site adapters.

A :class:`~comix_dl.sites.base.SiteAdapter` may declare multiple
mirror URLs that point at the same logical backend. The framework
chooses one as the *active* mirror per session and remembers that
choice across runs so cold-start probing is amortised.

Resolution order (highest priority first):

1. Explicit override (CLI ``--mirror``). Trusted as-is, no probe.
2. The mirror persisted from the last successful run, if it still
   appears in :attr:`SiteAdapter.mirrors`.
3. The first entry in :attr:`SiteAdapter.mirrors`.

After the engine has cleared any required challenges, the framework
runs :meth:`SiteAdapter.probe_alive` once and records the outcome in
the persistent state file. A failed probe currently logs a warning
but does not auto-switch to a different mirror; on-the-fly engine
re-initialisation is intentionally deferred until a multi-mirror
adapter forces the question.

State is stored at ``~/.config/comix-dl/mirror_state.json`` and is
keyed by adapter name so multiple adapters can coexist.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from comix_dl.core.config import validate_public_https_url
from comix_dl.core.errors import ConfigurationError
from comix_dl.core.fileio import atomic_write_text

if TYPE_CHECKING:
    from comix_dl.sites.base import Engine, SiteAdapter

logger = logging.getLogger(__name__)


_MAX_HISTORY_ENTRIES = 20


@dataclass(frozen=True)
class MirrorProbeRecord:
    """One entry in a mirror's probe history."""

    mirror: str
    succeeded: bool
    checked_at: str  # ISO-8601 UTC timestamp


@dataclass
class MirrorState:
    """Persisted state for a single adapter's mirror resolution."""

    active: str | None = None
    history: list[MirrorProbeRecord] = field(default_factory=list)


def _default_state_file() -> Path:
    return Path.home() / ".config" / "comix-dl" / "mirror_state.json"


class MirrorStateRepository:
    """Atomic JSON-backed mirror state persistence.

    The on-disk schema is::

        {
          "<adapter.name>": {
            "active": "<url>" | null,
            "history": [
              {"mirror": "<url>", "succeeded": true, "checked_at": "..."},
              ...
            ]
          }
        }

    Multiple adapters share one file so a fork that adds a second
    adapter does not need to rewrite the persistence layer.
    """

    def __init__(self, state_file: Path | None = None) -> None:
        self._state_file = state_file if state_file is not None else _default_state_file()

    @property
    def state_file(self) -> Path:
        return self._state_file

    def _read_root(self) -> dict[str, object]:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def load(self, adapter_name: str) -> MirrorState:
        """Return persisted state for *adapter_name* (defaults if absent)."""
        per_adapter = self._read_root().get(adapter_name)
        if not isinstance(per_adapter, dict):
            return MirrorState()

        active_raw = per_adapter.get("active")
        active: str | None = active_raw if isinstance(active_raw, str) else None

        history_raw = per_adapter.get("history", [])
        history: list[MirrorProbeRecord] = []
        if isinstance(history_raw, list):
            for entry in history_raw[-_MAX_HISTORY_ENTRIES:]:
                if not isinstance(entry, dict):
                    continue
                mirror = entry.get("mirror")
                succeeded = entry.get("succeeded")
                checked_at = entry.get("checked_at")
                if (
                    isinstance(mirror, str)
                    and isinstance(succeeded, bool)
                    and isinstance(checked_at, str)
                ):
                    history.append(MirrorProbeRecord(
                        mirror=mirror, succeeded=succeeded, checked_at=checked_at,
                    ))
        return MirrorState(active=active, history=history)

    def save(self, adapter_name: str, state: MirrorState) -> None:
        """Persist *state* for *adapter_name*, preserving other adapters' state."""
        root = self._read_root()
        root[adapter_name] = {
            "active": state.active,
            "history": [
                {
                    "mirror": h.mirror,
                    "succeeded": h.succeeded,
                    "checked_at": h.checked_at,
                }
                for h in state.history[-_MAX_HISTORY_ENTRIES:]
            ],
        }
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._state_file,
            json.dumps(root, indent=2, ensure_ascii=False) + "\n",
        )


def select_initial_mirror(
    adapter: SiteAdapter,
    *,
    override: str | None = None,
    repository: MirrorStateRepository | None = None,
) -> str:
    """Choose the URL the engine should be constructed with.

    Pure synchronous decision — no probing happens here. The runtime
    later calls :func:`record_probe_outcome` to verify the choice and
    update the persistent state once the engine is ready.

    Raises:
        ConfigurationError: if ``adapter.mirrors`` is empty.
    """
    if override:
        try:
            validate_public_https_url(override, label="mirror override")
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        return override

    if not adapter.mirrors:
        raise ConfigurationError(f"Adapter '{adapter.name}' has no mirrors configured.")

    repo = repository or MirrorStateRepository()
    state = repo.load(adapter.name)
    if state.active and state.active in adapter.mirrors:
        return state.active
    return adapter.mirrors[0]


async def record_probe_outcome(
    adapter: SiteAdapter,
    engine: Engine,
    chosen_mirror: str,
    *,
    repository: MirrorStateRepository | None = None,
    skipped: bool = False,
) -> bool:
    """Run :meth:`SiteAdapter.probe_alive` and persist the outcome.

    Pass ``skipped=True`` for callers who already trust the mirror
    (e.g. an explicit ``--mirror`` override) and want the choice
    recorded without paying for another round-trip; the recorded
    history entry will mark the probe as succeeded.

    Returns the boolean probe result.
    """
    repo = repository or MirrorStateRepository()
    state = repo.load(adapter.name)

    if skipped:
        outcome = True
    else:
        try:
            outcome = await adapter.probe_alive(engine)
        except Exception as exc:
            logger.warning(
                "Mirror probe for %s raised an unexpected error: %s",
                chosen_mirror, exc,
            )
            outcome = False

    record = MirrorProbeRecord(
        mirror=chosen_mirror,
        succeeded=outcome,
        checked_at=datetime.now(UTC).isoformat(),
    )

    state.active = chosen_mirror if outcome else state.active
    state.history = [*state.history, record]
    repo.save(adapter.name, state)

    if not outcome:
        logger.warning(
            "Mirror %s for adapter '%s' did not respond to probe_alive; "
            "this session continues to use it because automatic mirror "
            "switching is not yet implemented. Pass --mirror to override.",
            chosen_mirror, adapter.name,
        )
    else:
        logger.info("Active mirror: %s (adapter '%s')", chosen_mirror, adapter.name)

    return outcome


__all__ = [
    "MirrorProbeRecord",
    "MirrorState",
    "MirrorStateRepository",
    "record_probe_outcome",
    "select_initial_mirror",
]
