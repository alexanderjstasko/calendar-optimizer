"""
review_model.py — pure shaping of proposals for the review tabs and the email.

No database access here: callers pass plain dicts (proposed_events rows, as
db.get_proposals() returns them), so this is unit-testable with literals.

    group_series(proposals)     -> list[Series]   recurring plan items folded into one card each
    summarize_series(occs)      -> cadence/time/duration/count/first/last/summary for one series
    build_summary(...)          -> the GET /review/summary document

Series key: (item_type, item_id) for goal/task/event rows that carry an id,
(item_type, normalized title) for free time and travel. A travel block is not
a series of its own when it can be attached to its destination occurrence:
by `ref` (the travel's item_id names the destination's ref in the same run)
or, for rows stored before `ref` was persisted, by the heuristic "same run,
travel.end == destination.start, title == 'Travel to ' + destination title".
Travel to a fixed calendar event (item_id = an events.id) has no destination
row and stays a series keyed by its title.

Copy examples: "daily 8:15 · 30 min · 24×", "Mon & Wed 17:30 · 3 h · 8× · + travel",
"every 3 days · varies · 15 min · 8×".
"""

from __future__ import annotations

import datetime as dt
import html
import re
import statistics
from collections import Counter
from zoneinfo import ZoneInfo

DISPLAY_TZ = ZoneInfo("America/New_York")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAYS_PLURAL = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")
TIME_SHARE = 0.6          # the modal start time must cover this share of occurrences, else "varies"
IRREGULAR_GAP_CV = 0.5    # day gaps more spread than this (stdev / mean) read as a rate, "4× a week"
OPEN_STATUSES = ("proposed", "promoted")  # what series/items list (the UI filters further)
_INVISIBLE = re.compile(r"[­​‌‍﻿]")  # soft hyphen, zero-width chars


# --- small helpers ------------------------------------------------------------------


def clean_text(value: str | None) -> str:
    """HTML entities decoded (LLM-extracted titles carry '&shy;'), invisible characters dropped."""
    return _INVISIBLE.sub("", html.unescape(value or "")).strip()


def normalize_title(title: str | None) -> str:
    return re.sub(r"\s+", " ", clean_text(title)).lower()


def local_dt(value: str, all_day: int | bool = False) -> dt.datetime:
    """A stored start/end as an aware datetime in the display zone (all-day = local midnight)."""
    if all_day or "T" not in value:
        return dt.datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=DISPLAY_TZ)
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DISPLAY_TZ)
    return parsed.astimezone(DISPLAY_TZ)


def is_past(p: dict, now: dt.datetime) -> bool:
    """Same rule as db.expire_past_proposals: timed rows once their end passed, all-day once the end date is before today."""
    now = now.astimezone(DISPLAY_TZ)
    try:
        if p.get("all_day"):
            return p["end"][:10] < now.date().isoformat()
        return local_dt(p["end"]) <= now
    except (ValueError, TypeError, KeyError):
        return False  # unparseable rows (held-back ones) are never hidden for being "past"


def start_key(p: dict) -> tuple:
    """Sort key by actual start time (stored offsets can differ); unparseable rows last, by text."""
    try:
        return (0, local_dt(p["start"], p.get("all_day")).timestamp(), "")
    except (ValueError, TypeError, KeyError):
        return (1, 0.0, str(p.get("start")))


def series_key(p: dict) -> str:
    item_type = p.get("item_type") or ""
    if item_type in ("goal", "task", "event") and p.get("item_id"):
        return f"{item_type}:{p['item_id']}"
    return f"{item_type}:{normalize_title(p.get('title'))}"


def format_time(hhmm: str) -> str:
    """'08:15' -> '8:15' (copy style); other values pass through."""
    if re.fullmatch(r"\d{2}:\d{2}", hhmm or ""):
        return f"{int(hhmm[:2])}:{hhmm[3:]}"
    return hhmm


def format_duration(minutes: int | None) -> str:
    if minutes is None:
        return ""
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" if rest == 0 else f"{hours} h {rest} min"


def _weekday_names(days: list[int]) -> str:
    names = [WEEKDAYS[d] for d in sorted(days)]
    if len(names) == 1:
        return WEEKDAYS_PLURAL[days[0]]
    return ", ".join(names[:-1]) + " & " + names[-1]


def _weekly_on_each_day(days: list[dt.date], weekdays: set[int]) -> bool:
    """Every weekday recurs (at least twice) and, typically, exactly a week apart -- "Mon & Wed" is then true."""
    gaps: list[int] = []
    for weekday in weekdays:
        same = [d for d in days if d.weekday() == weekday]
        if len(same) < 2:
            return False
        gaps += [(b - a).days for a, b in zip(same, same[1:])]
    return statistics.median_low(gaps) == 7


