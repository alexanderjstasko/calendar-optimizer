"""slot.py: what a tapped free slot offers, and booking the pick."""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import db
import hours
import main
import slot
from optimizer_schema import DISPLAY_TIMEZONE

NY = ZoneInfo(DISPLAY_TIMEZONE)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()


def _goal(gid, name, **kw):
    row = {"source": "notion", "source_goal_id": gid, "goal": name, "frequency": "1x Week", "end_date": None,
           "required_time_hr": 1.0, "priority": "Medium", "location": "", "days_of_week": None, "time_of_day_window": None,
           "show_on_calendar": 1, "notes_for_llm": None, "last_occurred": None, "goal_type": "Flexible",
           "status": "In progress", "last_modified": "x"}
    row.update(kw)
    db.upsert_goal(row)
    return f"notion_{gid}"


def _task(tid, name, **kw):
    row = {"source": "notion", "source_task_id": tid, "task": name, "time_to_complete_hr": 2.0, "time_spent_hr": 0,
           "deadline": None, "priority": "Medium", "location": "", "status": "Not started", "last_modified": "x"}
    row.update(kw)
    db.upsert_task(row)
    return f"notion_{tid}"


def _discovered(eid, title, start, end=None, **kw):
    row = {"id": eid, "site": "nypl", "title": title, "start": start, "end": end, "all_day": 0, "timezone": DISPLAY_TIMEZONE,
           "location": "NYPL", "url": "https://x", "cost": "Free", "category": "talk", "summary": "", "website_id": None}
    row.update(kw)
    db.upsert_discovered_events([row], None, "2026-10-19T00:00:00+00:00")


SLOT = ("2026-10-24T13:00:00-04:00", "2026-10-24T17:00:00-04:00")  # a Saturday afternoon, four hours


def test_options_lists_fitting_events_and_items_in_the_agreed_order():
    hours.save({**hours.DEFAULT_HOURS, "event_keywords": ["jazz"]})
    _discovered("e-fit", "Afternoon talk", "2026-10-24T14:00:00-04:00", "2026-10-24T15:30:00-04:00")
    _discovered("e-jazz", "Jazz in the park", "2026-10-24T15:00:00-04:00", "2026-10-24T16:30:00-04:00")
    _discovered("e-rec", "Recommended reading", "2026-10-24T13:30:00-04:00", "2026-10-24T14:30:00-04:00")
    db.mark_discovered_recommended(["e-rec"], "2026-10-20T00:00:00+00:00")
    _discovered("e-long", "Runs past the slot", "2026-10-24T16:00:00-04:00", "2026-10-24T18:00:00-04:00")
    _discovered("e-early", "Starts before", "2026-10-24T12:00:00-04:00", "2026-10-24T13:30:00-04:00")
    _discovered("e-noend", "Two hours by default", "2026-10-24T15:30:00-04:00")  # 15:30 + 2 h = 17:30 > slot end
    _discovered("e-decided", "Already approved", "2026-10-24T14:00:00-04:00", "2026-10-24T15:00:00-04:00")
    db.set_discovered_decision("e-decided", "approved")
    soon = _task("t-soon", "Tax return", deadline="2026-10-27", priority="Low")
    high = _task("t-high", "Big report", priority="High")
    _task("t-tiny", "Nearly done", time_to_complete_hr=1.0, time_spent_hr=0.9)   # 0.1 h left: too short
    rec = _task("t-rec", "Recommended task", priority="Low")
    db.create_optimizer_run("run_x", "t", "api", "m", "a", "b")
    db.insert_proposals("run_x", [{"item_type": "task", "item_id": rec, "title": "Recommended task", "start": "2026-10-26T13:00:00-04:00",
                                   "end": "2026-10-26T14:00:00-04:00", "all_day": False, "timezone": DISPLAY_TIMEZONE, "status": "proposed"}], "t")
    climb = _goal("climb", "Rockclimb", required_time_hr=2.0, days_of_week=json.dumps(["Saturday"]), time_of_day_window="Afternoon", priority="High")
    _goal("long", "Hike", required_time_hr=6.0)                                  # longer than the slot
    _goal("weekday", "Money", days_of_week=json.dumps(["Monday"]))              # wrong day
    _goal("morning", "Stretch", time_of_day_window="Morning")                   # wrong time of day
    _goal("culture", "Culture Event", goal_type="Event Interest")              # the scout's, not a block
    _goal("ctx", "Work", show_on_calendar=0)

    out = slot.options(*SLOT)
    assert [e["id"] for e in out["events"]] == ["e-rec", "e-jazz", "e-fit"]
    assert out["events"][1]["keyword_match"] is True and out["events"][0]["recommended"] is True
    assert [i["id"] for i in out["items"]] == [rec, soon, high, climb]
    assert out["items"][1]["duration_hr"] == 2.0 and out["items"][2]["duration_hr"] == 2.0
    assert out["items"][3]["kind"] == "goal" and out["items"][3]["duration_hr"] == 2.0

    client = TestClient(main.app)
    r = client.get("/slot/options", params={"start": SLOT[0], "end": SLOT[1]})
    assert r.status_code == 200 and len(r.json()["events"]) == 3
    assert client.get("/slot/options", params={"start": SLOT[1], "end": SLOT[0]}).status_code == 422


def test_place_books_the_pick_at_once_and_refuses_overlaps():
    tid = _task("t1", "Tax return")
    out = slot.place("task", tid, "2026-10-24T13:00:00-04:00", "2026-10-24T15:00:00-04:00")
    assert out["event"]["title"] == "Tax return" and out["event"]["calendarId"] == "optimizer"
    p = db.get_proposal(out["proposal_id"])
    assert p["status"] == "promoted" and p["rationale"] == slot.RATIONALE
    assert db.get_optimizer_run(p["run_id"])["kind"] == "manual"
    assert json.loads(db.get_tasks()[0]["scheduled_event_ids"]) == [out["event_id"]]
    assert db.count_decisions() == 1  # the person's own choice is a decision
    with pytest.raises(ValueError, match="overlaps 'Tax return'"):
        slot.place("task", tid, "2026-10-24T14:00:00-04:00", "2026-10-24T16:00:00-04:00")
    with pytest.raises(LookupError):
        slot.place("goal", "notion_nope", "2026-10-24T16:00:00-04:00", "2026-10-24T17:00:00-04:00")

    _discovered("e1", "Afternoon talk", "2026-10-24T16:00:00-04:00", "2026-10-24T17:00:00-04:00")
    client = TestClient(main.app)
    r = client.post("/slot/place", json={"kind": "event", "id": "e1", "start": "2026-10-24T16:00:00-04:00", "end": "2026-10-24T17:00:00-04:00"})
    assert r.status_code == 200 and r.json()["event"]["title"] == "Afternoon talk"
    assert db.get_discovered_event("e1")["decision"] == "approved"
    assert client.post("/slot/place", json={"kind": "event", "id": "nope", "start": SLOT[0], "end": SLOT[1]}).status_code == 404
