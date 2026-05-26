from __future__ import annotations

import json
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import ContactConfig, DEFAULT_CONFIG_PATH, ConfigError, UserConfig, ensure_runtime_dirs, load_config, parse_config, write_example_config
from .gui_html import HTML
from .service import service_status, uninstall_service
from .tasks import login_user, run_all, run_user


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.running = False
        self.kind = ""
        self.message = "idle"
        self.result: Any = None
        self.error = ""
        self.steps = _initial_steps()
        self.started_at = ""
        self.ended_at = ""
        self.last_failed: dict[str, list[dict[str, str]]] = {}

    def start(self, kind: str) -> bool:
        with self.lock:
            preserve_failed = kind.startswith("retry-failed")
            if self.running:
                return False
            self.running = True
            self.cancel_event.clear()
            self.kind = kind
            self.message = "running"
            self.result = None
            self.error = ""
            self.steps = _initial_steps()
            self.started_at = _now()
            self.ended_at = ""
            if not preserve_failed:
                self.last_failed = {}
            return True

    def interrupt(self) -> bool:
        with self.lock:
            if not self.running:
                return False
            self.cancel_event.set()
            self.message = "interrupting"
            self._mark_running_step("failed", "已中断")
            return True

    def is_interrupted(self) -> bool:
        return self.cancel_event.is_set()

    def finish(self, message: str, result: Any = None, error: str = "") -> None:
        with self.lock:
            if error:
                self._mark_running_step("failed", error)
            self.running = False
            self.message = "interrupted" if self.cancel_event.is_set() and not error else message
            self.result = result
            self.error = error
            self.ended_at = _now()
            self.last_failed = _failed_contacts_by_user(_as_plain_result(result))

    def update_step(self, key: str, status: str, detail: str = "") -> None:
        with self.lock:
            now = _now()
            for step in self.steps:
                if step["key"] == key:
                    step["status"] = status
                    step["detail"] = detail
                    if status == "running":
                        step["started_at"] = step["started_at"] or now
                    if status in {"done", "failed"}:
                        step["ended_at"] = now
                    return

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = self.result
            result = _as_plain_result(result)
            return {
                "running": self.running,
                "kind": self.kind,
                "message": self.message,
                "result": result,
                "error": self.error,
                "steps": self.steps,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "duration_seconds": _duration_seconds(self.started_at, self.ended_at),
                "last_failed": self.last_failed,
                "cancel_requested": self.cancel_event.is_set(),
            }

    def _mark_running_step(self, status: str, error: str) -> None:
        for step in self.steps:
            if step["status"] == "running":
                step["status"] = status
                step["error"] = error
                step["ended_at"] = _now()
                return


