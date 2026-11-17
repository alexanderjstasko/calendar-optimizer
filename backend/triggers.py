"""
triggers.py — what changes the plan between Sunday runs (since 2026-10-26).

The full plan runs once a week (Sunday 06:30) and on the Plan tab's Re-plan
button. In between, only two things touch it, and both land as *pending*
proposals the person approves in the Plan tab -- nothing is applied by code:

  notion_check()    hourly: re-read Notion. A new task or goal is placed on
                    its own (one small model call, or code for a routine
                    goal); a deleted row's blocks leave the calendar. Then the
                    change email goes out (at most one an hour).
  conflict_check()  after the calendar syncs: an approved goal/task block that
                    a synced timed event now overlaps is flagged (the
                    calendar and Plan tab show the clash) and a move for it is
                    proposed. An approved public event in a clash is only
                    flagged: it happens when it happens. All-day entries never
                    conflict.

How one item is placed (place_item), the ladder a person would use:
  1. fit it in a free slot in the rest of the week that meets its day,
     time-of-day, length and the free-time floor; nothing else changes;
  2. no slot and it matters more (deadline this week, higher priority): the
     model may propose moving an approved goal/task block out of the way
     (`replaces_event_id` on the proposal), never a synced event or an
     approved public event; the displaced block is re-placed the same way;
  3. otherwise it waits for Sunday (`unscheduled`).
A proposal that carries replaces_event_id is a move: approving it takes the
old block (and its travel) off the calendar and promotes the new one
(main.py _approve -> db.replace_block). Rejecting a move keeps the old block
where it is; rejecting a new item's placement leaves it for Sunday.

    python3 triggers.py notion [--dry-run]      # the hourly check, now
    python3 triggers.py conflicts [--dry-run]   # the conflict check, now
    python3 triggers.py place task|goal <id>    # place one item, now
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import db
import hours
import llm_client
import optimizer
import routine
from optimizer_schema import DISPLAY_TIMEZONE, PROPOSED_EVENTS_SCHEMA, SUBMIT_TOOL_DESCRIPTION, SUBMIT_TOOL_NAME

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
logger = logging.getLogger(__name__)

NY = ZoneInfo(DISPLAY_TIMEZONE)
RUN_KIND = "trigger"
CONFLICT_LOOKAHEAD_DAYS = 14   # approved blocks this far ahead are checked (next week's plan included)
DEFAULT_PLACEMENT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "placement_prompt.md"
DEFAULT_MAX_OUTPUT_TOKENS = 4000


def placement_prompt_path() -> Path:
    override = (os.environ.get("LLM_PLACEMENT_PROMPT_PATH") or "").strip()
    return Path(override).expanduser() if override else DEFAULT_PLACEMENT_PROMPT_PATH


def _now(now: dt.datetime | None) -> dt.datetime:
    return (now or dt.datetime.now(NY)).astimezone(NY)


def _utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _clock(value: dt.datetime) -> str:
    """"Tue 5:30 PM" for the email lines and rationales."""
    value = value.astimezone(NY)
    hour = value.hour % 12 or 12
    minute = f":{value.minute:02d}" if value.minute else ""
    return f"{value:%a} {hour}{minute} {'AM' if value.hour < 12 else 'PM'}"


def week_end_after(moment: dt.datetime) -> dt.datetime:
    """Midnight starting the Monday after `moment`'s week (Monday-Sunday weeks)."""
    moment = moment.astimezone(NY)
    monday = moment.date() + dt.timedelta(days=7 - moment.weekday())
    return dt.datetime.combine(monday, dt.time(0, 0), tzinfo=NY)


# --- one item ---------------------------------------------------------------------------


def _new_run(now_iso: str, window_start: dt.datetime, window_end: dt.datetime, *, backend: str, model: str,
             system: str | None = None, payload: str | None = None) -> str:
    run_id = f"trigger_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    db.create_optimizer_run(run_id, now_iso, backend, model, window_start.isoformat(), window_end.isoformat(),
                            system_prompt=system, payload_json=payload, kind=RUN_KIND)
    return run_id


