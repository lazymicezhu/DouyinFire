"""Pure daily scheduling; execution and deduplication belong to TaskManager."""
from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import ScheduleConfig


def _local(now: datetime, schedule: ScheduleConfig) -> datetime:
    zone = ZoneInfo(schedule.timezone)
    return now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)


def _daily_target(day: date, schedule: ScheduleConfig, seed: str = "") -> datetime:
    zone = ZoneInfo(schedule.timezone)
    hour, minute = map(int, schedule.time.split(":"))
    # Do not let a late-night jitter become tomorrow's work.
    available = 23 * 60 + 59 - (hour * 60 + minute)
    jitter_limit = min(schedule.jitter_minutes, available)
    token = f"{seed}:{day.isoformat()}:{schedule.time}:{schedule.timezone}".encode()
    offset = int.from_bytes(hashlib.sha256(token).digest()[:8], "big") % (jitter_limit + 1)
    target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone) + timedelta(minutes=offset)
    # A spring-forward wall-clock gap maps to the first corresponding valid time.
    return target.astimezone(timezone.utc).astimezone(zone)


def _same_awareness(target: datetime, reference: datetime) -> datetime:
    return target.replace(tzinfo=None) if reference.tzinfo is None else target


def daily_due(now: datetime, schedule: ScheduleConfig, seed: str = "") -> datetime | None:
    """Return today's target while due inside its catch-up window, else None.

    Caller must persist/claim the returned calendar day before execution. This
    function deliberately has no mutable state and never catches up prior days.
    A zero catch-up window still permits the target minute's normal polling.
    """
    if not schedule.enabled:
        return None
    local = _local(now, schedule)
    target = _daily_target(local.date(), schedule, seed)
    elapsed = local.timestamp() - target.timestamp()
    if target.date() != local.date() or elapsed < 0 or elapsed >= max(60, schedule.catchup_minutes * 60):
        return None
    return _same_awareness(target, now)


def next_run_at(now: datetime, schedule: ScheduleConfig, rng: random.Random | None = None, *, seed: str = "") -> datetime:
    """Show the next fixed target, even when scheduling is currently disabled.

    rng is retained for old callers but is intentionally unused: displaying the
    schedule must not re-randomize it or disagree with the background worker.
    """
    local = _local(now, schedule)
    target = _daily_target(local.date(), schedule, seed)
    if target.timestamp() <= local.timestamp():
        target = _daily_target(local.date() + timedelta(days=1), schedule, seed)
    return _same_awareness(target, now)


def delay_until(target: datetime, now: datetime | None = None) -> int:
    current = now or datetime.now(tz=target.tzinfo)
    if target.tzinfo is not None and current.tzinfo is not None:
        seconds = target.timestamp() - current.timestamp()
    else:
        seconds = (target - current).total_seconds()
    return max(0, int(seconds))
