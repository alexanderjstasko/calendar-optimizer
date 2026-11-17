"""
Unit tests for optimizer.py against a throwaway SQLite file. The model call
and the Notion refresh are replaced with fakes, so nothing touches the
network. Exercises payload building, validation, storage, superseding, and
promotion into `events`.
"""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

import db
import llm_client
import optimizer
from optimizer_schema import DISPLAY_TIMEZONE, PROPOSED_EVENTS_SCHEMA

NY = ZoneInfo(DISPLAY_TIMEZONE)
NOW = dt.datetime(2026, 9, 18, 17, 30, tzinfo=NY)  # a Friday evening
WINDOW_START = dt.datetime(2026, 9, 18, 18, 0, tzinfo=NY)  # plan_window: next full hour
HORIZON = 14  # these tests use the rolling window; the week rule has its own test below
REVIEW = optimizer.CALENDAR_REVIEW_ID
GOALS_REVIEW = optimizer.GOALS_REVIEW_ID


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()
    monkeypatch.setattr(optimizer, "_refresh_notion", lambda: None)
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.setenv("LLM_MODEL", "claude-opus-5")
    monkeypatch.setenv("LLM_HOME_LOCATION", "Brooklyn")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "16000")  # .env may set another cap
    monkeypatch.setenv("LLM_PREFERENCES_PATH", str(tmp_path / "preferences.md"))
    monkeypatch.setattr(optimizer.preferences, "refresh_learned", lambda **kw: {"refreshed": False, "new_decisions": 0})


def _seed():
    db.upsert_event({
        "source": "outlook", "source_event_id": "mtg1", "calendar_id": "outlook_default",
        "title": "Team meeting", "location": "https://zoom.us/j/1",
        "start": "2026-09-21T09:00:00-04:00", "end": "2026-09-21T12:00:00-04:00",
        "all_day": False, "timezone": DISPLAY_TIMEZONE, "last_modified": "x",
    })
    db.upsert_event({
        "source": "google", "source_event_id": "dentist", "calendar_id": "google_primary",
        "title": "Dentist", "location": "123 Main St, Brooklyn",
        "start": "2026-09-22T13:00:00-04:00", "end": "2026-09-22T15:00:00-04:00",
        "all_day": False, "timezone": DISPLAY_TIMEZONE, "last_modified": "x",
    })
    db.upsert_event({  # all-day trip: present in payload, must not block timed slots
        "source": "apple", "source_event_id": "trip", "calendar_id": "apple_default",
        "title": "Trip", "start": "2026-09-26", "end": "2026-09-27",
        "all_day": True, "timezone": None, "last_modified": "x",
    })
    db.upsert_event({  # outside the 14-day window
        "source": "google", "source_event_id": "far", "calendar_id": "google_primary",
        "title": "Far away", "start": "2026-12-01T09:00:00-05:00", "end": "2026-12-01T10:00:00-05:00",
        "all_day": False, "timezone": DISPLAY_TIMEZONE, "last_modified": "x",
    })
    db.upsert_task({
        "source": "notion", "source_task_id": "t1", "task": "Renew registration",
        "time_to_complete_hr": 1.0, "time_spent_hr": 0.25, "deadline": "2026-09-23", "priority": "High",
        "location": "Online", "status": "Not started", "last_modified": "x",
    })
    db.upsert_task({
        "source": "notion", "source_task_id": "t-done", "task": "Old thing",
        "time_to_complete_hr": 1.0, "deadline": None, "priority": "Low",
        "location": None, "status": "Done", "last_modified": "x",
    })
    db.upsert_goal({
        "source": "notion", "source_goal_id": "g1", "goal": "Rockclimb", "frequency": "2x Week",
        "end_date": None, "required_time_hr": 2.0, "priority": "High", "location": "Brooklyn Boulders",
        "days_of_week": json.dumps(["Monday", "Wednesday"]), "time_of_day_window": "Afterwork",
        "show_on_calendar": 1, "notes_for_llm": "Prefer the 6pm session.", "last_occurred": "2026-09-16",
        "status": "In progress", "last_modified": "x",
    })
    db.upsert_goal({
        "source": "notion", "source_goal_id": "work", "goal": "Work", "frequency": None,
        "end_date": None, "required_time_hr": None, "priority": None, "location": "3 WTC",
        "days_of_week": None, "time_of_day_window": None,
        "show_on_calendar": 0, "notes_for_llm": "WFH Mon/Fri, office Tue-Thu.",
        "status": "In progress", "last_modified": "x",
    })


def _event(ref, item_type, item_id, title, start, end, location=""):
    return {
        "ref": ref, "item_type": item_type, "item_id": item_id, "title": title,
        "description": "", "location": location, "start": start, "end": end,
        "all_day": False, "timezone": DISPLAY_TIMEZONE, "rationale": "because", "replaces_event_id": "",
    }


