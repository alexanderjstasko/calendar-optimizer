"""
optimizer.py — the runner that turns Notion tasks/goals plus the existing
calendar into proposed events via Claude.

Flow (see CLAUDE.md, "Design decision"):
  1. sync_notion() refreshes the tasks/goals tables (skippable with
     refresh_notion=False; a Notion failure logs a warning and the run
     continues on the rows already in events.db).
  2. build_payload() assembles one structured JSON document: planning window,
     preferences, every calendar event in the window (fixed commitments plus
     anything the optimizer already placed), open tasks and goals.
  3. llm_client.complete_structured() sends it with the events schema. On the
     api backend the model answers by calling the submit_schedule tool; on the
     cli backend the same schema goes through --json-schema.
  4. validate_proposals() checks ids, window, ordering, overlaps and travel
     references. Bad rows are kept with status 'invalid' so they can be seen.
  5. Everything lands in proposed_events under a new run id, earlier
     undecided proposals are superseded, and the run is recorded.

Nothing reaches `events` until a proposal is promoted (db.promote_proposal,
POST /optimizer/proposals/{id}/approve). Existing calendar events are never
moved or changed; the optimizer only adds.

    python3 optimizer.py --dry-run                     # payload + token count, no call
    python3 optimizer.py --dry-run --horizon-days 365
    python3 optimizer.py                               # live run: the rest of this week (next week on a Sunday)
    python3 optimizer.py --backend api --horizon-days 7
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import db
import hours
import llm_client
import preferences
import routine
from optimizer_schema import (
    DISPLAY_TIMEZONE,
    OPTIMIZER_ITEM_TYPES,
    PROPOSED_EVENTS_SCHEMA,
    SUBMIT_TOOL_DESCRIPTION,
    SUBMIT_TOOL_NAME,
)

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

logger = logging.getLogger(__name__)


def _setting(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()

DEFAULT_MAX_OUTPUT_TOKENS = 16000
DEFAULT_TRAVEL_MINUTES = 30
NY = ZoneInfo(DISPLAY_TIMEZONE)

# The weekly Calendar Review: a built-in goal the planner places on the
# Sunday of every window (the sit-down with the approval email). Its length
# comes from the Hours tab; it is proposed and approved like any other block.
CALENDAR_REVIEW_ID = "builtin_calendar_review"
CALENDAR_REVIEW_TITLE = "Calendar Review"
# The monthly Goals and Telos review (2026-10-26): one block every
# `goals_review_every_months` from the Hours tab, `goals_review_minutes` long,
# placed by the planner like the Calendar Review. Nothing in Notion.
GOALS_REVIEW_ID = "builtin_goals_review"
GOALS_REVIEW_TITLE = "Goals and Telos review"
BUILTIN_IDS = (CALENDAR_REVIEW_ID, GOALS_REVIEW_ID)

# Personal scheduling preferences the model plans around. Sleep, work and free
# hours and the free-time floor are NOT here: they come from hours.py (the
# app's Hours tab) and are added to the payload under preferences. Time-of-day
# names mirror the Notion "Time of Day" select on the goals database.
PREFERENCES: dict[str, Any] = {
    "time_of_day_windows": routine.TIME_OF_DAY_WINDOWS,
    "task_block_hours": {"min": .5, "max": 4},
    "max_hours_per_week_for_tasks_without_deadline": 15,
}

# The system prompt lives in <project root>/system_prompt.md so it can be edited
# without touching code. It is re-read on every run. Override the path with
# LLM_SYSTEM_PROMPT_PATH in .env.
DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "system_prompt.md"
_TOOL_INSTRUCTION = f"Hand the plan back by calling the `{SUBMIT_TOOL_NAME}` tool exactly once."
_HTML_COMMENT = re.compile(r"<!--.*?-->\s*", re.DOTALL)


def system_prompt_path() -> Path:
    override = _setting("LLM_SYSTEM_PROMPT_PATH", "")
    return Path(override).expanduser() if override else DEFAULT_SYSTEM_PROMPT_PATH


def load_prompt_file(path: Path, tool_name: str, setting_name: str) -> str:
    """
    Read a prompt file, drop HTML comments (editing notes at the top), and
    make sure the model is still told to answer through `tool_name` -- the one
    line the caller cannot work without.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise llm_client.LLMConfigError(
            f"Prompt file not found at {path}. Create it or set {setting_name} in .env."
        ) from e
    text = _HTML_COMMENT.sub("", raw).strip()
    if not text:
        raise llm_client.LLMConfigError(f"Prompt file {path} is empty")
    if tool_name not in text:
        logger.warning("prompt %s does not mention the %s tool; appending the instruction", path.name, tool_name)
        text = f"{text}\n\nHand the result back by calling the `{tool_name}` tool exactly once."
    return text + "\n"


