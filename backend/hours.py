"""
Sleep and work hours, where the person lives and works, and the planning
settings the Hours tab holds: the single source of truth for every part of
the system that needs to know when the person is asleep, at work or free.

The values live in the `settings` table (key `hours`) and are edited from the
calendar app's Hours tab (GET/PUT /hours in main.py). The optimizer sends them
to the model, the events scout keeps only candidates that start inside the
free window, and the calendar draws the sleep, work and free bands from the
same answer. Nothing else in the code carries a clock time for these.

Shape (every time is local New York, 24-hour HH:MM):

    {"home": "1 Home St, New York",                                 # where days start and end
     "weekdays": {"sleep": {"start": "23:00", "end": "07:00"},
                  "work":  {"start": "09:00", "end": "17:00",
                            "location": "100 Office Plaza",         # the office
                            "office_days": ["Tue", "Wed", "Thu"]}}, # other weekdays: work from home
     "weekends": {"sleep": {"start": "23:00", "end": "08:00"},
                  "work": null},                                     # never work at the weekend
     "free_hours_per_week": 10,         # the planner leaves at least this many hours unscheduled
     "calendar_review_minutes": 20,     # the Sunday block for approving next week's plan
     "goals_review_every_months": 1,    # the Goals and Telos review: how often ...
     "goals_review_minutes": 30,        # ... and how long (one block, placed by the planner)
     "event_keywords": ["jazz", "AI talks"]}   # what the scout should look out for this week

Sleep is one range that may cross midnight (bed at 23:00, up at 07:00). Work
stays inside one day; weekend work is always null. **Free time is derived**
(since 2026-10-26, no longer a stored range): on a weekday from the end of
work to bedtime (from waking when there is no work), at the weekend from
waking to bedtime. `ranges_for()` adds it as "free" so every consumer reads
one answer. `home` and `work.location` are free text for the model (travel
planning); either may be empty. An empty home falls back to
LLM_HOME_LOCATION in .env.
"""

from __future__ import annotations

import copy
import datetime as dt
from zoneinfo import ZoneInfo
import json
import re
from typing import Any

import db

SETTING_KEY = "hours"
PROFILES = ("weekdays", "weekends")
RANGES = ("sleep", "work")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_NAMES = DAY_NAMES[:5]
# Longer names and lower case are accepted on input and normalised to these.
_DAY_ALIASES = {name.lower(): name for name in DAY_NAMES}
_DAY_ALIASES.update({
    "monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu", "friday": "Fri",
    "saturday": "Sat", "sunday": "Sun",
})

MAX_LOCATION_CHARS = 200
MAX_KEYWORDS = 30
MAX_KEYWORD_CHARS = 40
DEFAULT_FREE_HOURS = 10
DEFAULT_REVIEW_MINUTES = 20
DEFAULT_GOALS_REVIEW_MONTHS = 1
DEFAULT_GOALS_REVIEW_MINUTES = 30
# What the app showed before the Hours tab existed (awake 07-23, work 09-17
# Mon-Fri), so an untouched install behaves as before.
DEFAULT_HOURS: dict[str, Any] = {
    "home": "",
    "weekdays": {
        "sleep": {"start": "23:00", "end": "07:00"},
        "work": {"start": "09:00", "end": "17:00", "location": "", "office_days": []},
    },
    "weekends": {
        "sleep": {"start": "23:00", "end": "08:00"},
        "work": None,
    },
    "free_hours_per_week": DEFAULT_FREE_HOURS,
    "calendar_review_minutes": DEFAULT_REVIEW_MINUTES,
    "goals_review_every_months": DEFAULT_GOALS_REVIEW_MONTHS,
    "goals_review_minutes": DEFAULT_GOALS_REVIEW_MINUTES,
    "event_keywords": [],
}

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class HoursError(ValueError):
    """The submitted hours are not usable; the message says which field and why."""


def minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _check_time(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _HHMM.match(value):
        raise HoursError(f"{where} must be a time like 09:30, got {value!r}")
    return value


def _check_text(value: Any, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HoursError(f"{where} must be text")
    text = " ".join(value.split())
    if len(text) > MAX_LOCATION_CHARS:
        raise HoursError(f"{where} is longer than {MAX_LOCATION_CHARS} characters")
    return text


def _check_range(value: Any, where: str, *, may_wrap: bool, optional: bool) -> dict[str, str] | None:
    if value is None:
        if optional:
            return None
        raise HoursError(f"{where} is required")
    if not isinstance(value, dict):
        raise HoursError(f"{where} must have a start and an end")
    start = _check_time(value.get("start"), f"{where}.start")
    end = _check_time(value.get("end"), f"{where}.end")
    if start == end:
        raise HoursError(f"{where}: start and end are the same time")
    if not may_wrap and minutes(end) < minutes(start):
        raise HoursError(f"{where}: the end ({end}) is before the start ({start})")
    return {"start": start, "end": end}


def _check_office_days(value: Any, where: str) -> list[str]:
    """Weekday names (Mon..Fri), de-duplicated and in week order; None/[] = always at home."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\s]+", value) if part]
    if not isinstance(value, list):
        raise HoursError(f"{where} must be a list of weekday names")
    chosen: set[str] = set()
    for item in value:
        name = _DAY_ALIASES.get(str(item).strip().lower())
        if name is None:
            raise HoursError(f"{where}: unknown day {item!r}")
        if name not in WEEKDAY_NAMES:
            raise HoursError(f"{where}: {name} is not a weekday")
        chosen.add(name)
    return [d for d in WEEKDAY_NAMES if d in chosen]


def _check_number(value: Any, where: str, *, low: float, high: float, default: float, integer: bool = False) -> float | int:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise HoursError(f"{where} must be a number") from None
    if integer and number != int(number):
        raise HoursError(f"{where} must be a whole number")
    if not low <= number <= high:
        raise HoursError(f"{where} must be between {low:g} and {high:g}")
    return int(number) if integer else (int(number) if number == int(number) else round(number, 2))


def parse_keywords(value: Any, where: str = "event_keywords") -> list[str]:
    """A list, or one comma-separated string; trimmed, de-duplicated (case-insensitive), order kept."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise HoursError(f"{where} must be a list of words or a comma-separated text")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise HoursError(f"{where} must contain text only")
        word = " ".join(item.split())
        if not word:
            continue
        if len(word) > MAX_KEYWORD_CHARS:
            raise HoursError(f"{where}: {word[:20]!r}… is longer than {MAX_KEYWORD_CHARS} characters")
        if word.lower() in seen:
            continue
        seen.add(word.lower())
        out.append(word)
    if len(out) > MAX_KEYWORDS:
        raise HoursError(f"{where}: at most {MAX_KEYWORDS} key words")
    return out


def validate(data: Any) -> dict[str, Any]:
    """Return a clean copy of `data` (only the known keys), or raise HoursError."""
    if not isinstance(data, dict):
        raise HoursError("hours must be an object with weekdays and weekends")
    out: dict[str, Any] = {"home": _check_text(data.get("home"), "home")}
    for profile in PROFILES:
        block = data.get(profile)
        if not isinstance(block, dict):
            raise HoursError(f"{profile} is required")
        sleep = _check_range(block.get("sleep"), f"{profile}.sleep", may_wrap=True, optional=False)
        work = None
        if profile == "weekdays":  # weekends never have work (2026-10-26)
            work = _check_range(block.get("work"), f"{profile}.work", may_wrap=False, optional=True)
            if work is not None:
                work["location"] = _check_text(block["work"].get("location"), f"{profile}.work.location")
                work["office_days"] = _check_office_days(block["work"].get("office_days"), f"{profile}.work.office_days")
                if _inside_sleep(work, sleep):
                    raise HoursError(f"{profile}.work lies entirely inside sleep")
        out[profile] = {"sleep": sleep, "work": work}
    out["free_hours_per_week"] = _check_number(data.get("free_hours_per_week"), "free_hours_per_week",
                                               low=0, high=100, default=DEFAULT_FREE_HOURS)
    out["calendar_review_minutes"] = _check_number(data.get("calendar_review_minutes"), "calendar_review_minutes",
                                                   low=5, high=180, default=DEFAULT_REVIEW_MINUTES, integer=True)
    out["goals_review_every_months"] = _check_number(data.get("goals_review_every_months"), "goals_review_every_months",
                                                     low=1, high=12, default=DEFAULT_GOALS_REVIEW_MONTHS, integer=True)
    out["goals_review_minutes"] = _check_number(data.get("goals_review_minutes"), "goals_review_minutes",
                                                low=5, high=240, default=DEFAULT_GOALS_REVIEW_MINUTES, integer=True)
    out["event_keywords"] = parse_keywords(data.get("event_keywords"))
    return out


def _inside_sleep(rng: dict[str, str], sleep: dict[str, str]) -> bool:
    """True when a same-day range sits wholly within the (possibly wrapping) sleep range."""
    s, e = minutes(sleep["start"]), minutes(sleep["end"])
    a, b = minutes(rng["start"]), minutes(rng["end"])
    if s < e:  # sleep inside one day, e.g. a nap-like 01:00-08:00
        return s <= a and b <= e
    # wraps midnight: asleep from s to 24:00 and from 00:00 to e
    return (s <= a) or (b <= e)


def load() -> dict[str, Any]:
    """The saved hours, or the defaults when nothing was saved yet (or the saved value is unreadable)."""
    raw = db.get_setting(SETTING_KEY)
    if raw:
        try:
            return validate(json.loads(raw))
        except (ValueError, TypeError):
            pass
    return copy.deepcopy(DEFAULT_HOURS)


def status() -> dict[str, Any]:
    """What GET /hours returns: the hours plus whether they are the defaults and when they were last saved."""
    raw = db.get_setting(SETTING_KEY)
    saved_at = None
    if raw:
        with db.get_conn() as conn:
            row = conn.execute("SELECT updated_at FROM settings WHERE key = ?", (SETTING_KEY,)).fetchone()
            saved_at = row["updated_at"] if row else None
    return {"hours": load(), "saved": bool(raw), "saved_at": saved_at}


def save(data: Any, now: dt.datetime | None = None) -> dict[str, Any]:
    """Validate and store; returns status()."""
    clean = validate(data)
    stamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    db.set_setting(SETTING_KEY, json.dumps(clean), stamp)
    return status()


# --- derived views --------------------------------------------------------------


def profile_for(day: dt.date) -> str:
    return "weekends" if day.weekday() >= 5 else "weekdays"


def bedtime_end(sleep: dict[str, str]) -> str:
    """The clock time the awake day ends: bedtime, or "24:00" when bed is after midnight."""
    return sleep["start"] if minutes(sleep["start"]) > minutes(sleep["end"]) else "24:00"


def free_range(block: dict[str, Any]) -> dict[str, str]:
    """
    The derived free window of a profile: after work until bed on a work day,
    from waking until bed otherwise. "24:00" as an end means midnight.
    """
    work = block.get("work")
    start = work["end"] if work else block["sleep"]["end"]
    end = bedtime_end(block["sleep"])
    if minutes(end) <= minutes(start):  # work runs to bedtime: no free time that day
        end = start
    return {"start": start, "end": end}


def ranges_for(day: dt.date, hours: dict[str, Any] | None = None) -> dict[str, Any]:
    """The sleep/work ranges that apply on `day`, plus the derived `free` window."""
    block = (hours or load())[profile_for(day)]
    return {**block, "free": free_range(block)}


def too_soon_after_work(start: dt.datetime, hours: dict[str, Any] | None = None,
                        travel: dt.timedelta = dt.timedelta(minutes=30)) -> bool:
    """
    True when something out of the house starts on a work day before work
    ends plus `travel`: a 5 PM talk is out of reach when work ends at 5, since
    the person still has to get there. Weekends and days off never are.
    `start` is aware; it is read in New York time.
    """
    local = start.astimezone(ZoneInfo("America/New_York"))
    work = ranges_for(local.date(), hours).get("work")
    if not work:
        return False
    tz = local.tzinfo
    work_start = dt.datetime.combine(local.date(), dt.time.fromisoformat(work["start"]), tzinfo=tz)
    work_end = dt.datetime.combine(local.date(), dt.time.fromisoformat(work["end"]), tzinfo=tz)
    return work_start - travel < local < work_end + travel


def in_range(t: dt.time, rng: dict[str, str] | None) -> bool:
    """True when the clock time falls inside the range (end exclusive); a wrapping range is handled."""
    if not rng:
        return False
    m = t.hour * 60 + t.minute
    s, e = minutes(rng["start"]), minutes(rng["end"])
    if s == e:
        return False  # empty (work ends at bedtime), not the whole day
    return s <= m < e if s < e else m >= s or m < e


def awake(rng_block: dict[str, Any]) -> dict[str, str]:
    """The awake window of a profile: from waking up to going to bed."""
    return {"start": rng_block["sleep"]["end"], "end": rng_block["sleep"]["start"]}


def is_office_day(day: dt.date, hours: dict[str, Any] | None = None) -> bool:
    block = (hours or load())["weekdays"] if day.weekday() < 5 else None
    work = block.get("work") if block else None
    return bool(work and DAY_NAMES[day.weekday()] in (work.get("office_days") or []))


def for_model(hours: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    The preferences entry the optimizer and scout send: per profile the sleep
    and work ranges, the derived free window and the awake window; for work
    the office days spelled out (every other weekday is a work-from-home day).
    """
    h = hours or load()
    out: dict[str, Any] = {}
    for profile in PROFILES:
        block = h[profile]
        entry: dict[str, Any] = {"sleep": block["sleep"], "work": None, "free": free_range(block), "awake": awake(block)}
        if block.get("work"):
            work = dict(block["work"])
            days = work.get("office_days") or []
            work["office_days"] = days
            work["home_days"] = [d for d in WEEKDAY_NAMES if d not in days]
            entry["work"] = work
        out[profile] = entry
    return out


def home_location(hours: dict[str, Any] | None = None, fallback: str = "") -> str:
    """The home address from the Hours tab, else `fallback` (the .env setting)."""
    return (hours or load()).get("home") or fallback


def free_hours_per_week(hours: dict[str, Any] | None = None) -> float:
    return float((hours or load()).get("free_hours_per_week", DEFAULT_FREE_HOURS))


def calendar_review_minutes(hours: dict[str, Any] | None = None) -> int:
    return int((hours or load()).get("calendar_review_minutes", DEFAULT_REVIEW_MINUTES))


def goals_review_every_months(hours: dict[str, Any] | None = None) -> int:
    return int((hours or load()).get("goals_review_every_months", DEFAULT_GOALS_REVIEW_MONTHS))


def goals_review_minutes(hours: dict[str, Any] | None = None) -> int:
    return int((hours or load()).get("goals_review_minutes", DEFAULT_GOALS_REVIEW_MINUTES))


def event_keywords(hours: dict[str, Any] | None = None) -> list[str]:
    return list((hours or load()).get("event_keywords") or [])
