"""POST /events/{id}/move: moving an approved optimizer block from the calendar's event details."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import HTTPException

import db
import main


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()


NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc).isoformat()


def _promoted(start="2026-10-18T11:15:00-04:00", end="2026-10-18T14:15:00-04:00"):
    db.create_optimizer_run("r1", NOW, "api", "m", "a", "b")
    [pid] = db.insert_proposals("r1", [{
        "item_type": "goal", "item_id": "", "title": "Laundry", "description": "", "location": "",
        "start": start, "end": end, "all_day": False, "timezone": db.DISPLAY_TZ, "rationale": "", "status": "proposed",
    }], NOW)
    event_id = db.promote_proposal(pid, NOW)
    assert event_id
    return pid, event_id


def test_move_updates_event_and_proposal_and_keeps_the_first_original_times():
    pid, eid = _promoted()
    out = main.move_event(eid, main.Times(start="2026-10-18T20:15", end="2026-10-18T23:15"))
    assert out["proposal_id"] == pid
    assert out["event"]["start"] == "2026-10-18T20:15:00-04:00[America/New_York]"
    row = db.get_event(eid)
    assert (row["start"], row["end"]) == ("2026-10-18T20:15:00-04:00", "2026-10-18T23:15:00-04:00")
    assert row["timezone"] == db.DISPLAY_TZ and row["last_modified"] and row["status"] == "confirmed"
    p = db.get_proposal(pid)
    assert p["status"] == "promoted" and p["start"] == "2026-10-18T20:15:00-04:00"
    assert p["original_start"] == "2026-10-18T11:15:00-04:00" and p["adjusted_at"]
    # A second move keeps the model's original times, so the learner sees the whole delta.
    main.move_event(eid, main.Times(start="2026-10-19T09:00", end="2026-10-19T12:00"))
    assert db.get_proposal(pid)["original_start"] == "2026-10-18T11:15:00-04:00"
    assert db.get_schedule_x_events()[0]["start"].startswith("2026-10-19T09:00")
    # The move shows up in what the preference learner reads.
    history = db.get_plan_decision_history()
    assert history and history[0]["original_start"] == "2026-10-18T11:15:00-04:00"


def test_move_refuses_synced_events_bad_times_and_unknown_ids():
    db.upsert_event({
        "source": "google", "source_event_id": "g1", "calendar_id": "google_primary", "title": "Tennis",
        "start": "2026-10-18T17:30:00-04:00", "end": "2026-10-18T18:30:00-04:00", "all_day": False,
        "timezone": db.DISPLAY_TZ, "status": "confirmed",
    })
    with pytest.raises(HTTPException) as exc:
        main.move_event("google_g1", main.Times(start="2026-10-18T18:00", end="2026-10-18T19:00"))
    assert exc.value.status_code == 422 and "google" in exc.value.detail
    assert db.get_event("google_g1")["start"] == "2026-10-18T17:30:00-04:00"

    pid, eid = _promoted()
    with pytest.raises(HTTPException) as exc:
        main.move_event(eid, main.Times(start="2026-10-18T12:00", end="2026-10-18T11:00"))
    assert exc.value.status_code == 422
    assert db.get_event(eid)["start"] == "2026-10-18T11:15:00-04:00"

    with pytest.raises(HTTPException) as exc:
        main.move_event("optimizer_nope", main.Times(start="2026-10-18T12:00", end="2026-10-18T13:00"))
    assert exc.value.status_code == 404


def test_move_refuses_settled_and_revoked_blocks():
    pid, eid = _promoted()
    with db.get_conn() as conn:
        conn.execute("UPDATE proposed_events SET settled_at = ? WHERE id = ?", (NOW, pid))
    with pytest.raises(HTTPException) as exc:
        main.move_event(eid, main.Times(start="2026-10-18T12:00", end="2026-10-18T13:00"))
    assert exc.value.status_code == 422 and "Notion" in exc.value.detail

    db.soft_delete_event(db.OPTIMIZER_SOURCE, pid, NOW)  # what revoke does to the calendar row
    with pytest.raises(HTTPException) as exc:
        main.move_event(eid, main.Times(start="2026-10-18T12:00", end="2026-10-18T13:00"))
    assert exc.value.status_code == 404


# --- POST /events/{id}/delete -------------------------------------------------------


def test_delete_takes_the_block_off_and_records_a_rejection():
    pid, eid = _promoted()
    out = main.delete_event(eid)
    assert out == {"proposal_id": pid, "status": "rejected", "event_id": eid, "travel": []}
    row = db.get_event(eid)
    assert row["status"] == "cancelled" and row["deleted_at"]
    p = db.get_proposal(pid)
    assert p["status"] == "rejected" and p["decided_at"]
    assert db.get_schedule_x_events() == []
    # Gone already: a second delete is a 404, and so is a move.
    with pytest.raises(HTTPException) as exc:
        main.delete_event(eid)
    assert exc.value.status_code == 404


def test_delete_takes_attached_travel_with_it():
    db.create_optimizer_run("r1", NOW, "api", "m", "a", "b")
    dest, travel = db.insert_proposals("r1", [
        {"item_type": "goal", "item_id": "", "title": "Swim", "description": "", "location": "Gym",
         "start": "2026-10-18T18:00:00-04:00", "end": "2026-10-18T21:00:00-04:00", "all_day": False,
         "timezone": db.DISPLAY_TZ, "rationale": "", "status": "proposed", "ref": "swim"},
        {"item_type": "travel", "item_id": "swim", "title": "Travel to Swim", "description": "", "location": "",
         "start": "2026-10-18T17:30:00-04:00", "end": "2026-10-18T18:00:00-04:00", "all_day": False,
         "timezone": db.DISPLAY_TZ, "rationale": "", "status": "proposed", "ref": "t1"},
    ], NOW)
    eid = db.promote_proposal(dest, NOW)
    db.promote_proposal(travel, NOW)
    out = main.delete_event(eid)
    assert out["travel"] == [travel]
    assert db.get_proposal(travel)["status"] == "superseded"  # travel is not a decision of its own
    assert db.get_schedule_x_events() == []


def test_delete_refuses_synced_and_settled_blocks():
    db.upsert_event({
        "source": "google", "source_event_id": "g1", "calendar_id": "google_primary", "title": "Tennis",
        "start": "2026-10-18T17:30:00-04:00", "end": "2026-10-18T18:30:00-04:00", "all_day": False,
        "timezone": db.DISPLAY_TZ, "status": "confirmed",
    })
    with pytest.raises(HTTPException) as exc:
        main.delete_event("google_g1")
    assert exc.value.status_code == 422 and "google" in exc.value.detail
    pid, eid = _promoted()
    with db.get_conn() as conn:
        conn.execute("UPDATE proposed_events SET settled_at = ? WHERE id = ?", (NOW, pid))
    with pytest.raises(HTTPException) as exc:
        main.delete_event(eid)
    assert exc.value.status_code == 422 and "Notion" in exc.value.detail
    assert db.get_event(eid)["status"] == "confirmed"  # nothing happened
    with pytest.raises(HTTPException) as exc:
        main.delete_event("optimizer_nope")
    assert exc.value.status_code == 404
