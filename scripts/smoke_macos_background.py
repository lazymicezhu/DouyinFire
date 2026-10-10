#!/usr/bin/env python3
"""Explicit macOS integration smoke test with a disposable launchd label.

Uses an empty, private configuration and a supplied locally built .app. It does
not launch a browser, visit Douyin, send messages, or touch the production label.
Every registered test job is removed in finally; the verified test server is
stopped separately without invoking the GUI's production service controls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import traceback
from pathlib import Path
from uuid import uuid4

# A path entry is a Python string, not a colon-separated environment variable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from douyinfire import service
from douyinfire.config import save_config
from douyinfire.desktop import _config_identity, _metadata_path, existing_server


def file_digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--verify-recovery", action="store_true",
                        help="Also stop the isolated backend and wait for the 60-second watchdog to restore it")
    args = parser.parse_args()
    app = args.app.expanduser().resolve()
    executable = app / "Contents" / "MacOS" / "DouyinFire"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise SystemExit(f"Packaged executable not found: {executable}")
    root = Path(tempfile.mkdtemp(prefix="douyinfire-launchd-smoke-"))
    config_path = root / "config" / "smoke.json"
    save_config({"users": [], "schedule": {"enabled": False}}, config_path)
    label = "com.lazymice.douyinfire.smoke." + uuid4().hex
    target = root / f"{label}.plist"
    original_label, original_path_function = service.LABEL, service.plist_path
    production_path = original_path_function()
    production_digest = file_digest(production_path)
    report = {"ok": False, "app": str(app), "test_label": label, "test_root": str(root),
              "empty_accounts": True, "cleanup_errors": []}
    service.LABEL = label
    service.plist_path = lambda: target
    server_info = None
    try:
        service.install_service(root, config_path, executable=executable)
        report["registered_status"] = service.service_status()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if existing_server(config_path):
                server_info = json.loads(_metadata_path(config_path).read_text())
                break
            time.sleep(.2)
        if server_info is None:
            raise RuntimeError("Isolated test server did not become healthy")
        if server_info.get("config_id") != _config_identity(config_path):
            raise RuntimeError("Test server configuration identity did not match")
        report["server_pid"] = server_info["pid"]
        report["server_healthy"] = True
        # Background is a short watchdog launcher. Its detached backend must
        # remain healthy after the launcher's initial invocation has returned.
        time.sleep(2)
        report["detached_backend_survived"] = existing_server(config_path) is not None
        report["ok"] = report["registered_status"] == "installed and loaded" and report["detached_backend_survived"]
        if args.verify_recovery:
            original_pid = int(server_info["pid"])
            os.kill(original_pid, signal.SIGTERM)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and _metadata_path(config_path).exists():
                time.sleep(.1)
            if _metadata_path(config_path).exists():
                raise RuntimeError("The isolated backend did not stop before recovery testing")
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                if existing_server(config_path):
                    recovered = json.loads(_metadata_path(config_path).read_text())
                    if int(recovered["pid"]) != original_pid:
                        report["recovered_server_pid"] = int(recovered["pid"])
                        report["watchdog_recovered_backend"] = True
                        break
                time.sleep(.25)
            if not report.get("watchdog_recovered_backend"):
                raise RuntimeError("The 60-second watchdog did not restore the isolated backend")
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        try:
            service.uninstall_service()
            report["test_registration_removed"] = service.service_status() == "not installed"
            unloaded = service._run("print", f"gui/{os.getuid()}/{label}")
            report["test_job_unloaded"] = unloaded.returncode != 0 and "could not find service" in (unloaded.stderr + unloaded.stdout).lower()
        except Exception as exc:
            report["cleanup_errors"].append(f"Test launchd cleanup: {exc}")
        try:
            # Trust only our private metadata and matching loopback health.
            if existing_server(config_path):
                info = json.loads(_metadata_path(config_path).read_text())
                if info.get("config_id") != _config_identity(config_path):
                    raise RuntimeError("Refusing to stop a server with a different config ID")
                os.kill(int(info["pid"]), signal.SIGTERM)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and _metadata_path(config_path).exists():
                    time.sleep(.1)
            report["test_server_stopped"] = not _metadata_path(config_path).exists()
        except Exception as exc:
            report["cleanup_errors"].append(f"Test server cleanup: {exc}")
        finally:
            service.LABEL, service.plist_path = original_label, original_path_function
        report["production_plist_unchanged"] = file_digest(production_path) == production_digest
        report["ok"] = bool(report["ok"] and not report["cleanup_errors"] and report.get("test_registration_removed")
                            and report.get("test_job_unloaded") and report.get("test_server_stopped")
                            and report["production_plist_unchanged"]
                            and (not args.verify_recovery or report.get("watchdog_recovered_backend")))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
