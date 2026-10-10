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


def test_jitter_is_fixed_for_the_day_and_display_matches_due():
    from douyinfire.scheduler import daily_due
    schedule = ScheduleConfig(time="12:00", enabled=True, jitter_minutes=20)
    morning = datetime(2026, 10, 10, 8, 0)
    first = next_run_at(morning, schedule)
    assert next_run_at(morning, schedule) == first
    assert daily_due(first, schedule) == first


def test_catchup_never_runs_a_previous_day():
    from douyinfire.scheduler import daily_due
    schedule = ScheduleConfig(time="23:55", enabled=True, jitter_minutes=0, catchup_minutes=180)
    assert daily_due(datetime(2026, 10, 10, 23, 58), schedule) == datetime(2026, 10, 10, 23, 55)
    assert daily_due(datetime(2026, 10, 11, 0, 5), schedule) is None
    assert daily_due(datetime(2026, 10, 11, 21, 0), schedule) is None


def test_catchup_cutoff_and_disabled_schedule():
    from douyinfire.scheduler import daily_due
    schedule = ScheduleConfig(time="12:00", enabled=True, jitter_minutes=0, catchup_minutes=30)
    assert daily_due(datetime(2026, 10, 10, 12, 29), schedule)
    assert daily_due(datetime(2026, 10, 10, 12, 30), schedule) is None
    schedule.enabled = False
    assert daily_due(datetime(2026, 10, 10, 12, 0), schedule) is None


def test_aware_time_uses_configured_zone():
    from datetime import timezone
    from douyinfire.scheduler import daily_due
    schedule = ScheduleConfig(time="12:00", enabled=True, jitter_minutes=0)
    now = datetime(2026, 10, 10, 4, 5, tzinfo=timezone.utc)
    due = daily_due(now, schedule)
    assert due.hour == 12
    assert due.utcoffset().total_seconds() == 8 * 3600
    assert delay_until(due, now) == 0


def test_midnight_jitter_stays_in_current_day():
    schedule = ScheduleConfig(time="23:59", jitter_minutes=20)
    now = datetime(2026, 10, 10, 12)
    assert next_run_at(now, schedule) == datetime(2026, 10, 10, 23, 59)


def test_dst_gap_maps_to_valid_time_once():
    from zoneinfo import ZoneInfo
    from douyinfire.scheduler import daily_due
    schedule = ScheduleConfig(time="02:30", jitter_minutes=0, timezone="America/New_York", enabled=True)
    start = datetime(2026, 3, 8, 0, tzinfo=ZoneInfo(schedule.timezone))
    target = next_run_at(start, schedule)
    assert target.hour == 3 and target.minute == 30
    assert daily_due(target, schedule) == target