class GuiServer:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.job = JobState()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self) -> None:
                owner.handle_head(self)

            def do_GET(self) -> None:
                owner.handle_get(self)

            def do_POST(self) -> None:
                owner.handle_post(self)

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler

    def handle_head(self, request: BaseHTTPRequestHandler) -> None:
        if urlparse(request.path).path == "/":
            request.send_response(HTTPStatus.OK)
            request.send_header("Content-Type", "text/html; charset=utf-8")
            request.end_headers()
        else:
            request.send_response(HTTPStatus.NOT_FOUND)
            request.end_headers()

    def handle_get(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(request.path)
        if parsed.path == "/":
            _send_html(request, HTML)
        elif parsed.path == "/api/state":
            _send_json(request, self.state())
        elif parsed.path == "/api/config":
            _send_json(request, {"text": self._config_text()})
        elif parsed.path == "/api/config/form":
            _send_json(request, {"config": self._config_form()})
        elif parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["200"])[0])
            _send_json(request, {"text": self._logs(limit)})
        elif parsed.path == "/api/screenshot":
            try:
                query = parse_qs(parsed.query)
                path = str(query.get("path", [""])[0])
                _send_screenshot(request, self._screenshot_path(path))
            except Exception as exc:
                _send_json(request, {"error": str(exc)}, HTTPStatus.NOT_FOUND)
        else:
            _send_json(request, {"error": "not found"}, HTTPStatus.NOT_FOUND)

    def handle_post(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(request.path)
        try:
            payload = _read_json(request)
            if parsed.path == "/api/init":
                write_example_config(self.config_path, overwrite=bool(payload.get("overwrite", False)))
                _send_json(request, {"ok": True})
            elif parsed.path == "/api/config":
                text = str(payload.get("text", ""))
                load_config_from_text(text, self.config_path)
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(text, encoding="utf-8")
                _send_json(request, {"ok": True})
            elif parsed.path == "/api/config/form":
                text = form_payload_to_yaml(payload)
                load_config_from_text(text, self.config_path)
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(text, encoding="utf-8")
                _send_json(request, {"ok": True, "text": text})
            elif parsed.path == "/api/login":
                user = str(payload.get("user", "")).strip()
                wait_seconds = int(payload.get("wait_seconds", 180))
                self._start_job(f"login:{user}", lambda: self._login(user, wait_seconds))
                _send_json(request, {"ok": True})
            elif parsed.path == "/api/login-new":
                user = str(payload.get("user", "")).strip()
                source = str(payload.get("source", "")).strip()
                wait_seconds = int(payload.get("wait_seconds", 180))
                self._add_login_user(user, source)
                self._start_job(f"login:{user}", lambda: self._login(user, wait_seconds))
                _send_json(request, {"ok": True})
            elif parsed.path == "/api/run":
                user = str(payload.get("user", "")).strip()
                self._start_job(f"run:{user}", lambda: self._run_user(user))
                _send_json(request, {"ok": True})
            elif parsed.path == "/api/run-all":
                self._start_job("run-all", self._run_all)
                _send_json(request, {"ok": True})
            elif parsed.path == "/api/retry-failed":
                self._start_job("retry-failed", self._retry_failed)
                _send_json(request, {"ok": True})
            elif parsed.path == "/api/interrupt":
                interrupted = self.job.interrupt()
                _send_json(request, {"ok": True, "interrupted": interrupted})
            elif parsed.path == "/api/service/uninstall":
                removed = uninstall_service()
                _send_json(request, {"ok": True, "removed": removed})
            else:
                _send_json(request, {"error": "not found"}, HTTPStatus.NOT_FOUND)
        except BusyError as exc:
            _send_json(request, {"error": str(exc)}, HTTPStatus.CONFLICT)
        except Exception as exc:
            _send_json(request, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def state(self) -> dict[str, Any]:
        config_info: dict[str, Any]
        try:
            config = load_config(self.config_path)
            ensure_runtime_dirs(config)
            config_info = {
                "ok": True,
                "users": [
                    {
                        "name": user.name,
                        "enabled": user.enabled,
                        "contacts": [asdict(contact) for contact in user.contacts],
                        "profile": (config.data_dir / "profiles" / user.name).exists(),
                        "storage_state": _storage_state_path_for_user(config.browser.storage_state_path, user.name).exists(),
                    }
                    for user in config.users
                ],
                "paths": {
                    "config": str(self.config_path),
                    "log": str(config.log_dir / "douyinfire.log"),
                    "data": str(config.data_dir),
                    "screenshots": str(config.screenshot_dir),
                },
                "timeouts": asdict(config.timeouts),
                "browser": _browser_to_json(config.browser),
            }
        except Exception as exc:
            config_info = {"ok": False, "error": str(exc), "users": [], "paths": {"config": str(self.config_path)}}

        return {"config": config_info, "job": self.job.snapshot(), "service": service_status()}

    def _start_job(self, kind: str, target: Any) -> None:
        if not self.job.start(kind):
            raise BusyError("another job is already running")

        def wrapped() -> None:
            try:
                result = target()
                self.job.finish("completed", result=result)
            except Exception as exc:
                self.job.finish("failed", error=str(exc))

        threading.Thread(target=wrapped, daemon=True).start()

    def _login(self, user_name: str, wait_seconds: int) -> dict[str, str]:
        config = load_config(self.config_path)
        login_user(config, config.user(user_name), wait_seconds=wait_seconds)
        return {"user": user_name, "status": "login window closed"}

    def _add_login_user(self, user_name: str, source_name: str = "") -> None:
        if not user_name:
            raise ConfigError("请输入用户名")

        config = load_config(self.config_path)
        if any(user.name == user_name for user in config.users):
            return

        source = config.user(source_name) if source_name else config.users[0]
        users = list(config.users)
        users.append(
            UserConfig(
                name=user_name,
                enabled=True,
                contacts=[
                    ContactConfig(name=contact.name, profile_url=contact.profile_url, message=contact.message)
                    for contact in source.contacts
                ],
                message=source.message,
            )
        )

        data = _config_to_yaml_data(config, users)
        data["browser"]["storage_state_path"] = "data/states/{user}.json"
        text = _dump_yaml(data)
        load_config_from_text(text, self.config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(text, encoding="utf-8")

    def _run_user(self, user_name: str) -> Any:
        config = load_config(self.config_path)
        return run_user(config, config.user(user_name), progress=self.job.update_step, should_stop=self.job.is_interrupted)

    def _run_all(self) -> Any:
        config = load_config(self.config_path)
        return run_all(config, progress=self.job.update_step, should_stop=self.job.is_interrupted)

    def _retry_failed(self) -> Any:
        failed = self.job.snapshot().get("last_failed", {})
        if not failed:
            raise ConfigError("没有可重试失败项")

        config = load_config(self.config_path)
        user_results = []
        for user_name, contacts_raw in failed.items():
            if not contacts_raw:
                continue
            original = config.user(user_name)
            retry_user = UserConfig(
                name=original.name,
                enabled=original.enabled,
                message=original.message,
                contacts=[
                    ContactConfig(
                        name=str(item.get("name", "")),
                        profile_url=str(item.get("profile_url", "")),
                        message=str(item.get("message", "")),
                    )
                    for item in contacts_raw
                    if str(item.get("name", "")).strip()
                ],
            )
            if retry_user.contacts:
                user_results.append(run_user(config, retry_user, progress=self.job.update_step, should_stop=self.job.is_interrupted))
        if not user_results:
            raise ConfigError("没有可重试失败项")
        return user_results[0] if len(user_results) == 1 else {"users": user_results}

    def _config_text(self) -> str:
        if not self.config_path.exists():
            return ""
        return self.config_path.read_text(encoding="utf-8")

    def _config_form(self) -> dict[str, Any]:
        config = load_config(self.config_path)
        return {
            "data_dir": str(config.data_dir),
            "log_dir": str(config.log_dir),
            "screenshot_dir": str(config.screenshot_dir),
            "headless": config.headless,
            "failure_notify_threshold": config.failure_notify_threshold,
            "schedule": asdict(config.schedule),
            "timeouts": asdict(config.timeouts),
            "browser": _browser_to_json(config.browser),
            "users": [asdict(user) for user in config.users],
        }

    def _logs(self, limit: int) -> str:
        try:
            config = load_config(self.config_path)
            path = config.log_dir / "douyinfire.log"
        except Exception:
            path = Path("logs/douyinfire.log")
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-limit:])

    def _screenshot_path(self, raw_path: str) -> Path:
        if not raw_path:
            raise ConfigError("missing screenshot path")

        config = load_config(self.config_path)
        screenshot_root = config.screenshot_dir.resolve()
        path = Path(raw_path)
        if not path.is_absolute():
            path = (config.screenshot_dir / path).resolve()
        else:
            path = path.resolve()

        if screenshot_root != path and screenshot_root not in path.parents:
            raise ConfigError("screenshot path is outside screenshot_dir")
        if path.suffix.lower() != ".png" or not path.exists():
            raise ConfigError("screenshot does not exist")
        return path


class BusyError(RuntimeError):
    pass


def load_config_from_text(text: str, config_path: Path) -> None:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is required for YAML config files.") from exc
    raw = yaml.safe_load(text) or {}
    parse_config(raw, base_dir=config_path.parent.parent)


def form_payload_to_yaml(payload: dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is required for YAML config files.") from exc

    users = []
    for item in payload.get("users", []):
        contacts = _contacts_from_payload(item.get("contacts", []))
        users.append(
            {
                "name": str(item.get("name", "")).strip(),
                "enabled": _as_bool(item.get("enabled", True)),
                "contacts": contacts,
                "message": str(item.get("message", "")),
            }
        )

    data = {
        "data_dir": str(payload.get("data_dir", "data")),
        "log_dir": str(payload.get("log_dir", "logs")),
        "screenshot_dir": str(payload.get("screenshot_dir", "screenshots")),
        "headless": _as_bool(payload.get("headless", False)),
        "failure_notify_threshold": int(payload.get("failure_notify_threshold", 1)),
        "schedule": {
            "time": str(payload.get("schedule", {}).get("time", "00:05")),
            "jitter_minutes": int(payload.get("schedule", {}).get("jitter_minutes", 20)),
            "min_contact_interval_seconds": int(payload.get("schedule", {}).get("min_contact_interval_seconds", 10)),
        },
        "timeouts": {
            "home_ready_seconds": int(payload.get("timeouts", {}).get("home_ready_seconds", 5)),
            "message_panel_seconds": int(payload.get("timeouts", {}).get("message_panel_seconds", 8)),
            "contact_search_seconds": int(payload.get("timeouts", {}).get("contact_search_seconds", 15)),
            "input_box_seconds": int(payload.get("timeouts", {}).get("input_box_seconds", 10)),
            "after_send_seconds": int(payload.get("timeouts", {}).get("after_send_seconds", 2)),
        },
        "browser": {
            "backend": str(payload.get("browser", {}).get("backend", "playwright")),
            "run_headless": _as_bool(payload.get("browser", {}).get("run_headless", False)),
            "login_headless": _as_bool(payload.get("browser", {}).get("login_headless", False)),
            "storage_state_path": str(payload.get("browser", {}).get("storage_state_path", "data/states/main.json")),
        },
        "users": users,
    }
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    parse_config(yaml.safe_load(text), base_dir=Path("."))
    return text


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on", "是"}


def _contacts_from_payload(raw: Any) -> list[dict[str, str] | str]:
    if isinstance(raw, list):
        contacts = []
        for item in raw:
            if isinstance(item, dict):
                contacts.append(
                    {
                        "name": str(item.get("name", "")).strip(),
                        "profile_url": str(item.get("profile_url", "")).strip(),
                        "message": str(item.get("message", "")).strip(),
                    }
                )
            elif str(item).strip():
                contacts.append(str(item).strip())
        return contacts

    contacts = []
    for line in str(raw).replace(",", "\n").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if not parts or not parts[0]:
            continue
        if len(parts) == 1:
            contacts.append(parts[0])
        else:
            contacts.append(
                {
                    "name": parts[0],
                    "profile_url": parts[1] if len(parts) > 1 else "",
                    "message": parts[2] if len(parts) > 2 else "",
                }
            )
    return contacts


def _browser_to_json(browser: Any) -> dict[str, Any]:
    return {
        "backend": browser.backend,
        "run_headless": browser.run_headless,
        "login_headless": browser.login_headless,
        "storage_state_path": str(browser.storage_state_path),
    }


def _storage_state_path_for_user(path: Path, user_name: str) -> Path:
    raw = str(path)
    if "{user}" in raw:
        return Path(raw.format(user=user_name))
    return path


def _config_to_yaml_data(config: Any, users: list[UserConfig]) -> dict[str, Any]:
    return {
        "data_dir": str(config.data_dir),
        "log_dir": str(config.log_dir),
        "screenshot_dir": str(config.screenshot_dir),
        "headless": config.headless,
        "failure_notify_threshold": config.failure_notify_threshold,
        "schedule": asdict(config.schedule),
        "timeouts": asdict(config.timeouts),
        "browser": _browser_to_json(config.browser),
        "users": [asdict(user) for user in users],
    }


def _dump_yaml(data: dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is required for YAML config files.") from exc
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _initial_steps() -> list[dict[str, str]]:
    return [
        _step("open_home", "打开抖音首页"),
        _step("open_messages", "打开私信面板"),
        _step("open_contact_profile", "打开联系人主页"),
        _step("profile_message_entry", "点击主页私信"),
        _step("contact_recent_list", "最近会话兜底"),
        _step("open_conversation", "进入会话"),
        _step("input_box", "查找输入框"),
        _step("input_message", "输入消息"),
        _step("press_enter", "发送消息"),
        _step("done", "完成"),
    ]


def _step(key: str, label: str) -> dict[str, str]:
    return {"key": key, "label": label, "status": "pending", "detail": "", "error": "", "started_at": "", "ended_at": ""}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _duration_seconds(started_at: str, ended_at: str = "") -> float:
    if not started_at:
        return 0
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at) if ended_at else datetime.now()
    except ValueError:
        return 0
    return max(0.0, (end - start).total_seconds())


def _as_plain_result(result: Any) -> Any:
    if result is not None and is_dataclass(result):
        return asdict(result)
    if isinstance(result, list):
        return [_as_plain_result(item) for item in result]
    if isinstance(result, dict):
        return {str(key): _as_plain_result(value) for key, value in result.items()}
    return result


def _failed_contacts_by_user(result: Any) -> dict[str, list[dict[str, str]]]:
    failed: dict[str, list[dict[str, str]]] = {}
    if not result:
        return failed

    if isinstance(result, dict) and "users" in result:
        users = result.get("users") or []
    else:
        users = [result]

    for user in users:
        if not isinstance(user, dict):
            continue
        user_name = str(user.get("user", ""))
        contacts = []
        for item in user.get("results", []) or []:
            if not isinstance(item, dict) or item.get("success"):
                continue
            contacts.append(
                {
                    "name": str(item.get("contact", "")),
                    "profile_url": str(item.get("profile_url", "")),
                    "message": "",
                }
            )
        if user_name and contacts:
            failed[user_name] = contacts
    return failed


def run_gui(config_path: Path = DEFAULT_CONFIG_PATH, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = GuiServer(config_path)
    httpd = ThreadingHTTPServer((host, port), server.handler())
    print(f"DouyinFire GUI: http://{host}:{port}")
    httpd.serve_forever()


def _read_json(request: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(request.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    return json.loads(request.rfile.read(length).decode("utf-8"))


def _send_json(request: BaseHTTPRequestHandler, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request.send_response(status)
    request.send_header("Content-Type", "application/json; charset=utf-8")
    request.send_header("Content-Length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


def _send_html(request: BaseHTTPRequestHandler, html: str) -> None:
    body = html.encode("utf-8")
    request.send_response(HTTPStatus.OK)
    request.send_header("Content-Type", "text/html; charset=utf-8")
    request.send_header("Content-Length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


def _send_screenshot(request: BaseHTTPRequestHandler, path: Path) -> None:
    body = path.read_bytes()
    request.send_response(HTTPStatus.OK)
    request.send_header("Content-Type", "image/png")
    request.send_header("Content-Length", str(len(body)))
    request.send_header("Cache-Control", "no-store")
    request.end_headers()
    request.wfile.write(body)


LEGACY_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DouyinFire</title>
  <style>
    :root { color-scheme: light; --bg: #f6f7f8; --panel: #fff; --text: #202428; --muted: #66707a; --line: #d9dee3; --accent: #0f766e; --danger: #b42318; }
    * { box-sizing: border-box; }
    body { margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    header { display: flex; align-items: center; justify-content: space-between; padding: 14px 20px; border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 2; }
    h1 { font-size: 18px; margin: 0; letter-spacing: 0; }
    main { display: grid; grid-template-columns: 320px minmax(0, 1fr); min-height: calc(100vh - 57px); }
    aside { border-right: 1px solid var(--line); padding: 16px; background: #fbfbfc; }
    section { padding: 16px 20px; }
    h2 { font-size: 15px; margin: 0 0 10px; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
    button, select { height: 34px; border: 1px solid var(--line); background: var(--panel); color: var(--text); border-radius: 6px; padding: 0 10px; font: inherit; }
    button.primary { background: var(--accent); border-color: var(--accent); color: white; }
    button.danger { border-color: #f0b6b0; color: var(--danger); }
    button:disabled { opacity: .55; cursor: not-allowed; }
    textarea { width: 100%; min-height: 390px; resize: vertical; border: 1px solid var(--line); border-radius: 6px; padding: 12px; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--panel); color: var(--text); }
    pre { margin: 0; min-height: 260px; max-height: 420px; overflow: auto; border: 1px solid var(--line); border-radius: 6px; padding: 12px; background: #111827; color: #e5e7eb; font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
    .status { display: grid; gap: 8px; margin-bottom: 16px; }
    .line { display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--line); padding-bottom: 6px; }
    .muted { color: var(--muted); }
    .users { display: grid; gap: 8px; margin-bottom: 16px; }
    .user { border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: var(--panel); }
    .user strong { display: block; margin-bottom: 3px; }
    .tabs { display: flex; gap: 6px; margin-bottom: 12px; border-bottom: 1px solid var(--line); }
    .tab { border: 0; border-radius: 0; background: transparent; border-bottom: 2px solid transparent; }
    .tab.active { border-bottom-color: var(--accent); color: var(--accent); }
    .hidden { display: none; }
    @media (max-width: 820px) { main { grid-template-columns: 1fr; } aside { border-right: 0; border-bottom: 1px solid var(--line); } }
  </style>
</head>
<body>
  <header>
    <h1>DouyinFire</h1>
    <div class="row" style="margin:0">
      <button id="refresh">刷新</button>
      <button id="uninstall" class="danger">取消自动化</button>
    </div>
  </header>
  <main>
    <aside>
      <h2>状态</h2>
      <div class="status" id="status"></div>
      <h2>用户</h2>
      <div class="users" id="users"></div>
      <div class="row">
        <select id="userSelect"></select>
        <button id="login">登录</button>
        <button id="runUser" class="primary">运行用户</button>
        <button id="runAll">运行全部</button>
      </div>
    </aside>
    <section>
      <div class="tabs">
        <button class="tab active" data-tab="config">配置</button>
        <button class="tab" data-tab="logs">日志</button>
        <button class="tab" data-tab="result">结果</button>
      </div>
      <div id="tab-config">
        <div class="row">
          <button id="save" class="primary">保存配置</button>
          <button id="initConfig">生成默认配置</button>
          <span class="muted" id="saveStatus"></span>
        </div>
        <textarea id="configText" spellcheck="false"></textarea>
      </div>
      <div id="tab-logs" class="hidden">
        <div class="row"><button id="reloadLogs">刷新日志</button></div>
        <pre id="logs"></pre>
      </div>
      <div id="tab-result" class="hidden">
        <pre id="result"></pre>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let state = null;

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    }
    async function refresh() {
      state = await api('/api/state');
      renderState();
      const cfg = await api('/api/config');
      if (!$('configText').dataset.dirty) $('configText').value = cfg.text;
      await loadLogs();
    }
    function renderState() {
      const c = state.config;
      $('status').innerHTML = [
        row('配置', c.ok ? 'ok' : c.error),
        row('服务', state.service),
        row('任务', state.job.running ? `${state.job.kind} running` : state.job.message),
        row('路径', c.paths?.config || '')
      ].join('');
      $('users').innerHTML = (c.users || []).map(u => `<div class="user"><strong>${escapeHtml(u.name)}</strong><div class="muted">${u.enabled ? 'enabled' : 'disabled'} · ${u.contacts.length} contacts · profile ${u.profile ? 'present' : 'missing'}</div></div>`).join('');
      $('userSelect').innerHTML = (c.users || []).map(u => `<option value="${escapeHtml(u.name)}">${escapeHtml(u.name)}</option>`).join('');
      $('result').textContent = JSON.stringify(state.job, null, 2);
      document.querySelectorAll('button').forEach(b => { if (!['refresh'].includes(b.id)) b.disabled = state.job.running; });
      $('refresh').disabled = false;
    }
    function row(k, v) { return `<div class="line"><span>${escapeHtml(k)}</span><span class="muted">${escapeHtml(String(v || ''))}</span></div>`; }
    function escapeHtml(s) { return s.replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
    async function loadLogs() {
      const data = await api('/api/logs?limit=220');
      $('logs').textContent = data.text || '';
    }
    async function post(path, payload = {}) {
      await api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      await refresh();
    }
    $('refresh').onclick = refresh;
    $('reloadLogs').onclick = loadLogs;
    $('save').onclick = async () => {
      try {
        await post('/api/config', { text: $('configText').value });
        $('configText').dataset.dirty = '';
        $('saveStatus').textContent = 'saved';
      } catch (e) { $('saveStatus').textContent = e.message; }
    };
    $('initConfig').onclick = () => post('/api/init', { overwrite: true });
    $('login').onclick = () => post('/api/login', { user: $('userSelect').value, wait_seconds: 180 });
    $('runUser').onclick = () => post('/api/run', { user: $('userSelect').value });
    $('runAll').onclick = () => post('/api/run-all');
    $('uninstall').onclick = () => post('/api/service/uninstall');
    $('configText').addEventListener('input', () => $('configText').dataset.dirty = '1');
    document.querySelectorAll('.tab').forEach(btn => btn.onclick = () => {
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      ['config','logs','result'].forEach(name => $('tab-' + name).classList.toggle('hidden', name !== btn.dataset.tab));
    });
    refresh();
    setInterval(refresh, 2500);
  </script>
</body>
</html>
"""