def load_system_prompt(path: Path | None = None) -> str:
    """The optimizer's system prompt (system_prompt.md), see load_prompt_file()."""
    return load_prompt_file(path or system_prompt_path(), SUBMIT_TOOL_NAME, "LLM_SYSTEM_PROMPT_PATH")


# --- settings -----------------------------------------------------------------


def _int_setting(name: str, default: int) -> int:
    raw = _setting(name, str(default))
    try:
        return int(raw)
    except ValueError as e:
        raise llm_client.LLMConfigError(f"{name}={raw!r} is not an integer") from e


# --- payload -------------------------------------------------------------------


def _fmt(value: dt.datetime, all_day: bool) -> str:
    return value.strftime("%Y-%m-%d") if all_day else value.isoformat()


def _calendar_entry(event: dict) -> dict:
    all_day = bool(event["all_day"])
    entry = {
        "id": event["id"],
        "title": (event.get("title") or "").strip()[:120],
        "start": _fmt(event["start_dt"], all_day),
        "end": _fmt(event["end_dt"], all_day),
        "all_day": all_day,
        "location": (event.get("location") or "").strip()[:160],
        "source": event["source"],
    }
    if event["source"] == db.OPTIMIZER_SOURCE:
        try:
            raw = json.loads(event.get("raw_json") or "{}")
        except json.JSONDecodeError:
            raw = {}
        entry["placed_by_optimizer_for"] = {"item_type": raw.get("item_type"), "item_id": raw.get("item_id")}
    return entry


def _json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return loaded if isinstance(loaded, list) else []


def _hours_remaining(total: float | None, spent: float | None) -> float | None:
    """Work left on a task: time_to_complete_hr minus the hours Notion says were spent."""
    if total is None:
        return None
    return round(max(0.0, float(total) - float(spent or 0.0)), 2)


def _tree_fields(item: dict) -> dict:
    """Where an item sits in the Telos tree (db.get_schedulable_items attaches these)."""
    return {
        "telos": item.get("telos") or "",
        "path": list(item.get("path") or []),
        "ancestor_notes": list(item.get("ancestor_notes") or []),
    }


def goal_entry(g: dict) -> dict:
    """A goals row as the models see it."""
    return {
        "id": g["id"],
        "title": g["goal"],
        "type": routine.goal_type(g) or "",
        "frequency": g["frequency"],
        "times_per_week": g.get("times_per_week"),
        "required_time_hr": g["required_time_hr"],
        "days_of_week": _json_list(g.get("days_of_week")),
        "time_of_day": g["time_of_day_window"] or "Any",
        "end_date": g["end_date"],
        "priority": g["priority"],
        "location": g["location"] or "",
        "status": g["status"],
        "notes_for_llm": (g.get("notes_for_llm") or "").strip(),
        "last_occurred": (g.get("last_occurred") or None) and str(g["last_occurred"])[:10],
        "sessions": g.get("sessions"),
        # "Routine", or "Goal"/"Project" when the node itself is planned as sessions
        # (notion_adapter.node_plan_kind); then Time to Complete, if set, caps the total.
        "level": g.get("level") or "Routine",
        **({"time_to_complete_hr": g["time_to_complete_hr"], "time_spent_hr": g.get("time_spent_hr"),
            "hours_remaining": _hours_remaining(g["time_to_complete_hr"], g.get("time_spent_hr"))}
           if g.get("time_to_complete_hr") is not None else {}),
        **_tree_fields(g),
    }


