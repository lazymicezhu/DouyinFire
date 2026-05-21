from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH


LABEL = "com.lazymice.douyinfire"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install_service(project_dir: Path, config_path: Path = DEFAULT_CONFIG_PATH) -> Path:
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    python_bin = Path(sys.executable)
    script = [
        str(python_bin),
        "-m",
        "douyinfire.cli",
        "run-all",
        "--config",
        str((project_dir / config_path).resolve() if not config_path.is_absolute() else config_path),
        "--jitter",
    ]
    plist = {
        "Label": LABEL,
        "ProgramArguments": script,
        "WorkingDirectory": str(project_dir.resolve()),
        "StartCalendarInterval": {"Hour": 0, "Minute": 5},
        "StandardOutPath": str((project_dir / "logs" / "launchd.out.log").resolve()),
        "StandardErrorPath": str((project_dir / "logs" / "launchd.err.log").resolve()),
        "RunAtLoad": False,
    }
    with target.open("wb") as fh:
        plistlib.dump(plist, fh)
    _launchctl("unload", target, check=False)
    _launchctl("load", target, check=False)
    return target


def uninstall_service() -> bool:
    target = plist_path()
    _launchctl("unload", target, check=False)
    if target.exists():
        target.unlink()
        return True
    return False


def service_status() -> str:
    target = plist_path()
    if not target.exists():
        return "not installed"
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
    if LABEL in result.stdout:
        return "installed and loaded"
    return "installed but not loaded"


def _launchctl(action: str, target: Path, check: bool) -> None:
    if os.uname().sysname != "Darwin":
        return
    subprocess.run(["launchctl", action, str(target)], check=check, capture_output=True, text=True)