def _fake_result(data, **kw):
    return llm_client.LLMResult(
        data=data, backend="api", model="claude-opus-5",
        usage={"input_tokens": 100, "cache_creation_input_tokens": 50, "output_tokens": 40}, cost_usd=0.01, **kw,
    )


# --- payload ------------------------------------------------------------------


def test_payload_contains_window_events_and_open_items_only():
    _seed()
    start, end = optimizer.plan_window(NOW, HORIZON)
    assert start == WINDOW_START and end == WINDOW_START + dt.timedelta(days=14)

    payload, fixed = optimizer.build_payload(start, end)
    ids = [e["id"] for e in payload["calendar"]]
    assert ids == ["outlook_mtg1", "google_dentist", "apple_trip"]  # sorted by start; 'far' excluded
    assert payload["calendar"][0]["start"] == "2026-09-21T09:00:00-04:00"
    assert payload["calendar"][2] == {
        "id": "apple_trip", "title": "Trip", "start": "2026-09-26", "end": "2026-09-27",
        "all_day": True, "location": "", "source": "apple",
    }
    assert [t["id"] for t in payload["tasks"]] == ["notion_t1"]  # Done task filtered out
    assert payload["tasks"][0]["time_spent_hr"] == 0.25
    assert payload["tasks"][0]["hours_remaining"] == 0.75
    assert [g["id"] for g in payload["goals"]] == ["notion_g1", REVIEW, GOALS_REVIEW]  # the built-ins come last
    assert payload["goals"][1]["required_time_hr"] == 0.33 and payload["goals"][1]["days_of_week"] == ["Sunday"]
    assert payload["goals"][0]["days_of_week"] == ["Monday", "Wednesday"]
    assert payload["goals"][0]["notes_for_llm"] == "Prefer the 6pm session."
    assert payload["goals"][0]["last_occurred"] == "2026-09-16"
    assert payload["context_goals"][0]["last_occurred"] is None
    assert payload["context_goals"] == [{
        "id": "notion_work", "title": "Work", "type": "", "frequency": None, "required_time_hr": None,
        "days_of_week": [], "time_of_day": "Any", "end_date": None, "priority": None,
        "location": "3 WTC", "status": "In progress", "notes_for_llm": "WFH Mon/Fri, office Tue-Thu.",
        "last_occurred": None, "times_per_week": None, "sessions": None, "level": "Routine", "telos": "", "path": [], "ancestor_notes": [],
    }]
    assert "already_scheduled_event_ids" not in payload["context_goals"][0]
    assert payload["preferences"]["home_location"] == "Brooklyn"  # .env fallback while the Hours tab has no address
    import hours
    hours.save({**hours.DEFAULT_HOURS, "home": "Queens"})
    assert optimizer.build_payload(start, end)[0]["preferences"]["home_location"] == "Queens"
    assert payload["preferences"]["default_travel_minutes"] == 30
    assert payload["preferences"]["free_time_hours_per_week"] == 10
    assert payload["person"]["stated_preferences"] == "" and payload["person"]["decision_stats"]["decisions"] == 0
    assert len(fixed) == 3 and all("start_dt" in f for f in fixed)


def test_payload_marks_optimizer_placed_events(monkeypatch):
    _seed()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    run_id = "run_seed"
    db.create_optimizer_run(run_id, now, "api", "m", "a", "b")
    db.insert_proposals(run_id, [_event("p1", "goal", "notion_g1", "Rockclimb", "2026-09-21T18:00:00-04:00", "2026-09-21T20:00:00-04:00")], now)
    assert db.promote_proposal("run_seed_1", now) == "optimizer_run_seed_1"

    payload, _ = optimizer.build_payload(*optimizer.plan_window(NOW, HORIZON))
    placed = [e for e in payload["calendar"] if e["source"] == "optimizer"]
    assert placed[0]["placed_by_optimizer_for"] == {"item_type": "goal", "item_id": "notion_g1"}
    assert payload["goals"][0]["already_scheduled_event_ids"] == ["optimizer_run_seed_1"]


# --- validation ---------------------------------------------------------------