def _item_summary(i: dict) -> dict:
    if i["kind"] == "goal":
        return {"kind": "goal", "title": i["title"], "frequency": i.get("frequency") or "",
                "session_hr": i.get("required_time_hr"),
                **({"hours_remaining": _hours_remaining(i["time_to_complete_hr"], i.get("time_spent_hr"))}
                   if i.get("time_to_complete_hr") is not None else {})}
    return {"kind": "task", "title": i["title"], "deadline": i.get("deadline"),
            "hours_remaining": _hours_remaining(i.get("time_to_complete_hr"), i.get("time_spent_hr"))}


def _tree_entry(node: dict) -> dict:
    """One node of the Telos tree for the payload: facts only, nested."""
    entry = {
        "title": node.get("title"), "level": node.get("level"), "status": node.get("status") or "",
        "priority": node.get("priority") or "", "target_date": node.get("target_date"),
        "notes_for_llm": (node.get("notes_for_llm") or "").strip(),
        "items": [_item_summary(i) for i in node.get("items") or []],
        "children": [_tree_entry(c) for c in node.get("children") or []],
    }
    if node.get("planned"):
        # The goal/project is itself in `goals` (sessions) or `tasks` (one piece of work) under its own id.
        entry["planned_as"] = {k: v for k, v in _item_summary(node["planned"]).items() if k != "title"}
    return entry


def tree_payload() -> list[dict]:
    """The Telos tree (Telos -> goals/projects -> their routines and tasks) as the model sees it."""
    return [_tree_entry(n) for n in db.get_tree()]


def event_interest_goals() -> list[dict]:
    """
    The goals the scout serves: Type "Event Interest", plus untyped goals that
    show on the calendar (the behaviour before Type existed). Each carries
    already_scheduled_event_ids like a planner goal.
    """
    out = []
    for g in db.get_schedulable_items():
        if g["type"] != "goal" or routine.goal_type(g) not in (routine.EVENT, None):
            continue
        if g.get("show_on_calendar") is not None and not g["show_on_calendar"]:
            continue
        entry = goal_entry(g)
        entry["already_scheduled_event_ids"] = _json_list(g.get("scheduled_event_ids"))
        out.append(entry)
    return out


def calendar_review_goal(window_start: dt.datetime, window_end: dt.datetime, hours_doc: dict | None = None) -> dict | None:
    """
    The built-in weekly Calendar Review as a planner goal, or None when the
    window holds no Sunday. Blocks already approved for it in the window are
    listed as already_scheduled_event_ids so the model counts them.
    """
    days = {(window_start + dt.timedelta(days=i)).astimezone(NY).weekday() for i in range((window_end - window_start).days + 2)
            if window_start + dt.timedelta(days=i) < window_end}
    if 6 not in days:
        return None
    minutes = hours.calendar_review_minutes(hours_doc)
    already = []
    for p in db.get_proposals_for_item("goal", CALENDAR_REVIEW_ID, ("promoted",)):
        try:
            start = db.parse_event_time(p["start"], p["all_day"])
        except (ValueError, TypeError):
            continue
        if window_start <= start < window_end and p.get("promoted_event_id"):
            already.append(p["promoted_event_id"])
    return {
        "id": CALENDAR_REVIEW_ID, "title": CALENDAR_REVIEW_TITLE, "type": "flexible", "frequency": "1x Week",
        "required_time_hr": round(minutes / 60, 2), "days_of_week": ["Sunday"], "time_of_day": "Any", "end_date": None,
        "priority": "High", "location": "", "status": "In progress",
        "notes_for_llm": ("The weekly sit-down to go through the approval email and approve next week's plan. "
                          "One block on the Sunday of the window, in free time, at a calm hour."),
        "last_occurred": None, "already_scheduled_event_ids": already, "telos": "", "path": [], "ancestor_notes": [],
    }


