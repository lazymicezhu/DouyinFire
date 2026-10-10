"""Double-click app launcher and resident loopback service. No terminal required."""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .config import AppConfig, load_config, save_config
from .locking import FileLock, LockBusy
from .runtime import default_config_path, migrate_legacy


APP_VERSION = __version__


def _metadata_path(config_path):
    return Path(config_path).resolve().parent.parent / 'server.json'


def _config_identity(config_path):
    return hashlib.sha256(str(Path(config_path).resolve()).encode()).hexdigest()


class _NoHealthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def _open_health(url):
    # Health checks are strictly loopback: ignore global proxy settings and
    # never follow a port occupant's redirect to an external website.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoHealthRedirect())
    return opener.open(url, timeout=1)


def existing_server(config_path):
    path = _metadata_path(config_path)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        port, pid = int(data['port']), int(data['pid'])
        if not 1 <= port <= 65535 or pid <= 0:
            return None
        os.kill(pid, 0)
        url = f'http://127.0.0.1:{port}'
        with _open_health(url + '/api/health') as response:
            health = json.load(response)
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return None
    if not isinstance(health, dict) or health.get('app') != 'DouyinFire' or health.get('pid') != pid:
        return None
    # A healthy but different service is not ours to stop or silently reuse.
    if data.get('version') != APP_VERSION or health.get('version') != APP_VERSION:
        raise RuntimeError('后台仍在运行其他版本。请先在旧网页中退出整个后台，再打开当前应用。')
    identity = _config_identity(config_path)
    if data.get('config_id') != identity or health.get('config_id') != identity:
        raise RuntimeError('该目录已有使用其他配置或旧启动信息的后台。请先在原网页中退出整个后台，再打开此配置。')
    return url


def _legacy_project():
    override = os.environ.get('DOUYINFIRE_LEGACY_PROJECT')
    candidates = [Path(override)] if override else []
    candidates += [Path.cwd(), Path(__file__).resolve().parent.parent]
    if getattr(sys, 'frozen', False):
        candidates += list(Path(sys.executable).resolve().parents)[:6]
    for path in candidates:
        if any((path/'config'/name).is_file() for name in ('douyinfire.yaml', 'douyinfire.json')):
            return path
    return None


def prepare_config(config_path):
    path = Path(config_path).resolve()
    if path.exists():
        load_config(path)
        return path
    base = path.parent.parent
    # A caller choosing a custom configuration is starting a separate setup.
    # Only the standard destination participates in automatic legacy migration.
    legacy = _legacy_project() if path.name == 'douyinfire.yaml' and path.parent.name == 'config' else None
    if legacy:
        result = migrate_legacy(legacy, base)
        if result.config_path == path and path.exists():
            return path
    save_config(AppConfig(users=[], data_dir=base/'data', log_dir=base/'logs', screenshot_dir=base/'screenshots'), path)
    return path


def _command(command, config_path, port=8765):
    if getattr(sys, 'frozen', False):
        args = [sys.executable]
    else:
        args = [sys.executable, '-m', 'douyinfire.desktop']
    return args + [command, '--config', str(config_path), '--port', str(port)]


def _stop_started_process(process):
    """Clean up only the process/session created by this startup attempt."""
    if process.poll() is not None:
        return
    group = None
    try:
        candidate = os.getpgid(process.pid)
        if candidate == process.pid and candidate != os.getpgrp():
            group = candidate
    except ProcessLookupError:
        return
    try:
        if group is not None:
            os.killpg(group, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=3)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=3)