# --- cadence ------------------------------------------------------------------------------


def cadence(starts: list[dt.datetime]) -> str:
    """
    Human cadence for a set of local start datetimes:
      one day                            -> "once"
      median gap 1, >=6 weekdays         -> "daily"
      median gap 1, Sat/Sun only         -> "weekends"
      median gap 1, Mon-Fri only (>=4)   -> "weekdays"
      Sat & Sun as the only days         -> "weekends"
      <=3 weekdays, each weekly          -> "Mon & Wed" / "Mondays"
      irregular gaps (or several a day)  -> "4× a week" / "2× a month" (rate over the span)
      gap 7 / 14 / 2 / n                 -> "weekly" / "every 2 weeks" / "every other day" / "every n days"
    The median is median_low, so an alternating 2/5 (Mon & Wed) reads as 2.
    """
    days = sorted({s.date() for s in starts})
    if len(days) < 2:
        return "once"
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    gap = statistics.median_low(gaps)
    weekdays = {d.weekday() for d in days}
    several_a_day = len(starts) > len(days)

    if gap <= 1 and not several_a_day:
        if len(weekdays) >= 6:
            return "daily"
        if weekdays <= {5, 6}:
            return "weekends"
        if weekdays <= {0, 1, 2, 3, 4} and len(weekdays) >= 4:
            return "weekdays"
        if len(weekdays) <= 3:
            return _weekday_names(sorted(weekdays))
        return "daily"
    if weekdays == {5, 6} and gap <= 7:
        return "weekends"
    if len(weekdays) <= 3 and 2 <= gap <= 7 and not several_a_day and _weekly_on_each_day(days, weekdays):
        return _weekday_names(sorted(weekdays))
    irregular = len(gaps) >= 3 and statistics.pstdev(gaps) / statistics.mean(gaps) > IRREGULAR_GAP_CV
    if several_a_day or irregular:
        span_days = (days[-1] - days[0]).days + 1
        per_week = round(len(starts) / (span_days / 7))
        if per_week >= 1:
            return "weekly" if per_week == 1 else f"{per_week}× a week"
        per_month = max(1, round(len(starts) / (span_days / 30)))
        return "monthly" if per_month == 1 else f"{per_month}× a month"
    if len(weekdays) == 1 and gap == 14:
        return f"every other {WEEKDAYS[next(iter(weekdays))]}"
    if gap == 7:
        return "weekly"
    if gap == 14:
        return "every 2 weeks"
    if gap == 2:
        return "every other day"
    return f"every {gap} days"


# --- series ---------------------------------------------------------------------------------