def _supersede_pending_moves(event_id: str, now_iso: str) -> int:
    """An older pending move for the same block gives way to the new one."""
    n = 0
    for p in db.get_proposals(status="proposed"):
        if p.get("replaces_event_id") == event_id:
            db.set_proposal_status(p["id"], "superseded", now_iso)
            n += 1
    return n


def _item_label(item: dict) -> str:
    return item.get("task") or item.get("goal") or item["id"]


def _routine_move(block: dict, goal: dict, window_end: dt.datetime, now: dt.datetime, reason: str,
                  dry_run: bool) -> dict[str, Any]:
    """A routine block in a clash: the next free slot for that one occurrence, by code."""
    hours_doc = hours.load()
    duration = block["end_dt"] - block["start_dt"]
    busy_by_day: dict[dt.date, list[tuple[dt.datetime, dt.datetime]]] = {}
    for ev in db.get_events_between(now, window_end + dt.timedelta(days=1)):
        if ev["all_day"] or ev["id"] == block["promoted_event_id"]:
            continue
        routine._mark_busy(busy_by_day, ev["start_dt"].astimezone(NY), ev["end_dt"].astimezone(NY))
    for p in db.get_proposals(status="proposed"):
        if p["item_type"] in ("event",) or p.get("all_day"):
            continue
        try:
            s, e = db.parse_event_time(p["start"], False), db.parse_event_time(p["end"], False)
        except (ValueError, TypeError):
            continue
        busy_by_day.setdefault(s.date(), []).append((s, e))
    first_day = max(now, block["start_dt"]).astimezone(NY).date()
    last_day = (window_end - dt.timedelta(minutes=1)).astimezone(NY).date()
    placed = None
    day = first_day
    while day <= last_day:
        not_before = now + dt.timedelta(minutes=30) if day == now.date() else None
        placed = routine.find_slot(goal, day, duration, hours_doc, busy_by_day, not_before)
        if placed:
            break
        day += dt.timedelta(days=1)
    out: dict[str, Any] = {"item": _item_label(goal), "method": "code", "placed": bool(placed), "dry_run": dry_run}
    if not placed:
        out["note"] = "no free slot this week; it waits for Sunday"
        return out
    _, start, end = placed
    proposal = {
        "item_type": "goal", "item_id": goal["id"], "title": goal["goal"], "description": "",
        "location": goal.get("location") or "", "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"), "all_day": False, "timezone": DISPLAY_TIMEZONE,
        "rationale": f"Moved from {_clock(block['start_dt'])}: {reason}.", "ref": "move-1", "status": "proposed",
        "replaces_event_id": block["promoted_event_id"],
    }
    out["proposals"] = [proposal]
    if dry_run:
        return out
    now_iso = _utc_iso()
    _supersede_pending_moves(block["promoted_event_id"], now_iso)
    run_id = _new_run(now_iso, now, window_end, backend="code", model="routine")
    ids = db.insert_proposals(run_id, [proposal], now_iso)
    db.finish_optimizer_run(run_id, status="succeeded", finished_at=now_iso, proposed_count=1, invalid_count=0,
                            notes=f"{goal['goal']}: move proposed ({reason})")
    out.update(run_id=run_id, proposal_ids=ids)
    return out


def _routine_new(goal: dict, window_start: dt.datetime, window_end: dt.datetime, now: dt.datetime,
                 dry_run: bool) -> dict[str, Any]:
    """A new routine goal: its occurrences for the rest of the week, by the expander, as pending proposals."""
    pending = []
    for p in db.get_proposals(status="proposed"):
        if p["item_type"] == "event" or p.get("all_day"):
            continue
        try:
            pending.append((db.parse_event_time(p["start"], False), db.parse_event_time(p["end"], False)))
        except (ValueError, TypeError):
            continue
    summary = routine.expand(window_start, window_end, now=now, dry_run=dry_run, only_goal_ids={goal["id"]},
                             count_pending=True, run_kind=RUN_KIND, busy_extra=pending)
    out = {"item": goal["goal"], "method": "code", "placed": summary["placed"] > 0, "dry_run": dry_run,
           "blocks": summary.get("blocks", []), "run_id": summary.get("run_id")}
    if summary.get("run_id"):
        for proposal_id in summary.get("proposal_ids", []):
            with db.get_conn() as conn:
                conn.execute("UPDATE proposed_events SET rationale = 'New in Notion. ' || COALESCE(rationale, '') WHERE id = ?",
                             (proposal_id,))
    if not out["placed"]:
        out["note"] = "nothing due this week" if not summary.get("unplaced") else "no free slot this week; it waits for Sunday"
    return out


