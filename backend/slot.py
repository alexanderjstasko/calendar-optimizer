"""
slot.py — a tapped free band on the calendar: what could go there, and booking the pick.

    options(start, end)   GET /slot/options: scouted public events whose time lies
                          inside the slot (recommended ones first, then key-word
                          matches, then by start) and open tasks and flexible goals
                          that fit its length and rules (the ones the current plan
                          proposes first, then by deadline, then priority). No model
                          call: everything comes from what is already in the database.
    place(kind, id, ...)  POST /slot/place: the person chose one, so it is booked at
                          once -- a proposal of its own (run kind 'manual') promoted
                          straight away, so settlement, Notion write-back and the
                          scout's bookkeeping work exactly as for any approved block.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import db
import geo
import hours
import optimizer
import routine
from optimizer_schema import DISPLAY_TIMEZONE

NY = ZoneInfo(DISPLAY_TIMEZONE)
PRIORITY_RANK = {"critical": -1, "high": 0, "medium": 1, "low": 2}
MIN_BLOCK_HOURS = 0.5
MAX_BLOCK_HOURS = 4.0
DEFAULT_EVENT_HOURS = 2.0
RATIONALE = "Chosen from a free slot on the calendar."


def _parse(value: str) -> dt.datetime:
    return db.parse_event_time(value, False)


def _slot(start: str, end: str) -> tuple[dt.datetime, dt.datetime]:
    try:
        s, e = _parse(start), _parse(end)
    except (ValueError, TypeError) as exc:
        raise ValueError("start and end must be ISO 8601 date-times") from exc
    if e <= s:
        raise ValueError("end must be after start")
    return s, e


def _keyword_match(event: dict, keywords: list[str]) -> bool:
    text = " ".join(str(event.get(k) or "") for k in ("title", "summary", "category")).lower()
    return any(k.lower() in text for k in keywords)


_HOME_WORDS = re.compile(r"^\s*(home|house|apartment|apt|my place|at home|online|virtual|zoom|remote)\b", re.I)


def where(location: str | None, home: str = "") -> str:
    """
    "home" or "out", for the slot sheet's Location filter: no location, a
    link (virtual), a word like "Home" or the Hours tab's home address count
    as home; anything else is somewhere to go.
    """
    text = (location or "").strip()
    if not text or re.match(r"https?://", text) or _HOME_WORDS.match(text):
        return "home"
    if home and geo.place_key(home) == geo.place_key(text):
        return "home"
    return "out"


def _priority(value: str | None) -> int:
    return PRIORITY_RANK.get((value or "").strip().lower(), 3)


def options(start: str, end: str) -> dict[str, Any]:
    s, e = _slot(start, end)
    slot_hours = (e - s).total_seconds() / 3600
    keywords = hours.event_keywords()
    saved_hours = hours.load()
    home = hours.home_location(saved_hours)
    pending = [p for p in db.get_proposals() if p["status"] in ("proposed", "queued")]
    pending_items = {(p["item_type"], p.get("item_id")) for p in pending}
    offered = {p.get("discovered_event_id") for p in pending if p["item_type"] == "event"}

    events = []
    # The query works by date prefix (half-open on the end), so ask for the slot's days in full.
    day_from = s.astimezone(NY).date().isoformat()
    day_to = (e.astimezone(NY).date() + dt.timedelta(days=1)).isoformat()
    for ev in db.get_discovered_events(day_from, day_to, undecided_only=True):
        if ev.get("all_day"):
            continue
        try:
            es = _parse(ev["start"])
            ee = _parse(ev["end"]) if ev.get("end") else es + dt.timedelta(hours=DEFAULT_EVENT_HOURS)
        except (ValueError, TypeError):
            continue
        if ee <= es:
            ee = es + dt.timedelta(hours=DEFAULT_EVENT_HOURS)
        if es < s or ee > e:
            continue
        if hours.too_soon_after_work(es, saved_hours):  # still has to get there from work
            continue
        events.append({
            "kind": "event", "id": ev["id"], "title": ev["title"], "start": es.isoformat(), "end": ee.isoformat(),
            "site": ev.get("site"), "location": ev.get("location") or "", "cost": ev.get("cost") or "",
            "category": ev.get("category") or "", "summary": ev.get("summary") or "", "url": ev.get("url") or "",
            "recommended": ev["id"] in offered or bool(ev.get("recommended_at")),
            "keyword_match": _keyword_match(ev, keywords),
            # A public event with no venue given still happens somewhere else.
            "where": where(ev.get("location"), home) if (ev.get("location") or "").strip() else "out",
        })
    events.sort(key=lambda x: (not x["recommended"], not x["keyword_match"], x["start"]))

    day = s.astimezone(NY).date()
    items = []
    for item in db.get_schedulable_items():
        if item["type"] == "task":
            remaining = optimizer._hours_remaining(item.get("time_to_complete_hr"), item.get("time_spent_hr"))
            if remaining is None:
                remaining = 1.0
            if remaining <= 0:
                continue
            duration = min(remaining, slot_hours, MAX_BLOCK_HOURS)
            if duration < MIN_BLOCK_HOURS:
                continue
            items.append({
                "kind": "task", "id": item["id"], "title": item["task"], "duration_hr": round(duration, 2),
                "hours_remaining": remaining, "deadline": item.get("deadline"), "priority": item.get("priority") or "",
                "location": item.get("location") or "", "recommended": ("task", item["id"]) in pending_items,
                "where": where(item.get("location"), home),
            })
            continue
        kind = routine.goal_type(item)
        if kind == routine.EVENT or (item.get("show_on_calendar") is not None and not item["show_on_calendar"]):
            continue
        required = float(item.get("required_time_hr") or routine.DEFAULT_DURATION_HR)
        if required > slot_hours + 1e-9:
            continue
        pool = routine._pool(item)
        if pool and day.weekday() not in pool:
            continue
        tod = (item.get("time_of_day_window") or "").strip()
        if tod and tod != "Any" and tod in routine.TIME_OF_DAY_WINDOWS:
            a, b = routine.TIME_OF_DAY_WINDOWS[tod].split("-")
            if not (s < routine._at(day, b) and e > routine._at(day, a)):
                continue
        items.append({
            "kind": "goal", "id": item["id"], "title": item["goal"], "duration_hr": round(required, 2),
            "frequency": item.get("frequency") or "", "deadline": item.get("end_date"), "priority": item.get("priority") or "",
            "location": item.get("location") or "", "goal_type": kind or "", "time_of_day": tod or "Any",
            "recommended": ("goal", item["id"]) in pending_items,
            "where": where(item.get("location"), home),
        })
    items.sort(key=lambda x: (not x["recommended"], x.get("deadline") or "9999-12-31", _priority(x["priority"]), x["title"].lower()))
    return {"slot": {"start": s.isoformat(), "end": e.isoformat()}, "events": events, "items": items}


def place(kind: str, item_id: str, start: str, end: str, now: dt.datetime | None = None) -> dict[str, Any]:
    """Book the pick: raises LookupError (unknown id) or ValueError (bad times, overlap)."""
    s, e = _slot(start, end)
    now_iso = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    if kind == "event":
        cand = db.get_discovered_event(item_id)
        if cand is None:
            raise LookupError("unknown event")
        row = {
            "item_type": "event", "item_id": cand["id"], "discovered_event_id": cand["id"], "website_id": cand.get("website_id"),
            "goal_id": None, "url": cand.get("url"), "cost": cand.get("cost") or "", "title": cand["title"],
            "description": " | ".join(x for x in (cand.get("summary") or "", cand.get("cost") or "", cand.get("url") or "") if x)[:400],
            "location": cand.get("location") or "", "start": s.isoformat(), "end": e.isoformat(), "all_day": False,
            "timezone": DISPLAY_TIMEZONE, "rationale": RATIONALE, "ref": "slot-1", "status": "proposed",
        }
    else:
        item = db.get_schedulable_item(kind, item_id)
        if item is None:
            raise LookupError(f"unknown {kind}")
        row = {
            "item_type": kind, "item_id": item["id"], "title": item.get("task") or item.get("goal"), "description": "",
            "location": item.get("location") or "", "start": s.isoformat(), "end": e.isoformat(), "all_day": False,
            "timezone": DISPLAY_TIMEZONE, "rationale": RATIONALE, "ref": "slot-1", "status": "proposed",
        }
    for ev in db.get_events_between(s, e):
        if not ev["all_day"] and ev["start_dt"] < e and ev["end_dt"] > s:
            raise ValueError(f"overlaps {ev.get('title') or ev['id']!r}")
    run_id = f"manual_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    db.create_optimizer_run(run_id, now_iso, "person", "slot", s.isoformat(), e.isoformat(), kind="manual")
    proposal_id = db.insert_proposals(run_id, [row], now_iso)[0]
    event_id = db.promote_proposal(proposal_id, now_iso)
    out: dict[str, Any] = {"proposal_id": proposal_id, "event_id": event_id, "superseded_overlaps": []}
    if kind == "event":
        out["superseded_overlaps"] = db.supersede_overlapping_options(proposal_id, now_iso)
        try:
            import scout  # lazy: needs NOTION_* set

            out["scout"] = scout.record_decision(db.get_proposal(proposal_id), "approved")
        except Exception as exc:  # noqa: BLE001 -- the booking itself has happened
            out["scout"] = {"error": f"{type(exc).__name__}: {exc}"}
    db.finish_optimizer_run(run_id, status="succeeded", finished_at=now_iso, proposed_count=1, invalid_count=0,
                            notes=f"{row['title']}: booked from a free slot")
    event = db.get_event(event_id) if event_id else None
    out["event"] = db.to_schedule_x_event(event) if event else None
    return out
