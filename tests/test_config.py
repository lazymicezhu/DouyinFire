from pathlib import Path

import pytest

from douyinfire.config import ConfigError, parse_config
from douyinfire.gui import form_payload_to_yaml


def test_parse_config_applies_defaults() -> None:
    config = parse_config(
        {
            "users": [
                {
                    "name": "main",
                    "contacts": ["a"],
                    "message": "hello",
                }
            ]
        },
        base_dir=Path("/tmp/douyinfire"),
    )

    assert config.users[0].name == "main"
    assert config.schedule.time == "00:05"
    assert config.schedule.jitter_minutes == 20
    assert config.timeouts.home_ready_seconds == 5
    assert config.timeouts.contact_search_seconds == 15
    assert config.data_dir == Path("/tmp/douyinfire/data")
    assert config.users[0].contacts[0].name == "a"
    assert config.users[0].contacts[0].profile_url == ""


def test_parse_config_accepts_timeouts() -> None:
    config = parse_config(
        {
            "timeouts": {
                "home_ready_seconds": 9,
                "message_panel_seconds": 12,
                "contact_search_seconds": 30,
                "input_box_seconds": 14,
                "after_send_seconds": 4,
            },
            "users": [{"name": "main", "contacts": ["a"], "message": "hello"}],
        }
    )

    assert config.timeouts.home_ready_seconds == 9
    assert config.timeouts.message_panel_seconds == 12
    assert config.timeouts.contact_search_seconds == 30
    assert config.timeouts.input_box_seconds == 14
    assert config.timeouts.after_send_seconds == 4


def test_parse_config_accepts_browser_config() -> None:
    config = parse_config(
        {
            "browser": {
                "backend": "cloakbrowser",
                "run_headless": True,
                "login_headless": False,
                "storage_state_path": "data/states/main.json",
            },
            "users": [{"name": "main", "contacts": ["a"], "message": "hello"}],
        },
        base_dir=Path("/tmp/douyinfire"),
    )

    assert config.browser.backend == "cloakbrowser"
    assert config.browser.run_headless is True
    assert config.browser.storage_state_path == Path("/tmp/douyinfire/data/states/main.json")


def test_parse_config_accepts_profile_contact_objects() -> None:
    config = parse_config(
        {
            "users": [
                {
                    "name": "main",
                    "contacts": [
                        {
                            "name": "friend",
                            "profile_url": "https://www.douyin.com/user/example",
                            "message": "custom",
                        }
                    ],
                    "message": "hello",
                }
            ]
        }
    )

    contact = config.users[0].contacts[0]
    assert contact.name == "friend"
    assert contact.profile_url == "https://www.douyin.com/user/example"
    assert contact.message == "custom"


def test_parse_config_rejects_bad_browser_backend() -> None:
    with pytest.raises(ConfigError):
        parse_config(
            {
                "browser": {"backend": "bad"},
                "users": [{"name": "main", "contacts": ["a"], "message": "hello"}],
            }
        )


def test_form_payload_to_yaml_round_trips() -> None:
    text = form_payload_to_yaml(
        {
            "data_dir": "data",
            "log_dir": "logs",
            "screenshot_dir": "screenshots",
            "failure_notify_threshold": 1,
            "browser": {
                "backend": "cloakbrowser",
                "run_headless": True,
                "login_headless": False,
                "storage_state_path": "data/states/main.json",
            },
            "timeouts": {
                "home_ready_seconds": 5,
                "message_panel_seconds": 8,
                "contact_search_seconds": 15,
                "input_box_seconds": 10,
                "after_send_seconds": 2,
            },
            "schedule": {
                "time": "00:05",
                "jitter_minutes": 20,
                "min_contact_interval_seconds": 10,
            },
            "users": [{"name": "main", "enabled": True, "contacts": "a | custom\nb", "message": "hello"}],
        }
    )
    config = parse_config(__import__("yaml").safe_load(text))

    assert config.browser.backend == "cloakbrowser"
    assert config.users[0].contacts[0].name == "a"
    assert config.users[0].contacts[0].profile_url == ""
    assert config.users[0].contacts[0].message == "custom"
    assert config.users[0].contacts[1].name == "b"


def test_form_payload_accepts_structured_contact_rows() -> None:
    text = form_payload_to_yaml(
        {
            "users": [
                {
                    "name": "main",
                    "enabled": True,
                    "contacts": [
                        {"name": "呱唧唧呱", "message": "测试可选消息"},
                        {"name": "朋友A", "message": ""},
                    ],
                    "message": "全局消息",
                }
            ],
        }
    )
    config = parse_config(__import__("yaml").safe_load(text))

    assert config.users[0].message == "全局消息"
    assert config.users[0].contacts[0].name == "呱唧唧呱"
    assert config.users[0].contacts[0].profile_url == ""
    assert config.users[0].contacts[0].message == "测试可选消息"
    assert config.users[0].contacts[1].message == ""


def test_parse_config_rejects_bad_timeout() -> None:
    with pytest.raises(ConfigError):
        parse_config(
            {
                "timeouts": {"contact_search_seconds": 0},
                "users": [{"name": "main", "contacts": ["a"], "message": "hello"}],
            }
        )


def test_parse_config_rejects_missing_users() -> None:
    with pytest.raises(ConfigError):
        parse_config({})


def test_parse_config_rejects_bad_time() -> None:
    with pytest.raises(ConfigError):
        parse_config(
            {
                "schedule": {"time": "25:99"},
                "users": [{"name": "main", "contacts": ["a"], "message": "hello"}],
            }
        )