def _latest_block_date(item_id: str, statuses: tuple[str, ...]) -> dt.date | None:
    latest: dt.date | None = None
    for p in db.get_proposals_for_item("goal", item_id, statuses):
        try:
            day = db.parse_event_time(p["start"], p["all_day"]).date()
        except (ValueError, TypeError):
            continue
        if latest is None or day > latest:
            latest = day
    return latest


def goals_review_goal(window_start: dt.datetime, window_end: dt.datetime, hours_doc: dict | None = None,
                      now: dt.datetime | None = None) -> dict | None:
    """
    The monthly Goals and Telos review as a planner goal, or None when one is
    not due in this window: a block (approved or still pending) placed within
    the last `goals_review_every_months` months of the window's end means it
    is covered. The length comes from the Hours tab.
    """
    months = hours.goals_review_every_months(hours_doc)
    minutes = hours.goals_review_minutes(hours_doc)
    last = _latest_block_date(GOALS_REVIEW_ID, ("promoted", "proposed", "retired"))
    due_after = (window_end.astimezone(NY).date() - dt.timedelta(days=int(round(months * routine.MONTH_DAYS)) - 3))
    if last is not None and last > due_after:
        return None
    return {
        "id": GOALS_REVIEW_ID, "title": GOALS_REVIEW_TITLE, "type": "flexible",
        "frequency": "1x Month" if months == 1 else f"Once every {int(round(months * routine.MONTH_DAYS))} days",
        "required_time_hr": round(minutes / 60, 2), "days_of_week": [], "time_of_day": "Any", "end_date": None,
        "priority": "Medium", "location": "", "status": "In progress",
        "notes_for_llm": (f"A sit-down every {months} month(s) to look over the Telos tree: which goals moved, which "
                          "routines actually happened (sessions), what to drop or add. One block in this window, in free "
                          "time at a calm hour, at the weekend if possible."),
        "last_occurred": last.isoformat() if last else None, "already_scheduled_event_ids": [],
        "telos": "", "path": [], "ancestor_notes": [],
    }


def _pending_entry(p: dict) -> dict:
    """A pending routine block as a calendar entry the model must plan around."""
    return {
        "id": f"proposal:{p['id']}", "title": (p.get("title") or "")[:120], "start": p["start"], "end": p["end"],
        "all_day": bool(p.get("all_day")), "location": (p.get("location") or "")[:160],
        "source": "routine (proposed in this run; treat as fixed)",
        "placed_by_optimizer_for": {"item_type": p["item_type"], "item_id": p.get("item_id")},
    }


def pending_as_fixed(pending: list[dict]) -> list[dict]:
    """Pending proposals shaped like get_events_between() rows, for overlap validation."""
    out = []
    for p in pending:
        try:
            start = db.parse_event_time(p["start"], p.get("all_day"))
            end = db.parse_event_time(p["end"], p.get("all_day"))
        except (ValueError, TypeError):
            continue
        out.append({"id": f"proposal:{p['id']}", "title": p.get("title"), "all_day": bool(p.get("all_day")),
                    "start_dt": start, "end_dt": end, "source": "proposal"})
    return out


