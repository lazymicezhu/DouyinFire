"""Application-owned paths and explicit, reversible legacy migration."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def default_app_dir() -> Path:
    override = os.environ.get("DOUYINFIRE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / "DouyinFire"


def default_config_path() -> Path:
    return default_app_dir() / "config" / "douyinfire.yaml"


@dataclass
class MigrationResult:
    config_path: Path
    migrated: bool = False
    backup_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)


def _private_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)


def _copy_profile(source: Path, target: Path) -> None:
    # Chromium's singleton entries are live-process coordination, never login data.
    if (source / "SingletonLock").exists() or (source / "SingletonLock").is_symlink():
        raise RuntimeError("请先关闭旧版登录浏览器，再迁移登录态。")
    shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns("Singleton*", "DevToolsActivePort"))
    # Do not let copied profile symlinks escape the application directory.
    for entry in target.rglob("*"):
        if entry.is_symlink():
            entry.unlink()
    _private_tree(target)


def migrate_legacy(legacy_project: Path | str, destination: Path | str | None = None) -> MigrationResult:
    """Copy legacy data once; never modify the old project or register a service.

    New configuration is published last. If interrupted, no partially migrated
    configuration becomes active and a later attempt can recover safely.
    """
    from .config import config_to_dict, load_config, parse_config, save_config

    source_root = Path(legacy_project).expanduser().resolve()
    dest = Path(destination).expanduser().resolve() if destination else default_app_dir()
    config_path = dest / "config" / "douyinfire.yaml"
    if config_path.exists():
        return MigrationResult(config_path=config_path)
    candidates = [source_root / "config" / "douyinfire.yaml", source_root / "config" / "douyinfire.json"]
    source_config = next((path for path in candidates if path.is_file()), None)
    if source_config is None:
        return MigrationResult(config_path=config_path, warnings=["未发现旧配置，请在网页中添加账号。"])
    if dest == source_root or source_root in dest.parents:
        raise ValueError("迁移目标必须位于旧项目目录之外。")

    config = load_config(source_config)
    warnings = list(config.migration_warnings)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = dest / "backups" / ("legacy-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(source_config, backup / source_config.name)
    # Backups include source data without following symlinks. Keep them private.
    for label, old_path in (("profiles", config.data_dir / "profiles"), ("states", config.data_dir / "states")):
        if old_path.is_dir():
            shutil.copytree(old_path, backup / label, symlinks=True)
    _private_tree(backup)

    staging = Path(tempfile.mkdtemp(prefix=".migration-", dir=dest))
    try:
        data = config_to_dict(config)
        data.update(data_dir=str(dest / "data"), log_dir=str(dest / "logs"), screenshot_dir=str(dest / "screenshots"))
        data["browser"].update(backend="playwright", run_headless=True, login_headless=False,
                               storage_state_path=str(dest / "data" / "states" / "{user}.json"))
        # Never activate an old unattended schedule while importing accounts.
        data["schedule"]["enabled"] = False
        for user in config.users:
            legacy_profile = (config.data_dir / "profiles" / user.name).resolve()
            profile_root = (config.data_dir / "profiles").resolve()
            if profile_root not in legacy_profile.parents:
                warnings.append(f"账号 {user.name} 的旧路径不安全，需重新登录。")
                continue
            if legacy_profile.is_dir():
                _copy_profile(legacy_profile, staging / user.id)
            else:
                warnings.append(f"账号 {user.name} 未找到独立浏览器登录态，需重新登录。")
        warnings.append("旧版发送记录未自动标记为已确认；首次发送前请核实当天是否已经发送。")
        data["migration_warnings"] = list(dict.fromkeys(warnings))
        migrated = parse_config(data, base_dir=dest)
        profiles = dest / "data" / "profiles"
        profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
        for staged in staging.iterdir():
            target = profiles / staged.name
            if not target.exists():
                staged.rename(target)
        save_config(migrated, config_path)
        marker = dest / "migration.json"
        marker.write_text(json.dumps({"source": str(source_root), "backup": str(backup), "completed_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False), encoding="utf-8")
        marker.chmod(0o600)
        return MigrationResult(config_path, True, backup, data["migration_warnings"])
    finally:
        shutil.rmtree(staging, ignore_errors=True)
