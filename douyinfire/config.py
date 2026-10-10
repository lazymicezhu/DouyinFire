from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .identities import validate_contact_identity
from .runtime import default_config_path

DEFAULT_CONFIG_PATH = default_config_path()
DEFAULT_DATA_DIR = Path("data")
DEFAULT_LOG_DIR = Path("logs")
DEFAULT_SCREENSHOT_DIR = Path("screenshots")
_CONFIG_LOCK = threading.RLock()


class ConfigError(ValueError):
    """Raised when the DouyinFire config is missing or invalid."""


def stable_id(kind: str, value: str) -> str:
    return uuid5(NAMESPACE_URL, f"douyinfire:{kind}:{value}").hex


@dataclass(slots=True)
class ScheduleConfig:
    time: str = "00:05"
    jitter_minutes: int = 20
    min_contact_interval_seconds: int = 10
    enabled: bool = False
    timezone: str = "Asia/Hong_Kong"
    catchup_minutes: int = 180
    extra: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class TimeoutsConfig:
    home_ready_seconds: int = 5
    message_panel_seconds: int = 8
    contact_search_seconds: int = 15
    input_box_seconds: int = 10
    after_send_seconds: int = 2
    extra: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class BrowserConfig:
    backend: str = "playwright"
    run_headless: bool = True
    login_headless: bool = False
    storage_state_path: Path = Path("data/states/{user}.json")
    extra: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class ContactConfig:
    name: str
    profile_url: str = ""
    message: str = ""
    id: str = ""
    enabled: bool = True
    identity: str = ""
    extra: dict[str, Any] = field(default_factory=dict, repr=False)
    kind: str = "direct"

    def __post_init__(self) -> None:
        if not self.id:
            namespace = "group-contact" if self.kind == "group" else "contact"
            self.id = stable_id(namespace, self.identity or self.profile_url or self.name)


@dataclass(slots=True)
class UserConfig:
    name: str
    contacts: list[ContactConfig]
    message: str
    enabled: bool = True
    id: str = ""
    identity: str = ""
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = stable_id("user", self.identity or self.name)


@dataclass(slots=True)
class AppConfig:
    users: list[UserConfig]
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    data_dir: Path = DEFAULT_DATA_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    screenshot_dir: Path = DEFAULT_SCREENSHOT_DIR
    failure_notify_threshold: int = 1
    headless: bool = True
    migration_warnings: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def user(self, name: str) -> UserConfig:
        for user in self.users:
            if user.id == name:
                return user
        for user in self.users:
            if user.name == name:
                return user
        raise ConfigError(f"User not found in config: {name}")

    @property
    def enabled_users(self) -> list[UserConfig]:
        return [user for user in self.users if user.enabled]


def _extra(raw: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in raw.items() if key not in keys and key != "extra"}


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AppConfig:
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise ConfigError(f"Config file does not exist: {config_path}")
    return parse_config(_read_config_file(config_path), base_dir=config_path.resolve().parent.parent)


def parse_config(raw: dict[str, Any], base_dir: Path | None = None) -> AppConfig:
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be an object")
    users_raw = raw.get("users")
    if not isinstance(users_raw, list):
        raise ConfigError("Config users must be a list (empty is allowed during setup)")
    users = [_parse_user(item, index) for index, item in enumerate(users_raw)]
    _unique([user.id for user in users], "user IDs")
    _unique([user.name for user in users], "user names")
    _unique([user.identity for user in users if user.identity], "account identities")
    base = Path(base_dir) if base_dir is not None else Path(".")
    notes = raw.get("migration_warnings", [])
    if not isinstance(notes, list) or not all(isinstance(item, str) for item in notes):
        raise ConfigError("migration_warnings must be a list of strings")
    notes = list(notes)
    browser_raw = {} if raw.get("browser") is None else raw.get("browser")
    if isinstance(browser_raw, dict) and browser_raw.get("backend") == "cloakbrowser":
        notes.append("旧 CloakBrowser 配置已迁移为 Playwright；首次后台运行前请确认各账号登录态。")
    return AppConfig(
        users=users,
        schedule=_parse_schedule(raw.get("schedule", {})),
        timeouts=_parse_timeouts(raw.get("timeouts", {})),
        browser=_parse_browser(browser_raw, base, raw),
        data_dir=_path_from(raw.get("data_dir", str(DEFAULT_DATA_DIR)), base),
        log_dir=_path_from(raw.get("log_dir", str(DEFAULT_LOG_DIR)), base),
        screenshot_dir=_path_from(raw.get("screenshot_dir", str(DEFAULT_SCREENSHOT_DIR)), base),
        failure_notify_threshold=_positive_int(raw.get("failure_notify_threshold", 1), "failure_notify_threshold", False),
        headless=_bool(raw.get("headless", True), "headless"),
        migration_warnings=list(dict.fromkeys(notes)),
        extra=_extra(raw, {"users", "schedule", "timeouts", "browser", "data_dir", "log_dir", "screenshot_dir", "failure_notify_threshold", "headless", "migration_warnings", "schema_version"}),
    )


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    from dataclasses import fields, is_dataclass

    def plain(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, list):
            return [plain(item) for item in value]
        if is_dataclass(value):
            result = copy.deepcopy(getattr(value, "extra", {}))
            result.update({item.name: plain(getattr(value, item.name)) for item in fields(value) if item.name != "extra"})
            return result
        return copy.deepcopy(value)

    result = plain(config)
    result["schema_version"] = 2
    return result