def test_validation_flags_each_failure_mode():
    _seed()
    start, end = optimizer.plan_window(NOW, HORIZON)
    payload, fixed = optimizer.build_payload(start, end)
    events = [
        _event("ok", "task", "notion_t1", "Renew", "2026-09-21T13:00:00-04:00", "2026-09-21T14:00:00-04:00"),
        _event("travel-ok", "travel", "google_dentist", "Travel to Dentist", "2026-09-22T12:30:00-04:00", "2026-09-22T13:00:00-04:00", "123 Main St"),
        _event("travel-ref", "travel", "ok", "Travel to Renew", "2026-09-21T12:40:00-04:00", "2026-09-21T13:00:00-04:00"),
        _event("bad-id", "goal", "notion_nope", "Ghost", "2026-09-23T18:00:00-04:00", "2026-09-23T20:00:00-04:00"),
        _event("bad-travel", "travel", "no-such-ref", "Travel", "2026-09-24T08:00:00-04:00", "2026-09-24T08:30:00-04:00"),
        _event("overlap-fixed", "goal", "notion_g1", "Climb", "2026-09-21T10:00:00-04:00", "2026-09-21T12:00:00-04:00"),
        _event("overlap-prop", "goal", "notion_g1", "Climb", "2026-09-21T13:30:00-04:00", "2026-09-21T15:30:00-04:00"),
        _event("outside", "goal", "notion_g1", "Climb", "2026-10-20T18:00:00-04:00", "2026-10-20T20:00:00-04:00"),
        _event("backwards", "goal", "notion_g1", "Climb", "2026-09-23T20:00:00-04:00", "2026-09-23T18:00:00-04:00"),
        {**_event("tz", "goal", "notion_g1", "Climb", "2026-09-25T18:00:00-04:00", "2026-09-25T20:00:00-04:00"), "timezone": "UTC"},
        _event("ctx", "goal", "notion_work", "Work", "2026-09-24T09:00:00-04:00", "2026-09-24T17:00:00-04:00"),
        _event("free", "free", "", "Free time", "2026-09-26T13:00:00-04:00", "2026-09-26T16:00:00-04:00"),
        _event("evt", "event", "abc", "Public event", "2026-09-27T13:00:00-04:00", "2026-09-27T15:00:00-04:00"),
        {**_event("move", "goal", "notion_g1", "Climb", "2026-09-28T18:00:00-04:00", "2026-09-28T20:00:00-04:00"), "replaces_event_id": "optimizer_x"},
    ]
    checked = {c["ref"]: c for c in optimizer.validate_proposals(events, payload, start, end, fixed)}
    assert "never moved by a plan run" in checked["move"]["invalid_reason"]

    assert checked["ok"]["status"] == "proposed"
    assert checked["travel-ok"]["status"] == "proposed"
    assert checked["travel-ref"]["status"] == "proposed"
    assert "unknown goal id" in checked["bad-id"]["invalid_reason"]
    assert "travel destination" in checked["bad-travel"]["invalid_reason"]
    assert "overlaps existing event 'Team meeting'" == checked["overlap-fixed"]["invalid_reason"]
    assert checked["overlap-prop"]["invalid_reason"] == "overlaps proposal 'ok'"
    assert checked["outside"]["invalid_reason"] == "outside the planning window"
    assert checked["backwards"]["invalid_reason"] == "end is not after start"
    assert "timezone" in checked["tz"]["invalid_reason"]
    assert checked["ctx"]["invalid_reason"] == "goal is context-only (Show on Calendar unchecked)"
    assert checked["free"]["status"] == "invalid" and "derived" in checked["free"]["invalid_reason"]
    assert "scout" in checked["evt"]["invalid_reason"]


def test_validation_allows_travel_to_abut_destination():
    _seed()
    start, end = optimizer.plan_window(NOW, HORIZON)
    payload, fixed = optimizer.build_payload(start, end)
    events = [
        _event("p1", "goal", "notion_g1", "Climb", "2026-09-21T18:00:00-04:00", "2026-09-21T20:00:00-04:00", "Brooklyn Boulders"),
        _event("t1", "travel", "p1", "Travel to Climb", "2026-09-21T17:30:00-04:00", "2026-09-21T18:00:00-04:00", "Brooklyn Boulders"),
    ]
    checked = optimizer.validate_proposals(events, payload, start, end, fixed)
    assert [c["status"] for c in checked] == ["proposed", "proposed"]


# --- run + storage --------------------------------------------------------------


