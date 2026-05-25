from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config/douyinfire.yaml")
DEFAULT_DATA_DIR = Path("data")
DEFAULT_LOG_DIR = Path("logs")
DEFAULT_SCREENSHOT_DIR = Path("screenshots")


@dataclass(slots=True)
class ScheduleConfig:
    time: str = "00:05"
    jitter_minutes: int = 20
    min_contact_interval_seconds: int = 10


@dataclass(slots=True)
class TimeoutsConfig:
    home_ready_seconds: int = 5
    message_panel_seconds: int = 8
    contact_search_seconds: int = 15
    input_box_seconds: int = 10
    after_send_seconds: int = 2


@dataclass(slots=True)
class BrowserConfig:
    backend: str = "playwright"
    run_headless: bool = False
    login_headless: bool = False
    storage_state_path: Path = Path("data/states/main.json")


@dataclass(slots=True)
class ContactConfig:
    name: str
    profile_url: str = ""
    message: str = ""


@dataclass(slots=True)
class UserConfig:
    name: str
    contacts: list[ContactConfig]
    message: str
    enabled: bool = True


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
    headless: bool = False

    def user(self, name: str) -> UserConfig:
        for user in self.users:
            if user.name == name:
                return user
        raise ConfigError(f"User not found in config: {name}")

    @property
    def enabled_users(self) -> list[UserConfig]:
        return [user for user in self.users if user.enabled]


class ConfigError(ValueError):
    """Raised when the DouyinFire config is missing or invalid."""


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")

    raw = _read_config_file(config_path)
    return parse_config(raw, base_dir=config_path.parent.parent)


def parse_config(raw: dict[str, Any], base_dir: Path | None = None) -> AppConfig:
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be an object")

    users_raw = raw.get("users")
    if not isinstance(users_raw, list) or not users_raw:
        raise ConfigError("Config must contain at least one user")

    users = [_parse_user(item, index) for index, item in enumerate(users_raw)]
    schedule = _parse_schedule(raw.get("schedule", {}))
    timeouts = _parse_timeouts(raw.get("timeouts", {}))
    base = base_dir or Path(".")
    browser = _parse_browser(raw.get("browser", {}), base, raw)

    return AppConfig(
        users=users,
        schedule=schedule,
        timeouts=timeouts,
        browser=browser,
        data_dir=_path_from(raw.get("data_dir", str(DEFAULT_DATA_DIR)), base),
        log_dir=_path_from(raw.get("log_dir", str(DEFAULT_LOG_DIR)), base),
        screenshot_dir=_path_from(raw.get("screenshot_dir", str(DEFAULT_SCREENSHOT_DIR)), base),
        failure_notify_threshold=_positive_int(raw.get("failure_notify_threshold", 1), "failure_notify_threshold", allow_zero=False),
        headless=bool(raw.get("headless", False)),
    )


def ensure_runtime_dirs(config: AppConfig) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    config.screenshot_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "profiles").mkdir(parents=True, exist_ok=True)
    (config.data_dir / "states").mkdir(parents=True, exist_ok=True)