def summarize_series(occurrences: list[dict]) -> dict:
    """
    Cadence, modal time (HH:MM, or "varies" / "all day"), modal duration in
    minutes, count, first/last start strings, pending/approved counts and the
    one-line summary for one series' occurrences (each may carry `travel`).
    """
    occs = sorted(occurrences, key=start_key)
    starts: list[dt.datetime] = []
    times: Counter = Counter()
    durations: Counter = Counter()
    for o in occs:
        try:
            start, end = local_dt(o["start"], o.get("all_day")), local_dt(o["end"], o.get("all_day"))
        except (ValueError, TypeError):
            continue
        starts.append(start)
        times["all day" if o.get("all_day") else start.strftime("%H:%M")] += 1
        durations[int((end - start).total_seconds() // 60)] += 1

    count = len(occs)
    time_value, time_n = times.most_common(1)[0] if times else ("varies", 0)
    time_label = time_value if count and time_n / count >= TIME_SHARE else "varies"
    duration = durations.most_common(1)[0][0] if durations else None
    has_travel = any(o.get("travel") for o in occs)
    how_often = cadence(starts)

    if count == 1 and starts:
        when = f"{starts[0]:%a %b} {starts[0].day}"
        head = when if time_label == "all day" else f"{when} {format_time(time_label)}"
        parts = [head, format_duration(duration)]
    else:
        joined = time_label not in ("varies", "all day") and not how_often.startswith("every") and "×" not in how_often
        head = f"{how_often} {format_time(time_label)}" if joined else f"{how_often} · {format_time(time_label)}"
        parts = [head, format_duration(duration), f"{count}×"]
    if has_travel:
        parts.append("+ travel")

    return {
        "count": count,
        "pending": sum(1 for o in occs if o.get("status") == "proposed"),
        "approved": sum(1 for o in occs if o.get("status") == "promoted"),
        "cadence": how_often,
        "time": time_label,
        "duration_min": duration,
        "first": occs[0]["start"] if occs else None,
        "last": occs[-1]["start"] if occs else None,
        "has_travel": has_travel,
        "summary": " · ".join(p for p in parts if p),
    }


def _same_instant(a: str, b: str) -> bool:
    try:
        return local_dt(a) == local_dt(b)
    except (ValueError, TypeError):
        return False


def attach_travel(proposals: list[dict]) -> dict[str, str]:
    """
    {travel proposal id: destination proposal id} for every travel block whose
    destination is among `proposals`: by ref first, then by the legacy
    heuristic (same run, travel ends when the destination starts, title
    "Travel to <destination>"). One travel block per destination.
    """
    by_ref: dict[tuple[str, str], dict] = {}
    destinations = [p for p in proposals if p.get("item_type") != "travel"]
    for d in destinations:
        if d.get("ref"):
            by_ref.setdefault((d["run_id"], d["ref"]), d)
    links: dict[str, str] = {}
    taken: set[str] = set()
    for t in sorted((p for p in proposals if p.get("item_type") == "travel"), key=start_key):
        dest = by_ref.get((t.get("run_id"), t.get("item_id") or ""))
        if dest is None or dest["id"] in taken:
            dest = None
            want = normalize_title(t.get("title"))
            for d in destinations:
                if (d["id"] not in taken and d.get("run_id") == t.get("run_id")
                        and want == normalize_title(f"Travel to {d.get('title') or ''}")
                        and _same_instant(t["end"], d["start"])):
                    dest = d
                    break
        if dest is not None:
            links[t["id"]] = dest["id"]
            taken.add(dest["id"])
    return links


def group_series(proposals: list[dict]) -> list[dict]:
    """
    Fold proposals into series (see the module docstring). Each Series:
    {key, item_type, item_id, title, count, pending, approved, cadence, time,
    duration_min, first, last, summary, has_travel, occurrences}, where each
    occurrence is the proposal dict plus `travel` (the attached travel
    proposal or None). Recurring series come first (most occurrences first),
    then single items by start. Input dicts are not modified.
    """
    by_id = {p["id"]: p for p in proposals}
    links = attach_travel(proposals)
    travel_for = {dest: by_id[travel] for travel, dest in links.items()}
    groups: dict[str, list[dict]] = {}
    for p in sorted(proposals, key=start_key):
        if p["id"] in links:
            continue  # shown with its destination
        groups.setdefault(series_key(p), []).append({**p, "travel": travel_for.get(p["id"])})

    series: list[dict] = []
    for key, occs in groups.items():
        titles = Counter(clean_text(o.get("title")) for o in occs)
        series.append({
            "key": key,
            "item_type": occs[0]["item_type"],
            "item_id": occs[0].get("item_id") or "",
            "title": titles.most_common(1)[0][0],
            **summarize_series(occs),
            "occurrences": occs,
        })
    series.sort(key=lambda s: (s["count"] == 1, -s["count"] if s["count"] > 1 else 0, start_key(s["occurrences"][0])))
    return series


# --- GET /review/summary ------------------------------------------------------------------


RUN_SUMMARY_FIELDS = ("run_id", "kind", "status", "started_at", "finished_at", "proposed_count", "invalid_count", "notes")


def run_summary(run: dict | None) -> dict | None:
    return {k: run.get(k) for k in RUN_SUMMARY_FIELDS} if run else None


def build_summary(rows: list[dict], held_back: list[dict], runs: dict[str, dict | None], now: dt.datetime,
                  *, page_size: int) -> dict:
    """
    The review document both tabs (and the badges) read. `rows` = open
    proposals (proposed / queued / promoted); past ones are dropped here.
    events.queued counts the options waiting unseen in the reload queue
    ("6 more ready"); events.reload is what POST /scout/reload would do now:
    "queue" (instant) while any are waiting, else "model" (~2 min).
    """
    upcoming = [r for r in rows if not is_past(r, now)]
    events = [r for r in upcoming if r["item_type"] == "event"]
    plan = [r for r in upcoming if r["item_type"] != "event"]
    series = group_series([r for r in plan if r["status"] in OPEN_STATUSES])
    queued = sum(1 for r in events if r["status"] == "queued" and not r.get("shown_at"))
    return {
        "generated_at": now.astimezone(DISPLAY_TZ).isoformat(timespec="seconds"),
        "events": {
            "pending": sum(1 for r in events if r["status"] == "proposed"),
            "queued": queued,
            "approved_upcoming": sum(1 for r in events if r["status"] == "promoted"),
            "page_size": page_size,
            "reload": "queue" if queued else "model",
            "items": [r for r in events if r["status"] in OPEN_STATUSES],
        },
        "plan": {
            "pending": sum(1 for r in plan if r["status"] == "proposed"),
            "approved_upcoming": sum(1 for r in plan if r["status"] == "promoted"),
            "pending_series": sum(1 for s in series if s["pending"]),
            "series": series,
            "held_back": [r for r in held_back if not is_past(r, now)],
        },
        "runs": {kind: run_summary(run) for kind, run in runs.items()},
    }
