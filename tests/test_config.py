from pathlib import Path

import pytest

from douyinfire.config import ConfigError, parse_config


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