def write_example_config(path: Path | str = DEFAULT_CONFIG_PATH, overwrite: bool = False) -> Path:
    config_path = Path(path)
    if config_path.exists() and not overwrite:
        raise ConfigError(f"Config already exists: {config_path}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    return config_path


def _read_config_file(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is required for YAML config files. Install requirements.txt first.") from exc

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded or {}


def _parse_user(raw: Any, index: int) -> UserConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"User #{index + 1} must be an object")

    name = _required_str(raw, "name", f"users[{index}]")
    contacts_raw = raw.get("contacts")
    if not isinstance(contacts_raw, list) or not contacts_raw:
        raise ConfigError(f"users[{index}].contacts must be a non-empty list")
    contacts = [_parse_contact(item, index, contact_index) for contact_index, item in enumerate(contacts_raw)]

    message = _required_str(raw, "message", f"users[{index}]")
    return UserConfig(
        name=name,
        contacts=contacts,
        message=message,
        enabled=bool(raw.get("enabled", True)),
    )


def _parse_contact(raw: Any, user_index: int, contact_index: int) -> ContactConfig:
    prefix = f"users[{user_index}].contacts[{contact_index}]"
    if isinstance(raw, str):
        name = raw.strip()
        if not name:
            raise ConfigError(f"{prefix} must not be empty")
        return ContactConfig(name=name)

    if not isinstance(raw, dict):
        raise ConfigError(f"{prefix} must be a string or object")

    name = _required_str(raw, "name", prefix)
    return ContactConfig(
        name=name,
        profile_url=str(raw.get("profile_url", "")).strip(),
        message=str(raw.get("message", "")).strip(),
    )


def _parse_schedule(raw: Any) -> ScheduleConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("schedule must be an object")

    time_value = str(raw.get("time", "00:05"))
    _validate_hhmm(time_value)
    return ScheduleConfig(
        time=time_value,
        jitter_minutes=_positive_int(raw.get("jitter_minutes", 20), "schedule.jitter_minutes", allow_zero=True),
        min_contact_interval_seconds=_positive_int(
            raw.get("min_contact_interval_seconds", 10),
            "schedule.min_contact_interval_seconds",
            allow_zero=False,
        ),
    )


def _parse_timeouts(raw: Any) -> TimeoutsConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("timeouts must be an object")

    return TimeoutsConfig(
        home_ready_seconds=_positive_int(raw.get("home_ready_seconds", 5), "timeouts.home_ready_seconds", allow_zero=False),
        message_panel_seconds=_positive_int(raw.get("message_panel_seconds", 8), "timeouts.message_panel_seconds", allow_zero=False),
        contact_search_seconds=_positive_int(raw.get("contact_search_seconds", 15), "timeouts.contact_search_seconds", allow_zero=False),
        input_box_seconds=_positive_int(raw.get("input_box_seconds", 10), "timeouts.input_box_seconds", allow_zero=False),
        after_send_seconds=_positive_int(raw.get("after_send_seconds", 2), "timeouts.after_send_seconds", allow_zero=False),
    )


def _parse_browser(raw: Any, base: Path, root: dict[str, Any]) -> BrowserConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("browser must be an object")

    backend = str(raw.get("backend", "playwright")).strip().lower()
    if backend not in {"playwright", "cloakbrowser"}:
        raise ConfigError("browser.backend must be either playwright or cloakbrowser")

    legacy_headless = bool(root.get("headless", False))
    storage_state = _path_from(raw.get("storage_state_path", "data/states/main.json"), base)
    return BrowserConfig(
        backend=backend,
        run_headless=bool(raw.get("run_headless", legacy_headless)),
        login_headless=bool(raw.get("login_headless", False)),
        storage_state_path=storage_state,
    )


def _path_from(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    return base / path


def _required_str(raw: dict[str, Any], key: str, prefix: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{prefix}.{key} is required")
    return value.strip()


def _positive_int(value: Any, name: str, allow_zero: bool) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if number < 0 or (number == 0 and not allow_zero):
        raise ConfigError(f"{name} must be greater than zero")
    return number


def _validate_hhmm(value: str) -> None:
    parts = value.split(":")
    if len(parts) != 2:
        raise ConfigError("schedule.time must use HH:MM format")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ConfigError("schedule.time must use HH:MM format") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError("schedule.time must be a valid 24-hour time")


EXAMPLE_CONFIG = """# Copy this file to config/douyinfire.yaml and edit it locally.
data_dir: data
log_dir: logs
screenshot_dir: screenshots
headless: false
failure_notify_threshold: 1

schedule:
  time: "00:05"
  jitter_minutes: 20
  min_contact_interval_seconds: 10

timeouts:
  home_ready_seconds: 5
  message_panel_seconds: 8
  contact_search_seconds: 15
  input_box_seconds: 10
  after_send_seconds: 2

browser:
  backend: playwright
  run_headless: false
  login_headless: false
  storage_state_path: data/states/main.json

users:
  - name: "main"
    enabled: true
    contacts:
      - name: "联系人备注名"
        profile_url: "https://www.douyin.com/user/..."
    message: "续火花咯"
"""
