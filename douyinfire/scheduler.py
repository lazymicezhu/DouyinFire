from __future__ import annotations

import random
from datetime import datetime, timedelta

from .config import ScheduleConfig


def next_run_at(now: datetime, schedule: ScheduleConfig, rng: random.Random | None = None) -> datetime:
    hour, minute = [int(part) for part in schedule.time.split(":")]
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if base <= now:
        base = base + timedelta(days=1)

    generator = rng or random.Random()
    jitter = generator.randint(0, schedule.jitter_minutes) if schedule.jitter_minutes else 0
    return base + timedelta(minutes=jitter)


def delay_until(target: datetime, now: datetime | None = None) -> int:
    current = now or datetime.now()
    return max(0, int((target - current).total_seconds()))
