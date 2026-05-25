from pathlib import Path

from douyinfire.config import AppConfig, ContactConfig, UserConfig
from douyinfire.tasks import ContactResult, UserRunResult, _recently_sent, _record_sent


def test_user_run_result_counts_success_and_failure() -> None:
    result = UserRunResult(
        user="main",
        started_at="2026-05-21T00:00:00",
        ended_at="2026-05-21T00:00:01",
        results=[
            ContactResult(contact="a", success=True),
            ContactResult(contact="b", success=False, reason="not found"),
        ],
    )

    assert result.success_count == 1
    assert result.failure_count == 1


def test_recently_sent_tracks_user_contact_and_message(tmp_path: Path) -> None:
    contact_a = ContactConfig(name="a", profile_url="https://www.douyin.com/user/a")
    contact_b = ContactConfig(name="b")
    user = UserConfig(name="main", contacts=[contact_a], message="hello")
    config = AppConfig(users=[user], data_dir=tmp_path)

    assert _recently_sent(config, user, contact_a) is False
    _record_sent(config, user, contact_a)

    assert _recently_sent(config, user, contact_a) is True
    assert _recently_sent(config, user, contact_b) is False