def ensure_started(config_path, port=8765):
    config_path = Path(config_path).resolve()
    base = config_path.parent.parent
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with FileLock(base/'startup.lock'):
            existing = existing_server(config_path)
            if existing:
                return existing
            prepare_config(config_path)
            config = load_config(config_path)
            config.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            log = config.log_dir/'startup.log'
            env = os.environ.copy()
            # PYTHONPATH splits on ':' on macOS, which is legal in a project
            # folder name. Source launches import from cwd instead; frozen apps
            # need neither the checkout nor the build virtual environment.
            child_cwd = base if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
            with log.open('ab') as stream:
                process = subprocess.Popen(_command('serve', config_path, port), cwd=child_cwd, env=env,
                                           stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                           start_new_session=True)
            log.chmod(0o600)
            try:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    url = existing_server(config_path)
                    if url:
                        return url
                    if process.poll() is not None:
                        raise RuntimeError('后台没有成功启动。请打开应用数据目录中的 logs/startup.log 查看原因。')
                    time.sleep(.2)
                raise RuntimeError('后台启动超时，本次启动已停止。请查看 logs/startup.log 后重试。')
            except Exception:
                _stop_started_process(process)
                raise
    except LockBusy:
        for _ in range(150):
            url = existing_server(config_path)
            if url:
                return url
            time.sleep(.2)
        raise RuntimeError('另一个启动过程尚未完成，请稍后重试。') from None


def serve(config_path, port=8765):
    from .gui import GuiServer
    from .manager import TaskManager
    from .service import install_service, service_status, uninstall_service

    config_path = prepare_config(config_path)
    config = load_config(config_path)
    with FileLock(config.data_dir/'locks'/'application.lock'):
        manager = TaskManager(config_path)
        httpd = None
        metadata = _metadata_path(config_path)
        try:
            def service_controller(action):
                if action == 'status':
                    return {'status': service_status()}
                if action == 'enable':
                    executable = Path(sys.executable) if getattr(sys, 'frozen', False) else None
                    install_service(project_dir=Path(__file__).resolve().parent.parent if not executable else config_path.parent.parent,
                                    config_path=config_path, executable=executable, daemon_command='background')
                    return {'status': service_status(), 'message': '已启用登录后恢复，网页关闭后仍在后台运行。'}
                if action in {'disable','exit'}:
                    # LaunchAgent is only a watchdog launcher, not this HTTP process.
                    uninstall_service()
                    if action == 'exit':
                        current = load_config(config_path)
                        current.schedule.enabled = False
                        save_config(current, config_path)
                    return {'status': service_status(), 'message': '已关闭登录后恢复。'}
                raise ValueError('无效的后台操作。')

            server = GuiServer(config_path, manager=manager, service_controller=service_controller)
            try:
                httpd = ThreadingHTTPServer(('127.0.0.1', port), server.handler())
            except OSError as exc:
                if exc.errno not in {48,98}:
                    raise
                # A different local app may own 8765; publish the actual chosen port.
                httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.handler())
            httpd.daemon_threads = True
            server.shutdown = httpd.shutdown
            info = {'pid': os.getpid(), 'port': httpd.server_port, 'version': APP_VERSION,
                    'config_id': _config_identity(config_path)}
            tmp = metadata.with_suffix('.tmp')
            tmp.write_text(json.dumps(info), encoding='utf-8')
            tmp.chmod(0o600)
            tmp.replace(metadata)
            import signal
            def stop(signum, frame):
                threading.Thread(target=httpd.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            httpd.serve_forever(poll_interval=.25)
        finally:
            if httpd:
                httpd.server_close()
            manager.close()
            try:
                if json.loads(metadata.read_text()).get('pid') == os.getpid():
                    metadata.unlink()
            except (OSError, ValueError):
                pass


def show_error(message):
    # Separate argv values keep user paths and quotes out of AppleScript code.
    script = 'on run argv\ndisplay alert "DouyinFire 无法启动" message (item 1 of argv) as critical buttons {"好"}\nend run'
    if sys.platform == 'darwin':
        subprocess.run(['osascript','-e',script,'--',str(message)], capture_output=True, timeout=120, check=False)
    elif sys.stderr:
        print(str(message), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description='DouyinFire desktop launcher')
    parser.add_argument('command', choices=['open','serve','background'], nargs='?', default='open')
    parser.add_argument('--config', type=Path, default=default_config_path())
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            serve(args.config, args.port)
        else:
            url = ensure_started(args.config, args.port)
            if args.command == 'open':
                webbrowser.open(url)
        return 0
    except Exception as exc:
        if args.command == 'open':
            show_error(exc)
        elif sys.stderr:
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
