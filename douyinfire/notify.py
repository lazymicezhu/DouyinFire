from __future__ import annotations

import logging
import platform
import subprocess


def notify(title: str, message: str) -> bool:
    if platform.system() != "Darwin":
        logging.info("Notification skipped on non-macOS platform: %s - %s", title, message)
        return False

    script = f'display notification {message!r} with title {title!r}'
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
        return True
    except Exception as exc:
        logging.warning("Failed to send macOS notification: %s", exc)
        return False
