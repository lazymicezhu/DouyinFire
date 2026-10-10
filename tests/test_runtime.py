import json
import stat
from pathlib import Path

import pytest

from douyinfire.config import ConfigError, config_to_dict, load_config, parse_config, patch_config, patch_user, save_config
from douyinfire.runtime import default_app_dir, default_config_path, migrate_legacy


def example():
    return {"users": [{"name": "main", "contacts": [{"name": "friend"}], "message": "hello"}]}


def test_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DOUYINFIRE_HOME", str(tmp_path))
    assert default_app_dir() == tmp_path
    assert default_config_path() == tmp_path / "config" / "douyinfire.yaml"


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_atomic_private_round_trip(tmp_path, suffix):
    raw = example()
    raw["future_setting"] = {"keep": True}
    raw["users"][0]["future_account_setting"] = "preserved"
    raw["users"][0]["contacts"][0]["future_contact_setting"] = 123
    path = tmp_path / "config" / ("app" + suffix)
    save_config(raw, path)
    saved = load_config(path)
    updated = patch_user(saved, saved.users[0].id, {"name": "renamed"})
    save_config(updated, path)
    loaded = load_config(path)
    assert loaded.users[0].id == saved.users[0].id
    assert loaded.users[0].name == "renamed"
    assert loaded.extra["future_setting"] == {"keep": True}
    assert loaded.users[0].extra["future_account_setting"] == "preserved"
    assert loaded.users[0].contacts[0].extra["future_contact_setting"] == 123
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    if suffix == ".json":
        assert json.loads(path.read_text())["users"][0]["name"] == "renamed"
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        save_config({"users": "invalid"}, path)
    assert path.read_bytes() == before


def test_empty_first_setup_and_partial_account_are_valid():
    assert parse_config({"users": []}).users == []
    config = parse_config({"users": [{"name": "new", "contacts": [], "message": ""}]})
    assert config.users[0].contacts == []
    assert config.browser.run_headless is True
    assert config.browser.login_headless is False
    assert not config.schedule.enabled


def test_ids_stable_renames_and_other_users_preserved():
    raw = example()
    raw["users"].append({"name": "second", "contacts": [], "message": ""})
    first = parse_config(raw)
    second = parse_config(raw)
    assert first.users[0].id == second.users[0].id
    updated = patch_user(first, first.users[0].id, {"name": "renamed", "enabled": False})
    assert updated.users[0].id == first.users[0].id
    assert updated.users[1] == first.users[1]
    assert first.users[0].name == "main"
    patched = patch_config(updated, {"schedule": {"time": "18:30"}})
    assert patched.schedule.time == "18:30"
    assert patched.schedule.jitter_minutes == updated.schedule.jitter_minutes


@pytest.mark.parametrize("change", [
    {"enabled": "false"}, {"id": "../escape"}, {"id": "/absolute"}, {"id": 0},
])
def test_reject_unsafe_user_settings(change):
    raw = example()
    raw["users"][0].update(change)
    with pytest.raises(ConfigError):
        parse_config(raw)


@pytest.mark.parametrize("url", ["file:///tmp/a", "http://www.douyin.com/user/a", "https://douyin.com.evil.test/a", "https://a@www.douyin.com/user/a", "https://www.douyin.com:bad/user/a"])
def test_reject_external_contact_urls(url):
    raw = example()
    raw["users"][0]["contacts"][0]["profile_url"] = url
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_reject_duplicate_ids_and_bad_timezone():
    raw = example()
    raw["users"].append({"name": "other", "id": parse_config(raw).users[0].id})
    with pytest.raises(ConfigError, match="Duplicate"):
        parse_config(raw)
    with pytest.raises(ConfigError, match="timezone"):
        parse_config({"users": [], "schedule": {"timezone": "Not/AZone"}})


def test_cloak_migration_is_explicit():
    config = parse_config({"users": [], "browser": {"backend": "cloakbrowser"}})
    assert config.browser.backend == "playwright"
    assert "CloakBrowser" in config.migration_warnings[0]
    assert config_to_dict(config)["migration_warnings"]


def legacy_project(tmp_path):
    old = tmp_path / "old project 中文"
    (old / "config").mkdir(parents=True)
    (old / "config" / "douyinfire.json").write_text(json.dumps(example()))
    profile = old / "data" / "profiles" / "main"
    profile.mkdir(parents=True)
    (profile / "Login Data").write_text("simulated login state")
    return old, profile


def test_migration_is_explicit_private_and_idempotent(tmp_path):
    old, profile = legacy_project(tmp_path)
    dest = tmp_path / "Application Support" / "DouyinFire"
    before = (old / "config" / "douyinfire.json").read_bytes()
    result = migrate_legacy(old, dest)
    assert result.migrated
    assert result.backup_dir.is_dir()
    config = load_config(result.config_path)
    copied = config.data_dir / "profiles" / config.users[0].id / "Login Data"
    assert copied.read_text() == "simulated login state"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600
    assert not config.schedule.enabled
    assert config.browser.run_headless
    assert (old / "config" / "douyinfire.json").read_bytes() == before
    assert (profile / "Login Data").exists()
    second = migrate_legacy(old, dest)
    assert not second.migrated
    assert len(list((dest / "backups").iterdir())) == 1


def test_migration_open_profile_never_publishes_partial_config(tmp_path):
    old, profile = legacy_project(tmp_path)
    (profile / "SingletonLock").symlink_to("missing-host-lock")
    dest = tmp_path / "destination"
    with pytest.raises(RuntimeError, match="关闭"):
        migrate_legacy(old, dest)
    assert not (dest / "config" / "douyinfire.yaml").exists()
    assert (profile / "Login Data").exists()
    assert list((dest / "backups").iterdir())


def test_relative_paths_do_not_duplicate_after_save(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = parse_config({"users": []}, base_dir=Path("project"))
    path = tmp_path / "project" / "config" / "app.json"
    save_config(config, path)
    loaded = load_config(path)
    assert loaded.data_dir == tmp_path / "project" / "data"


def test_wrong_browser_section_type_is_rejected():
    with pytest.raises(ConfigError):
        parse_config({"users": [], "browser": []})


def test_same_platform_identity_cannot_bypass_ids():
    raw = {"users": [
        {"id": "one", "name": "one", "identity": "platform-user"},
        {"id": "two", "name": "two", "identity": "platform-user"},
    ]}
    with pytest.raises(ConfigError, match="account identities"):
        parse_config(raw)
    raw = {"users": [{"name": "main", "contacts": [
        {"id": "one", "name": "one", "identity": "same-friend"},
        {"id": "two", "name": "two", "identity": "same-friend"},
    ]}]}
    with pytest.raises(ConfigError, match="contact identities"):
        parse_config(raw)