def build_payload(window_start: dt.datetime, window_end: dt.datetime,
                  pending_fixed: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """
    The structured input for one run, plus the raw window events (with parsed
    datetimes) that validate_proposals() uses for overlap checks.
    `pending_fixed`: proposals (the routine blocks of this pass) the model
    must plan around as if they were on the calendar.
    """
    events = db.get_events_between(window_start, window_end)
    items = db.get_schedulable_items()

    tasks = [
        {
            "id": t["id"],
            "title": t["task"],
            "time_to_complete_hr": t["time_to_complete_hr"],
            "time_spent_hr": t.get("time_spent_hr"),
            "hours_remaining": _hours_remaining(t["time_to_complete_hr"], t.get("time_spent_hr")),
            "deadline": t["deadline"],
            "priority": t["priority"],
            "location": t["location"] or "",
            "status": t["status"],
            "already_scheduled_event_ids": _json_list(t.get("scheduled_event_ids")),
            **_tree_fields(t),
        }
        for t in items
        if t["type"] == "task"
    ]
    goals: list[dict] = []
    context_goals: list[dict] = []
    for g in items:
        if g["type"] != "goal":
            continue
        # Routine goals are already on the calendar (routine.expand) and event
        # interests belong to the scout: the planner never sees either.
        if routine.goal_type(g) in (routine.ROUTINE, routine.EVENT):
            continue
        entry = goal_entry(g)
        # NULL (row synced before the column existed) counts as shown.
        if g.get("show_on_calendar") is None or g["show_on_calendar"]:
            entry["already_scheduled_event_ids"] = _json_list(g.get("scheduled_event_ids"))
            goals.append(entry)
        else:
            context_goals.append(entry)

    saved_hours = hours.load()
    review = calendar_review_goal(window_start, window_end, saved_hours)
    if review:
        goals.append(review)
    goals_review = goals_review_goal(window_start, window_end, saved_hours)
    if goals_review:
        goals.append(goals_review)
    pending_fixed = pending_fixed or []
    payload = {
        "generated_at": dt.datetime.now(NY).isoformat(timespec="minutes"),
        "timezone": DISPLAY_TIMEZONE,
        "planning_window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "preferences": {
            **PREFERENCES,
            "hours": hours.for_model(saved_hours),
            # The Hours tab's home address; LLM_HOME_LOCATION in .env only as a fallback.
            "home_location": hours.home_location(saved_hours, _setting("LLM_HOME_LOCATION", "Home (address not provided)")),
            "default_travel_minutes": _int_setting("LLM_DEFAULT_TRAVEL_MIN", DEFAULT_TRAVEL_MINUTES),
            # The Hours tab's floor: hours of free time the plan must leave unbooked each week.
            "free_time_hours_per_week": hours.free_hours_per_week(saved_hours),
        },
        "calendar": [_calendar_entry(e) for e in events] + [_pending_entry(p) for p in pending_fixed],
        "tasks": tasks,
        "goals": goals,
        "context_goals": context_goals,
        # The Telos tree: what every task and routine is for (facts, never events).
        "tree": tree_payload(),
        # What the person has told us and what their decisions have taught us.
        "person": preferences.person_context(),
    }
    return payload, events + pending_as_fixed(pending_fixed)


# --- validation ------------------------------------------------------------------


