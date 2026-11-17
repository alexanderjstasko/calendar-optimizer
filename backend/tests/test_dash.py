"""GET /dash/day: one New York day for the Daily Dash (dash.py)."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import HTTPException

import db
import main

NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc).isoformat()
DAY = "2026-10-21"  # a Wednesday: default hours wake 07:00, bed 23:00, free from 17:00


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()


def _node(node_id, title, level, parent=None):
    db.upsert_tree_node({
        "source": "notion", "source_node_id": node_id, "title": title, "level": level,
        "parent_id": f"notion_{parent}" if parent else None, "status": "In-Progress", "priority": None,
        "location": None, "notes_for_llm": None, "target_date": None, "hours_per_week": None,
        "time_spent_hr": None, "last_modified": "x", "deleted_at": None, "raw_json": None,
    })


def _tree():
    _node("telos-love", "Love", "Telos")
    _node("goal-friends", "See friends", "Goal", parent="telos-love")
    db.upsert_task({
        "source": "notion", "source_task_id": "t1", "task": "Plan the trip", "time_to_complete_hr": 1.0,
        "time_spent_hr": 0.0, "deadline": None, "priority": "High", "location": None, "status": "Not started",
        "level": "Task", "parent_id": "notion_goal-friends", "last_modified": "x",
    })
    db.upsert_goal({
        "source": "notion", "source_goal_id": "g1", "goal": "Go to talks", "frequency": None, "end_date": None,
        "required_time_hr": 2.0, "priority": None, "location": None, "days_of_week": None,
        "time_of_day_window": None, "show_on_calendar": 1, "notes_for_llm": None, "status": "In-Progress",
        "level": "Routine", "parent_id": "notion_telos-love", "last_modified": "x",
    })


def _sync(source, sid, title, start, end, all_day=False):
    db.upsert_event({
        "source": source, "source_event_id": sid, "calendar_id": f"{source}_default", "title": title,
        "start": start, "end": end, "all_day": all_day, "timezone": db.DISPLAY_TZ, "status": "confirmed",
        "last_modified": "x",
    })


def _block(n, item_type, item_id, title, start, end, **extra):
    run = f"r{n}"
    db.create_optimizer_run(run, NOW, "api", "m", "a", "b")
    [pid] = db.insert_proposals(run, [{
        "item_type": item_type, "item_id": item_id, "title": title, "description": "", "location": "",
        "start": start, "end": end, "all_day": False, "timezone": db.DISPLAY_TZ, "rationale": "",
        "status": "proposed", **extra,
    }], NOW)
    event_id = db.promote_proposal(pid, NOW)
    assert event_id
    return pid, event_id


def test_day_lists_events_with_kind_telos_and_editable():
    _tree()
    _sync("google", "g1", "Dinner", f"{DAY}T19:00:00-04:00", f"{DAY}T20:00:00-04:00")
    _sync("apple", "a1", "Holiday", DAY, "2026-10-22", all_day=True)
    _sync("google", "g2", "Tomorrow", "2026-10-22T09:00:00-04:00", "2026-10-22T10:00:00-04:00")
    _, task_block = _block(1, "task", "notion_t1", "Plan the trip", f"{DAY}T17:30:00-04:00", f"{DAY}T18:30:00-04:00")
    _, travel = _block(2, "travel", "google_g1", "Travel to dinner", f"{DAY}T18:30:00-04:00", f"{DAY}T19:00:00-04:00")
    _, talk = _block(3, "event", "", "A talk", f"{DAY}T21:00:00-04:00", f"{DAY}T22:00:00-04:00", goal_id="notion_g1")

    out = main.dash_day(DAY)
    assert out["date"] == DAY and out["timezone"] == "America/New_York"
    assert out["window"] == {"start": "07:00", "end": "23:00"}
    by_id = {e["id"]: e for e in out["events"]}
    assert set(by_id) == {"google_g1", "apple_a1", task_block, travel, talk}

    dinner = by_id["google_g1"]
    assert (dinner["kind"], dinner["telosId"], dinner["editable"], dinner["allDay"]) == ("synced", None, False, False)
    assert dinner["start"] == f"{DAY}T19:00:00-04:00" and dinner["end"] == f"{DAY}T20:00:00-04:00"
    assert by_id["apple_a1"]["allDay"] and (by_id["apple_a1"]["start"], by_id["apple_a1"]["end"]) == (DAY, "2026-10-22")

    assert by_id[task_block]["kind"] == "block" and by_id[task_block]["telosId"] == "telos-love"
    assert by_id[task_block]["editable"] is True and by_id[task_block]["source"] == "optimizer"
    assert by_id[travel]["kind"] == "travel" and by_id[travel]["telosId"] is None
    assert by_id[talk]["kind"] == "scouted" and by_id[talk]["telosId"] == "telos-love"


def test_free_bands_are_the_gaps_of_half_an_hour_or_more_in_the_free_window():
    _sync("google", "g1", "Dinner", f"{DAY}T19:00:00-04:00", f"{DAY}T20:00:00-04:00")
    _sync("google", "g2", "Call", f"{DAY}T17:10:00-04:00", f"{DAY}T17:40:00-04:00")  # leaves a 10-minute gap
    _sync("google", "g3", "Late", f"{DAY}T22:15:00-04:00", f"{DAY}T23:45:00-04:00")  # runs past bedtime
    _sync("apple", "a1", "Holiday", DAY, "2026-10-22", all_day=True)  # all-day never blocks time
    free = main.dash_day(DAY)["free"]
    assert free == [
        {"start": f"{DAY}T17:40:00-04:00", "end": f"{DAY}T19:00:00-04:00"},
        {"start": f"{DAY}T20:00:00-04:00", "end": f"{DAY}T22:15:00-04:00"},
    ]


def test_settled_blocks_are_not_editable_and_pending_proposals_are_left_out():
    pid, eid = _block(1, "goal", "", "Laundry", f"{DAY}T18:00:00-04:00", f"{DAY}T19:00:00-04:00")
    with db.get_conn() as conn:
        conn.execute("UPDATE proposed_events SET settled_at = ? WHERE id = ?", (NOW, pid))
    db.create_optimizer_run("r9", NOW, "api", "m", "a", "b")
    db.insert_proposals("r9", [{
        "item_type": "goal", "item_id": "", "title": "Pending", "description": "", "location": "",
        "start": f"{DAY}T20:00:00-04:00", "end": f"{DAY}T21:00:00-04:00", "all_day": False,
        "timezone": db.DISPLAY_TZ, "rationale": "", "status": "proposed",
    }], NOW)
    events = main.dash_day(DAY)["events"]
    assert [e["id"] for e in events] == [eid] and events[0]["editable"] is False


def test_weekend_window_runs_to_bedtime_and_a_bad_date_is_422():
    hours_doc = db.get_setting("hours")
    assert hours_doc is None  # defaults: weekends sleep 23:00-08:00
    assert main.dash_day("2026-10-24")["window"] == {"start": "08:00", "end": "23:00"}
    with pytest.raises(HTTPException) as exc:
        main.dash_day("10/24/2026")
    assert exc.value.status_code == 422
