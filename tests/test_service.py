import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from douyinfire import notify, service
from douyinfire.config import save_config


def completed(code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout="", stderr=stderr)


def prepare(tmp_path, monkeypatch):
    config = tmp_path / "config" / "douyinfire.json"
    save_config({"users": []}, config)
    target = tmp_path / "LaunchAgents" / "test.plist"
    monkeypatch.setattr(service, "plist_path", lambda: target)
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    return config, target


def test_service_resident_backend_uses_config_and_creates_logs(tmp_path, monkeypatch):
    config, target = prepare(tmp_path, monkeypatch)
    mock = Mock(side_effect=[completed(113, "Could not find service"), completed(), completed()])
    monkeypatch.setattr(service, "_run", mock)
    assert service.install_service(tmp_path, config) == target
    plist = plistlib.loads(target.read_bytes())
    assert plist["ProgramArguments"][-3:] == ["background", "--config", str(config)]
    assert "StartCalendarInterval" not in plist
    assert plist["RunAtLoad"] and plist["StartInterval"] == 60
    assert "KeepAlive" not in plist
    assert Path(plist["StandardOutPath"]).parent.is_dir()
    assert mock.call_args_list[1].args[0] == "bootstrap"


def test_failed_load_raises_and_removes_new_plist(tmp_path, monkeypatch):
    config, target = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_run", Mock(side_effect=[completed(113), completed(5, "Load failed")]))
    with pytest.raises(service.ServiceError, match="Load failed"):
        service.install_service(tmp_path, config)
    assert not target.exists()


def test_frozen_app_has_no_python_module_argument(tmp_path, monkeypatch):
    config, target = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_run", Mock(side_effect=[completed(113), completed(), completed()]))
    service.install_service(tmp_path, config, executable=sys.executable)
    args = plistlib.loads(target.read_bytes())["ProgramArguments"]
    assert args[1] == "background"
    assert "-m" not in args


def test_failed_unload_keeps_registration(tmp_path, monkeypatch):
    _, target = prepare(tmp_path, monkeypatch)
    target.parent.mkdir()
    target.write_bytes(b"old registration")
    monkeypatch.setattr(service, "_run", Mock(side_effect=[completed(), completed(5, "denied")]))
    with pytest.raises(service.ServiceError, match="denied"):
        service.uninstall_service()
    assert target.read_bytes() == b"old registration"


def test_notification_values_are_arguments_not_script(monkeypatch):
    monkeypatch.setattr(notify.platform, "system", lambda: "Darwin")
    run = Mock(return_value=completed())
    monkeypatch.setattr(notify.subprocess, "run", run)
    title = '中文 "quoted" title'
    message = 'a\nb\'c" & do shell script "not executed"'
    assert notify.notify(title, message)
    args = run.call_args.args[0]
    assert args[-2:] == [title, message]
    assert message not in args[2]


def test_failed_confirmation_stops_new_service_and_restores_old(tmp_path, monkeypatch):
    config, target = prepare(tmp_path, monkeypatch)
    target.parent.mkdir()
    old = b"old plist bytes"
    target.write_bytes(old)
    mock = Mock(side_effect=[completed(), completed(), completed(), completed(5, "cannot confirm"), completed(), completed()])
    monkeypatch.setattr(service, "_run", mock)
    with pytest.raises(service.ServiceError, match="cannot confirm"):
        service.install_service(tmp_path, config)
    assert target.read_bytes() == old
    assert mock.call_args_list[-2].args[0] == "bootout"
    assert mock.call_args_list[-1].args[0] == "bootstrap"