def _movable_blocks(window_start: dt.datetime, window_end: dt.datetime, items_by_id: dict[str, dict],
                    exclude_event_id: str | None) -> list[dict]:
    """Approved goal/task blocks in the window the model may displace, with what they are for."""
    out = []
    for b in db.get_promoted_plan_blocks(window_start, window_end):
        if b["item_type"] not in ("goal", "task") or b["promoted_event_id"] == exclude_event_id:
            continue
        if b["item_id"] in optimizer.BUILTIN_IDS:
            continue
        item = items_by_id.get(b["item_id"]) or {}
        out.append({
            "event_id": b["promoted_event_id"], "item_type": b["item_type"], "item_id": b["item_id"],
            "title": b.get("event_title") or b.get("title"), "start": b["start_dt"].isoformat(), "end": b["end_dt"].isoformat(),
            "priority": item.get("priority") or "", "deadline": item.get("deadline") or "",
            "frequency": item.get("frequency") or "", "telos": item.get("telos") or "",
            "has_travel": bool(db.get_travel_blocks_for(b)),
        })
    return out


def _model_place(item: dict, window_start: dt.datetime, window_end: dt.datetime, now: dt.datetime, reason: str,
                 block: dict | None, dry_run: bool) -> dict[str, Any]:
    """Place one flexible goal or task (or move its clashing block) with one small model call."""
    # Pending plan blocks count as taken, as in the routine paths: approving
    # both the plan and this placement must never double-book (event options
    # are alternatives, not plans, so they don't count).
    pending = [p for p in db.get_proposals(status="proposed") if p["item_type"] != "event" and not p.get("all_day")]
    payload, fixed = optimizer.build_payload(window_start, window_end, pending_fixed=pending)
    item_type = item["type"]
    if item_type == "task":
        entry = next((t for t in payload["tasks"] if t["id"] == item["id"]), None)
    else:
        entry = next((g for g in payload["goals"] if g["id"] == item["id"]), None)
    if entry is None:
        return {"item": _item_label(item), "method": "model", "placed": False, "note": "the item is not open for planning"}
    items_by_id = {t["id"]: t for t in db.get_schedulable_items()}
    exclude = block["promoted_event_id"] if block else None
    movable = _movable_blocks(window_start, window_end, items_by_id, exclude)
    movable_ids = {m["event_id"] for m in movable}
    if block:
        movable_ids.add(block["promoted_event_id"])
    if block and item_type == "goal":
        # The clashing occurrence is the one to re-place; the others stay where they are.
        entry = {**entry, "already_scheduled_event_ids": [e for e in entry.get("already_scheduled_event_ids", [])
                                                           if e != block["promoted_event_id"]]}
    payload = {
        **{k: payload[k] for k in ("generated_at", "timezone", "preferences", "calendar", "context_goals", "person")},
        "planning_window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "reason": reason,
        "item_to_place": {"item_type": item_type, **entry},
        "block_to_move": ({"event_id": block["promoted_event_id"], "start": block["start_dt"].isoformat(),
                           "end": block["end_dt"].isoformat(), "title": block.get("event_title") or block.get("title")}
                          if block else None),
        "movable_blocks": movable,
        "tasks": [entry] if item_type == "task" else [],
        "goals": [entry] if item_type == "goal" else [],
    }
    # The validator needs every item a move may be proposed for.
    validation_payload = {**payload,
                          "tasks": payload["tasks"] + [t for t in items_by_id.values() if t["type"] == "task"],
                          "goals": payload["goals"] + [g for g in items_by_id.values() if g["type"] == "goal"]}
    user = json.dumps(payload, separators=(",", ":"))
    system = optimizer.load_prompt_file(placement_prompt_path(), SUBMIT_TOOL_NAME, "LLM_PLACEMENT_PROMPT_PATH")
    model = llm_client._env("LLM_MODEL", llm_client.DEFAULT_MODEL) or llm_client.DEFAULT_MODEL
    backend = llm_client.get_backend()
    out: dict[str, Any] = {"item": _item_label(item), "method": "model", "backend": backend, "model": model,
                           "movable": len(movable), "dry_run": dry_run}
    if dry_run:
        tokens, method = llm_client.estimate_tokens(system, user, PROPOSED_EVENTS_SCHEMA, model=model,
                                                    tool_name=SUBMIT_TOOL_NAME, tool_description=SUBMIT_TOOL_DESCRIPTION)
        out.update(placed=None, input_tokens_estimate=tokens, estimate_method=method)
        return out
    now_iso = _utc_iso()
    run_id = _new_run(now_iso, window_start, window_end, backend=backend, model=model, system=system, payload=user)
    clock = time.monotonic()
    try:
        result = llm_client.complete_structured(system, user, PROPOSED_EVENTS_SCHEMA, effort="low",
                                                max_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                                                tool_name=SUBMIT_TOOL_NAME, tool_description=SUBMIT_TOOL_DESCRIPTION)
    except llm_client.LLMError as exc:
        db.finish_optimizer_run(run_id, status="failed", finished_at=_utc_iso(), error=f"{type(exc).__name__}: {exc}")
        raise
    events = result.data["events"]
    if block:
        # A conflict move: the item's own proposal is the replacement, said or not.
        for e in events:
            if e["item_type"] == item_type and e["item_id"] == item["id"] and not e.get("replaces_event_id"):
                e["replaces_event_id"] = block["promoted_event_id"]
                break
    for e in events:
        prefix = f"Moved from {_clock(block['start_dt'])}: {reason}. " if block and e.get("replaces_event_id") == exclude else (
            f"Moved to make room for {_item_label(item)}. " if e.get("replaces_event_id") else
            (f"{reason}. " if e["item_type"] in (item_type,) and e["item_id"] == item["id"] else ""))
        e["rationale"] = (prefix + (e.get("rationale") or "")).strip()
    proposals = optimizer.validate_proposals(events, validation_payload, window_start, window_end, fixed, movable_ids=movable_ids)
    now_iso = _utc_iso()
    if block:
        _supersede_pending_moves(block["promoted_event_id"], now_iso)
    db.insert_proposals(run_id, proposals, now_iso)
    usage = result.usage or {}
    proposed = sum(1 for p in proposals if p["status"] == "proposed")
    db.finish_optimizer_run(run_id, status="succeeded", finished_at=now_iso,
                            input_tokens=sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")) or None,
                            output_tokens=usage.get("output_tokens"), cost_usd=result.cost_usd,
                            proposed_count=proposed, invalid_count=len(proposals) - proposed, notes=result.data.get("notes"))
    out.update(run_id=run_id, placed=proposed > 0, proposed=proposed, invalid=len(proposals) - proposed,
               invalid_reasons=[p["invalid_reason"] for p in proposals if p["status"] == "invalid"],
               unscheduled=result.data.get("unscheduled", []), notes=result.data.get("notes", ""),
               elapsed_s=round(time.monotonic() - clock, 1))
    if not out["placed"]:
        out["note"] = "; ".join(u.get("reason", "") for u in out["unscheduled"]) or "no slot this week; it waits for Sunday"
    return out