def test_run_stores_proposals_and_run_row(monkeypatch):
    _seed()
    captured = {}

    def fake_complete(system, user, schema, **kw):
        captured.update(system=system, user=json.loads(user), schema=schema, kw=kw)
        return _fake_result({
            "events": [
                _event("p1", "task", "notion_t1", "Renew", "2026-09-21T13:00:00-04:00", "2026-09-21T14:00:00-04:00"),
                _event("p2", "goal", "notion_g1", "Climb", "2026-09-21T18:00:00-04:00", "2026-09-21T20:00:00-04:00", "Brooklyn Boulders"),
                _event("p3", "travel", "p2", "Travel to Climb", "2026-09-21T17:30:00-04:00", "2026-09-21T18:00:00-04:00", "Brooklyn Boulders"),
                _event("bad", "goal", "notion_ghost", "Ghost", "2026-09-23T18:00:00-04:00", "2026-09-23T20:00:00-04:00"),
            ],
            "unscheduled": [{"item_type": "goal", "item_id": "notion_g1", "reason": "only one slot free"}],
            "notes": "Plan notes.",
        })

    monkeypatch.setattr(optimizer.llm_client, "complete_structured", fake_complete)
    summary = optimizer.run_optimizer(now=NOW, horizon_days=HORIZON)

    assert captured["schema"] is PROPOSED_EVENTS_SCHEMA
    assert captured["kw"]["tool_name"] == "submit_schedule"
    assert captured["kw"]["max_tokens"] == 16000
    assert captured["user"]["planning_window"]["start"] == WINDOW_START.isoformat()
    assert "submit_schedule" in captured["system"]

    assert summary["proposed"] == 3 and summary["invalid"] == 1
    assert summary["goals"] == 3 and summary["context_goals"] == 1  # Rockclimb + the two built-in reviews
    assert "context_goals" in captured["system"] and "notes_for_llm" in captured["system"]
    assert "learned_preferences" in captured["system"]
    assert summary["learning"]["refreshed"] is False
    assert summary["input_tokens"] == 150 and summary["output_tokens"] == 40
    assert summary["unscheduled"][0]["reason"] == "only one slot free"
    assert summary["superseded"] == 0

    run = db.get_optimizer_runs()[0]
    assert run["run_id"] == summary["run_id"]
    assert "payload_json" not in run and "system_prompt" not in run  # list view stays small
    full = db.get_optimizer_run(summary["run_id"])
    assert full["system_prompt"] == captured["system"]
    assert json.loads(full["payload_json"]) == captured["user"]
    assert db.get_optimizer_run("nope") is None
    assert (run["status"], run["proposed_count"], run["invalid_count"], run["notes"]) == ("succeeded", 3, 1, "Plan notes.")
    assert run["input_tokens"] == 150

    rows = db.get_proposals(run_id=summary["run_id"])
    assert [r["status"] for r in rows] == ["proposed", "proposed", "proposed", "invalid"]
    assert rows[0]["id"] == f"{summary['run_id']}_1"  # ordered by start: the 13:00 task block first
    assert rows[1]["item_type"] == "travel"  # 17:30 travel block precedes the 18:00 climb
    assert summary["run_id"].startswith("run_") and len(summary["run_id"].split("_")) == 4
    assert "unknown goal id" in rows[-1]["invalid_reason"]
    assert db.get_schedule_x_events(calendar_id="optimizer") == []  # nothing promoted yet


def test_second_run_supersedes_open_proposals(monkeypatch):
    _seed()
    plan = {"events": [_event("p1", "task", "notion_t1", "Renew", "2026-09-21T13:00:00-04:00", "2026-09-21T14:00:00-04:00")],
            "unscheduled": [], "notes": ""}
    monkeypatch.setattr(optimizer.llm_client, "complete_structured", lambda *a, **k: _fake_result(plan))
    first = optimizer.run_optimizer(now=NOW, horizon_days=HORIZON)
    second = optimizer.run_optimizer(now=NOW + dt.timedelta(seconds=1), horizon_days=HORIZON)
    assert second["superseded"] == 1
    assert db.get_proposals(run_id=first["run_id"])[0]["status"] == "superseded"
    assert db.get_proposals(run_id=second["run_id"])[0]["status"] == "proposed"


def test_failed_model_call_marks_run_failed(monkeypatch):
    _seed()

    def boom(*a, **k):
        raise llm_client.LLMRateLimitError("slow down")

    monkeypatch.setattr(optimizer.llm_client, "complete_structured", boom)
    with pytest.raises(llm_client.LLMRateLimitError):
        optimizer.run_optimizer(now=NOW)
    run = db.get_optimizer_runs()[0]
    assert run["status"] == "failed" and "LLMRateLimitError" in run["error"]


def test_dry_run_counts_tokens_without_calling_the_model(monkeypatch):
    _seed()
    monkeypatch.setattr(optimizer.llm_client, "complete_structured", lambda *a, **k: pytest.fail("model was called"))
    monkeypatch.setattr(optimizer.llm_client, "estimate_tokens", lambda *a, **k: (4321, "count_tokens"))
    summary = optimizer.run_optimizer(now=NOW, dry_run=True, horizon_days=30)
    assert summary["dry_run"] is True
    assert summary["input_tokens_estimate"] == 4321
    assert summary["horizon_days"] == 30
    assert db.get_optimizer_runs() == []


# --- promotion -----------------------------------------------------------------


