from douyinfire.tasks import ContactResult, UserRunResult


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
