"""routine.py: goal types, frequency parsing, due dates, placement, the run, and what the rest of the code does with it."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

import db
import hours
import optimizer
import routine
from optimizer_schema import DISPLAY_TIMEZONE

NY = ZoneInfo(DISPLAY_TIMEZONE)
NOW = dt.datetime(2026, 10, 19, 8, 30, tzinfo=NY)  # a Monday morning
WS = dt.datetime(2026, 10, 19, 9, 0, tzinfo=NY)
WE = WS + dt.timedelta(days=14)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()


def _goal(gid, name, **kw):
    row = {"source": "notion", "source_goal_id": gid, "goal": name, "frequency": "1x Week", "end_date": None,
           "required_time_hr": 0.5, "priority": None, "location": "Home", "days_of_week": None, "time_of_day_window": None,
           "show_on_calendar": 1, "notes_for_llm": None, "last_occurred": None, "goal_type": "Routine",
           "status": "In progress", "last_modified": "x"}
    row.update(kw)
    db.upsert_goal(row)
    return f"notion_{gid}"


def _event(eid, start, end):
    db.upsert_event({"source": "google", "source_event_id": eid, "calendar_id": "google_primary", "title": eid,
                     "start": start, "end": end, "all_day": False, "timezone": DISPLAY_TIMEZONE, "last_modified": "x"})


# --- types and frequencies -------------------------------------------------------------


def test_goal_type_normalizes_notion_values():
    assert routine.goal_type({"goal_type": "Routine"}) == "routine"
    assert routine.goal_type({"goal_type": "Event Interest"}) == "event"
    assert routine.goal_type({"goal_type": "flexible"}) == "flexible"
    assert routine.goal_type({"goal_type": None}) is None
    assert routine.goal_type({"goal_type": "Something else"}) == "flexible"


@pytest.mark.parametrize("text, days", [
    ("Daily", 1), ("daily", 1), ("Once every 3 days", 3), ("every 2 days", 2), ("Every other day", 2),
    ("1x Week", 7), ("2x Week", 3.5), ("3x week", 7 / 3), ("1x Every Other Week", 14), ("2x Every Other Week", 7),
    ("1x Month", 30.4), ("2x Month", 15.2), ("Monthly", 30.4), ("weekly", 7), ("1x/week", 7), ("2x a month", 15.2),
    ("", None), (None, None), ("whenever", None), ("0x Week", None),
])
def test_parse_frequency(text, days):
    got = routine.parse_frequency(text)
    assert got == pytest.approx(days) if days is not None else got is None


# --- due dates -----------------------------------------------------------------------------


def test_due_dates_step_from_last_occurred_and_snap_to_the_day_pool():
    first, last = dt.date(2026, 10, 19), dt.date(2026, 11, 1)
    weekly_monday = {"last_occurred": "2026-10-12", "days_of_week": '["Monday"]'}
    assert routine.due_dates(weekly_monday, 7, first, last, set()) == [dt.date(2026, 10, 19), dt.date(2026, 10, 26)]
    every3 = {"last_occurred": "2026-10-17", "days_of_week": None}
    assert routine.due_dates(every3, 3, first, last, set()) == [dt.date(2026, 10, 20), dt.date(2026, 10, 23), dt.date(2026, 10, 26), dt.date(2026, 10, 29), dt.date(2026, 11, 1)]
    monthly_sunday = {"last_occurred": "2026-10-03", "days_of_week": '["Sunday"]'}
    # 10-03 + 30 days = Monday 11-02, which snaps forward to Sunday 11-08: outside a window ending 11-01, inside one ending 11-08.
    assert routine.due_dates(monthly_sunday, 30.4, first, last, set()) == []
    assert routine.due_dates(monthly_sunday, 30.4, first, dt.date(2026, 11, 8), set()) == [dt.date(2026, 11, 8)]
    daily = {"last_occurred": "2026-10-18"}
    assert len(routine.due_dates(daily, 1, first, last, set())) == 14
    # Three a week lands on about three days per week, never twice on one day.
    thrice = routine.due_dates({"last_occurred": "2026-10-18"}, 7 / 3, first, last, set())
    assert 5 <= len(thrice) <= 7 and len(set(thrice)) == len(thrice)


def test_due_dates_skip_taken_dates_and_anchor_on_the_latest_block():
    first, last = dt.date(2026, 10, 19), dt.date(2026, 11, 1)
    goal = {"last_occurred": "2026-10-12", "days_of_week": None}
    taken = {dt.date(2026, 10, 22)}  # a block already placed later than Last Occurred
    assert routine.due_dates(goal, 7, first, last, taken) == [dt.date(2026, 10, 29)]
    # No Last Occurred and nothing placed: due at once.
    assert routine.due_dates({"last_occurred": None}, 7, first, last, set())[0] == first


# --- placement -----------------------------------------------------------------------------


def test_find_slot_prefers_time_of_day_then_free_time_and_avoids_work_and_events():
    h = hours.load()  # defaults: work 09-17 weekdays, free 17-23, sleep 23-07; weekends free 10-23
    monday = dt.date(2026, 10, 19)
    busy = {monday: [(dt.datetime(2026, 10, 19, 17, 0, tzinfo=NY), dt.datetime(2026, 10, 19, 18, 30, tzinfo=NY))]}
    thirty = dt.timedelta(minutes=30)
    # Morning window 08-12 minus work 09-17 -> 08:00.
    assert routine.find_slot({"time_of_day_window": "Morning"}, monday, thirty, h, busy)[1:] == (
        dt.datetime(2026, 10, 19, 8, 0, tzinfo=NY), dt.datetime(2026, 10, 19, 8, 30, tzinfo=NY))
    # No window: free time from 17:00, but 17:00-18:30 is taken -> 18:30.
    name, start, _ = routine.find_slot({}, monday, thirty, h, busy)
    assert name == "free time" and start == dt.datetime(2026, 10, 19, 18, 30, tzinfo=NY)
    # Afternoon window lies inside work hours on a weekday: falls through to free time.
    assert routine.find_slot({"time_of_day_window": "Afternoon"}, monday, thirty, h, busy)[0] == "free time"
    # On a Saturday the afternoon window is open.
    saturday = dt.date(2026, 10, 24)
    assert routine.find_slot({"time_of_day_window": "Afternoon"}, saturday, thirty, h, {})[1] == dt.datetime(2026, 10, 24, 12, 0, tzinfo=NY)
    # Nothing fits a 7-hour block in a weekday evening (17-23 is 6 h) -> awake window is also blocked by work -> None.
    assert routine.find_slot({}, monday, dt.timedelta(hours=7), h, {}) is None
    # not_before: today's first slot starts after now, rounded up to the step.
    now = dt.datetime(2026, 10, 24, 12, 7, tzinfo=NY)
    assert routine.find_slot({}, saturday, thirty, h, {}, not_before=now)[1] == dt.datetime(2026, 10, 24, 12, 15, tzinfo=NY)


# --- the run -------------------------------------------------------------------------------


def test_expand_proposes_routine_goals_as_pending_blocks_and_is_idempotent(monkeypatch):
    floss = _goal("floss", "Floss", frequency="Once every 3 days", required_time_hr=0.25, last_occurred="2026-10-17")
    plants = _goal("plants", "Water Plants", frequency="1x Week", days_of_week='["Monday"]', time_of_day_window="Morning",
                   required_time_hr=0.25, last_occurred="2026-10-12")
    _goal("swim", "Swim", goal_type="Flexible", frequency="2x Week")      # not routine: untouched
    _goal("culture", "Culture Event", goal_type="Event Interest", frequency="2x Month")
    _goal("odd", "Odd one", frequency="whenever you like")                       # unparsable
    _event("dinner", "2026-10-20T17:00:00-04:00", "2026-10-20T21:00:00-04:00")

    summary = routine.expand(WS, WE, now=NOW)
    assert summary["goals"] == 3 and summary["unparsed"] == [{"goal": "Odd one", "frequency": "whenever you like"}]
    blocks = {(b["goal"], b["start"]) for b in summary["blocks"]}
    # Floss every 3 days from the 17th: 20th (evening, after dinner), 23rd, 26th, 29th, Nov 1st.
    floss_starts = sorted(b["start"] for b in summary["blocks"] if b["goal"] == "Floss")
    assert [s[:10] for s in floss_starts] == ["2026-10-20", "2026-10-23", "2026-10-26", "2026-10-29", "2026-11-01"]
    assert floss_starts[0] == "2026-10-20T21:00-04:00"  # dinner 17-21 pushed it to 21:00
    # Water Plants on Mondays: the 19th (the morning window is gone by 09:00 and work follows, so it lands in free time), 26th, Nov 2nd.
    plants_blocks = sorted((b["start"] for b in summary["blocks"] if b["goal"] == "Water Plants"))
    assert [s[:10] for s in plants_blocks] == ["2026-10-19", "2026-10-26", "2026-11-02"]
    assert plants_blocks[0] == "2026-10-19T17:00-04:00" and plants_blocks[1] == "2026-10-26T08:00-04:00"
    # The last window day ends 09:00: that block ends by then.
    assert plants_blocks[2] <= "2026-11-02T08:45"
    assert summary["placed"] == len(summary["blocks"]) == len(summary["proposal_ids"])

    # Pending proposals, nothing on the calendar: the person approves them in the Plan tab.
    pending = db.get_proposals(status="proposed")
    assert len(pending) == summary["placed"] and all(p["auto_approved"] == 0 for p in pending)
    assert db.get_events(source="optimizer") == [] and db.get_proposals(status="promoted") == []
    assert pending[0]["rationale"].startswith("Routine (")
    run = db.get_optimizer_runs(limit=1)[0]
    assert run["kind"] == "routine" and run["status"] == "succeeded" and run["proposed_count"] == summary["placed"]
    assert db.count_decisions() == 0 and db.plan_decision_stats()["decisions"] == 0

    # Running again adds nothing: pending blocks count as taken.
    again = routine.expand(WS, WE, now=NOW)
    assert again["placed"] == 0 and "run_id" not in again
    # ...unless the caller says pending ones do not count (the plan run, which supersedes them afterwards).
    assert routine.expand(WS, WE, now=NOW, count_pending=False, dry_run=True)["placed"] == summary["placed"]
    assert len(db.get_proposals(status="proposed")) == summary["placed"]


def test_rejecting_a_routine_block_leaves_the_day_empty_and_approving_one_puts_it_on_the_calendar():
    _goal("floss", "Floss", frequency="Daily", required_time_hr=0.25, last_occurred="2026-10-18")
    summary = routine.expand(WS, WE, now=NOW)
    assert summary["placed"] == 15  # Oct 19 .. Nov 2 (the window ends 09:00 on the 2nd; that block ends before it)
    assert max(b["end"] for b in summary["blocks"]) <= "2026-11-02T09:00"
    rows = sorted(db.get_proposals(status="proposed"), key=lambda p: p["start"])
    now_iso = "2026-10-19T13:00:00+00:00"
    assert db.reject_proposal(rows[0]["id"], now_iso) is True
    assert db.promote_proposal(rows[1]["id"], now_iso) == f"optimizer_{rows[1]['id']}"
    assert db.count_decisions() == 2  # the person's own decisions count
    assert routine.expand(WS, WE, now=NOW)["placed"] == 0  # rejected and approved dates stay as they are
    assert len(db.get_proposals(status="proposed")) == 13
    # Taking an approved routine block back makes it pending again (not a rejection any more).
    assert db.revoke_proposal(rows[1]["id"], now_iso) == f"optimizer_{rows[1]['id']}"
    assert db.get_proposal(rows[1]["id"])["status"] == "proposed"


def test_expand_can_be_limited_to_one_goal_and_plan_around_extra_busy_blocks():
    floss = _goal("floss", "Floss", frequency="Daily", required_time_hr=0.25, last_occurred="2026-10-18")
    _goal("plants", "Water Plants", frequency="1x Week", days_of_week='["Monday"]', required_time_hr=0.25, last_occurred="2026-10-12")
    busy = [(dt.datetime(2026, 10, 19, 17, 0, tzinfo=NY), dt.datetime(2026, 10, 19, 21, 0, tzinfo=NY))]
    summary = routine.expand(WS, WE, now=NOW, only_goal_ids={floss}, busy_extra=busy, run_kind="trigger")
    assert {b["goal"] for b in summary["blocks"]} == {"Floss"}
    assert next(b for b in summary["blocks"] if b["start"].startswith("2026-10-19"))["start"] == "2026-10-19T21:00-04:00"
    assert db.get_optimizer_runs(limit=1)[0]["kind"] == "trigger"


def test_expand_now_refreshes_goals_from_notion_first(monkeypatch):
    import sync_notion

    calls = []
    monkeypatch.setattr(sync_notion, "sync_goals", lambda: calls.append("synced"))
    monkeypatch.setattr(routine.db, "get_schedulable_items", lambda: [])
    assert routine.expand_now(dry_run=True, now=NOW)["placed"] == 0 and calls == ["synced"]

    def boom():
        raise RuntimeError("Notion down")

    monkeypatch.setattr(sync_notion, "sync_goals", boom)
    assert routine.expand_now(dry_run=True, now=NOW)["notion_error"] == "RuntimeError: Notion down"
    assert "notion_error" not in routine.expand_now(dry_run=True, now=NOW, refresh_notion=False)


def test_planner_and_scout_see_only_their_goal_types(monkeypatch):
    _goal("floss", "Floss", frequency="Daily", required_time_hr=0.25)
    _goal("swim", "Swim", goal_type="Flexible", frequency="2x Week")
    _goal("culture", "Culture Event", goal_type="Event Interest", frequency="2x Month")
    _goal("old", "Untyped", goal_type=None, frequency="1x Week")
    _goal("ctx", "Learn piano", goal_type="Flexible", show_on_calendar=0)
    monkeypatch.setenv("LLM_HOME_LOCATION", "Brooklyn")
    payload, _ = optimizer.build_payload(WS, WE)
    assert [g["title"] for g in payload["goals"]] == ["Swim", "Untyped", "Calendar Review", "Goals and Telos review"]
    assert [g["title"] for g in payload["context_goals"]] == ["Learn piano"]
    assert payload["goals"][0]["type"] == "flexible" and payload["goals"][1]["type"] == ""
    assert [g["title"] for g in optimizer.event_interest_goals()] == ["Culture Event", "Untyped"]


def test_run_optimizer_expands_routines_before_planning(monkeypatch):
    import json
    import llm_client

    _goal("floss", "Floss", frequency="Daily", required_time_hr=0.25, last_occurred="2026-10-18")
    monkeypatch.setattr(optimizer, "_refresh_notion", lambda: None)
    monkeypatch.setattr(optimizer.preferences, "refresh_learned", lambda **kw: {"refreshed": False})
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.setenv("LLM_PREFERENCES_PATH", str(db.DB_PATH.parent / "preferences.md"))
    captured = {}

    def fake_complete(system, user, schema, **kw):
        captured["payload"] = json.loads(user)
        return llm_client.LLMResult(data={"events": [], "unscheduled": [], "notes": "nothing"}, backend="api",
                                    model="claude-opus-5", usage={"input_tokens": 1, "output_tokens": 1}, cost_usd=0.0)

    monkeypatch.setattr(llm_client, "complete_structured", fake_complete)
    summary = optimizer.run_optimizer(now=NOW, refresh_notion=False, horizon_days=14)
    assert summary["routine"]["placed"] == 15
    assert [g["title"] for g in captured["payload"]["goals"]] == ["Calendar Review", "Goals and Telos review"]  # the routine is not a goal to plan...
    floss = [e for e in captured["payload"]["calendar"] if e["title"] == "Floss"]
    assert len(floss) == 15 and floss[0]["source"].startswith("routine (proposed")  # ...it is a fixed entry to plan around
    assert len(db.get_proposals(run_id=summary["routine"]["run_id"], status="proposed")) == 15  # still pending afterwards


def test_due_dates_keep_the_days_before_a_later_block():
    # A daily routine with one block already on Friday: Monday to Thursday are still due.
    first, last = dt.date(2026, 11, 16), dt.date(2026, 11, 22)
    goal = {"last_occurred": "2026-11-15", "days_of_week": None}
    taken = {dt.date(2026, 11, 20)}
    assert routine.due_dates(goal, 1, first, last, taken) == [
        dt.date(2026, 11, 16), dt.date(2026, 11, 17), dt.date(2026, 11, 18), dt.date(2026, 11, 19),
        dt.date(2026, 11, 21), dt.date(2026, 11, 22)]
    # A block in the past (before the window) still anchors the stepping.
    weekly = {"last_occurred": "2026-10-01", "days_of_week": None}
    assert routine.due_dates(weekly, 7, first, last, {dt.date(2026, 11, 12)}) == [dt.date(2026, 11, 19)]