def test_promote_puts_event_on_calendar_and_marks_goal(monkeypatch):
    _seed()
    plan = {"events": [
        _event("p1", "goal", "notion_g1", "Rockclimb", "2026-09-21T18:00:00-04:00", "2026-09-21T20:00:00-04:00", "Brooklyn Boulders"),
        _event("p2", "travel", "p1", "Travel to Rockclimb", "2026-09-21T17:30:00-04:00", "2026-09-21T18:00:00-04:00", "Brooklyn Boulders"),
    ], "unscheduled": [], "notes": ""}
    monkeypatch.setattr(optimizer.llm_client, "complete_structured", lambda *a, **k: _fake_result(plan))
    summary = optimizer.run_optimizer(now=NOW, horizon_days=HORIZON)
    run_id = summary["run_id"]
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    goal_event = db.promote_proposal(f"{run_id}_1", now)
    assert goal_event == f"optimizer_{run_id}_1"
    assert db.promote_proposal(f"{run_id}_1", now) is None  # already decided
    assert db.promote_proposal("nope", now) is None

    shown = db.get_schedule_x_events(calendar_id="optimizer")
    assert len(shown) == 1
    assert shown[0]["id"] == goal_event
    assert shown[0]["start"] == f"2026-09-21T18:00:00-04:00[{DISPLAY_TIMEZONE}]"
    assert shown[0]["title"] == "Rockclimb"

    goal = db.get_goals()[0]
    assert json.loads(goal["scheduled_event_ids"]) == [goal_event]
    proposal = db.get_proposal(f"{run_id}_1")
    assert (proposal["status"], proposal["promoted_event_id"]) == ("promoted", goal_event)

    assert db.set_proposal_status(f"{run_id}_2", "rejected", now) is True
    assert db.promote_proposal(f"{run_id}_2", now) is None
    assert db.get_proposals(status="rejected")[0]["item_type"] == "travel"


# --- schema migration ----------------------------------------------------------


