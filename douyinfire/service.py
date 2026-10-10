"""Manage the per-user resident backend, never a second sending scheduler."""
from __future__ import annotations

import os
import platform
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, load_config

LABEL = "com.lazymice.douyinfire"


class ServiceError(RuntimeError):
    pass


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"无法操作 macOS 后台服务：{exc}") from exc


def _require_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode:
        raise ServiceError(f"{action}失败（{result.returncode}）：{(result.stderr or result.stdout).strip()}")


def _unload_if_loaded() -> bool:
    target = f"{_domain()}/{LABEL}"
    status = _run("print", target)
    if status.returncode:
        # launchctl uses 113 for a missing service. Do not hide permission errors.
        error = (status.stderr or status.stdout).lower()
        if status.returncode in {3, 113} or "could not find service" in error:
            return False
        _require_success(status, "查询后台服务")
    _require_success(_run("bootout", target), "停止后台服务")
    return True


def install_service(project_dir: Path | None = None, config_path: Path | None = None, *, executable: Path | str | None = None, python_bin: Path | str | None = None, daemon_command: str = "background") -> Path:
    if platform.system() != "Darwin":
        raise ServiceError("后台常驻服务仅支持 macOS")
    workdir = (Path(project_dir) if project_dir else Path(config_path or DEFAULT_CONFIG_PATH).parent.parent).expanduser().resolve()
    config_file = Path(config_path or DEFAULT_CONFIG_PATH).expanduser()
    config_file = config_file.resolve() if config_file.is_absolute() else (workdir / config_file).resolve()
    config = load_config(config_file)
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    workdir.mkdir(parents=True, exist_ok=True)

    frozen = executable is not None or bool(getattr(sys, "frozen", False))
    binary = Path(executable or python_bin or sys.executable).expanduser().absolute()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ServiceError(f"后台程序不可执行：{binary}")
    command = [str(binary)] if frozen else [str(binary), "-m", "douyinfire.cli"]
    command += [daemon_command, "--config", str(config_file)]
    environment = {"PYTHONUNBUFFERED": "1", "DOUYINFIRE_HOME": str(config_file.parent.parent)}
    for name in ("PLAYWRIGHT_BROWSERS_PATH",):
        if name in os.environ:
            environment[name] = os.environ[name]
    plist = {
        "Label": LABEL, "ProgramArguments": command, "WorkingDirectory": str(workdir),
        "RunAtLoad": True, "StartInterval": 60, "ThrottleInterval": 15,
        "StandardOutPath": str(config.log_dir / "launchd.out.log"),
        "StandardErrorPath": str(config.log_dir / "launchd.err.log"),
        "EnvironmentVariables": environment,
    }
    encoded = plistlib.dumps(plist)
    previous = target.read_bytes() if target.exists() else None
    was_loaded = _unload_if_loaded()
    loaded = False
    fd, temporary = tempfile.mkstemp(prefix=f".{LABEL}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _require_success(_run("bootstrap", _domain(), str(target)), "载入后台服务")
        loaded = True
        _require_success(_run("print", f"{_domain()}/{LABEL}"), "确认后台服务")
    except Exception as original:
        rollback_errors = []
        if loaded:
            try:
                _require_success(_run("bootout", f"{_domain()}/{LABEL}"), "撤销新后台服务")
            except ServiceError as exc:
                rollback_errors.append(str(exc))
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(previous)
            target.chmod(0o600)
            if was_loaded:
                try:
                    _require_success(_run("bootstrap", _domain(), str(target)), "恢复旧后台服务")
                except ServiceError as exc:
                    rollback_errors.append(str(exc))
        if rollback_errors:
            raise ServiceError(f"{original}；恢复时发生错误：{'; '.join(rollback_errors)}") from original
        raise
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def uninstall_service() -> bool:
    if platform.system() != "Darwin":
        raise ServiceError("后台常驻服务仅支持 macOS")
    target = plist_path()
    existed = target.exists()
    unloaded = _unload_if_loaded()
    target.unlink(missing_ok=True)
    return existed or unloaded


def service_status() -> str:
    if platform.system() != "Darwin":
        return "unsupported platform"
    target = plist_path()
    if not target.exists():
        return "not installed"
    try:
        result = _run("print", f"{_domain()}/{LABEL}")
    except ServiceError as exc:
        return f"status unavailable: {exc}"
    if result.returncode == 0:
        return "installed and loaded"
    return f"installed but not loaded: {(result.stderr or result.stdout).strip()}"
