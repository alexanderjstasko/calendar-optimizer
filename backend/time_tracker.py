"""
time_tracker.py — writes back to Notion what the optimizer's approved events
did, once they have actually happened:

  * task blocks   -> hours added to the task's "Time Spent" (or the older
                     "Time Spent (hr)"; whichever the page has)
  * goal events   -> the routine's "Last Occurred" date moved forward to the
                     occurrence date (never backwards; a later date already
                     in Notion is left alone) and its "Sessions" count +1;
                     a Goal/Project planned as sessions (goals.level is Goal
                     or Project) also gets the hours added to its "Time Spent"
  * scout events  -> the website's "Events Attended" count +1, its "Upcoming
                     Event" text cleared if it named this event, the served
                     goal's "Last Occurred" moved forward, and the discovered
                     event marked `attended` (the scout's preference signal)

This is the only place the system writes to Notion, and it is deliberately
narrow:

  * Only promoted `task`, `goal` and `event` proposals count (travel and free
    blocks never do), and only once their end time is in the past.
  * Each block is settled exactly once: proposed_events.settled_at /
    settled_hours are stamped after a successful write, and the query that
    finds work skips stamped rows, so re-running is safe.
  * The write touches exactly one property. The current value is read fresh
    from Notion immediately before adding this block's hours, the PATCH body
    names only "Time Spent (hr)", the property must already exist as a
    number, and the value Notion reports back is checked against what was
    written. Any failure leaves the block unsettled for the next pass.
  * A block whose calendar event was cancelled or soft-deleted is settled
    with 0 hours and never written.

Runs from APScheduler every 15 minutes (see main.py), from
POST /tasks/settle-time, or by hand:

    python3 time_tracker.py --dry-run     # show what would be written
    python3 time_tracker.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import threading
from typing import Any
from zoneinfo import ZoneInfo

import db
import notion_adapter

logger = logging.getLogger(__name__)

DISPLAY_TZ = ZoneInfo(db.DISPLAY_TZ)
TIME_SPENT_PROP = notion_adapter.TIME_SPENT_PROP
TIME_SPENT_PROPS = notion_adapter.TIME_SPENT_PROPS
SESSIONS_PROP = notion_adapter.SESSIONS_PROP
LAST_OCCURRED_PROP = notion_adapter.LAST_OCCURRED_PROP
EVENTS_ATTENDED_PROP = notion_adapter.EVENTS_ATTENDED_PROP
UPCOMING_EVENT_PROP = notion_adapter.UPCOMING_EVENT_PROP
NODE_LEVELS = notion_adapter.LEVEL_TREE  # a goals row at one of these levels is a Goal/Project planned as sessions


# Two passes at once (the 15-minute job and POST /tasks/settle-time) would both
# read the same unsettled blocks and both add their hours.
_SETTLE_LOCK = threading.Lock()


def _hours_between(start: dt.datetime, end: dt.datetime) -> float:
    return round((end - start).total_seconds() / 3600.0, 2)


def _time_spent_prop(page_id: str) -> str:
    """Which name the page uses for its hours ("Time Spent" since 2026-10-26, "Time Spent (hr)" before)."""
    return notion_adapter.resolve_property_name(page_id, TIME_SPENT_PROPS, "number")


def _add_once(proposal_id: str, key: str, page_id: str, prop_name: str, delta: float, *, whole: bool = False) -> tuple[Any, float]:
    """
    Add `delta` to a Notion number property exactly once for this block, even
    across retries: the value about to be written is recorded on the proposal
    first, so when a pass fails after Notion took the write (a timeout on the
    reply, a locked database before the block was marked settled), the next
    pass finds that value already there and adds nothing. Returns (before,
    after); raises when Notion does not confirm the write.
    """
    current = notion_adapter.read_number_property(page_id, prop_name)
    target = db.get_settle_target(proposal_id, key)
    if target is not None and current is not None and abs(float(current) - float(target)) < 1e-6:
        return None, float(current)  # an earlier pass's write landed
    new_value = int((current or 0) + delta) if whole else round((current or 0.0) + delta, 2)
    db.set_settle_target(proposal_id, key, new_value)
    written = notion_adapter.update_number_property(page_id, prop_name, new_value)
    if written is None or abs(float(written) - new_value) > 1e-6:
        raise RuntimeError(f"Notion reports {written!r} after writing {new_value} to {prop_name}")
    return current, new_value


def _bump_sessions(page_id: str, goal_id: str, entry: dict[str, Any], dry_run: bool) -> None:
    """
    Add 1 to the routine's "Sessions" after a block has happened. Best effort:
    the block is settled on Last Occurred alone, so a failure here is logged
    (and noted in the summary entry) rather than retried, which would double
    count once the first write had landed.
    """
    try:
        current = notion_adapter.read_number_property(page_id, SESSIONS_PROP)
        new_count = int((current or 0) + 1)
        entry.update(sessions_before=current, sessions_after=new_count)
        if dry_run:
            return
        written = notion_adapter.update_number_property(page_id, SESSIONS_PROP, new_count)
        if written is None or int(written) != new_count:
            raise RuntimeError(f"Notion reports {written!r} after writing {new_count}")
        db.set_goal_sessions(goal_id, new_count)
    except Exception as exc:  # noqa: BLE001 -- property missing on an older page, network
        entry["sessions_error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("time_tracker: could not add a session on %s: %s", page_id, exc)


def settle_task_time(now: dt.datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
    """
    One settlement pass over task blocks and goal occurrences. Returns a
    summary with `settled` (written, each entry tagged item_type), `pending`
    (not yet occurred), `skipped` (nothing to write, e.g. cancelled), and
    `errors` (left unsettled, retried next pass). With dry_run=True nothing is
    written anywhere; `settled` then lists what would be written.
    """
    now = now or dt.datetime.now(DISPLAY_TZ)
    now_iso = now.astimezone(dt.timezone.utc).isoformat()
    summary: dict[str, Any] = {"dry_run": dry_run, "checked": 0, "settled": [], "pending": 0, "skipped": [], "errors": []}
    if not _SETTLE_LOCK.acquire(blocking=False):
        summary["busy"] = "another settlement pass is running; nothing done"
        return summary
    try:
        _settle_tasks(now, now_iso, dry_run, summary)
        _settle_goals(now, now_iso, dry_run, summary)
        _settle_events(now, now_iso, dry_run, summary)
    finally:
        _SETTLE_LOCK.release()
    return summary


settle_occurrences = settle_task_time  # clearer name for callers that care about goals too


def _settle_tasks(now: dt.datetime, now_iso: str, dry_run: bool, summary: dict[str, Any]) -> None:
    for p in db.get_unsettled_task_proposals():
        summary["checked"] += 1
        label = {"proposal_id": p["id"], "item_type": "task", "task": p["task_title"], "start": p["start"], "end": p["end"]}

        if p["all_day"]:
            summary["skipped"].append({**label, "reason": "all-day block has no duration"})
            if not dry_run:
                db.mark_proposal_settled(p["id"], now_iso, 0.0)
            continue
        if p["task_source"] != "notion":
            summary["skipped"].append({**label, "reason": f"task source {p['task_source']!r} has no Notion page"})
            continue

        start = db.parse_event_time(p["start"], p["all_day"])
        end = db.parse_event_time(p["end"], p["all_day"])
        if end > now:
            summary["pending"] += 1
            continue

        if p["event_deleted_at"] or p["event_status"] == "cancelled":
            summary["skipped"].append({**label, "reason": "calendar event was cancelled; 0 hours"})
            if not dry_run:
                db.mark_proposal_settled(p["id"], now_iso, 0.0)
            continue

        hours = _hours_between(start, end)
        if hours <= 0:
            summary["skipped"].append({**label, "reason": "zero-length block"})
            if not dry_run:
                db.mark_proposal_settled(p["id"], now_iso, 0.0)
            continue

        page_id = p["source_task_id"]
        try:
            prop_name = _time_spent_prop(page_id)
            if dry_run:
                current = notion_adapter.read_number_property(page_id, prop_name)
                summary["settled"].append({**label, "hours": hours, "notion_before": current,
                                           "notion_after": round((current or 0.0) + hours, 2), "property": prop_name})
                continue
            current, new_value = _add_once(p["id"], "time_spent", page_id, prop_name, hours)
            entry = {**label, "hours": hours, "notion_before": current, "notion_after": new_value, "property": prop_name}
            if not db.mark_proposal_settled(p["id"], now_iso, hours):
                # Someone settled it concurrently; the Notion write already happened, so say so loudly.
                logger.error("proposal %s was settled concurrently; Notion may now double count %s h", p["id"], hours)
            db.set_task_time_spent(p["item_id"], new_value)
            summary["settled"].append(entry)
            logger.info("time_tracker: %s +%sh -> %s = %s", p["task_title"], hours, prop_name, new_value)
        except Exception as exc:  # network, auth, property missing/mistyped, verification mismatch
            logger.warning("time_tracker: could not settle %s (%s): %s", p["id"], p["task_title"], exc)
            summary["errors"].append({**label, "error": f"{type(exc).__name__}: {exc}"})


def _settle_goals(now: dt.datetime, now_iso: str, dry_run: bool, summary: dict[str, Any]) -> None:
    for p in db.get_unsettled_goal_proposals():
        summary["checked"] += 1
        label = {"proposal_id": p["id"], "item_type": "goal", "goal": p["goal_title"], "start": p["start"], "end": p["end"]}

        if p["goal_source"] != "notion":
            summary["skipped"].append({**label, "reason": f"goal source {p['goal_source']!r} has no Notion page"})
            continue

        start = db.parse_event_time(p["start"], p["all_day"])
        end = db.parse_event_time(p["end"], p["all_day"])
        if p["all_day"] and end <= start:
            end = start + dt.timedelta(days=1)
        if end > now:
            summary["pending"] += 1
            continue

        if p["event_deleted_at"] or p["event_status"] == "cancelled":
            summary["skipped"].append({**label, "reason": "calendar event was cancelled; not an occurrence"})
            if not dry_run:
                db.mark_proposal_settled(p["id"], now_iso, 0.0)
            continue

        occurred_on = start.strftime("%Y-%m-%d")
        hours = _hours_between(start, end)
        page_id = p["source_goal_id"]
        # A Goal/Project planned as sessions (notion_adapter.node_plan_kind) also logs its
        # hours to the page's Time Spent, like a task, so Hours Logged and hours_remaining move.
        logs_hours = (p.get("goal_level") or "") in NODE_LEVELS and hours > 0
        try:
            current = notion_adapter.read_date_property(page_id, LAST_OCCURRED_PROP)
            current_day = (current or "")[:10]
            entry = {**label, "occurred_on": occurred_on, "notion_before": current}
            if current_day >= occurred_on:
                # Notion already records this or a later occurrence: nothing to write,
                # but the block is done and must not be re-examined every pass.
                entry.update(notion_after=current, note="Notion already holds a later or equal date; not written")
                mirrored = current_day or None
            else:
                entry["notion_after"] = occurred_on
                mirrored = occurred_on
                if not dry_run:
                    written = notion_adapter.update_date_property(page_id, LAST_OCCURRED_PROP, occurred_on)
                    if (written or "")[:10] != occurred_on:
                        raise RuntimeError(f"Notion reports {written!r} after writing {occurred_on}")
            if logs_hours:
                # Raises on failure: the block stays unsettled and the pass is retried. Last
                # Occurred never moves backwards, so redoing it then is harmless.
                _log_goal_hours(p["id"], page_id, p["item_id"], hours, entry, dry_run)
            if not dry_run:
                if not db.mark_proposal_settled(p["id"], now_iso, hours):
                    logger.error("proposal %s was settled concurrently after writing %s", p["id"], LAST_OCCURRED_PROP)
                db.set_goal_last_occurred(p["item_id"], mirrored)
            _bump_sessions(page_id, p["item_id"], entry, dry_run)
            summary["settled"].append(entry)
            if entry.get("notion_after") == occurred_on and not dry_run:
                logger.info("time_tracker: %s -> %s = %s", p["goal_title"], LAST_OCCURRED_PROP, occurred_on)
        except Exception as exc:  # network, auth, property missing/mistyped, verification mismatch
            logger.warning("time_tracker: could not settle %s (%s): %s", p["id"], p["goal_title"], exc)
            summary["errors"].append({**label, "error": f"{type(exc).__name__}: {exc}"})


def _log_goal_hours(proposal_id: str, page_id: str, goal_id: str, hours: float, entry: dict[str, Any], dry_run: bool) -> None:
    """Add a session's hours to a Goal/Project page's Time Spent (once per block, verified)."""
    prop_name = _time_spent_prop(page_id)
    if dry_run:
        current = notion_adapter.read_number_property(page_id, prop_name)
        entry.update(hours=hours, time_spent_before=current, time_spent_after=round((current or 0.0) + hours, 2),
                     time_spent_property=prop_name)
        return
    current, new_value = _add_once(proposal_id, "time_spent", page_id, prop_name, hours)
    entry.update(hours=hours, time_spent_before=current, time_spent_after=new_value, time_spent_property=prop_name)
    db.set_goal_time_spent(goal_id, new_value)


def _advance_last_occurred(page_id: str, occurred_on: str) -> tuple[str | None, bool]:
    """Move a goal's Last Occurred forward to occurred_on unless Notion already holds a later or equal date."""
    current = notion_adapter.read_date_property(page_id, LAST_OCCURRED_PROP)
    if (current or "")[:10] >= occurred_on:
        return current, False
    written = notion_adapter.update_date_property(page_id, LAST_OCCURRED_PROP, occurred_on)
    if (written or "")[:10] != occurred_on:
        raise RuntimeError(f"Notion reports {written!r} after writing {occurred_on}")
    return written, True


