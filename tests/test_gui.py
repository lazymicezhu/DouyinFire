from pathlib import Path

import pytest

from douyinfire.config import ConfigError
from douyinfire.config import load_config
from douyinfire.gui import GuiServer, JobState
from douyinfire.tasks import ContactResult, UserRunResult


def test_job_state_tracks_steps() -> None:
    job = JobState()

    assert job.start("run:main") is True
    job.update_step("open_home", "running", "main")
    job.update_step("open_home", "done", "main")
    snapshot = job.snapshot()

    assert snapshot["running"] is True
    assert snapshot["steps"][0]["key"] == "open_home"
    assert snapshot["steps"][0]["status"] == "done"


def test_job_state_marks_running_step_failed() -> None:
    job = JobState()

    job.start("run:main")
    job.update_step("profile_message_entry", "running", "main")
    job.finish("failed", error="not found")
    contact_step = [step for step in job.snapshot()["steps"] if step["key"] == "profile_message_entry"][0]

    assert contact_step["status"] == "failed"
    assert contact_step["error"] == "not found"


def test_job_state_records_duration_and_failed_contacts() -> None:
    job = JobState()
    result = UserRunResult(
        user="main",
        started_at="2026-05-25T00:00:00",
        ended_at="2026-05-25T00:00:10",
        results=[
            ContactResult(contact="ok", success=True),
            ContactResult(contact="bad", success=False, reason="not found", profile_url="https://www.douyin.com/user/bad"),
        ],
    )

    job.start("run:main")
    job.finish("completed", result=result)
    snapshot = job.snapshot()

    assert snapshot["running"] is False
    assert snapshot["duration_seconds"] >= 0
    assert snapshot["last_failed"] == {
        "main": [{"name": "bad", "profile_url": "https://www.douyin.com/user/bad", "message": ""}]
    }


def test_retry_failed_requires_failed_contacts() -> None:
    server = GuiServer(Path("config/douyinfire.yaml"))

    with pytest.raises(ConfigError, match="没有可重试失败项"):
        server._retry_failed()


def test_add_login_user_appends_user_and_uses_user_state(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "douyinfire.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
data_dir: data
log_dir: logs
screenshot_dir: screenshots
browser:
  backend: cloakbrowser
  run_headless: true
  login_headless: false
  storage_state_path: data/states/main.json
users:
  - name: main
    enabled: true
    contacts:
      - name: friend
        profile_url: https://www.douyin.com/user/friend
    message: hello
""",
        encoding="utf-8",
    )
    server = GuiServer(config_path)

    server._add_login_user("work", "main")
    config = load_config(config_path)

    assert [user.name for user in config.users] == ["main", "work"]
    assert str(config.browser.storage_state_path).endswith("data/states/{user}.json")
    assert config.user("work").contacts[0].name == "friend"
