"""triggers.py: the hourly Notion check, the conflict check, one-item placement, moves and their approval."""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import db
import llm_client
import main
import notifier
import optimizer
import triggers
from optimizer_schema import DISPLAY_TIMEZONE

NY = ZoneInfo(DISPLAY_TIMEZONE)
NOW = dt.datetime(2026, 10, 20, 10, 0, tzinfo=NY)  # a Tuesday morning; the week ends Mon Oct 26 00:00


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.setenv("LLM_MODEL", "claude-opus-5")
    monkeypatch.setenv("LLM_HOME_LOCATION", "Brooklyn")
    monkeypatch.setenv("LLM_PREFERENCES_PATH", str(tmp_path / "preferences.md"))
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "")  # emails off: notify_changes reports "skipped"


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
           "deadline": "2026-10-23", "priority": "High", "location": "", "status": "Not started", "last_modified": "x"}
    row.update(kw)
    db.upsert_task(row)
    return f"notion_{tid}"


def _fixed(eid, start, end, title=None, source="google"):
    db.upsert_event({"source": source, "source_event_id": eid, "calendar_id": f"{source}_primary", "title": title or eid,
                     "start": start, "end": end, "all_day": False, "timezone": DISPLAY_TIMEZONE, "last_modified": "x"})
    return f"{source}_{eid}"


def _approved_block(item_type, item_id, title, start, end, ref="p1", run_id="run_plan", travel=None):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if db.get_optimizer_run(run_id) is None:
        db.create_optimizer_run(run_id, now, "api", "m", "a", "b")
    rows = [{"ref": ref, "item_type": item_type, "item_id": item_id, "title": title, "description": "", "location": "",
             "start": start, "end": end, "all_day": False, "timezone": DISPLAY_TIMEZONE, "rationale": "planned", "status": "proposed"}]
    if travel:
        rows.append({"ref": f"{ref}-t", "item_type": "travel", "item_id": ref, "title": f"Travel to {title}", "description": "",
                     "location": "", "start": travel[0], "end": travel[1], "all_day": False, "timezone": DISPLAY_TIMEZONE,
                     "rationale": "travel", "status": "proposed"})
    ids = db.insert_proposals(run_id, rows, now)
    for pid in ids:
        assert db.promote_proposal(pid, now)
    return ids[0]


def _fake_model(monkeypatch, events, captured=None):
    def fake(system, user, schema, **kw):
        if captured is not None:
            captured["system"], captured["payload"], captured["kw"] = system, json.loads(user), kw
        return llm_client.LLMResult(data={"events": events, "unscheduled": [], "notes": "n"}, backend="api",
                                    model="claude-opus-5", usage={"input_tokens": 10, "output_tokens": 5}, cost_usd=0.0)
    monkeypatch.setattr(llm_client, "complete_structured", fake)


def _ev(ref, item_type, item_id, title, start, end, replaces=""):
    return {"ref": ref, "item_type": item_type, "item_id": item_id, "title": title, "description": "", "location": "",
            "start": start, "end": end, "all_day": False, "timezone": DISPLAY_TIMEZONE, "rationale": "fits", "replaces_event_id": replaces}


# --- placing one item --------------------------------------------------------------------


def test_new_routine_goal_is_placed_by_code_as_pending_blocks():
    gid = _goal("floss", "Floss", goal_type="Routine", frequency="Daily", required_time_hr=0.25, last_occurred="2026-10-19")
    out = triggers.place_item("goal", gid, reason="New in Notion", now=NOW)
    assert out["method"] == "code" and out["placed"] is True
    rows = db.get_proposals(run_id=out["run_id"])
    assert [r["status"] for r in rows] == ["proposed"] * len(rows) and len(rows) == 6  # Tue .. Sun
    assert rows[0]["rationale"].startswith("New in Notion. Routine")
    assert db.get_optimizer_run(out["run_id"])["kind"] == "trigger"
    assert db.get_proposals(status="promoted") == []  # nothing applied


