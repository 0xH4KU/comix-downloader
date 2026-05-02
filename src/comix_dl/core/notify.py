"""Desktop notifications — platform-aware, best-effort.

On macOS uses ``osascript``; on Linux uses ``notify-send``.
Silently does nothing if the notification tool is unavailable.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess

logger = logging.getLogger(__name__)


def send_notification(title: str, body: str) -> None:
    """Send a desktop notification (best-effort, never raises)."""
    try:
        system = platform.system()
        if system == "Darwin":
            _notify_macos(title, body)
        elif system == "Linux":
            _notify_linux(title, body)
        else:
            logger.debug("Notifications not supported on %s", system)
    except Exception as exc:
        logger.debug("Notification failed: %s", exc)


def _notify_macos(title: str, body: str) -> None:
    """Send notification via osascript on macOS."""
    # Escape backslashes first, then double quotes (order matters)
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        timeout=5,
    )


def _notify_linux(title: str, body: str) -> None:
    """Send notification via notify-send on Linux."""
    if not shutil.which("notify-send"):
        return
    subprocess.run(
        ["notify-send", title, body],
        capture_output=True,
        timeout=5,
    )