def validate_proposals(
    events: list[dict],
    payload: dict,
    window_start: dt.datetime,
    window_end: dt.datetime,
    fixed_events: list[dict],
    movable_ids: set[str] | None = None,
) -> list[dict]:
    """
    Annotate each proposed event with status 'proposed' or 'invalid' (+
    invalid_reason). Nothing is dropped, so bad output stays inspectable.
    `movable_ids`: approved blocks a re-evaluation (triggers.py) may move; a
    proposal that names one in replaces_event_id takes its place, so that
    block no longer counts as fixed. A plan run passes none: moves are invalid.
    """
    known_items = {t["id"] for t in payload["tasks"]} | {g["id"] for g in payload["goals"]}
    context_ids = {g["id"] for g in payload.get("context_goals", [])}
    known_events = {e["id"] for e in payload["calendar"]}
    refs = [e.get("ref") for e in events if e.get("ref")]  # an empty ref names nothing
    duplicate_refs = {r for r in refs if refs.count(r) > 1}
    replaced = {e.get("replaces_event_id") for e in events if e.get("replaces_event_id") and movable_ids and e["replaces_event_id"] in movable_ids}

    checked: list[dict] = []
    for e in events:
        reason: str | None = None
        start = end = None
        try:
            start = db.parse_event_time(e["start"], e["all_day"])
            end = db.parse_event_time(e["end"], e["all_day"])
            if e["all_day"] and end <= start:
                end = start + dt.timedelta(days=1)
        except (ValueError, TypeError):
            reason = "unparseable start/end"

        if reason is None:
            if end <= start:
                reason = "end is not after start"
            elif start < window_start or start >= window_end:
                reason = "outside the planning window"
            elif e["timezone"] != DISPLAY_TIMEZONE:
                reason = f"timezone must be {DISPLAY_TIMEZONE}"
            elif e.get("ref") in duplicate_refs:
                reason = f"duplicate ref {e.get('ref')!r}"
            elif e.get("replaces_event_id") and not (movable_ids and e["replaces_event_id"] in movable_ids):
                reason = (f"replaces {e['replaces_event_id']!r}, which is not a block that may be moved"
                          if movable_ids else "approved blocks are never moved by a plan run (only by a re-evaluation)")
            elif e["item_type"] == "event":
                reason = "public events are proposed by the scout, not the optimizer"
            elif e["item_type"] == "free":
                reason = "free time is derived from the calendar (every unbooked hour), not planned as blocks"
            elif e["item_type"] == "goal" and e["item_id"] in context_ids:
                reason = "goal is context-only (Show on Calendar unchecked)"
            elif e["item_type"] in ("task", "goal") and e["item_id"] not in known_items:
                reason = f"unknown {e['item_type']} id {e['item_id']!r}"
            elif e["item_type"] == "travel" and e["item_id"] not in known_events and e["item_id"] not in refs:
                reason = f"travel destination {e['item_id']!r} matches no calendar id or ref"

        row = {**e, "status": "invalid" if reason else "proposed", "invalid_reason": reason, "_start": start, "_end": end}
        if reason is None and not e["all_day"]:
            # Stored as written, the model's offset can disagree with the New York
            # label (e.g. -05:00 on a summer-time day) and the calendar cannot draw
            # it; the same instant with New York's own offset always can.
            row["start"] = start.astimezone(NY).isoformat()
            row["end"] = end.astimezone(NY).isoformat()
        checked.append(row)

    fixed_timed = [(ev["start_dt"], ev["end_dt"], ev.get("title") or ev["id"]) for ev in fixed_events
                   if not ev["all_day"] and ev.get("id") not in replaced]
    for c in checked:
        if c["status"] != "proposed" or c["all_day"]:
            continue
        for f_start, f_end, f_title in fixed_timed:
            if c["_start"] < f_end and c["_end"] > f_start:
                c["status"], c["invalid_reason"] = "invalid", f"overlaps existing event {f_title!r}"
                break

    last_end = None
    last_ref = None
    for c in sorted(checked, key=lambda x: (x["_start"] is None, x["_start"] or window_start)):
        if c["status"] != "proposed" or c["all_day"]:
            continue
        if last_end is not None and c["_start"] < last_end:
            c["status"], c["invalid_reason"] = "invalid", f"overlaps proposal {last_ref!r}"
            continue
        last_end, last_ref = c["_end"], c.get("ref")

    # Travel to a proposal that was held back has nowhere to go.
    status_by_ref = {c.get("ref"): c["status"] for c in checked if c.get("ref")}
    for c in checked:
        if c["status"] == "proposed" and c["item_type"] == "travel" and status_by_ref.get(c["item_id"], "proposed") != "proposed":
            c["status"], c["invalid_reason"] = "invalid", f"travel destination {c['item_id']!r} was held back"

    for c in checked:
        c.pop("_start", None)
        c.pop("_end", None)
    return checked


# --- run -----------------------------------------------------------------------


def _refresh_notion() -> str | None:
    """Pull the latest tasks/goals; return a note when it failed (run continues)."""
    try:
        import sync_notion  # imported lazily: it needs NOTION_* in the environment

        sync_notion.sync_notion()
        return None
    except Exception as exc:  # network, auth, schema drift: none should abort planning
        logger.warning("Notion refresh failed, planning with the rows already in events.db: %s", exc)
        return f"Notion refresh failed: {type(exc).__name__}: {exc}"