def _settle_events(now: dt.datetime, now_iso: str, dry_run: bool, summary: dict[str, Any]) -> None:
    for p in db.get_unsettled_event_proposals():
        summary["checked"] += 1
        label = {"proposal_id": p["id"], "item_type": "event", "event": p["title"], "start": p["start"], "end": p["end"]}

        start = db.parse_event_time(p["start"], p["all_day"])
        end = db.parse_event_time(p["end"], p["all_day"])
        if p["all_day"] and end <= start:
            end = start + dt.timedelta(days=1)
        if end > now:
            summary["pending"] += 1
            continue

        if p["event_deleted_at"] or p["event_status"] == "cancelled":
            summary["skipped"].append({**label, "reason": "calendar event was cancelled; not attended"})
            if not dry_run:
                db.mark_proposal_settled(p["id"], now_iso, 0.0)
                if p.get("discovered_event_id"):
                    db.set_discovered_decision(p["discovered_event_id"], "rejected")
            continue

        occurred_on = start.strftime("%Y-%m-%d")
        hours = _hours_between(start, end)
        entry = {**label, "occurred_on": occurred_on}
        try:
            if p.get("website_source") == "notion" and p.get("source_website_id"):
                page_id = p["source_website_id"]
                upcoming = notion_adapter.read_text_property(page_id, UPCOMING_EVENT_PROP)
                clear_upcoming = bool(upcoming) and p["title"] in upcoming
                entry["clears_upcoming"] = clear_upcoming
                if dry_run:
                    current = notion_adapter.read_number_property(page_id, EVENTS_ATTENDED_PROP)
                    entry.update(attended_before=current, attended_after=int((current or 0) + 1))
                else:
                    current, new_count = _add_once(p["id"], "events_attended", page_id, EVENTS_ATTENDED_PROP, 1, whole=True)
                    new_count = int(new_count)
                    entry.update(attended_before=current, attended_after=new_count)
                    if clear_upcoming:
                        notion_adapter.update_text_property(page_id, UPCOMING_EVENT_PROP, "")
                    db.set_website_notion_mirror(p["website_id"], events_attended=new_count,
                                                 upcoming_event="" if clear_upcoming else None)
            if p.get("goal_source") == "notion" and p.get("source_goal_id"):
                if dry_run:
                    entry["goal_last_occurred"] = occurred_on
                else:
                    written, changed = _advance_last_occurred(p["source_goal_id"], occurred_on)
                    entry["goal_last_occurred"] = written
                    db.set_goal_last_occurred(p["goal_id"], (written or "")[:10] or None)
                _bump_sessions(p["source_goal_id"], p["goal_id"], entry, dry_run)
            if not dry_run:
                if not db.mark_proposal_settled(p["id"], now_iso, hours):
                    logger.error("proposal %s was settled concurrently after writing to Notion", p["id"])
                if p.get("discovered_event_id"):
                    db.set_discovered_decision(p["discovered_event_id"], "attended")
            summary["settled"].append(entry)
            logger.info("time_tracker: attended %s (%s)", p["title"], occurred_on)
        except Exception as exc:  # network, auth, property missing/mistyped, verification mismatch
            logger.warning("time_tracker: could not settle event %s (%s): %s", p["id"], p["title"], exc)
            summary["errors"].append({**label, "error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would be written; write nothing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db.init_db()
    summary = settle_task_time(dry_run=args.dry_run)
    print(json.dumps(summary, indent=2, default=str))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
