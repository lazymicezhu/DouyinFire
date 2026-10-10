import io
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from douyinfire import desktop
from douyinfire.config import save_config


def server_info(config_path, **changes):
    info = {"app": "DouyinFire", "pid": 12345, "port": 8765,
            "version": desktop.APP_VERSION, "config_id": desktop._config_identity(config_path)}
    info.update(changes)
    return info


def mock_server(tmp_path, monkeypatch, *, metadata_changes=None, health_changes=None):
    config_path = tmp_path / "config" / "a.json"
    data = server_info(config_path, **(metadata_changes or {}))
    health = server_info(config_path, **(health_changes or {}))
    desktop._metadata_path(config_path).write_text(json.dumps(data))
    monkeypatch.setattr(desktop.os, "kill", Mock())
    monkeypatch.setattr(desktop, "_open_health", lambda *args, **kwargs: io.BytesIO(json.dumps(health).encode()))
    return config_path


def test_existing_server_requires_matching_config_and_version(tmp_path, monkeypatch):
    config_path = mock_server(tmp_path, monkeypatch)
    assert desktop.existing_server(config_path) == "http://127.0.0.1:8765"
    with pytest.raises(RuntimeError, match="其他配置"):
        desktop.existing_server(config_path.with_name("b.json"))


@pytest.mark.parametrize("changes", [{"version": "old"}, {"version": None}])
def test_old_running_server_not_silently_reused(tmp_path, monkeypatch, changes):
    config_path = mock_server(tmp_path, monkeypatch, health_changes=changes)
    with pytest.raises(RuntimeError, match="其他版本"):
        desktop.existing_server(config_path)


def test_matching_metadata_does_not_override_wrong_health_identity(tmp_path, monkeypatch):
    config_path = mock_server(tmp_path, monkeypatch, health_changes={"config_id": "other"})
    with pytest.raises(RuntimeError, match="其他配置"):
        desktop.existing_server(config_path)


def test_unrelated_port_occupant_is_not_claimed_as_ours(tmp_path, monkeypatch):
    config_path = mock_server(tmp_path, monkeypatch, health_changes={"app": "Different app"})
    assert desktop.existing_server(config_path) is None


def test_malformed_metadata_and_dead_pid_are_stale(tmp_path, monkeypatch):
    config_path = tmp_path / "config" / "a.json"
    desktop._metadata_path(config_path).write_text("[]")
    assert desktop.existing_server(config_path) is None
    desktop._metadata_path(config_path).write_text(json.dumps(server_info(config_path)))
    monkeypatch.setattr(desktop.os, "kill", Mock(side_effect=ProcessLookupError))
    assert desktop.existing_server(config_path) is None


def test_startup_timeout_cleans_only_its_new_child(tmp_path, monkeypatch):
    config_path = tmp_path / "config" / "app.json"
    save_config({"users": []}, config_path)
    monkeypatch.setattr(desktop, "existing_server", lambda path: None)
    process = Mock()
    monkeypatch.setattr(desktop.subprocess, "Popen", Mock(return_value=process))
    stop = Mock()
    monkeypatch.setattr(desktop, "_stop_started_process", stop)
    monkeypatch.setattr(desktop.time, "monotonic", Mock(side_effect=[0, 26]))
    with pytest.raises(RuntimeError, match="启动超时"):
        desktop.ensure_started(config_path)
    stop.assert_called_once_with(process)
    assert desktop.subprocess.Popen.call_args.kwargs["start_new_session"] is True
    assert desktop.subprocess.Popen.call_args.kwargs["cwd"] == Path(desktop.__file__).resolve().parent.parent


def test_version_conflict_never_stops_an_existing_server(tmp_path, monkeypatch):
    config_path = tmp_path / "config" / "app.json"
    monkeypatch.setattr(desktop, "existing_server", Mock(side_effect=RuntimeError("其他版本")))
    start = Mock()
    stop = Mock()
    monkeypatch.setattr(desktop.subprocess, "Popen", start)
    monkeypatch.setattr(desktop, "_stop_started_process", stop)
    with pytest.raises(RuntimeError, match="其他版本"):
        desktop.ensure_started(config_path)
    start.assert_not_called()
    stop.assert_not_called()


def test_startup_cleanup_never_signals_inherited_group(monkeypatch):
    process = Mock(pid=12345)
    process.poll.return_value = None
    monkeypatch.setattr(desktop.os, "getpgid", lambda pid: 900)
    monkeypatch.setattr(desktop.os, "getpgrp", lambda: 900)
    group_kill = Mock()
    monkeypatch.setattr(desktop.os, "killpg", group_kill)
    desktop._stop_started_process(process)
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=3)
    group_kill.assert_not_called()


def test_startup_cleanup_escalates_only_private_group(monkeypatch):
    process = Mock(pid=12345)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("test", 3), 0]
    monkeypatch.setattr(desktop.os, "getpgid", lambda pid: 12345)
    monkeypatch.setattr(desktop.os, "getpgrp", lambda: 900)
    group_kill = Mock()
    monkeypatch.setattr(desktop.os, "killpg", group_kill)
    desktop._stop_started_process(process)
    assert [call.args for call in group_kill.call_args_list] == [
        (12345, desktop.signal.SIGTERM), (12345, desktop.signal.SIGKILL)]
    process.kill.assert_not_called()


def test_custom_config_does_not_implicitly_copy_real_accounts(tmp_path, monkeypatch):
    legacy = Mock()
    migrate = Mock()
    monkeypatch.setattr(desktop, "_legacy_project", legacy)
    monkeypatch.setattr(desktop, "migrate_legacy", migrate)
    path = tmp_path / "config" / "custom.json"
    assert desktop.prepare_config(path) == path
    assert json.loads(path.read_text())["users"] == []
    legacy.assert_not_called()
    migrate.assert_not_called()


def test_legacy_discovery_accepts_json(tmp_path, monkeypatch):
    old = tmp_path / "legacy"
    (old / "config").mkdir(parents=True)
    (old / "config" / "douyinfire.json").write_text('{"users":[]}')
    monkeypatch.setenv("DOUYINFIRE_LEGACY_PROJECT", str(old))
    assert desktop._legacy_project() == old


def test_health_probe_does_not_use_proxies_or_redirect(monkeypatch):
    opener = Mock()
    build = Mock(return_value=opener)
    monkeypatch.setattr(desktop.urllib.request, "build_opener", build)
    desktop._open_health("http://127.0.0.1:12345/api/health")
    proxy, redirect = build.call_args.args
    assert proxy.proxies == {}
    assert redirect.redirect_request(None, None, 302, "Found", {}, "https://outside.example") is None
    opener.open.assert_called_once_with("http://127.0.0.1:12345/api/health", timeout=1)