def plan_window(now: dt.datetime | None = None, horizon_days: int | None = None) -> tuple[dt.datetime, dt.datetime]:
    """
    The week the plan covers (since 2026-10-26): on a Sunday, the coming
    Monday 00:00 to the Monday after (the Sunday run plans next week and the
    current Sunday stays as it is); on any other day, from the next full hour
    to the coming Monday 00:00 (Re-plan covers the rest of this week). An
    explicit `horizon_days` (CLI/API) keeps the old rolling window instead.
    """
    now = (now or dt.datetime.now(NY)).astimezone(NY)
    start = (now + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    if horizon_days:
        return start, start + dt.timedelta(days=horizon_days)
    next_monday = dt.datetime.combine(now.date() + dt.timedelta(days=7 - now.weekday()), dt.time(0, 0), tzinfo=NY)
    if now.weekday() == 6:
        return next_monday, next_monday + dt.timedelta(days=7)
    return min(start, next_monday), next_monday


class RunInProgress(RuntimeError):
    """Another plan run is still going (the Sunday job, Re-plan on another device)."""


# Two plan runs at once would supersede each other's routine blocks and leave a
# plan built around blocks that are gone.
_RUN_LOCK = threading.Lock()


def run_optimizer(**kwargs) -> dict[str, Any]:
    """One optimizer pass at a time (a dry run needs no lock); see _run_optimizer."""
    if kwargs.get("dry_run"):
        return _run_optimizer(**kwargs)
    if not _RUN_LOCK.acquire(blocking=False):
        raise RunInProgress("a plan run is already in progress")
    try:
        return _run_optimizer(**kwargs)
    finally:
        _RUN_LOCK.release()


def _run_optimizer(
    *,
    horizon_days: int | None = None,
    dry_run: bool = False,
    refresh_notion: bool = True,
    backend: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """
    One optimizer pass. Returns a summary dict; with dry_run=True it stops
    after building the payload and estimating tokens (nothing is recorded).
    Raises llm_client.LLMError when the model call fails (the run row is
    marked failed first).
    """
    if backend:
        os.environ["LLM_BACKEND"] = backend
    db.init_db()

    window_start, window_end = plan_window(now, horizon_days)
    notion_note = _refresh_notion() if refresh_notion else "Notion refresh skipped"
    # Digest any new decisions into the learned-preferences note first (one
    # small model call only when enough new decisions exist; never fatal).
    learning = preferences.refresh_learned_safely() if not dry_run else {"refreshed": False, "skipped": "dry run"}
    # Routine goals first, in code, as pending proposals of their own run:
    # the model plans around them as if they were fixed, and the person
    # approves them with the rest. A failure here must not stop the plan.
    # Earlier pending routine blocks do not count as taken (they are
    # superseded with the rest of the old plan once this run succeeds).
    try:
        routine_summary = routine.expand(window_start, window_end, now=now, dry_run=dry_run, count_pending=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception("routine expansion failed; planning without it")
        routine_summary = {"error": f"{type(exc).__name__}: {exc}"}
    routine_run_id = routine_summary.get("run_id")
    pending_fixed = db.get_proposals(run_id=routine_run_id, status="proposed") if routine_run_id else []
    payload, fixed_events = build_payload(window_start, window_end, pending_fixed=pending_fixed)
    user = json.dumps(payload, separators=(",", ":"))
    system = load_system_prompt()
    model = llm_client._env("LLM_MODEL", llm_client.DEFAULT_MODEL) or llm_client.DEFAULT_MODEL
    backend_name = llm_client.get_backend()

    summary: dict[str, Any] = {
        "backend": backend_name,
        "model": model,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "horizon_days": (window_end - window_start).days,
        "calendar_events": len(payload["calendar"]),
        "tasks": len(payload["tasks"]),
        "goals": len(payload["goals"]),
        "context_goals": len(payload["context_goals"]),
        "notion": notion_note or "refreshed",
        "learning": learning,
        "routine": routine_summary,
        "system_prompt_path": str(system_prompt_path()),
    }

    if dry_run:
        tokens, method = llm_client.estimate_tokens(
            system, user, PROPOSED_EVENTS_SCHEMA, model=model,
            tool_name=SUBMIT_TOOL_NAME, tool_description=SUBMIT_TOOL_DESCRIPTION,
        )
        summary.update(dry_run=True, input_tokens_estimate=tokens, estimate_method=method, payload_chars=len(user))
        return summary

    started = dt.datetime.now(dt.timezone.utc)
    run_id = f"run_{started.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    db.create_optimizer_run(
        run_id, started.isoformat(), backend_name, model, summary["window_start"], summary["window_end"],
        system_prompt=system, payload_json=user,
    )
    clock = time.monotonic()
    try:
        result = llm_client.complete_structured(
            system, user, PROPOSED_EVENTS_SCHEMA,
            max_tokens=_int_setting("LLM_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS),
            tool_name=SUBMIT_TOOL_NAME, tool_description=SUBMIT_TOOL_DESCRIPTION,
        )
    except Exception as exc:  # the model call or anything unexpected: never leave the run 'running'
        failed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        db.finish_optimizer_run(run_id, status="failed", finished_at=failed_at, error=f"{type(exc).__name__}: {exc}")
        # Roll the routine part back too, so the old plan stays whole until a run succeeds.
        for p in pending_fixed:
            db.set_proposal_status(p["id"], "superseded", failed_at)
        raise
    elapsed = time.monotonic() - clock

    proposals = validate_proposals(result.data["events"], payload, window_start, window_end, fixed_events)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    # Only what was undecided when this run started: a new Notion item placed
    # by the hourly check during the ~6-minute model call stays.
    superseded = db.supersede_open_proposals(now_iso, OPTIMIZER_ITEM_TYPES,
                                             exclude_run_ids=(routine_run_id,) if routine_run_id else (),
                                             created_before=started.isoformat())
    db.insert_proposals(run_id, proposals, now_iso)

    usage = result.usage or {}
    input_tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    proposed = sum(1 for p in proposals if p["status"] == "proposed")
    invalid = len(proposals) - proposed
    db.finish_optimizer_run(
        run_id, status="succeeded", finished_at=now_iso,
        input_tokens=input_tokens or None, output_tokens=usage.get("output_tokens"),
        cost_usd=result.cost_usd, proposed_count=proposed, invalid_count=invalid,
        notes=result.data.get("notes"),
    )
    summary.update(
        run_id=run_id, model=result.model, proposed=proposed, invalid=invalid,
        invalid_reasons=[p["invalid_reason"] for p in proposals if p["status"] == "invalid"],
        unscheduled=result.data.get("unscheduled", []), notes=result.data.get("notes", ""),
        input_tokens=input_tokens, output_tokens=usage.get("output_tokens"),
        cost_usd=result.cost_usd, elapsed_s=round(elapsed, 1), superseded=superseded,
    )
    logger.info("optimizer %s: %d proposed, %d invalid, %d superseded, %.1fs", run_id, proposed, invalid, superseded, elapsed)
    return summary


# --- CLI ------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--horizon-days", type=int, help="a rolling window of N days instead of the rest of the week / next week")
    parser.add_argument("--dry-run", action="store_true", help="build the payload and count tokens; no model call")
    parser.add_argument("--no-notion", action="store_true", help="skip the Notion refresh")
    parser.add_argument("--backend", choices=llm_client.VALID_BACKENDS, help="override LLM_BACKEND for this run")
    parser.add_argument("--show-payload", action="store_true", help="also print the JSON sent to the model")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.show_payload:
        start, end = plan_window(horizon_days=args.horizon_days)
        payload, _ = build_payload(start, end)
        print(json.dumps(payload, indent=1))

    try:
        summary = run_optimizer(
            horizon_days=args.horizon_days, dry_run=args.dry_run,
            refresh_notion=not args.no_notion, backend=args.backend,
        )
    except llm_client.LLMError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, indent=2))
        return 1
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