def save_config(config: AppConfig | dict[str, Any], path: Path | str = DEFAULT_CONFIG_PATH) -> Path:
    """Validate before replacing the destination, preserve JSON/YAML, use 0600."""
    target = Path(path).expanduser()
    with _CONFIG_LOCK:
        raw = config_to_dict(config) if isinstance(config, AppConfig) else copy.deepcopy(config)
        # AppConfig paths have already been resolved relative to their source.
        validated = parse_config(raw, base_dir=Path(".") if isinstance(config, AppConfig) else target.resolve().parent.parent)
        for attr in ("data_dir", "log_dir", "screenshot_dir"):
            setattr(validated, attr, getattr(validated, attr).resolve())
        validated.browser.storage_state_path = validated.browser.storage_state_path.resolve()
        data = config_to_dict(validated)
        if target.suffix.lower() == ".json":
            text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        else:
            try:
                import yaml
            except ImportError as exc:
                raise ConfigError("PyYAML is required to save YAML configuration") from exc
            text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return target


def patch_user(config: AppConfig, user_id: str, updates: dict[str, Any]) -> AppConfig:
    if not isinstance(updates, dict):
        raise ConfigError("User updates must be an object")
    raw = config_to_dict(config)
    for user in raw["users"]:
        if user["id"] == user_id:
            if "id" in updates and updates["id"] != user_id:
                raise ConfigError("Account ID cannot be changed")
            user.update(copy.deepcopy(updates))
            return parse_config(raw)
    raise ConfigError(f"User not found in config: {user_id}")


def patch_config(config: AppConfig, updates: dict[str, Any]) -> AppConfig:
    raw = config_to_dict(config)
    def merge(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)
    merge(raw, updates)
    return parse_config(raw)


def ensure_runtime_dirs(config: AppConfig) -> None:
    for path in [config.data_dir, config.log_dir, config.screenshot_dir, config.data_dir / "profiles", config.data_dir / "states"]:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)


def write_example_config(path: Path | str = DEFAULT_CONFIG_PATH, overwrite: bool = False) -> Path:
    target = Path(path)
    if target.exists() and not overwrite:
        raise ConfigError(f"Config already exists: {target}")
    # An empty setup is safer than an enabled example recipient.
    return save_config({"users": []}, target)


def _read_config_file(path: Path) -> dict[str, Any]:
    try:
        if path.suffix.lower() == ".json":
            loaded = json.loads(path.read_text(encoding="utf-8"))
        else:
            try:
                import yaml
            except ImportError as exc:
                raise ConfigError("PyYAML is required for YAML configuration") from exc
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read configuration: {exc}") from exc
    except Exception as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc
    return loaded if loaded is not None else {}


def _parse_user(raw: Any, index: int) -> UserConfig:
    prefix = f"users[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{prefix} must be an object")
    name = _required_str(raw, "name", prefix)
    identity = _text(raw.get("identity", ""), f"{prefix}.identity")
    user_id = _id(raw.get("id", stable_id("user", identity or name)), prefix)
    contacts_raw = raw.get("contacts", [])
    if not isinstance(contacts_raw, list):
        raise ConfigError(f"{prefix}.contacts must be a list")
    contacts = [_parse_contact(item, index, pos) for pos, item in enumerate(contacts_raw)]
    _unique([contact.id for contact in contacts], f"{prefix} contact IDs")
    _unique([contact.identity for contact in contacts if contact.identity], f"{prefix} contact identities")
    return UserConfig(name=name, contacts=contacts, message=_text(raw.get("message", ""), f"{prefix}.message"),
                      enabled=_bool(raw.get("enabled", True), f"{prefix}.enabled"), id=user_id, identity=identity,
                      extra=_extra(raw, {"name", "contacts", "message", "enabled", "id", "identity"}))


