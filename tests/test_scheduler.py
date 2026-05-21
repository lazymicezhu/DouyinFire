from datetime import datetime
import random

from douyinfire.config import ScheduleConfig
from douyinfire.scheduler import delay_until, next_run_at


def test_next_run_uses_today_when_time_is_future() -> None:
    now = datetime(2026, 5, 21, 0, 0)
    schedule = ScheduleConfig(time="00:05", jitter_minutes=0)

    assert next_run_at(now, schedule) == datetime(2026, 5, 21, 0, 5)


def test_next_run_rolls_to_tomorrow_when_time_passed() -> None:
    now = datetime(2026, 5, 21, 1, 0)
    schedule = ScheduleConfig(time="00:05", jitter_minutes=0)

    assert next_run_at(now, schedule) == datetime(2026, 5, 22, 0, 5)


def test_next_run_adds_deterministic_jitter() -> None:
    now = datetime(2026, 5, 21, 0, 0)
    schedule = ScheduleConfig(time="00:05", jitter_minutes=20)

    target = next_run_at(now, schedule, rng=random.Random(1))

    assert datetime(2026, 5, 21, 0, 5) <= target <= datetime(2026, 5, 21, 0, 25)


def test_delay_until_never_negative() -> None:
    assert delay_until(datetime(2026, 5, 21, 0, 0), now=datetime(2026, 5, 21, 0, 1)) == 0
