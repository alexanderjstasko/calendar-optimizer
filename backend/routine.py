"""
Goal types and the routine expander.

Every Goals row in Notion carries a "Type" select (since 2026-10-19):

    Routine         expanded here, in code, with no model call: Shave, Laundry,
                    Water Plants... Since 2026-10-26 the occurrences are
                    ordinary pending proposals (nothing is auto-approved any
                    more): they are placed inside the Sunday run and by the
                    hourly triggers (triggers.py), the planner treats them as
                    fixed while it plans, and the person approves them in the
                    Plan tab like everything else.
    Flexible        sent to the optimizer exactly as before (Rockclimb, Money
                    Analysis...).
    Event Interest  only the scout reads them (quota and taste notes); the
                    optimizer never sees them, so it stops inventing blocks.

A goal with no Type keeps the old behaviour: the optimizer plans it and the
scout may count events toward it.

Expansion: parse_frequency() turns "2x Week", "Once every 3 days",
"1x Every Other Week", "1x Month", "Daily" into an interval in days; due
dates step from the goal's Last Occurred (or the latest block already on the
calendar) by that interval, snap forward to the Day of Week pool, and skip
dates that already hold a block (promoted or rejected). Each occurrence is
placed at the first free slot of required_time_hr inside, in order, the
goal's Time of Day window, the day's free window and the awake window (from
the Hours tab), never over a timed calendar event or the work hours; when the
due day is full the next two days are tried.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import db
import hours
from optimizer_schema import DISPLAY_TIMEZONE

logger = logging.getLogger(__name__)
NY = ZoneInfo(DISPLAY_TIMEZONE)

ROUTINE, FLEXIBLE, EVENT = "routine", "flexible", "event"
_TYPE_NAMES = {
    "routine": ROUTINE, "routines": ROUTINE, "fixed": ROUTINE,
    "flexible": FLEXIBLE, "flex": FLEXIBLE,
    "event interest": EVENT, "event": EVENT, "events": EVENT, "event-interest": EVENT,
}

# Time-of-day names mirror the Notion "Time of Day" select on the goals database.
TIME_OF_DAY_WINDOWS: dict[str, str] = {
    "Morning": "08:00-12:00",
    "Afternoon": "12:00-17:00",
    "Afterwork": "17:00-22:00",
    "Evening": "18:00-22:00",
    "Any": "08:00-23:00",
}
DEFAULT_DURATION_HR = 0.5
STEP_MINUTES = 15
PLACEMENT_SLACK_DAYS = 2        # a full due day: try up to this many days later
MONTH_DAYS = 30.4
WEEKDAYS = {name: i for i, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}


def goal_type(goal: dict) -> str | None:
    """'routine' | 'flexible' | 'event' | None (no Type set). Unknown values count as flexible (logged)."""
    raw = (goal.get("goal_type") or "").strip().lower()
    if not raw:
        return None
    kind = _TYPE_NAMES.get(raw)
    if kind is None:
        logger.warning("goal %r has an unknown Type %r; treating it as Flexible", goal.get("goal"), goal.get("goal_type"))
        return FLEXIBLE
    return kind


# --- frequency ----------------------------------------------------------------


_EVERY_N_DAYS = re.compile(r"^(?:once\s+)?every\s+(\d+)\s+days?$")
_N_PER = re.compile(r"^(\d+)\s*x\s*(?:a|per|/)?\s*(?:every\s+)?(other\s+week|day|days|week|weeks|wk|month|months|mo)$")
_WORDS = {
    "daily": 1.0, "every day": 1.0, "everyday": 1.0,
    "weekly": 7.0, "every week": 7.0, "once a week": 7.0,
    "every other week": 14.0, "biweekly": 14.0, "fortnightly": 14.0, "every two weeks": 14.0, "every 2 weeks": 14.0,
    "monthly": MONTH_DAYS, "every month": MONTH_DAYS, "once a month": MONTH_DAYS,
    "every other day": 2.0,
}


def parse_frequency(text: str | None) -> float | None:
    """The interval between occurrences in days, or None when the text is not understood."""
    if not text:
        return None
    s = " ".join(text.strip().lower().replace("×", "x").split())
    if s in _WORDS:
        return _WORDS[s]
    m = _EVERY_N_DAYS.match(s)
    if m:
        return float(m.group(1))
    m = _N_PER.match(s)
    if m:
        n = int(m.group(1))
        if n <= 0:
            return None
        unit = m.group(2)
        if unit.startswith("other"):
            return 14.0 / n
        if unit.startswith("day"):
            return 1.0  # several times a day is not expanded further
        if unit.startswith(("week", "wk")):
            return 7.0 / n
        return MONTH_DAYS / n
    return None


# --- due dates ----------------------------------------------------------------


def _pool(goal: dict) -> set[int]:
    try:
        names = json.loads(goal.get("days_of_week") or "[]")
    except (TypeError, ValueError):
        names = []
    return {WEEKDAYS[n.strip().lower()] for n in names if isinstance(n, str) and n.strip().lower() in WEEKDAYS}


def _snap(day: dt.date, pool: set[int]) -> dt.date:
    if not pool:
        return day
    for i in range(7):
        if (day + dt.timedelta(days=i)).weekday() in pool:
            return day + dt.timedelta(days=i)
    return day


def due_dates(goal: dict, interval: float, window_start: dt.date, window_end: dt.date, taken: set[dt.date]) -> list[dt.date]:
    """
    Dates the goal is due inside [window_start, window_end], stepping by the
    interval from the latest known occurrence (Last Occurred or a block already
    in `taken`), snapped forward to the Day of Week pool, skipping taken dates.
    """
    pool = _pool(goal)
    anchor: dt.date | None = None
    last = (goal.get("last_occurred") or "")[:10]
    if last:
        try:
            anchor = dt.date.fromisoformat(last)
        except ValueError:
            anchor = None
    if taken:
        latest = max(taken)
        anchor = latest if anchor is None or latest > anchor else anchor
    if anchor is None:
        anchor = window_start - dt.timedelta(days=round(interval))  # first one is due now
    out: list[dt.date] = []
    for k in range(1, 400):
        due = _snap(anchor + dt.timedelta(days=round(interval * k)), pool)
        if due > window_end:
            break
        if due < window_start or due in taken or due in out:
            continue
        out.append(due)
    return out


# --- placement ----------------------------------------------------------------


def _at(day: dt.date, hhmm: str) -> dt.datetime:
    if hhmm == "24:00":
        return dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), tzinfo=NY)
    return dt.datetime.combine(day, dt.time(int(hhmm[:2]), int(hhmm[3:5])), tzinfo=NY)


def _awake(day: dt.date, profile: dict) -> tuple[dt.datetime, dt.datetime]:
    sleep = profile["sleep"]
    start = _at(day, sleep["end"])
    # Bed after midnight (start < end on the clock): awake until the day's end.
    end = _at(day, "24:00") if hours.minutes(sleep["start"]) < hours.minutes(sleep["end"]) else _at(day, sleep["start"])
    return start, end


def _windows(goal: dict, day: dt.date, hours_doc: dict) -> list[tuple[str, dt.datetime, dt.datetime]]:
    profile = hours.ranges_for(day, hours_doc)
    awake_start, awake_end = _awake(day, profile)
    out: list[tuple[str, dt.datetime, dt.datetime]] = []
    tod = (goal.get("time_of_day_window") or "").strip()
    if tod and tod != "Any" and tod in TIME_OF_DAY_WINDOWS:
        a, b = TIME_OF_DAY_WINDOWS[tod].split("-")
        out.append((tod, _at(day, a), _at(day, b)))
    out.append(("free time", _at(day, profile["free"]["start"]), _at(day, profile["free"]["end"])))
    out.append(("awake hours", awake_start, awake_end))
    clipped = []
    for name, a, b in out:
        a, b = max(a, awake_start), min(b, awake_end)
        if a < b:
            clipped.append((name, a, b))
    return clipped


def _busy_for(day: dt.date, hours_doc: dict, busy_by_day: dict[dt.date, list[tuple[dt.datetime, dt.datetime]]]) -> list[tuple[dt.datetime, dt.datetime]]:
    busy = list(busy_by_day.get(day, []))
    work = hours.ranges_for(day, hours_doc).get("work")
    if work:
        busy.append((_at(day, work["start"]), _at(day, work["end"])))
    return busy


def find_slot(goal: dict, day: dt.date, duration: dt.timedelta, hours_doc: dict,
              busy_by_day: dict[dt.date, list[tuple[dt.datetime, dt.datetime]]],
              not_before: dt.datetime | None = None, not_after: dt.datetime | None = None) -> tuple[str, dt.datetime, dt.datetime] | None:
    """(window name, start, end) of the first free slot on `day` (inside [not_before, not_after] when given), or None."""
    busy = _busy_for(day, hours_doc, busy_by_day)
    step = dt.timedelta(minutes=STEP_MINUTES)
    for name, a, b in _windows(goal, day, hours_doc):
        if not_after and not_after < b:
            b = not_after
        t = a if not (not_before and not_before > a) else _ceil(not_before, step, a)
        while t + duration <= b:
            if all(t >= e or t + duration <= s for s, e in busy):
                return name, t, t + duration
            t += step
    return None


def _ceil(moment: dt.datetime, step: dt.timedelta, origin: dt.datetime) -> dt.datetime:
    """`moment` rounded up to the next step counted from `origin`."""
    if moment <= origin:
        return origin
    n = -(-(moment - origin) // step)
    return origin + n * step


# --- the run ------------------------------------------------------------------


def _fmt(value: dt.datetime) -> str:
    return value.isoformat(timespec="minutes")


def expand(window_start: dt.datetime, window_end: dt.datetime, *, now: dt.datetime | None = None,
           dry_run: bool = False, only_goal_ids: set[str] | None = None, count_pending: bool = True,
           run_kind: str = "routine", busy_extra: list[tuple[dt.datetime, dt.datetime]] | None = None) -> dict[str, Any]:
    """
    Propose every routine goal's missing occurrences in [window_start,
    window_end) as pending proposals (one run of kind `run_kind`). Idempotent:
    dates that already hold a block for the goal (approved or rejected, and
    pending ones too when `count_pending`) are left alone. `only_goal_ids`
    limits it to some goals (the hourly trigger places one new goal);
    `busy_extra` adds blocks to plan around (pending proposals the caller
    treats as fixed). Returns a summary.
    """
    now = now or dt.datetime.now(NY)
    hours_doc = hours.load()
    goals = [g for g in db.get_schedulable_items() if g["type"] == "goal" and goal_type(g) == ROUTINE
             and (only_goal_ids is None or g["id"] in only_goal_ids)]
    ws, we = window_start.astimezone(NY), window_end.astimezone(NY)
    first_day, last_day = ws.date(), (we - dt.timedelta(minutes=1)).date()

    busy_by_day: dict[dt.date, list[tuple[dt.datetime, dt.datetime]]] = {}
    for ev in db.get_events_between(ws - dt.timedelta(days=1), we + dt.timedelta(days=PLACEMENT_SLACK_DAYS + 1)):
        if ev["all_day"]:
            continue
        s, e = ev["start_dt"].astimezone(NY), ev["end_dt"].astimezone(NY)
        busy_by_day.setdefault(s.date(), []).append((s, e))
    for s, e in busy_extra or []:
        s, e = s.astimezone(NY), e.astimezone(NY)
        busy_by_day.setdefault(s.date(), []).append((s, e))

    summary: dict[str, Any] = {"goals": len(goals), "placed": 0, "blocks": [], "unparsed": [], "unplaced": [], "dry_run": dry_run}
    proposals: list[dict] = []
    for n, g in enumerate(goals, start=1):
        interval = parse_frequency(g.get("frequency"))
        if interval is None:
            summary["unparsed"].append({"goal": g["goal"], "frequency": g.get("frequency")})
            continue
        taken = db.get_goal_block_dates(g["id"], first_day - dt.timedelta(days=60), last_day + dt.timedelta(days=PLACEMENT_SLACK_DAYS),
                                        include_pending=count_pending)
        duration = dt.timedelta(hours=float(g.get("required_time_hr") or DEFAULT_DURATION_HR))
        duration = max(dt.timedelta(minutes=STEP_MINUTES), dt.timedelta(minutes=STEP_MINUTES * round(duration / dt.timedelta(minutes=STEP_MINUTES))))
        for due in due_dates(g, interval, first_day, last_day, taken):
            placed = None
            for shift in range(PLACEMENT_SLACK_DAYS + 1):
                day = due + dt.timedelta(days=shift)
                if day > last_day or day in taken:
                    continue
                not_before = ws if day == first_day else None
                not_after = we if day == last_day else None
                placed = find_slot(g, day, duration, hours_doc, busy_by_day, not_before, not_after)
                if placed:
                    break
            if not placed:
                summary["unplaced"].append({"goal": g["goal"], "due": due.isoformat()})
                continue
            window_name, start, end = placed
            busy_by_day.setdefault(start.date(), []).append((start, end))
            taken.add(start.date())
            proposals.append({
                "item_type": "goal", "item_id": g["id"], "title": g["goal"], "description": "",
                "location": g.get("location") or "", "start": _fmt(start), "end": _fmt(end), "all_day": False,
                "timezone": DISPLAY_TIMEZONE,
                "rationale": f"Routine ({g.get('frequency')}): due {due.isoformat()}, placed in {window_name}.",
                "ref": f"routine-{n}-{len(proposals) + 1}", "status": "proposed",
            })
            summary["blocks"].append({"goal": g["goal"], "start": _fmt(start), "end": _fmt(end)})
    summary["placed"] = len(proposals)
    if dry_run or not proposals:
        return summary

    started = dt.datetime.now(dt.timezone.utc)
    run_id = f"routine_{started.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    db.create_optimizer_run(run_id, started.isoformat(), "code", "routine", ws.isoformat(), we.isoformat(), kind=run_kind)
    now_iso = started.isoformat()
    ids = db.insert_proposals(run_id, proposals, now_iso)
    db.finish_optimizer_run(run_id, status="succeeded", finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                            proposed_count=len(ids), invalid_count=0,
                            notes=f"{len(ids)} routine blocks proposed for {len(goals)} goals")
    summary.update(run_id=run_id, proposal_ids=ids)
    logger.info("routine %s: %d blocks proposed, %d unplaced, %d unparsed", run_id, len(ids), len(summary["unplaced"]), len(summary["unparsed"]))
    return summary


def refresh_goals() -> str | None:
    """Re-read the Goals database so a Type or frequency changed in Notion counts today. Returns an error text, or None."""
    try:
        import sync_notion  # lazy: needs NOTION_* set

        sync_notion.sync_goals()
        return None
    except Exception as exc:  # noqa: BLE001 -- stale goals are better than no routine at all
        logger.warning("could not refresh goals from Notion before expanding routines: %s", exc)
        return f"{type(exc).__name__}: {exc}"


def expand_now(*, horizon_days: int | None = None, now: dt.datetime | None = None, dry_run: bool = False,
               refresh_notion: bool = True) -> dict[str, Any]:
    """Expand over the planner's window (optimizer.plan_window), after refreshing the goals from Notion."""
    import optimizer  # lazy: optimizer imports this module

    notion_error = refresh_goals() if refresh_notion else None
    window_start, window_end = optimizer.plan_window(now, horizon_days)
    summary = expand(window_start, window_end, now=now, dry_run=dry_run)
    if notion_error:
        summary["notion_error"] = notion_error
    return summary


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Propose the routine goals' occurrences for the planning window (no model call).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--horizon-days", type=int)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db.init_db()
    print(json.dumps(expand_now(horizon_days=args.horizon_days, dry_run=args.dry_run), indent=2))