def test_new_flexible_item_is_placed_by_one_model_call_with_movable_blocks(monkeypatch):
    tid = _task("t1", "Renew passport")
    gid = _goal("swim", "Swim", priority="Low")
    block = _approved_block("goal", gid, "Swim", "2026-10-21T18:00:00-04:00", "2026-10-21T20:00:00-04:00")
    block_event = db.get_proposal(block)["promoted_event_id"]
    captured = {}
    _fake_model(monkeypatch, [_ev("n1", "task", tid, "Renew passport", "2026-10-22T13:00:00-04:00", "2026-10-22T15:00:00-04:00")], captured)
    out = triggers.place_item("task", tid, reason="New in Notion", now=NOW)
    assert out["method"] == "model" and out["placed"] is True and out["proposed"] == 1
    assert captured["payload"]["item_to_place"]["id"] == tid and captured["payload"]["reason"] == "New in Notion"
    assert captured["payload"]["movable_blocks"][0]["event_id"] == block_event and captured["payload"]["movable_blocks"][0]["priority"] == "Low"
    assert captured["payload"]["block_to_move"] is None
    assert captured["payload"]["planning_window"]["end"] == "2026-10-26T00:00:00-04:00"
    assert captured["kw"]["effort"] == "low" and "movable_blocks" in captured["system"]
    row = db.get_proposals(run_id=out["run_id"])[0]
    assert row["status"] == "proposed" and row["rationale"].startswith("New in Notion. fits")
    assert row["replaces_event_id"] is None


def test_model_may_displace_a_lower_ranked_block_and_approval_applies_the_move(monkeypatch):
    tid = _task("t1", "Tax return", deadline="2026-10-22")
    gid = _goal("swim", "Swim", priority="Low")
    block = _approved_block("goal", gid, "Swim", "2026-10-21T18:00:00-04:00", "2026-10-21T20:00:00-04:00",
                            travel=("2026-10-21T17:30:00-04:00", "2026-10-21T18:00:00-04:00"))
    block_event = db.get_proposal(block)["promoted_event_id"]
    _fake_model(monkeypatch, [
        _ev("n1", "task", tid, "Tax return", "2026-10-21T18:00:00-04:00", "2026-10-21T20:00:00-04:00"),
        _ev("m1", "goal", gid, "Swim", "2026-10-22T18:00:00-04:00", "2026-10-22T20:00:00-04:00", replaces=block_event),
        _ev("bad", "goal", gid, "Swim", "2026-10-23T18:00:00-04:00", "2026-10-23T20:00:00-04:00", replaces="optimizer_nope"),
    ])
    out = triggers.place_item("task", tid, reason="New in Notion", now=NOW)
    rows = {r["ref"]: r for r in db.get_proposals(run_id=out["run_id"])}
    assert rows["n1"]["status"] == "proposed"  # overlaps the block, but the block is being moved in the same answer
    assert rows["m1"]["status"] == "proposed" and rows["m1"]["replaces_event_id"] == block_event
    assert rows["m1"]["rationale"].startswith("Moved to make room for Tax return.")
    assert "not a block that may be moved" in rows["bad"]["invalid_reason"]
    # Nothing has moved yet: the old block and its travel are still on the calendar.
    assert len(db.get_schedule_x_events(calendar_id="optimizer")) == 2

    client = TestClient(main.app)
    r = client.post(f"/optimizer/proposals/{rows['m1']['id']}/approve")
    assert r.status_code == 200 and sorted(r.json()["replaced"]) == sorted([block, f"{block}"[:-1] + "2"])
    old = db.get_proposal(block)
    assert old["status"] == "superseded" and old["replaced_by"] == rows["m1"]["id"]
    assert db.get_proposal(f"{block}"[:-1] + "2")["status"] == "superseded"  # the travel went with it
    shown = db.get_schedule_x_events(calendar_id="optimizer")
    assert [e["title"] for e in shown] == ["Swim"] and shown[0]["start"].startswith("2026-10-22T18:00")
    assert json.loads(db.get_goals()[0]["scheduled_event_ids"]) == [f"optimizer_{rows['m1']['id']}"]