def place_item(item_type: str, item_id: str, *, reason: str, block: dict | None = None,
               now: dt.datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
    """
    Place one task or goal in the rest of the week (next week on a Sunday) as
    pending proposals. With `block` (a promoted proposal row from
    db.get_promoted_plan_blocks), re-place that clashing occurrence instead:
    the proposal carries replaces_event_id. Routine goals are placed by code,
    everything else by one small model call. Raises llm_client.LLMError when
    that call fails.
    """
    now = _now(now)
    item = db.get_schedulable_item(item_type, item_id)
    if item is None:
        return {"item": item_id, "placed": False, "note": "not an open task or goal"}
    window_start, window_end = optimizer.plan_window(now)
    if block:
        window_end = max(window_end, week_end_after(block["start_dt"]))
    if item_type == "goal" and routine.goal_type(item) == routine.ROUTINE:
        return _routine_move(block, item, window_end, now, reason, dry_run) if block else _routine_new(item, window_start, window_end, now, dry_run)
    return _model_place(item, window_start, window_end, now, reason, block, dry_run)


# --- the hourly Notion check -----------------------------------------------------------------


def notion_check(*, now: dt.datetime | None = None, dry_run: bool = False, notify: bool = True) -> dict[str, Any]:
    """
    Re-read Notion and act on what changed: new rows are placed (pending),
    deleted rows' blocks leave the calendar. Edits to existing rows wait for
    Sunday. Ends with the change email when anything is waiting.
    """
    import sync_notion  # lazy: needs NOTION_* set

    now = _now(now)
    before_tasks = db.get_active_source_task_ids("notion")
    before_goals = db.get_active_source_goal_ids("notion")
    first_sync = not before_tasks and not before_goals
    summary: dict[str, Any] = {"new": [], "deleted": [], "placements": [], "errors": [], "dry_run": dry_run}
    try:
        sync_notion.sync_notion()
    except Exception as exc:  # noqa: BLE001 -- Notion down: try again next hour
        logger.warning("notion check: sync failed: %s", exc)
        summary["errors"].append(f"sync: {type(exc).__name__}: {exc}")
        return summary
    after_tasks = db.get_active_source_task_ids("notion")
    after_goals = db.get_active_source_goal_ids("notion")
    if first_sync:
        summary["note"] = "first sync on this database: nothing counts as new"
        return summary
    now_iso = _utc_iso()
    for item_type, before, after in (("task", before_tasks, after_tasks), ("goal", before_goals, after_goals)):
        for source_id in sorted(before - after):
            item_id = db.sanitize_id(f"notion_{source_id}")
            removed = {"pending": [], "approved": []} if dry_run else db.remove_item_blocks(item_type, item_id, now_iso)
            summary["deleted"].append({"item_type": item_type, "item_id": item_id, **removed})
        for source_id in sorted(after - before):
            item_id = db.sanitize_id(f"notion_{source_id}")
            summary["new"].append({"item_type": item_type, "item_id": item_id})
            try:
                summary["placements"].append(place_item(item_type, item_id, reason="New in Notion", now=now, dry_run=dry_run))
            except llm_client.LLMError as exc:
                logger.error("notion check: could not place %s %s: %s", item_type, item_id, exc)
                summary["errors"].append(f"{item_type} {item_id}: {type(exc).__name__}: {exc}")
    if summary["deleted"] and not dry_run:
        run_id = _new_run(now_iso, now, now, backend="code", model="notion")
        names = ", ".join(f"{d['item_type']} {d['item_id']}" for d in summary["deleted"])
        db.finish_optimizer_run(run_id, status="succeeded", finished_at=now_iso, proposed_count=0, invalid_count=0,
                                notes=f"Removed from the calendar (deleted in Notion): {names}")
    if notify and not dry_run:
        import notifier

        summary["email"] = notifier.notify_changes()
    return summary


# --- the conflict check ---------------------------------------------------------------------


def _overlaps(a_start: dt.datetime, a_end: dt.datetime, b_start: dt.datetime, b_end: dt.datetime) -> bool:
    return a_start < b_end and a_end > b_start


def _has_open_move(event_id: str, since: str | None) -> bool:
    """A pending move for the block, or one the person rejected since the clash was found (= keep it)."""
    for p in db.get_moves_for(event_id):
        if p["status"] == "proposed":
            return True
        if p["status"] == "rejected" and since and (p.get("created_at") or "") >= since:
            return True
    return False


def conflict_check(*, now: dt.datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
    """
    Flag approved blocks that a synced timed event now overlaps, propose a
    move for goal/task blocks (routine by code, the rest by the model), and
    clear flags whose clash has gone. All-day entries never conflict.
    """
    now = _now(now)
    horizon = now + dt.timedelta(days=CONFLICT_LOOKAHEAD_DAYS)
    fixed = [e for e in db.get_events_between(now, horizon) if e["source"] != db.OPTIMIZER_SOURCE and not e["all_day"]]
    summary: dict[str, Any] = {"checked": 0, "conflicts": [], "cleared": [], "proposed": [], "errors": [], "dry_run": dry_run}
    by_ref: dict[tuple[str, str], dict] = {}
    blocks = db.get_promoted_plan_blocks(now, horizon)
    for b in blocks:
        if b.get("ref"):
            by_ref[(b["run_id"], b["ref"])] = b
    fixed.sort(key=lambda e: e["start_dt"])
    for b in blocks:
        if b.get("event_all_day"):
            continue
        summary["checked"] += 1
        clash = next((e for e in fixed if _overlaps(b["start_dt"], b["end_dt"], e["start_dt"], e["end_dt"])), None)
        if clash is None:
            if b.get("conflict_event_id") and not dry_run:
                db.set_conflict(b["id"], None, None)
                _supersede_pending_moves(b["promoted_event_id"], _utc_iso())
                summary["cleared"].append(b["id"])
            continue
        title = clash.get("title") or clash["id"]
        same = b.get("conflict_event_id") == clash["id"]
        found_at = b.get("conflict_found_at") if same else _utc_iso()  # when this clash began
        if not same and not dry_run:
            db.set_conflict(b["id"], clash["id"], found_at)
        summary["conflicts"].append({"proposal_id": b["id"], "title": b.get("event_title") or b.get("title"),
                                     "with": title, "item_type": b["item_type"], "new": not same})
        # Which block gets re-placed: a travel block's destination, else the block itself.
        target = b
        if b["item_type"] == "travel":
            target = by_ref.get((b["run_id"], b.get("item_id") or ""))
            if target is None or target.get("event_all_day"):
                continue
        if target["item_type"] not in ("goal", "task") or target["item_id"] in optimizer.BUILTIN_IDS:
            continue
        if _has_open_move(target["promoted_event_id"], found_at):
            continue
        # One attempt per clash: when the model found no valid move (or failed),
        # asking again every 5 minutes would only spend tokens; Sunday's plan or
        # a change in the clash takes it up again.
        if found_at and (target.get("move_attempted_at") or "") >= found_at:
            continue
        if dry_run:
            summary["proposed"].append({"proposal_id": target["id"], "would": "propose a move"})
            continue
        db.set_move_attempted(target["id"], _utc_iso())
        try:
            summary["proposed"].append(place_item(target["item_type"], target["item_id"], reason=f"{title} landed on it",
                                                  block=target, now=now))
        except llm_client.LLMError as exc:
            logger.error("conflict check: could not re-place %s: %s", target["id"], exc)
            summary["errors"].append(f"{target['id']}: {type(exc).__name__}: {exc}")
    return summary


# --- CLI -------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["notion", "conflicts", "place"])
    parser.add_argument("item_type", nargs="?", choices=["task", "goal"])
    parser.add_argument("item_id", nargs="?")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db.init_db()
    try:
        if args.command == "notion":
            out = notion_check(dry_run=args.dry_run)
        elif args.command == "conflicts":
            out = conflict_check(dry_run=args.dry_run)
        else:
            if not (args.item_type and args.item_id):
                parser.error("place needs: task|goal <id>")
            out = place_item(args.item_type, args.item_id, reason="Placed by hand", dry_run=args.dry_run)
    except llm_client.LLMError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, indent=2))
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
