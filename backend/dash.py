"""
dash.py — GET /dash/day: one New York day in plain JSON for the Daily Dash,
the room-display dashboard on the Mac (a separate project that reads this
calendar instead of the four sources). Read-only: edits from the dashboard go
through the existing routes (/events/{id}/move, /events/{id}/delete,
/slot/options, /slot/place).

    day(date)   {date, timezone, window, free, events}

    window      the awake day from the Hours tab: wake-up to bedtime ("24:00"
                when bed is at or after midnight).
    free        stretches of at least FREE_MIN_MINUTES inside the day's free
                window with no timed event on them: the same rule as the
                calendar's Free bands (app.jsx freeTimeBackground).
    events      every active event overlapping the day, pending proposals left
                out. Each: id, source (google | apple | outlook | partiful |
                optimizer), calendarId, kind (synced | block | scouted | travel
                | free), title, location, description, allDay, start, end
                (ISO 8601 with the New York offset; all-day: dates, end
                exclusive), telosId (the Notion page id of the Telos above a
                block's task or goal, or above a scouted event's goal; null
                otherwise), editable (an approved, unsettled optimizer block)
                and conflict ({event_id, title} or null).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import db
import hours

NY = ZoneInfo(db.DISPLAY_TZ)
FREE_MIN_MINUTES = 30
KIND_BY_ITEM_TYPE = {"task": "block", "goal": "block", "event": "scouted", "travel": "travel", "free": "free"}


def _parse_day(date: str | None) -> dt.date:
    if not date:
        return dt.datetime.now(NY).date()
    try:
        return dt.date.fromisoformat(date)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def _at(day: dt.date, hhmm: str) -> dt.datetime:
    """A clock time on `day` in New York; "24:00" is the next midnight."""
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(0, 0), NY) + dt.timedelta(hours=h, minutes=m)


def _telos_of(parent_id: str | None, nodes_by_id: dict[str, dict]) -> str | None:
    chain = db.ancestry(parent_id, nodes_by_id)
    node = next((n for n in chain if (n.get("level") or "") == "Telos"), None)
    return node.get("source_node_id") if node else None


def _telos_for_proposal(proposal: dict, items_by_id: dict[str, dict], nodes_by_id: dict[str, dict]) -> str | None:
    """The Telos above the task or goal a block serves (a scouted event: the goal it was found for)."""
    if proposal["item_type"] in ("task", "goal"):
        item_id = proposal.get("item_id")
    elif proposal["item_type"] == "event":
        item_id = proposal.get("goal_id")
    else:
        return None
    if not item_id:
        return None
    item = items_by_id.get(item_id)
    if item is not None:
        return _telos_of(item.get("parent_id"), nodes_by_id)
    node = nodes_by_id.get(item_id)  # a Goal/Project planned as itself has a tree node of the same id
    if node is not None:
        return node.get("source_node_id") if node.get("level") == "Telos" else _telos_of(node.get("parent_id"), nodes_by_id)
    return None


def _free_bands(day: dt.date, timed: list[tuple[dt.datetime, dt.datetime]]) -> list[dict[str, str]]:
    window = hours.ranges_for(day)["free"]
    cursor, free_end = _at(day, window["start"]), _at(day, window["end"])
    bands: list[dict[str, str]] = []

    def push(start: dt.datetime, end: dt.datetime) -> None:
        if (end - start).total_seconds() >= FREE_MIN_MINUTES * 60:
            bands.append({"start": start.isoformat(), "end": end.isoformat()})

    for start, end in sorted(timed):
        if start >= free_end:
            break
        if start > cursor:
            push(cursor, start)
        cursor = max(cursor, end)
    if cursor < free_end:
        push(cursor, free_end)
    return bands


def day(date: str | None = None) -> dict[str, Any]:
    the_day = _parse_day(date)
    start = dt.datetime.combine(the_day, dt.time(0, 0), NY)
    rows = db.get_events_between(start, start + dt.timedelta(days=1))

    nodes_by_id = {n["id"]: n for n in db.get_tree_nodes()}
    items_by_id = {t["id"]: t for t in db.get_tasks()} | {g["id"]: g for g in db.get_goals()}
    conflicts = db.get_conflicts()

    events: list[dict[str, Any]] = []
    timed: list[tuple[dt.datetime, dt.datetime]] = []
    for row in rows:
        kind, telos_id, editable = "synced", None, False
        if row["source"] == "optimizer":
            proposal = db.get_proposal(row["source_event_id"])
            if proposal is None:
                kind = "block"
            else:
                kind = KIND_BY_ITEM_TYPE.get(proposal["item_type"], "block")
                telos_id = _telos_for_proposal(proposal, items_by_id, nodes_by_id)
                editable = proposal["status"] == "promoted" and not proposal.get("settled_at")
        all_day = bool(row["all_day"])
        if all_day:
            span = (row["start"][:10], row["end_dt"].date().isoformat())
        else:
            span = (row["start_dt"].isoformat(), row["end_dt"].isoformat())
            timed.append((row["start_dt"], row["end_dt"]))
        clash = conflicts.get(row["id"])
        events.append({
            "id": row["id"],
            "source": row["source"],
            "calendarId": row["calendar_id"],
            "kind": kind,
            "title": row.get("title") or "",
            "location": row.get("location") or "",
            "description": row.get("description") or "",
            "allDay": all_day,
            "start": span[0],
            "end": span[1],
            "telosId": telos_id,
            "editable": editable,
            "conflict": {"event_id": clash["event_id"], "title": clash.get("title")} if clash else None,
        })

    profile = hours.ranges_for(the_day)
    return {
        "date": the_day.isoformat(),
        "timezone": db.DISPLAY_TZ,
        "window": {"start": profile["sleep"]["end"], "end": hours.bedtime_end(profile["sleep"])},
        "free": _free_bands(the_day, timed),
        "events": events,
    }