def test_placing_an_unknown_or_done_item_does_nothing():
    assert triggers.place_item("task", "notion_nope", reason="x", now=NOW)["placed"] is False
    tid = _task("done", "Old", status="Done")
    assert triggers.place_item("task", tid, reason="x", now=NOW)["placed"] is False


# --- the hourly Notion check ---------------------------------------------------------------


def test_notion_check_places_new_rows_and_removes_deleted_ones(monkeypatch):
    import sync_notion

    gid = _goal("old", "Old goal")
    block = _approved_block("goal", gid, "Old goal", "2026-10-21T18:00:00-04:00", "2026-10-21T19:00:00-04:00")
    pending_run = "run_pending"
    db.create_optimizer_run(pending_run, "2026-10-19T00:00:00+00:00", "api", "m", "a", "b")
    db.insert_proposals(pending_run, [_ev("q", "goal", gid, "Old goal", "2026-10-23T18:00:00-04:00", "2026-10-23T19:00:00-04:00")], "t")

    def fake_sync():
        db.soft_delete_goal("notion", "old", "2026-10-20T14:00:00+00:00")
        _goal("floss", "Floss", goal_type="Routine", frequency="Daily", required_time_hr=0.25, last_occurred="2026-10-19")

    monkeypatch.setattr(sync_notion, "sync_notion", fake_sync)
    sent = {}
    monkeypatch.setattr(notifier, "notify_changes", lambda **kw: sent.setdefault("called", True) and {"count": 0, "sent": False})
    out = triggers.notion_check(now=NOW)
    assert out["new"] == [{"item_type": "goal", "item_id": "notion_floss"}]
    assert out["deleted"][0]["item_id"] == gid and out["deleted"][0]["approved"] == [block] and out["deleted"][0]["pending"] == [f"{pending_run}_1"]
    assert db.get_proposal(block)["status"] == "superseded" and db.get_schedule_x_events(calendar_id="optimizer") == []
    assert out["placements"][0]["placed"] is True and sent["called"] is True
    assert [r["kind"] for r in db.get_optimizer_runs(limit=2)] == ["trigger", "trigger"]  # the removal note + the placement


def test_notion_check_first_sync_and_sync_failure_do_nothing(monkeypatch):
    import sync_notion

    monkeypatch.setattr(sync_notion, "sync_notion", lambda: _task("t1", "First ever"))
    out = triggers.notion_check(now=NOW, notify=False)
    assert out["new"] == [] and "first sync" in out["note"]

    def boom():
        raise RuntimeError("Notion down")

    monkeypatch.setattr(sync_notion, "sync_notion", boom)
    out = triggers.notion_check(now=NOW, notify=False)
    assert out["errors"] == ["sync: RuntimeError: Notion down"] and out["new"] == []


# --- the conflict check ---------------------------------------------------------------------