def test_init_db_adds_new_goal_columns_to_an_old_database(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("""
            CREATE TABLE goals (
                id TEXT PRIMARY KEY, goal TEXT NOT NULL, frequency TEXT, end_date TEXT,
                required_time_hr REAL, priority TEXT, location TEXT, days_of_week TEXT,
                time_of_day_window TEXT, status TEXT, source TEXT NOT NULL,
                source_goal_id TEXT NOT NULL, last_modified TEXT, deleted_at TEXT,
                raw_json TEXT, scheduled_event_ids TEXT, UNIQUE(source, source_goal_id)
            )
        """)
        conn.execute(
            "INSERT INTO goals (id, goal, source, source_goal_id) VALUES ('notion_old', 'Old', 'notion', 'old')"
        )
    monkeypatch.setattr(db, "DB_PATH", old)
    db.init_db()
    db.init_db()  # idempotent

    with sqlite3.connect(old) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(goals)")}
    assert {"show_on_calendar", "notes_for_llm", "last_occurred"} <= cols
    legacy = db.get_goals()[0]
    assert legacy["show_on_calendar"] == 1  # legacy rows keep scheduling until re-synced
    assert legacy["notes_for_llm"] is None

    payload, _ = optimizer.build_payload(*optimizer.plan_window(NOW, HORIZON))
    assert [g["id"] for g in payload["goals"]] == ["notion_old", REVIEW, GOALS_REVIEW]


def test_init_db_adds_request_columns_to_an_old_optimizer_runs_table(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old_runs.db"
    with sqlite3.connect(old) as conn:
        conn.execute("""
            CREATE TABLE optimizer_runs (
                run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running', backend TEXT, model TEXT,
                horizon_start TEXT, horizon_end TEXT, input_tokens INTEGER, output_tokens INTEGER,
                cost_usd REAL, proposed_count INTEGER, invalid_count INTEGER, notes TEXT, error TEXT
            )
        """)
        conn.execute("INSERT INTO optimizer_runs (run_id, started_at) VALUES ('run_old', 't')")
    monkeypatch.setattr(db, "DB_PATH", old)
    db.init_db()
    assert db.get_optimizer_run("run_old")["payload_json"] is None
    db.create_optimizer_run("run_new", "t", "api", "m", "a", "b", system_prompt="SYS", payload_json="{}")
    assert db.get_optimizer_run("run_new")["system_prompt"] == "SYS"


# --- system prompt file --------------------------------------------------------


def test_system_prompt_is_loaded_from_the_project_file(monkeypatch):
    monkeypatch.delenv("LLM_SYSTEM_PROMPT_PATH", raising=False)
    assert optimizer.system_prompt_path() == optimizer.DEFAULT_SYSTEM_PROMPT_PATH
    text = optimizer.load_system_prompt()
    assert "<!--" not in text  # editing notes stripped
    assert "submit_schedule" in text and "context_goals" in text and "notes_for_llm" in text


def test_system_prompt_path_override_and_tool_fallback(tmp_path, monkeypatch):
    custom = tmp_path / "prompt.txt"
    custom.write_text("<!-- note -->\nBe terse.\n")
    monkeypatch.setenv("LLM_SYSTEM_PROMPT_PATH", str(custom))
    assert optimizer.system_prompt_path() == custom
    text = optimizer.load_system_prompt()
    assert text.startswith("Be terse.")
    assert "calling the `submit_schedule` tool exactly once" in text  # appended when missing

    seen = {}
    monkeypatch.setattr(optimizer.llm_client, "complete_structured",
                        lambda system, user, schema, **kw: (seen.update(system=system), _fake_result({"events": [], "unscheduled": [], "notes": ""}))[1])
    summary = optimizer.run_optimizer(now=NOW)
    assert seen["system"] == text
    assert summary["system_prompt_path"] == str(custom)
    assert db.get_optimizer_run(summary["run_id"])["system_prompt"] == text


def test_missing_or_empty_system_prompt_file_is_a_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_SYSTEM_PROMPT_PATH", str(tmp_path / "nope.md"))
    with pytest.raises(llm_client.LLMConfigError, match="not found"):
        optimizer.load_system_prompt()
    empty = tmp_path / "empty.md"
    empty.write_text("<!-- only a comment -->\n")
    monkeypatch.setenv("LLM_SYSTEM_PROMPT_PATH", str(empty))
    with pytest.raises(llm_client.LLMConfigError, match="empty"):
        optimizer.load_system_prompt()


def test_revoke_takes_a_promoted_event_off_the_calendar(monkeypatch):
    _seed()
    plan = {"events": [
        _event("p1", "goal", "notion_g1", "Rockclimb", "2026-09-21T18:00:00-04:00", "2026-09-21T20:00:00-04:00"),
    ], "unscheduled": [], "notes": ""}
    monkeypatch.setattr(optimizer.llm_client, "complete_structured", lambda *a, **k: _fake_result(plan))
    run_id = optimizer.run_optimizer(now=NOW, horizon_days=HORIZON)["run_id"]
    pid = f"{run_id}_1"
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    assert db.revoke_proposal(pid, now) is None  # not promoted yet
    event_id = db.promote_proposal(pid, now)
    assert len(db.get_schedule_x_events(calendar_id="optimizer")) == 1
    assert json.loads(db.get_goals()[0]["scheduled_event_ids"]) == [event_id]

    assert db.revoke_proposal(pid, now) == event_id
    assert db.get_schedule_x_events(calendar_id="optimizer") == []  # cancelled, hidden from the frontend
    assert json.loads(db.get_goals()[0]["scheduled_event_ids"]) == []
    proposal = db.get_proposal(pid)
    assert (proposal["status"], proposal["promoted_event_id"]) == ("proposed", event_id)  # back to pending
    assert db.revoke_proposal(pid, now) is None  # not promoted any more
    assert db.promote_proposal(pid, now) == event_id  # can be approved again; same event row revived
    assert len(db.get_schedule_x_events(calendar_id="optimizer")) == 1
    assert db.revoke_proposal(pid, now) == event_id
    # the runner treats the cancelled event as gone on the next pass
    payload, _ = optimizer.build_payload(*optimizer.plan_window(NOW, HORIZON))
    assert all(e["source"] != "optimizer" for e in payload["calendar"])


# --- the week window and the Calendar Review ------------------------------------


def test_plan_window_covers_the_rest_of_this_week_or_next_week_on_a_sunday():
    friday = dt.datetime(2026, 10, 23, 17, 30, tzinfo=NY)
    start, end = optimizer.plan_window(friday)
    assert start == dt.datetime(2026, 10, 23, 18, 0, tzinfo=NY)
    assert end == dt.datetime(2026, 10, 26, 0, 0, tzinfo=NY)  # the coming Monday, midnight
    sunday = dt.datetime(2026, 10, 25, 6, 30, tzinfo=NY)
    start, end = optimizer.plan_window(sunday)
    assert start == dt.datetime(2026, 10, 26, 0, 0, tzinfo=NY)  # the current Sunday is left alone
    assert end == dt.datetime(2026, 11, 2, 0, 0, tzinfo=NY)
    monday = dt.datetime(2026, 10, 26, 8, 0, tzinfo=NY)
    assert optimizer.plan_window(monday) == (dt.datetime(2026, 10, 26, 9, 0, tzinfo=NY), dt.datetime(2026, 11, 2, 0, 0, tzinfo=NY))
    late_saturday = dt.datetime(2026, 10, 24, 23, 40, tzinfo=NY)
    assert optimizer.plan_window(late_saturday) == (dt.datetime(2026, 10, 25, 0, 0, tzinfo=NY), dt.datetime(2026, 10, 26, 0, 0, tzinfo=NY))
    # An explicit horizon keeps the rolling window (CLI/API).
    assert optimizer.plan_window(friday, 3) == (dt.datetime(2026, 10, 23, 18, 0, tzinfo=NY), dt.datetime(2026, 10, 26, 18, 0, tzinfo=NY))


def test_calendar_review_is_a_built_in_sunday_goal_sized_from_the_hours_tab():
    import hours
    monday = dt.datetime(2026, 10, 26, 0, 0, tzinfo=NY)
    goal = optimizer.calendar_review_goal(monday, monday + dt.timedelta(days=7))
    assert goal["id"] == REVIEW and goal["days_of_week"] == ["Sunday"] and goal["required_time_hr"] == 0.33
    hours.save({**hours.DEFAULT_HOURS, "calendar_review_minutes": 45})
    assert optimizer.calendar_review_goal(monday, monday + dt.timedelta(days=7))["required_time_hr"] == 0.75
    # No Sunday in the window: no review item.
    assert optimizer.calendar_review_goal(monday, monday + dt.timedelta(days=3)) is None
    # Approving the review block for that Sunday shows up as already scheduled and promotes without a goals row.
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.create_optimizer_run("run_r", now, "api", "m", "a", "b")
    db.insert_proposals("run_r", [_event("r", "goal", REVIEW, "Calendar Review", "2026-11-01T10:00:00-05:00", "2026-11-01T10:20:00-05:00")], now)
    assert db.promote_proposal("run_r_1", now) == "optimizer_run_r_1"
    assert optimizer.calendar_review_goal(monday, monday + dt.timedelta(days=7))["already_scheduled_event_ids"] == ["optimizer_run_r_1"]
    assert db.revoke_proposal("run_r_1", now) == "optimizer_run_r_1"


def test_pending_routine_blocks_are_fixed_for_the_model_and_survive_the_run(monkeypatch):
    _seed()
    db.upsert_goal({
        "source": "notion", "source_goal_id": "shave", "goal": "Shave", "frequency": "Daily", "end_date": None,
        "required_time_hr": 0.25, "priority": None, "location": "Home", "days_of_week": None, "time_of_day_window": None,
        "show_on_calendar": 1, "notes_for_llm": None, "last_occurred": "2026-09-17", "goal_type": "Routine",
        "status": "In progress", "last_modified": "x",
    })
    captured = {}

    def fake_complete(system, user, schema, **kw):
        captured["payload"] = json.loads(user)
        shave = next(e for e in captured["payload"]["calendar"] if e["title"] == "Shave")
        # The model overlapping a pending routine block is held back like any clash.
        return _fake_result({"events": [
            _event("p1", "goal", "notion_g1", "Climb", shave["start"], shave["end"]),
            _event("p2", "task", "notion_t1", "Renew", "2026-09-21T13:00:00-04:00", "2026-09-21T14:00:00-04:00"),
        ], "unscheduled": [], "notes": ""})

    monkeypatch.setattr(optimizer.llm_client, "complete_structured", fake_complete)
    summary = optimizer.run_optimizer(now=NOW, horizon_days=HORIZON)
    assert summary["routine"]["placed"] == 15 and "Shave" not in [g["title"] for g in captured["payload"]["goals"]]
    pending_shave = [e for e in captured["payload"]["calendar"] if e["title"] == "Shave"]
    assert len(pending_shave) == 15 and "treat as fixed" in pending_shave[0]["source"]
    rows = db.get_proposals(run_id=summary["run_id"])  # by start: the Saturday clash first, the Monday task block second
    assert [r["status"] for r in rows] == ["invalid", "proposed"] and "overlaps existing event 'Shave'" in rows[0]["invalid_reason"]
    # The routine proposals are still pending (not superseded by their own pass) and nothing was promoted.
    assert len(db.get_proposals(run_id=summary["routine"]["run_id"], status="proposed")) == 15
    assert db.get_proposals(status="promoted") == []
    # A second run supersedes the old plan and the old routine proposals, and proposes the routine afresh.
    second = optimizer.run_optimizer(now=NOW + dt.timedelta(seconds=1), horizon_days=HORIZON)
    assert second["superseded"] == 16  # 1 plan block + 15 routine blocks
    assert len(db.get_proposals(status="proposed")) == 16


def test_failed_model_call_rolls_the_routine_proposals_back(monkeypatch):
    _seed()
    db.upsert_goal({
        "source": "notion", "source_goal_id": "shave", "goal": "Shave", "frequency": "Daily", "end_date": None,
        "required_time_hr": 0.25, "priority": None, "location": "Home", "days_of_week": None, "time_of_day_window": None,
        "show_on_calendar": 1, "notes_for_llm": None, "last_occurred": "2026-09-17", "goal_type": "Routine",
        "status": "In progress", "last_modified": "x",
    })
    plan = {"events": [_event("p1", "task", "notion_t1", "Renew", "2026-09-21T13:00:00-04:00", "2026-09-21T14:00:00-04:00")],
            "unscheduled": [], "notes": ""}
    monkeypatch.setattr(optimizer.llm_client, "complete_structured", lambda *a, **k: _fake_result(plan))
    first = optimizer.run_optimizer(now=NOW, horizon_days=HORIZON)
    before = len(db.get_proposals(status="proposed"))

    def boom(*a, **k):
        raise llm_client.LLMBackendUnavailable("down")

    monkeypatch.setattr(optimizer.llm_client, "complete_structured", boom)
    with pytest.raises(llm_client.LLMBackendUnavailable):
        optimizer.run_optimizer(now=NOW + dt.timedelta(seconds=1), horizon_days=HORIZON)
    # The old plan (model + routine) is untouched; the failed pass left no pending rows of its own.
    assert len(db.get_proposals(status="proposed")) == before
    assert all(p["run_id"] in (first["run_id"], first["routine"]["run_id"]) for p in db.get_proposals(status="proposed"))



def test_all_day_events_end_on_their_last_day_for_schedule_x():
    """Stored ends are exclusive (iCalendar/Google); Schedule-X wants the last day shown."""
    base = {"source": "google", "calendar_id": "g", "title": "", "timezone": None, "all_day": True}
    db.upsert_event({**base, "id": "one", "source_event_id": "one", "title": "One day", "start": "2026-10-06", "end": "2026-10-07"})
    db.upsert_event({**base, "id": "three", "source_event_id": "three", "title": "Three days", "start": "2026-10-06", "end": "2026-10-09"})
    shown = {e["title"]: (e["start"], e["end"]) for e in db.get_schedule_x_events(calendar_id="g")}
    assert shown == {"One day": ("2026-10-06", "2026-10-06"), "Three days": ("2026-10-06", "2026-10-08")}
    stored = {e["title"]: e["end"] for e in db.get_events(calendar_id="g")}
    assert stored == {"One day": "2026-10-07", "Three days": "2026-10-09"}  # storage unchanged


# --- review API support: refs, additive migration, one-transaction batches ----------------


def test_run_persists_the_model_ref_so_travel_attaches_to_its_destination(monkeypatch):
    import review_model

    _seed()
    plan = {"events": [
        _event("p1", "goal", "notion_g1", "Rockclimb", "2026-09-21T18:00:00-04:00", "2026-09-21T20:00:00-04:00"),
        _event("p2", "travel", "p1", "Commute", "2026-09-21T17:20:00-04:00", "2026-09-21T17:50:00-04:00"),
    ], "unscheduled": [], "notes": ""}
    monkeypatch.setattr(optimizer.llm_client, "complete_structured", lambda *a, **k: _fake_result(plan))
    run_id = optimizer.run_optimizer(now=NOW)["run_id"]
    rows = db.get_proposals(run_id=run_id)
    assert {r["title"]: r["ref"] for r in rows} == {"Rockclimb": "p1", "Commute": "p2"}
    [series] = review_model.group_series(rows)  # title and timing don't match: only the ref ties them
    assert series["title"] == "Rockclimb" and series["occurrences"][0]["travel"]["title"] == "Commute"


def test_init_db_adds_review_columns_to_an_old_proposed_events_table(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old_proposals.db"
    with sqlite3.connect(old) as conn:
        conn.execute("""
            CREATE TABLE proposed_events (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, item_type TEXT NOT NULL, item_id TEXT,
                title TEXT, description TEXT, location TEXT, start TEXT NOT NULL, end TEXT NOT NULL,
                all_day INTEGER DEFAULT 0, timezone TEXT, rationale TEXT,
                status TEXT NOT NULL DEFAULT 'proposed', invalid_reason TEXT, created_at TEXT,
                decided_at TEXT, promoted_event_id TEXT
            )
        """)
        conn.execute("INSERT INTO proposed_events (id, run_id, item_type, item_id, title, start, end) "
                     "VALUES ('run_old_1', 'run_old', 'goal', 'notion_g1', 'Old', '2026-09-21T18:00:00-04:00', "
                     "'2026-09-21T20:00:00-04:00')")
    monkeypatch.setattr(db, "DB_PATH", old)
    db.init_db()
    db.init_db()  # idempotent: the live DB gets this on deploy, maybe more than once
    with sqlite3.connect(old) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(proposed_events)")}
    assert {"ref", "rank", "shown_at", "goal_id", "cost", "adjusted_at"} <= cols
    legacy = db.get_proposal("run_old_1")
    assert (legacy["status"], legacy["ref"], legacy["rank"], legacy["shown_at"]) == ("proposed", None, None, None)
    assert [p["id"] for p in db.get_open_proposals(NOW)] == ["run_old_1"]


def test_upsert_events_writes_a_batch_in_one_transaction(monkeypatch):
    opened = []
    real = db.get_conn

    def counting():
        opened.append(1)
        return real()

    monkeypatch.setattr(db, "get_conn", counting)
    batch = [{"source": "partiful", "source_event_id": f"e{i}", "calendar_id": "partiful", "title": f"Party {i}",
              "start": "2026-09-25T20:00:00-04:00", "end": "2026-09-25T23:00:00-04:00", "all_day": False,
              "timezone": DISPLAY_TIMEZONE, "last_modified": "x"} for i in range(25)]
    db.upsert_events(batch)
    assert len(opened) == 1
    assert len(db.get_events(calendar_id="partiful")) == 25

    batch[3] = {**batch[3], "end": None}  # NOT NULL violation mid-batch: nothing of this batch is written
    batch = [{**e, "title": "changed"} if i != 3 else e for i, e in enumerate(batch)]
    with pytest.raises(Exception):
        db.upsert_events(batch)
    assert {e["title"] for e in db.get_events(calendar_id="partiful")} == {f"Party {i}" for i in range(25)}
