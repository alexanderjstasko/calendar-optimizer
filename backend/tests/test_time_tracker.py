"""
Unit tests for time_tracker.py against a throwaway SQLite file with the
Notion read/write helpers replaced by fakes. Nothing touches the network.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

import db
import time_tracker
from optimizer_schema import DISPLAY_TIMEZONE

NY = ZoneInfo(DISPLAY_TIMEZONE)
NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=NY)
PAGE = "3d318723-089b-80b3-9e4d-e84b11e9ff42"
GOAL_PAGE = "g1"


class FakeNotion:
    """Stands in for read_number_property / update_number_property."""

    def __init__(self, value=None, fail_on_write=False, wrong_type=False, last_occurred=None):
        self.values = {PAGE: value}
        self.dates = {GOAL_PAGE: last_occurred}
        self.date_reads: list[tuple[str, str]] = []
        self.date_writes: list[tuple[str, str, str]] = []
        self.fail_on_write = fail_on_write
        self.wrong_type = wrong_type
        self.reads: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, float]] = []

    def read(self, page_id, name):
        self.reads.append((page_id, name))
        if self.wrong_type:
            raise ValueError(f"property {name!r} is type 'rich_text', not number")
        return self.values[page_id]

    def write(self, page_id, name, value):
        self.writes.append((page_id, name, value))
        if self.fail_on_write:
            raise RuntimeError("503 from Notion")
        self.values[page_id] = value
        return value

    def read_date(self, page_id, name):
        self.date_reads.append((page_id, name))
        return self.dates[page_id]

    def write_date(self, page_id, name, value):
        self.date_writes.append((page_id, name, value))
        if self.fail_on_write:
            raise RuntimeError("503 from Notion")
        self.dates[page_id] = value
        return value


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()


@pytest.fixture
def notion(monkeypatch):
    fake = FakeNotion()
    monkeypatch.setattr(time_tracker.notion_adapter, "resolve_property_name", lambda page_id, names, ptype: names[-1])
    monkeypatch.setattr(time_tracker.notion_adapter, "read_number_property", fake.read)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_number_property", fake.write)
    monkeypatch.setattr(time_tracker.notion_adapter, "read_date_property", fake.read_date)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_date_property", fake.write_date)
    return fake


def _seed_task_and_goal():
    db.upsert_task({
        "source": "notion", "source_task_id": PAGE, "task": "Calendar Optimizer",
        "time_to_complete_hr": 20.0, "time_spent_hr": None, "deadline": None, "priority": "Low",
        "location": None, "status": "Not started", "last_modified": "x",
    })
    db.upsert_goal({
        "source": "notion", "source_goal_id": "g1", "goal": "Rockclimb", "frequency": "2x Week",
        "end_date": None, "required_time_hr": 2.0, "priority": "High", "location": None,
        "days_of_week": None, "time_of_day_window": "Afterwork", "status": "In progress", "last_modified": "x",
    })


def _proposal(item_type, item_id, start, end, all_day=False):
    return {
        "ref": "r", "item_type": item_type, "item_id": item_id, "title": "block", "description": "",
        "location": "", "start": start, "end": end, "all_day": all_day, "timezone": DISPLAY_TIMEZONE,
        "rationale": "", "status": "proposed",
    }


def _run_with(proposals, promote=True):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    run_id = f"run_{len(db.get_optimizer_runs())}"
    db.create_optimizer_run(run_id, now, "api", "m", "a", "b")
    ids = db.insert_proposals(run_id, proposals, now)
    if promote:
        for pid in ids:
            assert db.promote_proposal(pid, now)
    return ids


TASK_ID = f"notion_{PAGE}"


def test_past_task_block_is_written_once_and_future_blocks_wait(notion):
    _seed_task_and_goal()
    past, future, goal = _run_with([
        _proposal("task", TASK_ID, "2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00"),
        _proposal("task", TASK_ID, "2026-09-29T17:00:00-04:00", "2026-09-29T18:00:00-04:00"),
        _proposal("goal", "notion_g1", "2026-09-28T18:00:00-04:00", "2026-09-28T20:00:00-04:00"),
    ])

    first = time_tracker.settle_task_time(now=NOW)
    assert first["checked"] == 3
    assert first["pending"] == 2  # the future task block and the future goal
    assert [s["proposal_id"] for s in first["settled"]] == [past]
    assert first["settled"][0]["hours"] == 2.0
    assert first["settled"][0]["notion_before"] is None and first["settled"][0]["notion_after"] == 2.0
    assert notion.writes == [(PAGE, "Time Spent (hr)", 2.0)]
    assert notion.reads == [(PAGE, "Time Spent (hr)")]  # read fresh before the write

    row = db.get_proposal(past)
    assert row["settled_hours"] == 2.0 and row["settled_at"]
    assert db.get_proposal(future)["settled_at"] is None
    assert db.get_proposal(goal)["settled_at"] is None
    assert db.get_tasks()[0]["time_spent_hr"] == 2.0

    second = time_tracker.settle_task_time(now=NOW)
    assert second["settled"] == [] and second["pending"] == 2 and second["checked"] == 2
    assert len(notion.writes) == 1  # idempotent

    later = time_tracker.settle_task_time(now=NOW + dt.timedelta(days=7))
    assert [s["proposal_id"] for s in later["settled"]] == [future, goal]  # tasks first, then goals
    assert notion.date_writes == [(GOAL_PAGE, "Last Occurred", "2026-09-28")]
    assert db.get_goals()[0]["last_occurred"] == "2026-09-28"
    assert notion.writes[-1] == (PAGE, "Time Spent (hr)", 3.0)  # accumulates on the fresh value
    assert db.get_tasks()[0]["time_spent_hr"] == 3.0


def test_accumulates_onto_existing_notion_value_across_blocks(notion):
    notion.values[PAGE] = 1.5
    _seed_task_and_goal()
    _run_with([
        _proposal("task", TASK_ID, "2026-09-22T17:00:00-04:00", "2026-09-22T18:00:00-04:00"),
        _proposal("task", TASK_ID, "2026-09-23T17:00:00-04:00", "2026-09-23T18:30:00-04:00"),
    ])
    summary = time_tracker.settle_task_time(now=NOW)
    assert [s["hours"] for s in summary["settled"]] == [1.0, 1.5]
    assert [w[2] for w in notion.writes] == [2.5, 4.0]


def test_dry_run_writes_nothing(notion):
    _seed_task_and_goal()
    (pid,) = _run_with([_proposal("task", TASK_ID, "2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00")])
    summary = time_tracker.settle_task_time(now=NOW, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["settled"][0]["notion_after"] == 2.0
    assert notion.writes == []
    assert db.get_proposal(pid)["settled_at"] is None


def test_unpromoted_and_rejected_blocks_are_never_counted(notion):
    _seed_task_and_goal()
    _run_with([_proposal("task", TASK_ID, "2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00")], promote=False)
    summary = time_tracker.settle_task_time(now=NOW)
    assert summary["checked"] == 0 and notion.writes == []


def test_cancelled_event_settles_with_zero_hours_and_no_write(notion):
    _seed_task_and_goal()
    (pid,) = _run_with([_proposal("task", TASK_ID, "2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00")])
    event_id = db.get_proposal(pid)["promoted_event_id"]
    db.soft_delete_event("optimizer", pid, "2026-09-23T00:00:00+00:00")
    summary = time_tracker.settle_task_time(now=NOW)
    assert summary["skipped"][0]["reason"].startswith("calendar event was cancelled")
    assert notion.writes == []
    assert db.get_proposal(pid)["settled_hours"] == 0.0
    assert event_id.startswith("optimizer_")


def test_write_failure_leaves_block_unsettled_for_retry(monkeypatch):
    fake = FakeNotion(fail_on_write=True)
    monkeypatch.setattr(time_tracker.notion_adapter, "resolve_property_name", lambda page_id, names, ptype: names[-1])
    monkeypatch.setattr(time_tracker.notion_adapter, "read_number_property", fake.read)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_number_property", fake.write)
    _seed_task_and_goal()
    (pid,) = _run_with([_proposal("task", TASK_ID, "2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00")])
    summary = time_tracker.settle_task_time(now=NOW)
    assert summary["settled"] == []
    assert "503" in summary["errors"][0]["error"]
    assert db.get_proposal(pid)["settled_at"] is None
    assert db.get_tasks()[0]["time_spent_hr"] is None

    fake.fail_on_write = False
    retry = time_tracker.settle_task_time(now=NOW)
    assert [s["proposal_id"] for s in retry["settled"]] == [pid]


def test_wrong_property_type_never_writes(monkeypatch):
    fake = FakeNotion(wrong_type=True)
    monkeypatch.setattr(time_tracker.notion_adapter, "resolve_property_name", lambda page_id, names, ptype: names[-1])
    monkeypatch.setattr(time_tracker.notion_adapter, "read_number_property", fake.read)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_number_property", fake.write)
    _seed_task_and_goal()
    (pid,) = _run_with([_proposal("task", TASK_ID, "2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00")])
    summary = time_tracker.settle_task_time(now=NOW)
    assert "not number" in summary["errors"][0]["error"]
    assert fake.writes == []
    assert db.get_proposal(pid)["settled_at"] is None


def test_verification_mismatch_is_an_error(monkeypatch):
    fake = FakeNotion()
    monkeypatch.setattr(time_tracker.notion_adapter, "resolve_property_name", lambda page_id, names, ptype: names[-1])
    monkeypatch.setattr(time_tracker.notion_adapter, "read_number_property", fake.read)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_number_property", lambda *a: 99.0)
    _seed_task_and_goal()
    (pid,) = _run_with([_proposal("task", TASK_ID, "2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00")])
    summary = time_tracker.settle_task_time(now=NOW)
    assert "Notion reports 99.0" in summary["errors"][0]["error"]
    assert db.get_proposal(pid)["settled_at"] is None


def test_init_db_adds_time_tracking_columns_to_old_tables(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, task TEXT NOT NULL, time_to_complete_hr REAL, deadline TEXT, priority TEXT, location TEXT, status TEXT, source TEXT NOT NULL, source_task_id TEXT NOT NULL, last_modified TEXT, deleted_at TEXT, raw_json TEXT, UNIQUE(source, source_task_id))")  # no scheduled_event_ids: the live DB's shape
        conn.execute("CREATE TABLE proposed_events (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, item_type TEXT NOT NULL, item_id TEXT, title TEXT, description TEXT, location TEXT, start TEXT NOT NULL, end TEXT NOT NULL, all_day INTEGER DEFAULT 0, timezone TEXT, rationale TEXT, status TEXT NOT NULL DEFAULT 'proposed', invalid_reason TEXT, created_at TEXT, decided_at TEXT, promoted_event_id TEXT)")
    monkeypatch.setattr(db, "DB_PATH", old)
    db.init_db()
    with sqlite3.connect(old) as conn:
        task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert {"time_spent_hr", "scheduled_event_ids"} <= task_cols
        assert {"settled_at", "settled_hours"} <= {r[1] for r in conn.execute("PRAGMA table_info(proposed_events)")}
    # promotion on a migrated table must work end to end
    db.upsert_task({"source": "notion", "source_task_id": PAGE, "task": "T", "time_to_complete_hr": 1.0,
                    "deadline": None, "priority": None, "location": None, "status": "Not started", "last_modified": "x"})
    db.upsert_goal({"source": "notion", "source_goal_id": "g", "goal": "G", "frequency": None, "end_date": None,
                    "required_time_hr": None, "priority": None, "location": None, "days_of_week": None,
                    "time_of_day_window": None, "status": "In progress", "last_modified": "x"})
    db.init_db()
    (pid,) = _run_with([_proposal("task", TASK_ID, "2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00")])
    assert db.get_tasks()[0]["scheduled_event_ids"] == f'["optimizer_{pid}"]'


# --- goals: Last Occurred ---------------------------------------------------------


def test_goal_occurrence_moves_last_occurred_forward_once(notion):
    _seed_task_and_goal()
    early, late, future = _run_with([
        _proposal("goal", "notion_g1", "2026-09-21T18:00:00-04:00", "2026-09-21T20:00:00-04:00"),
        _proposal("goal", "notion_g1", "2026-09-23T18:00:00-04:00", "2026-09-23T20:00:00-04:00"),
        _proposal("goal", "notion_g1", "2026-09-30T18:00:00-04:00", "2026-09-30T20:00:00-04:00"),
    ])
    summary = time_tracker.settle_task_time(now=NOW)
    assert [(s["proposal_id"], s["occurred_on"]) for s in summary["settled"]] == [(early, "2026-09-21"), (late, "2026-09-23")]
    assert summary["pending"] == 1
    assert notion.date_writes == [(GOAL_PAGE, "Last Occurred", "2026-09-21"), (GOAL_PAGE, "Last Occurred", "2026-09-23")]
    assert notion.date_reads == [(GOAL_PAGE, "Last Occurred")] * 2  # read fresh before each write
    assert db.get_goals()[0]["last_occurred"] == "2026-09-23"
    assert db.get_proposal(early)["settled_hours"] == 2.0
    assert db.get_proposal(future)["settled_at"] is None

    again = time_tracker.settle_task_time(now=NOW)
    assert again["settled"] == [] and len(notion.date_writes) == 2  # idempotent


def test_goal_occurrence_older_than_notion_value_is_not_written(monkeypatch):
    fake = FakeNotion(last_occurred="2026-09-24")
    monkeypatch.setattr(time_tracker.notion_adapter, "read_date_property", fake.read_date)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_date_property", fake.write_date)
    _seed_task_and_goal()
    (pid,) = _run_with([_proposal("goal", "notion_g1", "2026-09-21T18:00:00-04:00", "2026-09-21T20:00:00-04:00")])
    summary = time_tracker.settle_task_time(now=NOW)
    entry = summary["settled"][0]
    assert entry["notion_before"] == "2026-09-24" and entry["notion_after"] == "2026-09-24"
    assert "not written" in entry["note"]
    assert fake.date_writes == []
    assert db.get_proposal(pid)["settled_at"] is not None  # done; never re-examined
    assert db.get_goals()[0]["last_occurred"] == "2026-09-24"


def test_goal_dry_run_and_cancelled_and_failure(monkeypatch):
    fake = FakeNotion()
    monkeypatch.setattr(time_tracker.notion_adapter, "read_date_property", fake.read_date)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_date_property", fake.write_date)
    _seed_task_and_goal()
    done, cancelled = _run_with([
        _proposal("goal", "notion_g1", "2026-09-21T18:00:00-04:00", "2026-09-21T20:00:00-04:00"),
        _proposal("goal", "notion_g1", "2026-09-22T18:00:00-04:00", "2026-09-22T20:00:00-04:00"),
    ])
    db.soft_delete_event("optimizer", cancelled, "2026-09-23T00:00:00+00:00")

    dry = time_tracker.settle_task_time(now=NOW, dry_run=True)
    assert dry["settled"][0]["notion_after"] == "2026-09-21"
    assert dry["skipped"][0]["reason"].startswith("calendar event was cancelled")
    assert fake.date_writes == [] and db.get_proposal(done)["settled_at"] is None

    fake.fail_on_write = True
    failed = time_tracker.settle_task_time(now=NOW)
    assert "503" in failed["errors"][0]["error"]
    assert db.get_proposal(done)["settled_at"] is None
    assert db.get_proposal(cancelled)["settled_hours"] == 0.0  # cancelled one is settled without a write

    fake.fail_on_write = False
    ok = time_tracker.settle_task_time(now=NOW)
    assert [s["proposal_id"] for s in ok["settled"]] == [done]
    assert fake.date_writes == [(GOAL_PAGE, "Last Occurred", "2026-09-21")] * 2  # the failed attempt, then the retry



def test_a_write_that_landed_is_not_added_again_on_retry(notion, monkeypatch):
    # Notion took the write, then the pass failed before the block was marked
    # settled (a reply timeout, a locked database): the retry must not add twice.
    notion.values[PAGE] = 1.0
    _seed_task_and_goal()
    (pid,) = _run_with([_proposal("task", TASK_ID, "2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00")])
    real_mark = db.mark_proposal_settled
    monkeypatch.setattr(db, "mark_proposal_settled", lambda *a: (_ for _ in ()).throw(RuntimeError("database is locked")))
    first = time_tracker.settle_task_time(now=NOW)
    assert first["errors"] and notion.values[PAGE] == 3.0
    monkeypatch.setattr(db, "mark_proposal_settled", real_mark)
    retry = time_tracker.settle_task_time(now=NOW)
    assert [s["proposal_id"] for s in retry["settled"]] == [pid]
    assert notion.values[PAGE] == 3.0 and len(notion.writes) == 1
    assert db.get_proposal(pid)["settled_at"] is not None


def test_only_one_settle_pass_runs_at_a_time(notion):
    time_tracker._SETTLE_LOCK.acquire()
    try:
        assert "busy" in time_tracker.settle_task_time(now=NOW)
    finally:
        time_tracker._SETTLE_LOCK.release()