def test_conflict_check_flags_a_clash_and_proposes_a_move_once(monkeypatch):
    gid = _goal("floss", "Floss", goal_type="Routine", frequency="Daily", required_time_hr=0.25, last_occurred="2026-10-19")
    block = _approved_block("goal", gid, "Floss", "2026-10-21T18:00:00-04:00", "2026-10-21T18:15:00-04:00")
    block_event = db.get_proposal(block)["promoted_event_id"]
    assert triggers.conflict_check(now=NOW)["conflicts"] == []
    dentist = _fixed("dentist", "2026-10-21T17:30:00-04:00", "2026-10-21T19:00:00-04:00", "Dentist")
    out = triggers.conflict_check(now=NOW)
    assert out["conflicts"] == [{"proposal_id": block, "title": "Floss", "with": "Dentist", "item_type": "goal", "new": True}]
    assert db.get_proposal(block)["conflict_event_id"] == dentist
    move = db.get_proposals(status="proposed")[0]
    assert move["replaces_event_id"] == block_event and move["rationale"].startswith("Moved from Wed 6 PM: Dentist landed on it.")
    assert move["item_id"] == gid and move["start"] != "2026-10-21T18:00-04:00"
    assert not ("2026-10-21T17:30" < move["start"][:16] < "2026-10-21T19:00")  # clear of the dentist
    # The calendar marks the clashing block; the old block is still there until the move is approved.
    shown = db.get_schedule_x_events(calendar_id="optimizer")
    assert shown[0]["conflict"]["title"] == "Dentist" and shown[0]["_options"]["additionalClasses"] == ["has-conflict"]
    # Running again proposes nothing new while the move is pending.
    again = triggers.conflict_check(now=NOW)
    assert again["conflicts"][0]["new"] is False and again["proposed"] == []
    assert len(db.get_proposals(status="proposed")) == 1
    # Rejecting the move = keep the block: no new move for the same clash.
    db.reject_proposal(move["id"], dt.datetime.now(dt.timezone.utc).isoformat())
    assert triggers.conflict_check(now=NOW)["proposed"] == []
    # The clash goes away: the flag is cleared.
    db.soft_delete_event("google", "dentist", "2026-10-20T15:00:00+00:00")
    out = triggers.conflict_check(now=NOW)
    assert out["cleared"] == [block] and db.get_proposal(block)["conflict_event_id"] is None


def test_conflict_on_a_flexible_block_asks_the_model_with_block_to_move(monkeypatch):
    gid = _goal("swim", "Swim")
    block = _approved_block("goal", gid, "Swim", "2026-10-21T18:00:00-04:00", "2026-10-21T20:00:00-04:00")
    block_event = db.get_proposal(block)["promoted_event_id"]
    _fixed("dinner", "2026-10-21T19:00:00-04:00", "2026-10-21T21:00:00-04:00", "Dinner")
    captured = {}
    # The model forgets replaces_event_id: the trigger fills it in, since the clash is the whole point.
    _fake_model(monkeypatch, [_ev("m", "goal", gid, "Swim", "2026-10-22T18:00:00-04:00", "2026-10-22T20:00:00-04:00")], captured)
    out = triggers.conflict_check(now=NOW)
    assert captured["payload"]["block_to_move"]["event_id"] == block_event
    assert captured["payload"]["goals"][0]["already_scheduled_event_ids"] == []
    assert block_event not in [m["event_id"] for m in captured["payload"]["movable_blocks"]]
    move = db.get_proposals(status="proposed")[0]
    assert move["replaces_event_id"] == block_event and move["rationale"].startswith("Moved from Wed 6 PM: Dinner landed on it.")
    assert out["proposed"][0]["placed"] is True


def test_conflicts_ignore_all_day_entries_and_only_flag_public_events():
    gid = _goal("swim", "Swim")
    _approved_block("goal", gid, "Swim", "2026-10-21T18:00:00-04:00", "2026-10-21T20:00:00-04:00")
    db.upsert_event({"source": "apple", "source_event_id": "trip", "calendar_id": "apple_default", "title": "Trip",
                     "start": "2026-10-21", "end": "2026-10-22", "all_day": True, "timezone": None, "last_modified": "x"})
    assert triggers.conflict_check(now=NOW)["conflicts"] == []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.create_optimizer_run("scout_1", now, "api", "m", "a", "b", kind="scout")
    db.insert_proposals("scout_1", [{"item_type": "event", "item_id": "d1", "discovered_event_id": None, "title": "Talk",
                                     "start": "2026-10-22T19:00:00-04:00", "end": "2026-10-22T21:00:00-04:00", "all_day": False,
                                     "timezone": DISPLAY_TIMEZONE, "status": "proposed", "rationale": ""}], now)
    assert db.promote_proposal("scout_1_1", now)
    _fixed("call", "2026-10-22T20:00:00-04:00", "2026-10-22T20:30:00-04:00", "Late call")
    out = triggers.conflict_check(now=NOW)
    assert [c["title"] for c in out["conflicts"]] == ["Talk"] and out["proposed"] == []  # flagged, never moved
    assert db.get_proposal("scout_1_1")["conflict_event_id"] == "google_call"


