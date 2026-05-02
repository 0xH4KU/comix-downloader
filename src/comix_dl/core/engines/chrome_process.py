"""Chrome subprocess utilities for the CDP browser engine.

Module-level helpers split out of :mod:`browser_session` for clarity:

- cross-platform advisory file locks (``_lock_file_handle`` /
  ``_unlock_file_handle``)
- TCP port allocation and probing (``_find_free_port`` /
  ``_is_port_in_use``)
- PID file persistence (``_write_pid_file`` / ``_remove_pid_file``)
- stale-Chrome detection and termination
  (``_cleanup_stale_profile_chrome`` and friends)
- Chrome executable auto-detection (``_find_chrome``)
- the live-session weakref registry (``_LIVE_SESSIONS``) and the
  process-wide atexit handler that walks it

Everything here is module-private: callers in ``browser_session.py``
import the names they need under leading underscores. There is no
intent to expose this module beyond the engine package.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import signal
import socket
import subprocess
import time
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from io import TextIOWrapper

logger = logging.getLogger(__name__)


class _AtExitCleanup(Protocol):
    """Minimal interface the live-session registry relies on."""

    def _atexit_cleanup(self) -> None: ...


# Live BrowserSessionManager-like instances tracked weakly so the
# atexit handler can clean up the Chrome subprocesses each one owns,
# without anyone holding a global pointer that would prevent garbage
# collection.
_LIVE_SESSIONS: weakref.WeakSet[_AtExitCleanup] = weakref.WeakSet()


def _atexit_cleanup_all() -> None:
    """Last-resort cleanup walked over every still-alive session."""
    for session in list(_LIVE_SESSIONS):
        session._atexit_cleanup()


atexit.register(_atexit_cleanup_all)


# -- Cross-platform file locks ---------------------------------------------

def _lock_file_handle(fileobj: TextIOWrapper) -> None:
    """Acquire a non-blocking exclusive file lock."""
    if os.name == "nt":
        import msvcrt

        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    fcntl.flock(fileobj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file_handle(fileobj: TextIOWrapper) -> None:
    """Release a previously acquired file lock."""
    if os.name == "nt":
        import msvcrt

        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    fcntl.flock(fileobj.fileno(), fcntl.LOCK_UN)


# -- TCP port helpers ------------------------------------------------------

def _find_free_port() -> int:
    """Find an available port for CDP."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _is_port_in_use(port: int) -> bool:
    """Check whether a TCP port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


# -- PID file helpers ------------------------------------------------------

def _write_pid_file(pid_file: Path, pid: int) -> None:
    """Persist the most recently launched Chrome PID for crash recovery."""
    with contextlib.suppress(OSError):
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(f"{pid}\n", encoding="utf-8")


def _remove_pid_file(pid_file: Path | None) -> None:
    """Remove a persisted Chrome PID file if present."""
    if pid_file is None:
        return
    with contextlib.suppress(OSError):
        pid_file.unlink(missing_ok=True)


# -- Stale-Chrome detection ------------------------------------------------

def _command_line_for_pid(pid: int) -> str | None:
    """Best-effort command line lookup for a live PID."""
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.exists():
        with contextlib.suppress(OSError):
            raw = proc_cmdline.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="ignore").strip()
            if raw:
                return raw

    if os.name == "nt":
        return None

    with contextlib.suppress(Exception):
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        command = result.stdout.strip()
        if command:
            return command

    return None


def _pid_matches_profile_chrome(pid: int, user_data_dir: Path) -> bool:
    """Return whether *pid* still looks like our Chrome for *user_data_dir*."""
    command = _command_line_for_pid(pid)
    if not command:
        return False

    expected_flag = f"--user-data-dir={user_data_dir}"
    lowered = command.lower()
    return expected_flag in command and ("chrome" in lowered or "chromium" in lowered)


def _terminate_pid(pid: int) -> None:
    """Terminate a live process, escalating to SIGKILL when available."""
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.3)

    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, kill_signal)


def _cleanup_stale_profile_chrome(pid_file: Path, user_data_dir: Path) -> None:
    """Terminate a stale Chrome process previously launched for *user_data_dir*."""
    if not pid_file.exists():
        return

    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        _remove_pid_file(pid_file)
        return

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        _remove_pid_file(pid_file)
        return
    except PermissionError as exc:
        raise RuntimeError(
            f"Chrome profile {user_data_dir} appears to still be in use by PID {pid}. "
            "Close the stale Chrome process and retry.",
        ) from exc

    if not _pid_matches_profile_chrome(pid, user_data_dir):
        _remove_pid_file(pid_file)
        return

    logger.warning("Found stale Chrome for %s (PID %d), terminating before startup", user_data_dir, pid)
    try:
        _terminate_pid(pid)
    except Exception as exc:
        raise RuntimeError(
            f"Chrome profile {user_data_dir} is still being used by stale process {pid}. "
            "Close it and retry.",
        ) from exc

    _remove_pid_file(pid_file)


# -- Chrome executable detection -------------------------------------------

def _find_chrome(system: str) -> str:
    """Auto-detect Chrome executable path for the current platform."""
    import shutil

    if system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/opt/homebrew/bin/chromium",
        ]
        for candidate_path in candidates:
            if Path(candidate_path).exists():
                return candidate_path
        return candidates[0]

    if system == "Linux":
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                return found
        return "google-chrome"

    env_candidates: list[Path] = []
    for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if base:
            env_candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")

    for env_candidate in env_candidates:
        if env_candidate.exists():
            return str(env_candidate)

    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        return found
    return "chrome.exe"


__all__ = [
    "_LIVE_SESSIONS",
    "_AtExitCleanup",
    "_atexit_cleanup_all",
    "_cleanup_stale_profile_chrome",
    "_command_line_for_pid",
    "_find_chrome",
    "_find_free_port",
    "_is_port_in_use",
    "_lock_file_handle",
    "_pid_matches_profile_chrome",
    "_remove_pid_file",
    "_terminate_pid",
    "_unlock_file_handle",
    "_write_pid_file",
]