def _parse_contact(raw: Any, user_index: int, contact_index: int) -> ContactConfig:
    prefix = f"users[{user_index}].contacts[{contact_index}]"
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{prefix} must be a string or object")
    name = _required_str(raw, "name", prefix)
    url = _text(raw.get("profile_url", ""), f"{prefix}.profile_url")
    if url:
        try:
            parsed = urlparse(url)
            valid = parsed.scheme == "https" and parsed.hostname in {"www.douyin.com", "douyin.com", "v.douyin.com"} and not parsed.username and not parsed.password and parsed.port in {None, 443}
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError(f"{prefix}.profile_url must be an HTTPS Douyin URL")
    kind = raw.get("kind", "direct")
    # Keep the old private-message parsing and IDs unchanged. Group identity
    # strings are canonical and must never be silently stripped or normalized.
    identity = raw.get("identity", "") if kind == "group" else _text(raw.get("identity", ""), f"{prefix}.identity")
    try:
        validate_contact_identity(kind, identity)
    except ValueError as exc:
        raise ConfigError(f"{prefix}: {exc}") from exc
    if kind == "group" and url:
        raise ConfigError(f"{prefix}: 群聊不能使用个人主页链接。")
    namespace = "group-contact" if kind == "group" else "contact"
    return ContactConfig(name=name, profile_url=url, message=_text(raw.get("message", ""), f"{prefix}.message"),
                         id=_id(raw.get("id", stable_id(namespace, identity or url or name)), prefix),
                         enabled=_bool(raw.get("enabled", True), f"{prefix}.enabled"), identity=identity,
                         extra=_extra(raw, {"name", "profile_url", "message", "id", "enabled", "identity", "kind"}), kind=kind)


def _object(raw: Any, name: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be an object")
    return raw


def _parse_schedule(raw: Any) -> ScheduleConfig:
    raw = _object(raw, "schedule")
    time_value = _text(raw.get("time", "00:05"), "schedule.time")
    _validate_hhmm(time_value)
    timezone = _text(raw.get("timezone", "Asia/Hong_Kong"), "schedule.timezone")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ConfigError("schedule.timezone must be an IANA timezone") from exc
    jitter = _positive_int(raw.get("jitter_minutes", 20), "schedule.jitter_minutes", True)
    catchup = _positive_int(raw.get("catchup_minutes", 180), "schedule.catchup_minutes", True)
    if jitter > 1439 or catchup > 1439:
        raise ConfigError("Schedule jitter and catch-up must be less than one day")
    return ScheduleConfig(time=time_value, jitter_minutes=jitter,
                          min_contact_interval_seconds=_positive_int(raw.get("min_contact_interval_seconds", 10), "schedule.min_contact_interval_seconds", False),
                          enabled=_bool(raw.get("enabled", False), "schedule.enabled"), timezone=timezone, catchup_minutes=catchup,
                          extra=_extra(raw, {"time", "jitter_minutes", "min_contact_interval_seconds", "enabled", "timezone", "catchup_minutes"}))


def _parse_timeouts(raw: Any) -> TimeoutsConfig:
    raw = _object(raw, "timeouts")
    defaults = {"home_ready_seconds": 5, "message_panel_seconds": 8, "contact_search_seconds": 15, "input_box_seconds": 10, "after_send_seconds": 2}
    return TimeoutsConfig(**{key: _positive_int(raw.get(key, value), f"timeouts.{key}", False) for key, value in defaults.items()}, extra=_extra(raw, set(defaults)))


def _parse_browser(raw: Any, base: Path, root: dict[str, Any]) -> BrowserConfig:
    raw = _object(raw, "browser")
    backend = _text(raw.get("backend", "playwright"), "browser.backend").lower()
    if backend not in {"playwright", "cloakbrowser"}:
        raise ConfigError("browser.backend must be playwright")
    # Keep legacy values readable, but only the supported Playwright backend runs.
    return BrowserConfig(backend="playwright", run_headless=_bool(raw.get("run_headless", True), "browser.run_headless"),
                         login_headless=_bool(raw.get("login_headless", False), "browser.login_headless"),
                         storage_state_path=_path_from(raw.get("storage_state_path", "data/states/{user}.json"), base),
                         extra=_extra(raw, {"backend", "run_headless", "login_headless", "storage_state_path"}))


def _path_from(value: Any, base: Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ConfigError("Runtime paths must be non-empty strings")
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value.strip()


def _required_str(raw: dict[str, Any], key: str, prefix: str) -> str:
    value = _text(raw.get(key), f"{prefix}.{key}")
    if not value:
        raise ConfigError(f"{prefix}.{key} is required")
    return value


def _bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{name} must be true or false, without quotes")
    return value


def _positive_int(value: Any, name: str, allow_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or (isinstance(value, str) and not re.fullmatch(r"\d+", value)):
        raise ConfigError(f"{name} must be an integer")
    number = int(value)
    if number < 0 or (number == 0 and not allow_zero):
        raise ConfigError(f"{name} must be {'zero or positive' if allow_zero else 'greater than zero'}")
    return number


def _id(value: Any, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ConfigError(f"{prefix}.id must be a safe identifier (letters, digits, _ and -)")
    return value


def _unique(values: list[str], name: str) -> None:
    if len(set(values)) != len(values):
        raise ConfigError(f"Duplicate {name}")


def _validate_hhmm(value: str) -> None:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ConfigError("schedule.time must be a valid HH:MM 24-hour time")


EXAMPLE_CONFIG = """# Complete initial setup in the local web interface.
users: []
schedule:
  enabled: false
  time: '00:05'
  timezone: Asia/Hong_Kong
  jitter_minutes: 20
  catchup_minutes: 180
browser:
  backend: playwright
  run_headless: true
  login_headless: false
"""