# --- the change email ---------------------------------------------------------------------------


def test_change_email_lists_each_trigger_proposal_once(monkeypatch):
    gid = _goal("floss", "Floss", goal_type="Routine", frequency="Daily", required_time_hr=0.25, last_occurred="2026-10-19")
    out = triggers.place_item("goal", gid, reason="New in Notion", now=NOW)
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "me@example.com")
    monkeypatch.setenv("NOTIFY_SMTP_USER", "me@example.com")
    monkeypatch.setenv("NOTIFY_SMTP_PASSWORD", "pw")
    sent = []
    monkeypatch.setattr(notifier, "send", lambda msg, config: sent.append(msg))
    first = notifier.notify_changes(now=NOW)
    assert first["sent"] is True and first["count"] == 6 and first["subject"].startswith("Calendar Changes to Review!! ")
    assert first["subject"][-1] in "".join(notifier.SUBJECT_EMOJI) or first["subject"].split()[-1] in notifier.SUBJECT_EMOJI
    assert all("New in Notion" in line for line in first["lines"])
    assert sent[0]["Subject"] == first["subject"]
    assert all(p["notified_at"] for p in db.get_proposals(run_id=out["run_id"]))
    assert notifier.notify_changes(now=NOW)["skipped"] == "no new changes"


def test_plan_subject_uses_the_window_dates():
    assert notifier.plan_subject("2026-10-26T00:00:00-04:00", "2026-11-02T00:00:00-05:00", emoji="🦉") == "10/26 - 11/1 Cal Review 🦉"
    assert notifier.plan_subject("2026-10-28T11:00:00-04:00", "2026-11-02T00:00:00-05:00", emoji="🦉") == "10/28 - 11/1 Cal Review 🦉"
    assert notifier.plan_subject(None, None, emoji="🦉") == "Cal Review 🦉"
    assert notifier.changes_subject(emoji="🎲") == "Calendar Changes to Review!! 🎲"


# --- the reset ------------------------------------------------------------------------------------


def test_retire_open_plan_clears_the_calendar_but_keeps_the_decisions():
    gid = _goal("swim", "Swim")
    past = _approved_block("goal", gid, "Swim", "2026-10-13T18:00:00-04:00", "2026-10-13T20:00:00-04:00", ref="a", run_id="run_a")
    ahead = _approved_block("goal", gid, "Swim", "2026-10-22T18:00:00-04:00", "2026-10-22T20:00:00-04:00", ref="b", run_id="run_b")
    db.create_optimizer_run("run_c", "t", "api", "m", "a", "b")
    db.insert_proposals("run_c", [_ev("c", "goal", gid, "Swim", "2026-10-23T18:00:00-04:00", "2026-10-23T19:00:00-04:00"),
                                  _ev("d", "goal", gid, "Swim", "2026-10-24T18:00:00-04:00", "2026-10-24T19:00:00-04:00")], "t")
    db.reject_proposal("run_c_1", "t")
    assert db.count_decisions() == 3
    out = db.retire_open_plan(NOW)
    assert out["superseded"] == 1 and out["retired"] == [ahead]
    assert db.get_proposal(past)["status"] == "promoted"      # already happened: untouched
    assert db.get_proposal(ahead)["status"] == "retired"
    assert db.get_proposal("run_c_1")["status"] == "rejected"  # decisions stay
    assert db.count_decisions() == 3 and db.plan_decision_stats()["by_kind"]["goal"]["approved"] == 2
    assert [e["id"] for e in db.get_schedule_x_events(calendar_id="optimizer")] == [f"optimizer_{past}"]
    assert json.loads(db.get_goals()[0]["scheduled_event_ids"]) == [f"optimizer_{past}"]
    # The review tabs no longer list the retired block.
    assert all(p["id"] != ahead for p in db.get_open_proposals(NOW))


