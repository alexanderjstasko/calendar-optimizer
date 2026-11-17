"""
schedule_config.py — how often each job runs, read from .env.

Every value is optional; the defaults below apply when a key is missing or
empty. "off" disables a job. Times are America/New_York. Bad values never
stop the server: they are reported (log + GET /schedule) and the default is
used instead.

    SYNC_CALENDARS_EVERY_MIN   calendar adapters (Google/Apple/Outlook/Partiful)   default 5
    SETTLE_EVERY_MIN           Notion write-backs for events that have passed     default 15
    SCOUT_FETCH_DAYS/TIME      read the event websites                            default mon 06:00
    SCOUT_RECOMMEND_DAYS/TIME  Claude recommends events (one model call)          default daily 07:00
    OPTIMIZER_RUN_DAYS/TIME    Claude plans the week (one model call)             default sun 06:30
    NOTION_CHECK_EVERY_MIN     re-read Notion; place new rows, drop deleted ones   default 60
    CONFLICT_CHECK_EVERY_MIN   approved blocks vs. newly synced events             default 5

The routine expander has no job of its own since 2026-10-26: routine goals
are proposed inside the Sunday run and by the Notion/conflict checks.

DAYS accepts day names separated by commas (mon,thu), a range (mon-fri),
"daily", or "off". TIME is 24-hour HH:MM.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

TIMEZONE = "America/New_York"
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DEFAULTS: dict[str, str] = {
    "SYNC_CALENDARS_EVERY_MIN": "5",
    "SETTLE_EVERY_MIN": "15",
    "SCOUT_FETCH_DAYS": "mon",
    "SCOUT_FETCH_TIME": "06:00",
    "SCOUT_RECOMMEND_DAYS": "daily",
    "SCOUT_RECOMMEND_TIME": "07:00",
    "OPTIMIZER_RUN_DAYS": "sun",
    "OPTIMIZER_RUN_TIME": "06:30",
    "NOTION_CHECK_EVERY_MIN": "60",
    "CONFLICT_CHECK_EVERY_MIN": "5",
}


@dataclass
class IntervalJob:
    minutes: int | None          # None = off

    @property
    def enabled(self) -> bool:
        return self.minutes is not None

    def describe(self) -> str:
        return f"every {self.minutes} min" if self.enabled else "off"


@dataclass
class CronJob:
    enabled: bool
    day_of_week: str | None      # APScheduler day_of_week expression; None = every day
    hour: int
    minute: int

    def describe(self) -> str:
        if not self.enabled:
            return "off"
        when = "daily" if self.day_of_week is None else self.day_of_week
        return f"{when} at {self.hour:02d}:{self.minute:02d} {TIMEZONE}"


@dataclass
class Schedule:
    calendars: IntervalJob
    settle: IntervalJob
    scout_fetch: CronJob
    scout_recommend: CronJob
    optimizer: CronJob
    notion_check: IntervalJob = field(default_factory=lambda: IntervalJob(60))
    conflict_check: IntervalJob = field(default_factory=lambda: IntervalJob(5))
    errors: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "timezone": TIMEZONE,
            "calendar_sync": self.calendars.describe(),
            "settlement": self.settle.describe(),
            "scout_fetch": self.scout_fetch.describe(),
            "scout_recommend": self.scout_recommend.describe(),
            "optimizer": self.optimizer.describe(),
            "notion_check": self.notion_check.describe(),
            "conflict_check": self.conflict_check.describe(),
            "errors": list(self.errors),
        }


def _raw(name: str, env: dict[str, str] | None) -> str:
    source = os.environ if env is None else env
    value = (source.get(name) or "").strip()
    return value if value else DEFAULTS[name]


def parse_interval(raw: str) -> int | None:
    """'off' or '0' -> None; otherwise a positive whole number of minutes."""
    value = raw.strip().lower()
    if value in ("off", "0", "none", "never"):
        return None
    if not re.fullmatch(r"\d+", value) or int(value) <= 0:
        raise ValueError(f"expected a positive number of minutes or 'off', got {raw!r}")
    return int(value)


def parse_days(raw: str) -> tuple[bool, str | None]:
    """(enabled, APScheduler day_of_week or None for every day)."""
    value = raw.strip().lower()
    if value in ("off", "none", "never", ""):
        return False, None
    if value in ("daily", "everyday", "every day", "*"):
        return True, None
    parts = []
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (x.strip() for x in part.split("-", 1))
            if a not in DAY_NAMES or b not in DAY_NAMES:
                raise ValueError(f"unknown day range {part!r}; use names like mon-fri")
            i, j = DAY_NAMES.index(a), DAY_NAMES.index(b)
            if i > j:  # a range across the weekend (fri-mon): APScheduler wants it spelled out
                part = ",".join(DAY_NAMES[i:] + DAY_NAMES[: j + 1])
        elif part not in DAY_NAMES:
            raise ValueError(f"unknown day {part!r}; use mon..sun, a range like mon-fri, daily, or off")
        parts.append(part)
    return True, ",".join(parts)


def parse_time(raw: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw.strip())
    if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
        raise ValueError(f"expected 24-hour HH:MM, got {raw!r}")
    return int(m.group(1)), int(m.group(2))


def load_schedule(env: dict[str, str] | None = None) -> Schedule:
    """Read every cadence setting; a bad value is recorded in `errors` and replaced by its default."""
    errors: list[str] = []

    def interval(name: str) -> IntervalJob:
        raw = _raw(name, env)
        try:
            return IntervalJob(parse_interval(raw))
        except ValueError as exc:
            errors.append(f"{name}: {exc}; using {DEFAULTS[name]}")
            return IntervalJob(parse_interval(DEFAULTS[name]))

    def cron(days_name: str, time_name: str) -> CronJob:
        raw_days, raw_time = _raw(days_name, env), _raw(time_name, env)
        try:
            enabled, dow = parse_days(raw_days)
        except ValueError as exc:
            errors.append(f"{days_name}: {exc}; using {DEFAULTS[days_name]}")
            enabled, dow = parse_days(DEFAULTS[days_name])
        try:
            hour, minute = parse_time(raw_time)
        except ValueError as exc:
            errors.append(f"{time_name}: {exc}; using {DEFAULTS[time_name]}")
            hour, minute = parse_time(DEFAULTS[time_name])
        return CronJob(enabled, dow, hour, minute)

    return Schedule(
        calendars=interval("SYNC_CALENDARS_EVERY_MIN"),
        settle=interval("SETTLE_EVERY_MIN"),
        scout_fetch=cron("SCOUT_FETCH_DAYS", "SCOUT_FETCH_TIME"),
        scout_recommend=cron("SCOUT_RECOMMEND_DAYS", "SCOUT_RECOMMEND_TIME"),
        optimizer=cron("OPTIMIZER_RUN_DAYS", "OPTIMIZER_RUN_TIME"),
        notion_check=interval("NOTION_CHECK_EVERY_MIN"),
        conflict_check=interval("CONFLICT_CHECK_EVERY_MIN"),
        errors=errors,
    )
