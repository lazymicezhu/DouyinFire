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
    assert config.data_dir == Path("/tmp/douyinfire/data")


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
