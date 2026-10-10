"""Loopback-only web interface. Browser work belongs to TaskManager workers."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import socket
import threading
from dataclasses import asdict, is_dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from . import __version__
from .config import (
    DEFAULT_CONFIG_PATH, ConfigError, UserConfig, config_to_dict,
    load_config, parse_config, patch_user, save_config,
)
from .gui_html import HTML

MAX_BODY_BYTES = 256 * 1024


class BusyError(RuntimeError):
    pass


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


class GuiServer:
    def __init__(self, config_path: Path, manager: Any = None,
                 service_controller: Callable[[str], dict[str, Any]] | None = None) -> None:
        self.config_path = Path(config_path).resolve()
        if manager is None:
            if not self.config_path.exists():
                save_config(parse_config({"users": []}, base_dir=self.config_path.parent.parent), self.config_path)
            from .manager import TaskManager
            manager = TaskManager(self.config_path)
        self.manager = manager
        self.lock = getattr(manager, "operation_lock", threading.RLock())
        self.csrf_token = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(24)
        self.service_controller = service_controller
        self.shutdown: Callable[[], None] | None = None

    def handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self) -> None:
                owner.handle(self, head=True)

            def do_GET(self) -> None:
                owner.handle(self)

            def do_POST(self) -> None:
                owner.handle(self, mutation=True)

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler

    def _validate_request(self, request: BaseHTTPRequestHandler, mutation: bool) -> None:
        try:
            parsed = urlparse("http://" + request.headers.get("Host", ""))
            hostname = parsed.hostname
            port = parsed.port or 80
        except ValueError:
            raise RequestError("无效的本地服务地址", 403) from None
        if hostname not in {"localhost", "127.0.0.1", "::1"} or port != request.server.server_port:
            raise RequestError("只接受本机服务地址", 403)
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise RequestError("无效的本地服务地址", 403)
        origin = request.headers.get("Origin")
        if origin and origin != "http://" + request.headers.get("Host", ""):
            raise RequestError("拒绝其他网页发起的请求", 403)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site":
            raise RequestError("拒绝跨站请求", 403)
        if mutation:
            token = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(token, self.csrf_token):
                raise RequestError("页面验证已失效，请重新打开应用", 403)
            if request.headers.get_content_type() != "application/json":
                raise RequestError("请求必须使用 JSON", 415)

    def handle(self, request: BaseHTTPRequestHandler, mutation: bool = False, head: bool = False) -> None:
        try:
            self._validate_request(request, mutation)
            parsed = urlparse(request.path)
            if mutation:
                payload = self._read_json(request)
                with self.lock:
                    data = self._mutate(parsed.path, payload)
                self._json(request, data)
                if parsed.path == "/api/service" and payload.get("action") == "exit" and self.shutdown:
                    threading.Thread(target=self.shutdown, daemon=True).start()
            elif parsed.path == "/":
                body = HTML.replace("__CSRF_TOKEN__", self.csrf_token).replace("__CSP_NONCE__", self.nonce)
                self._send(request, body.encode(), "text/html; charset=utf-8", head=head)
            elif parsed.path == "/api/health":
                self._json(request, {"app": "DouyinFire", "version": __version__, "pid": os.getpid(),
                                     "config_id": hashlib.sha256(str(self.config_path.resolve()).encode()).hexdigest()}, head=head)
            elif parsed.path == "/api/state":
                self._json(request, self.state(), head=head)
            elif parsed.path == "/api/history":
                user_id = parse_qs(parsed.query).get("user_id", [""])[0]
                self._json(request, {"attempts": self.manager.history(user_id)}, head=head)
            elif parsed.path == "/api/logs":
                config = self._config()
                path = config.log_dir / "douyinfire.log"
                # Read at most the final 64 KiB; a growing log must not block the UI.
                if path.exists():
                    with path.open("rb") as stream:
                        stream.seek(max(0, path.stat().st_size - 65536))
                        content = stream.read().decode("utf-8", errors="replace")
                else:
                    content = "暂无日志"
                self._json(request, {"text": content}, head=head)
            else:
                raise RequestError("页面不存在", 404)
        except (BusyError, FileExistsError) as exc:
            self._json(request, {"error": str(exc)}, 409)
        except RequestError as exc:
            self._json(request, {"error": str(exc)}, exc.status)
        except (ConfigError, ValueError, KeyError) as exc:
            self._json(request, {"error": str(exc)}, 400)
        except Exception as exc:
            # Worker/service errors stay visible instead of terminating the HTTP connection.
            self._json(request, {"error": str(exc) or type(exc).__name__}, 500)

    def _read_json(self, request: BaseHTTPRequestHandler) -> dict[str, Any]:
        try:
            size = int(request.headers.get("Content-Length", "0"))
        except ValueError:
            raise RequestError("请求长度无效") from None
        if size < 0 or size > MAX_BODY_BYTES:
            raise RequestError("配置内容过大", 413)
        if request.headers.get("Transfer-Encoding"):
            raise RequestError("不支持该请求传输方式")
        request.connection.settimeout(5)
        try:
            data = json.loads(request.rfile.read(size).decode("utf-8"))
        except (ValueError, UnicodeError, TimeoutError):
            raise RequestError("请求内容不是有效的 JSON") from None
        if not isinstance(data, dict):
            raise RequestError("请求内容必须是对象")
        return data

    def _config(self) -> Any:
        if self.config_path.exists():
            return load_config(self.config_path)
        return parse_config({"users": []}, base_dir=self.config_path.parent.parent)

    def _revision(self) -> str:
        data = self.config_path.read_bytes() if self.config_path.exists() else b""
        return hashlib.sha256(data).hexdigest()

    def _idle(self) -> None:
        if self.manager.snapshot().get("running"):
            raise BusyError("任务正在运行，请停止或等待完成后再修改配置")

    def _check_revision(self, payload: dict[str, Any]) -> None:
        if "revision" in payload and payload["revision"] != self._revision():
            raise RequestError("配置已在另一处更新。请先重新载入，再保存修改。", 409)

    def _service(self, action: str = "status") -> dict[str, Any]:
        if self.service_controller:
            return self.service_controller(action)
        if action == "status":
            from .service import service_status
            return {"status": service_status()}
        raise RequestError("当前启动方式不支持管理后台服务，请通过 DouyinFire 应用打开")

    def state(self) -> dict[str, Any]:
        with self.lock:
            try:
                config = config_to_dict(self._config())
                config_error = ""
            except Exception as exc:
                config, config_error = {"users": [], "schedule": {}}, str(exc)
            try:
                service = self._service()
            except Exception as exc:
                service = {"status": "error", "error": str(exc)}
            return {"config": config, "config_error": config_error,
                    "revision": self._revision(), "manager": self.manager.snapshot(), "service": service}

    def _mutate(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/api/cancel":
            return {"ok": True, "cancelled": self.manager.cancel()}
        if path == "/api/jobs":
            kind = payload.get("kind", "")
            if kind not in {"login", "check", "bind", "run", "retry", "list_conversations"}:
                raise RequestError("未知任务类型")
            self._idle()
            user_id = str(payload.get("user_id", ""))
            config = self._config()
            if not any(user.id == user_id for user in config.users):
                raise RequestError("请选择有效账号")
            contact_ids = payload.get("contact_ids")
            if contact_ids is not None and (not isinstance(contact_ids, list) or any(not isinstance(i, str) for i in contact_ids)):
                raise RequestError("联系人列表无效")
            worker_payload = payload.get("payload", {})
            if not isinstance(worker_payload, dict):
                raise RequestError("任务参数无效")
            return {"ok": True, "job": self.manager.start(kind, user_id=user_id, contact_ids=contact_ids, payload=worker_payload)}
        if path.startswith("/api/attempts/") and path.endswith("/resolve"):
            self._idle()
            attempt_id = path[len("/api/attempts/"):-len("/resolve")]
            resolution = payload.get("resolution")
            if resolution not in {"confirmed_sent", "retry_allowed"}:
                raise RequestError("请选择已核实发送或允许再次手动发送")
            return {"ok": True, "attempt": self.manager.resolve_attempt(attempt_id, resolution)}
        if path == "/api/service":
            action = payload.get("action")
            if action not in {"enable", "disable", "exit"}:
                raise RequestError("未知后台服务操作")
            self._idle()
            return {"ok": True, "service": self._service(action)}
        self._idle()
        self._check_revision(payload)
        config = self._config()
        if path == "/api/users":
            name = str(payload.get("name", "")).strip()
            if not name or len(name) > 80:
                raise RequestError("请输入 1–80 字的账号名称")
            if any(user.name == name for user in config.users):
                raise RequestError("该账号名称已存在")
            user = UserConfig(id=uuid4().hex, name=name, contacts=[], message="", enabled=True)
            config = replace(config, users=[*config.users, user])
            save_config(config, self.config_path)
            return {"ok": True, "user_id": user.id, "revision": self._revision()}
        if path.startswith("/api/users/"):
            user_id = path[len("/api/users/"):]
            user = next((item for item in config.users if item.id == user_id), None)
            if user is None:
                raise RequestError("账号不存在", 404)
            updates = {key: payload[key] for key in ("name", "message", "enabled") if key in payload}
            if "contacts" in payload:
                if not isinstance(payload["contacts"], list):
                    raise RequestError("联系人列表无效")
                raw_user = next(item for item in config_to_dict(config)["users"] if item["id"] == user_id)
                old = {item["id"]: item for item in raw_user["contacts"]}
                contacts, seen = [], set()
                for raw in payload["contacts"]:
                    if not isinstance(raw, dict):
                        raise RequestError("联系人配置无效")
                    contact_id = str(raw.get("id") or uuid4().hex)
                    if contact_id in seen:
                        raise RequestError("联系人 ID 重复")
                    if raw.get("id") and contact_id not in old:
                        raise RequestError("联系人不属于当前账号，请重新载入")
                    seen.add(contact_id)
                    previous = old.get(contact_id, {})
                    item = dict(previous)
                    item.update({key: raw[key] for key in ("name", "message", "enabled", "profile_url", "kind") if key in raw})
                    item["id"] = contact_id
                    kind_changed = item.get('kind', 'direct') != previous.get('kind', 'direct')
                    if kind_changed or any(item.get(key, "") != previous.get(key, "") for key in ("name", "profile_url")):
                        item["identity"] = ""
                    if item.get('kind') == 'group' or (kind_changed and 'profile_url' not in raw):
                        item['profile_url'] = ''
                    contacts.append(item)
                updates["contacts"] = contacts
            updated = patch_user(config, user_id, updates)
            save_config(updated, self.config_path)
            return {"ok": True, "revision": self._revision()}
        if path == "/api/schedule":
            allowed = {"enabled", "time", "timezone", "catchup_minutes", "jitter_minutes", "min_contact_interval_seconds"}
            data = config_to_dict(config)
            data.setdefault("schedule", {}).update({key: value for key, value in payload.items() if key in allowed})
            updated = parse_config(data, base_dir=self.config_path.parent.parent)
            save_config(updated, self.config_path)
            return {"ok": True, "revision": self._revision()}
        raise RequestError("接口不存在", 404)

    def _json(self, request: BaseHTTPRequestHandler, data: Any, status: int = 200, head: bool = False) -> None:
        self._send(request, json.dumps(_plain(data), ensure_ascii=False).encode(), "application/json; charset=utf-8", status, head)

    def _send(self, request: BaseHTTPRequestHandler, body: bytes, content_type: str,
              status: int = 200, head: bool = False) -> None:
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.send_header("Referrer-Policy", "no-referrer")
        request.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'nonce-{self.nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        request.end_headers()
        if not head:
            request.wfile.write(body)


def run_gui(config_path: Path = DEFAULT_CONFIG_PATH, host: str = "127.0.0.1", port: int = 8765,
            manager: Any = None, service_controller: Callable[[str], dict[str, Any]] | None = None) -> None:
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        except ValueError:
            raise ConfigError("图形界面仅允许绑定本机地址") from None
    elif host == "localhost":
        host = "127.0.0.1"
    server = GuiServer(config_path, manager=manager, service_controller=service_controller)
    server_class = ThreadingHTTPServer
    if ":" in host:
        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_class = IPv6Server
    httpd = server_class((host, port), server.handler())
    server.shutdown = httpd.shutdown
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        server.manager.close()
