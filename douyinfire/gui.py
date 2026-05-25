from __future__ import annotations

import json
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import DEFAULT_CONFIG_PATH, ConfigError, ensure_runtime_dirs, load_config, write_example_config
from .service import service_status, uninstall_service
from .tasks import login_user, run_all, run_user


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.kind = ""
        self.message = "idle"
        self.result: Any = None
        self.error = ""

    def start(self, kind: str) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.kind = kind
            self.message = "running"
            self.result = None
            self.error = ""
            return True

    def finish(self, message: str, result: Any = None, error: str = "") -> None:
        with self.lock:
            self.running = False
            self.message = message
            self.result = result
            self.error = error

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = self.result
            if result is not None and hasattr(result, "__dataclass_fields__"):
                result = asdict(result)
            return {
                "running": self.running,
                "kind": self.kind,
                "message": self.message,
                "result": result,
                "error": self.error,
            }


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
        elif parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["200"])[0])
            _send_json(request, {"text": self._logs(limit)})
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
            elif parsed.path == "/api/login":
                user = str(payload.get("user", "")).strip()
                wait_seconds = int(payload.get("wait_seconds", 180))
                self._start_job(f"login:{user}", lambda: self._login(user, wait_seconds))
                _send_json(request, {"ok": True})
            elif parsed.path == "/api/run":
                user = str(payload.get("user", "")).strip()
                self._start_job(f"run:{user}", lambda: self._run_user(user))
                _send_json(request, {"ok": True})
            elif parsed.path == "/api/run-all":
                self._start_job("run-all", self._run_all)
                _send_json(request, {"ok": True})
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
                        "contacts": user.contacts,
                        "profile": (config.data_dir / "profiles" / user.name).exists(),
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

    def _run_user(self, user_name: str) -> Any:
        config = load_config(self.config_path)
        return run_user(config, config.user(user_name))

    def _run_all(self) -> Any:
        config = load_config(self.config_path)
        return run_all(config)

    def _config_text(self) -> str:
        if not self.config_path.exists():
            return ""
        return self.config_path.read_text(encoding="utf-8")

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


class BusyError(RuntimeError):
    pass


def load_config_from_text(text: str, config_path: Path) -> None:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is required for YAML config files.") from exc
    raw = yaml.safe_load(text) or {}
    from .config import parse_config

    parse_config(raw, base_dir=config_path.parent.parent)


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


HTML = r"""<!doctype html>
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