def test_conflict_check_asks_the_model_once_per_clash(monkeypatch):
    # On the Pi (2026-10-29) a clash whose moves kept failing validation cost a
    # model call every 5 minutes; now one attempt per clash.
    gid = _goal("swim", "Swim")
    _approved_block("goal", gid, "Swim", "2026-10-21T18:00:00-04:00", "2026-10-21T20:00:00-04:00")
    _fixed("dinner", "2026-10-21T19:00:00-04:00", "2026-10-21T21:00:00-04:00", "Dinner")
    calls = []
    def nothing_fits(system, user, schema, **kw):
        calls.append(1)
        return llm_client.LLMResult(data={"events": [], "unscheduled": [], "notes": "no room"}, backend="api",
                                    model="claude-opus-5", usage={}, cost_usd=0.0)
    monkeypatch.setattr(llm_client, "complete_structured", nothing_fits)
    triggers.conflict_check(now=NOW)
    triggers.conflict_check(now=NOW)
    assert len(calls) == 1
    # A different event landing on the block is a new clash: one more attempt.
    db.soft_delete_event("google", "dinner", "2026-10-20T15:00:00+00:00")
    _fixed("drinks", "2026-10-21T18:30:00-04:00", "2026-10-21T19:30:00-04:00", "Drinks")
    triggers.conflict_check(now=NOW)
    assert len(calls) == 2


def _approved_move(monkeypatch):
    """An approved block with travel, a move for it approved over HTTP: (old block id, travel id, move id)."""
    gid = _goal("swim", "Swim", priority="Low")
    block = _approved_block("goal", gid, "Swim", "2026-10-21T18:00:00-04:00", "2026-10-21T20:00:00-04:00",
                            travel=("2026-10-21T17:30:00-04:00", "2026-10-21T18:00:00-04:00"))
    block_event = db.get_proposal(block)["promoted_event_id"]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.create_optimizer_run("trigger_m", now, "api", "m", "a", "b", kind="trigger")
    (move,) = db.insert_proposals("trigger_m", [{**_ev("m1", "goal", gid, "Swim", "2026-10-22T18:00:00-04:00",
                                                      "2026-10-22T20:00:00-04:00", replaces=block_event), "status": "proposed"}], now)
    assert TestClient(main.app).post(f"/optimizer/proposals/{move}/approve").status_code == 200
    return block, f"{block}"[:-1] + "2", move


class _Clock(dt.datetime):
    at = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.at.astimezone(tz) if tz else cls.at


def test_revoking_a_move_brings_back_the_block_it_replaced(monkeypatch):
    block, travel, move = _approved_move(monkeypatch)
    monkeypatch.setattr(main, "datetime", _Clock)  # Tuesday morning: the old Wednesday block is still ahead
    r = TestClient(main.app).post(f"/optimizer/proposals/{move}/revoke")
    assert r.status_code == 200 and sorted(r.json()["restored"]) == sorted([block, travel])
    assert db.get_proposal(block)["status"] == "promoted" and db.get_proposal(block)["replaced_by"] is None
    assert db.get_proposal(travel)["status"] == "promoted"
    shown = db.get_schedule_x_events(calendar_id="optimizer")
    assert sorted(e["start"][:16] for e in shown) == ["2026-10-21T17:30", "2026-10-21T18:00"]
    assert json.loads(db.get_goals()[0]["scheduled_event_ids"]) == [db.get_proposal(block)["promoted_event_id"]]


def test_revoking_a_move_after_the_old_time_restores_nothing(monkeypatch):
    block, travel, move = _approved_move(monkeypatch)
    _Clock.at = dt.datetime(2026, 10, 21, 21, 0, tzinfo=NY)  # the old block's time has passed
    monkeypatch.setattr(main, "datetime", _Clock)
    try:
        r = TestClient(main.app).post(f"/optimizer/proposals/{move}/revoke")
    finally:
        _Clock.at = NOW
    assert r.status_code == 200 and r.json()["restored"] == []
    assert db.get_proposal(block)["status"] == "superseded"
