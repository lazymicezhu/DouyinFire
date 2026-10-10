from __future__ import annotations

import logging
import platform
import subprocess

_NOTIFICATION_SCRIPT = """on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
"""


def notify(title: str, message: str) -> bool:
    if platform.system() != "Darwin":
        logging.info("Notification skipped on non-macOS platform")
        return False
    try:
        subprocess.run(["osascript", "-e", _NOTIFICATION_SCRIPT, "--", str(title), str(message)],
                       check=True, capture_output=True, text=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("Failed to send macOS notification: %s", exc)
        return False
