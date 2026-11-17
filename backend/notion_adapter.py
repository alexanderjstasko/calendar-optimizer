"""
notion_adapter.py — Fetches the one Notion database (the Telos tree, since
2026-10-26) and normalizes each page by its `Level`:

  * Telos / Goal / Project  -> `tree_nodes` rows (the ancestry of every action)
  * Routine                 -> `goals` rows (the planner's and the scout's goals)
  * Task                    -> `tasks` rows

Before the restructure the goals and tasks lived in two databases; the row
shapes below are unchanged, so everything downstream (optimizer, routine
expander, triggers, time tracker) keeps working. Property names are looked
up with fallbacks (`Time Spent` or `Time Spent (hr)`, `Target Date` or
`Deadline`, ...) so the switch never depends on a rename landing first.

Follows the same fetch -> normalize -> upsert shape as the other
calendar_optimizer adapters (google/outlook/apple/partiful). Unlike the
calendar sources, there's no windowed-diff logic here — tasks/goals aren't
recurring occurrences, so syncing is a plain full-fetch-and-diff each poll:
fetch everything non-archived, upsert by id, and anything previously seen
but absent from the latest fetch gets soft-deleted.

Run standalone first to sanity-check the property mapping against your real
Notion data before wiring sync_tasks()/sync_goals() into db.py's
upsert_task()/upsert_goal() and the APScheduler job in main.py:

    python3 notion_adapter.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# .env lives one directory above backend/, per project convention.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
# The one database (NOTION_TREE_DB_ID wins; NOTION_GOALS_DB_ID is the name the
# Goals database had before the restructure and still points at the same id).
NOTION_GOALS_DB_ID = os.environ.get("NOTION_TREE_DB_ID") or os.environ["NOTION_GOALS_DB_ID"]
NOTION_TREE_DB_ID = NOTION_GOALS_DB_ID
# The old Tasks database: read only while NOTION_TASKS_DB_ID is set AND the tree
# holds no Task rows yet (the switch-over). Empty after the move.
NOTION_TASKS_DB_ID = os.environ.get("NOTION_TASKS_DB_ID") or None
# Optional: the Website List database that feeds the events scout (scout.py).
NOTION_WEBSITES_DB_ID = os.environ.get("NOTION_WEBSITES_DB_ID") or None
NOTION_VERSION = "2022-06-28"

BASE_URL = "https://api.notion.com/v1"

# The only properties this system ever writes back to Notion (see time_tracker.py).
TIME_SPENT_PROPS = ("Time Spent", "Time Spent (hr)")   # tasks: hours, accumulated (first name found wins)
TIME_SPENT_PROP = TIME_SPENT_PROPS[-1]                  # legacy name, kept for callers/tests
LAST_OCCURRED_PROP = "Last Occurred"      # routines: date of the latest occurrence
SESSIONS_PROP = "Sessions"                # routines: +1 after each approved block has passed
# Levels of the tree (the `Level` select). Anything else is logged and skipped.
LEVEL_TREE = ("Telos", "Goal", "Project")
LEVEL_ROUTINE = "Routine"
LEVEL_TASK = "Task"
UPCOMING_EVENT_PROP = "Upcoming Event"    # websites: the next approved event from that site (text)
EVENTS_ATTENDED_PROP = "Events Attended"  # websites: count, incremented after an event has passed
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def fetch_all_pages(database_id: str) -> list[dict[str, Any]]:
    """Fetch every non-archived page in a Notion database, handling pagination."""
    pages: list[dict[str, Any]] = []
    payload: dict[str, Any] = {"page_size": 100}

    while True:
        resp = requests.post(
            f"{BASE_URL}/databases/{database_id}/query",
            headers=HEADERS,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        pages.extend(p for p in data["results"] if not p.get("archived", False))

        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return pages


def get_page(page_id: str) -> dict[str, Any]:
    """Fetch one page fresh from Notion (properties included)."""
    resp = requests.get(f"{BASE_URL}/pages/{page_id}", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _number_property(page: dict[str, Any], name: str) -> Any:
    prop = page["properties"].get(name)
    if prop is None:
        raise ValueError(f"property {name!r} does not exist on page {page['id']}")
    if prop["type"] != "number":
        raise ValueError(f"property {name!r} on page {page['id']} is type {prop['type']!r}, not number")
    return prop["number"]


def _date_property(page: dict[str, Any], name: str) -> str | None:
    prop = page["properties"].get(name)
    if prop is None:
        raise ValueError(f"property {name!r} does not exist on page {page['id']}")
    if prop["type"] != "date":
        raise ValueError(f"property {name!r} on page {page['id']} is type {prop['type']!r}, not date")
    return prop["date"]["start"] if prop["date"] else None


def read_date_property(page_id: str, name: str) -> str | None:
    """Start value of a date property ('YYYY-MM-DD' or ISO datetime), read fresh; None when empty."""
    return _date_property(get_page(page_id), name)


def update_date_property(page_id: str, name: str, date_str: str) -> str | None:
    """
    Set exactly one date property (date only, no time) on one page and return
    the start value Notion reports afterwards. Same narrowness as
    update_number_property: only this property is in the PATCH body, it must
    already exist as a date, and the result is read back for verification.
    """
    _date_property(get_page(page_id), name)  # existence + type check before writing
    resp = requests.patch(
        f"{BASE_URL}/pages/{page_id}",
        headers=HEADERS,
        json={"properties": {name: {"date": {"start": date_str}}}},
        timeout=30,
    )
    resp.raise_for_status()
    return _date_property(resp.json(), name)


def _text_property(page: dict[str, Any], name: str) -> str:
    prop = page["properties"].get(name)
    if prop is None:
        raise ValueError(f"property {name!r} does not exist on page {page['id']}")
    if prop["type"] != "rich_text":
        raise ValueError(f"property {name!r} on page {page['id']} is type {prop['type']!r}, not rich_text")
    return "".join(chunk.get("plain_text", "") for chunk in prop["rich_text"])


def read_text_property(page_id: str, name: str) -> str:
    """Plain text of a rich_text property, read fresh ('' when empty)."""
    return _text_property(get_page(page_id), name)


def update_text_property(page_id: str, name: str, text: str) -> str:
    """
    Set exactly one rich_text property (plain text, no formatting) and return
    what Notion reports afterwards. Same narrowness as the number/date
    helpers: only this property is in the PATCH body, it must already exist
    as rich_text, and the value is read back. Empty text clears it.
    """
    _text_property(get_page(page_id), name)  # existence + type check before writing
    value = [{"type": "text", "text": {"content": text[:2000]}}] if text else []
    resp = requests.patch(
        f"{BASE_URL}/pages/{page_id}",
        headers=HEADERS,
        json={"properties": {name: {"rich_text": value}}},
        timeout=30,
    )
    resp.raise_for_status()
    return _text_property(resp.json(), name)


def read_number_property(page_id: str, name: str) -> float | None:
    """Current value of a number property, read fresh. Raises ValueError if it is missing or not a number."""
    return _number_property(get_page(page_id), name)


def update_number_property(page_id: str, name: str, value: float) -> float | None:
    """
    Set exactly one number property on one page and return the value Notion
    reports afterwards.

    Deliberately narrow: the PATCH body names only this property, so Notion
    leaves every other property untouched; the property must already exist
    and be a number (this never creates or retypes one); and the value is
    read back from the response so the caller can confirm the write.
    """
    _number_property(get_page(page_id), name)  # existence + type check before writing
    resp = requests.patch(
        f"{BASE_URL}/pages/{page_id}",
        headers=HEADERS,
        json={"properties": {name: {"number": value}}},
        timeout=30,
    )
    resp.raise_for_status()
    return _number_property(resp.json(), name)


def get_prop(page: dict[str, Any], name: str) -> Any:
    """
    Extract a normalized Python value from a Notion page property, regardless
    of its underlying type.

    NOTE: Notion's "status" and "select" property types look identical in the
    UI but have different JSON shapes. This checks properties[name]["type"]
    at read time rather than assuming, so a Priority or Status column built
    as one type won't silently break the adapter if you expected the other.
    """
    prop = page["properties"].get(name)
    if prop is None:
        return None

    ptype = prop["type"]
    value = prop.get(ptype)
    if value is None:
        return None

    if ptype in ("title", "rich_text"):
        return "".join(chunk["plain_text"] for chunk in value) or None

    if ptype in ("select", "status"):
        return value["name"]

    if ptype == "multi_select":
        return [item["name"] for item in value]

    if ptype in ("number", "checkbox"):
        return value

    if ptype == "date":
        # "start" is always present; "end" only exists for date ranges.
        return {"start": value["start"], "end": value.get("end")}

    if ptype == "place" and isinstance(value, dict):
        # Notion's place property (2026-10-27): a name and/or a formatted address.
        return value.get("address") or value.get("name") or None

    # Fallback for property types not explicitly handled (people, relation, etc.)
    return value


def get_location(page: dict[str, Any]) -> Any:
    """`Location` (text), else the newer `Place` property."""
    return get_first(page, "Location", "Place")


def _positive(value: Any) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def node_plan_kind(page: dict[str, Any]) -> str | None:
    """
    How a Goal/Project row itself is planned, beside being ancestry (2026-10-27,
    the user's choice: "Session Time, not Hours per week"): with Display
    checked, a `Session Time (hr)` makes it sessions like a Flexible routine
    ("goal"; Times/Week sets the pace, Time to Complete caps the total), a
    `Time to Complete` alone makes it one piece of work like a task ("task").
    Anything else is a node only.
    """
    display_name = find_property(page, ("Display", "Show on Calendar"), "checkbox")
    if not display_name or not get_prop(page, display_name):
        return None
    if _positive(get_first(page, "Session Time (hr)", "Session Time", "Required Time (hr)")):
        return "goal"
    if _positive(get_first(page, "Time to Complete", "Time to Complete (hr)")):
        return "task"
    return None


def get_title(page: dict[str, Any]) -> str | None:
    """The title property's text whatever it is called (Goal, Task, Title, Name ...)."""
    for prop in page["properties"].values():
        if prop["type"] == "title":
            return "".join(chunk["plain_text"] for chunk in prop["title"]) or None
    return None


def get_first(page: dict[str, Any], *names: str) -> Any:
    """get_prop() for the first of `names` that exists on the page (renames during the restructure)."""
    for name in names:
        if name in page["properties"]:
            return get_prop(page, name)
    return None


def find_property(page: dict[str, Any], names: tuple[str, ...], ptype: str) -> str | None:
    """The first of `names` that exists on the page with type `ptype`, or None."""
    for name in names:
        prop = page["properties"].get(name)
        if prop is not None and prop["type"] == ptype:
            return name
    return None


def resolve_property_name(page_id: str, names: tuple[str, ...], ptype: str) -> str:
    """Read the page and return which of `names` holds the `ptype` property; raises when none does."""
    name = find_property(get_page(page_id), names, ptype)
    if name is None:
        raise ValueError(f"none of {names} is a {ptype} property on page {page_id}")
    return name


def _date_start(value: Any) -> str | None:
    return value["start"] if isinstance(value, dict) else None


def _relation_ids(page: dict[str, Any], name: str) -> list[str]:
    prop = page["properties"].get(name)
    if not prop or prop["type"] != "relation":
        return []
    return [r["id"] for r in prop["relation"]]


def page_level(page: dict[str, Any]) -> str | None:
    return get_prop(page, "Level")


def parent_page_id(page: dict[str, Any]) -> str | None:
    """The `Parent item` relation (sub-items): the first parent, or None at the root."""
    ids = _relation_ids(page, "Parent item") or _relation_ids(page, "Parent")
    return ids[0] if ids else None


def frequency_text(times_per_week: float | None) -> str | None:
    """
    `Times/Week` as the text the frequency parser and the prompts already
    understand: 7 -> "Daily", 2 -> "2x Week", 0.5 -> "1x Every Other Week",
    0.25 -> "1x Month", anything else -> "Once every N days".
    """
    if times_per_week is None:
        return None
    try:
        n = float(times_per_week)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if abs(n - 7) < 1e-9:
        return "Daily"
    if n >= 1 and abs(n - round(n)) < 1e-9:
        return f"{int(round(n))}x Week"
    if abs(n - 0.5) < 1e-9:
        return "1x Every Other Week"
    if abs(n - 0.25) < 1e-9:
        return "1x Month"
    return f"Once every {max(1, int(round(7 / n)))} days"


def normalize_task(page: dict[str, Any]) -> dict[str, Any]:
    deadline = get_first(page, "Target Date", "Deadline")
    return {
        "source": "notion",
        "source_task_id": page["id"],
        "task": get_title(page),
        "time_to_complete_hr": get_first(page, "Time to Complete", "Time to Complete (hr)"),
        # Hours already logged. Notion owns this number; time_tracker.py adds to
        # it after optimizer-scheduled blocks have actually happened.
        "time_spent_hr": get_first(page, *TIME_SPENT_PROPS),
        "deadline": _date_start(deadline),
        "priority": get_prop(page, "Priority"),
        "location": get_location(page),
        "status": get_prop(page, "Status"),
        "level": page_level(page) or LEVEL_TASK,
        "parent_source_id": parent_page_id(page),
        "last_modified": page["last_edited_time"],
        "raw_json": page,
    }


def normalize_goal(page: dict[str, Any]) -> dict[str, Any]:
    """A Routine row (or, before the restructure, a Goals-database row) as a `goals` row."""
    end_date = get_first(page, "Target Date", "End Date")
    last_occurred = get_prop(page, LAST_OCCURRED_PROP)

    days = get_prop(page, "Day of Week")
    if isinstance(days, str):  # tolerate single-select instead of multi-select
        days = [days]

    times_per_week = get_prop(page, "Times/Week")
    frequency = frequency_text(times_per_week) if times_per_week is not None else get_prop(page, "Frequency")
    # Display (was Show on Calendar). Unchecked (or missing) means the row is
    # context for the optimizer -- it shapes the plan but never becomes events.
    display_name = find_property(page, ("Display", "Show on Calendar"), "checkbox")
    show = bool(get_prop(page, display_name)) if display_name else False
    level = page_level(page) or LEVEL_ROUTINE
    # "Type" select: Routine | Flexible | Event Interest (see routine.py). None = untyped,
    # except on a Goal/Project planned as sessions, which is Flexible unless it says otherwise
    # (untyped would send it to the scout as well).
    goal_type = get_first(page, "Type", "Placement")
    if not goal_type and level in LEVEL_TREE:
        goal_type = "Flexible"
    return {
        "source": "notion",
        "source_goal_id": page["id"],
        "goal": get_title(page),
        "frequency": frequency,
        "times_per_week": times_per_week,
        "end_date": _date_start(end_date),
        "required_time_hr": get_first(page, "Session Time (hr)", "Session Time", "Required Time (hr)"),
        # A Goal/Project planned as sessions may cap the total: hours_remaining = this minus Time Spent.
        "time_to_complete_hr": get_first(page, "Time to Complete", "Time to Complete (hr)"),
        "time_spent_hr": get_first(page, *TIME_SPENT_PROPS),
        "priority": get_prop(page, "Priority"),
        "location": get_location(page),
        "days_of_week": json.dumps(days) if days else None,
        "time_of_day_window": get_prop(page, "Time of Day"),
        "show_on_calendar": int(show),
        # Free-text guidance the user writes for the optimizer about this goal
        # (or, on a context-only goal, about the plan as a whole).
        "notes_for_llm": get_prop(page, "Notes for LLM"),
        "goal_type": goal_type,
        # Date of the latest occurrence. Notion owns it; time_tracker.py moves it
        # forward after an approved occurrence has happened. Lets the model see
        # that a monthly goal already happened this month.
        "last_occurred": _date_start(last_occurred),
        "sessions": get_prop(page, SESSIONS_PROP),
        "status": get_prop(page, "Status"),
        "level": level,
        "parent_source_id": parent_page_id(page),
        "last_modified": page["last_edited_time"],
        "raw_json": page,
    }


def normalize_node(page: dict[str, Any]) -> dict[str, Any]:
    """A Telos, Goal or Project row: the ancestry and the facts the planner reads about it."""
    return {
        "source": "notion",
        "source_node_id": page["id"],
        "title": get_title(page),
        "level": page_level(page),
        "parent_source_id": parent_page_id(page),
        "status": get_prop(page, "Status"),
        "priority": get_prop(page, "Priority"),
        "location": get_location(page),
        "notes_for_llm": get_prop(page, "Notes for LLM"),
        "target_date": _date_start(get_first(page, "Target Date", "End Date")),
        "hours_per_week": get_first(page, "Hours per week", "Hours/Week"),
        "time_spent_hr": get_first(page, *TIME_SPENT_PROPS),
        "last_modified": page["last_edited_time"],
        "raw_json": page,
    }


def split_tree(pages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """
    Every page of the tree database sorted by Level into normalized rows:
    {"nodes": [...], "goals": [...], "tasks": [...], "skipped": [...]}. A page
    without a Level is read the old way: a Goals-database row (it has a
    Frequency or Times/Week) is a routine, anything with a Deadline is a task.
    A Goal/Project with Display and a Session Time (or a Time to Complete) is
    a node *and* a planner goal (or task) under the same id (node_plan_kind).
    """
    out: dict[str, list[dict[str, Any]]] = {"nodes": [], "goals": [], "tasks": [], "skipped": []}
    for page in pages:
        level = page_level(page)
        if level in LEVEL_TREE:
            out["nodes"].append(normalize_node(page))
            planned = node_plan_kind(page)
            if planned == "goal":
                out["goals"].append(normalize_goal(page))
            elif planned == "task":
                out["tasks"].append(normalize_task(page))
        elif level == LEVEL_ROUTINE:
            out["goals"].append(normalize_goal(page))
        elif level == LEVEL_TASK:
            out["tasks"].append(normalize_task(page))
        elif "Level" in page["properties"]:
            # A tree page with its Level unset (or an unknown one), e.g. a row typed by mistake.
            out["skipped"].append({"id": page["id"], "title": get_title(page), "level": level})
        elif "Frequency" in page["properties"] or "Times/Week" in page["properties"]:
            out["goals"].append(normalize_goal(page))  # a row of the old Goals database
        elif "Deadline" in page["properties"]:
            out["tasks"].append(normalize_task(page))  # a row of the old Tasks database
        else:
            out["skipped"].append({"id": page["id"], "title": get_title(page), "level": level})
    return out


def fetch_tree() -> dict[str, list[dict[str, Any]]]:
    """One fetch of the tree database, split by level. The old Tasks database is read only until the tree has tasks."""
    tree = split_tree(fetch_all_pages(NOTION_TREE_DB_ID))
    if not tree["tasks"] and NOTION_TASKS_DB_ID and NOTION_TASKS_DB_ID != NOTION_TREE_DB_ID:
        tree["tasks"] = [normalize_task(p) for p in fetch_all_pages(NOTION_TASKS_DB_ID)]
    return tree


def normalize_website(page: dict[str, Any]) -> dict[str, Any]:
    """One Website List row: the URL is the title; the other two columns are written back by time_tracker/scout."""
    return {
        "source": "notion",
        "source_website_id": page["id"],
        "url": (get_prop(page, "URL") or "").strip(),
        "upcoming_event": get_prop(page, UPCOMING_EVENT_PROP),
        "events_attended": get_prop(page, EVENTS_ATTENDED_PROP),
        "last_modified": page["last_edited_time"],
        "raw_json": page,
    }


def sync_websites() -> list[dict[str, Any]]:
    if not NOTION_WEBSITES_DB_ID:
        raise RuntimeError("NOTION_WEBSITES_DB_ID is not set in .env")
    return [w for w in (normalize_website(p) for p in fetch_all_pages(NOTION_WEBSITES_DB_ID)) if w["url"]]


def sync_tasks() -> list[dict[str, Any]]:
    return fetch_tree()["tasks"]


def sync_goals() -> list[dict[str, Any]]:
    return fetch_tree()["goals"]


if __name__ == "__main__":
    # Standalone sanity check — prints normalized data without touching
    # events.db. Once the mapping looks right, wire sync_tasks()/sync_goals()
    # into db.upsert_task()/db.upsert_goal() and add a job to main.py's
    # APScheduler lifespan alongside the calendar sources.
    tree = fetch_tree()
    for kind in ("nodes", "goals", "tasks", "skipped"):
        print(f"=== {kind} ({len(tree[kind])}) ===")
        for row in tree[kind]:
            print({k: v for k, v in row.items() if k != "raw_json"})