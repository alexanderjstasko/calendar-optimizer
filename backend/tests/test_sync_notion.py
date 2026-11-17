"""sync_notion against a temp database with the Notion fetch mocked: untitled rows are skipped."""

from __future__ import annotations

import pytest

import db
import sync_notion


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()


def _goal(page_id, name):
    return {"source": "notion", "source_goal_id": page_id, "goal": name, "frequency": "1x Week",
            "end_date": None, "required_time_hr": 1, "priority": None, "location": None,
            "days_of_week": None, "time_of_day_window": None, "show_on_calendar": 1,
            "notes_for_llm": None, "last_occurred": None, "status": "In progress",
            "last_modified": "2026-10-06T00:00:00Z", "raw_json": {}}


def _task(page_id, name):
    return {"source": "notion", "source_task_id": page_id, "task": name, "time_to_complete_hr": 2,
            "time_spent_hr": 0, "deadline": None, "priority": None, "location": None,
            "status": "Not started", "last_modified": "2026-10-06T00:00:00Z", "raw_json": {}}


def test_a_blank_goal_row_no_longer_breaks_the_sync(monkeypatch):
    rows = [_goal("g1", "Water Plants"), _goal("g2", None), _goal("g3", "  "), _goal("g4", "Laundry")]
    monkeypatch.setattr(sync_notion.notion_adapter, "sync_goals", lambda: rows)
    sync_notion.sync_goals()
    assert sorted(g["goal"] for g in db.get_goals()) == ["Laundry", "Water Plants"]


def test_clearing_a_title_for_a_moment_does_not_soft_delete_the_goal(monkeypatch):
    monkeypatch.setattr(sync_notion.notion_adapter, "sync_goals", lambda: [_goal("g1", "Water Plants")])
    sync_notion.sync_goals()
    monkeypatch.setattr(sync_notion.notion_adapter, "sync_goals", lambda: [_goal("g1", None)])
    sync_notion.sync_goals()
    assert [g["goal"] for g in db.get_goals()] == ["Water Plants"]  # still active, last good title


def test_blank_task_rows_are_skipped_too(monkeypatch):
    monkeypatch.setattr(sync_notion.notion_adapter, "sync_tasks", lambda: [_task("t1", "Taxes"), _task("t2", None)])
    sync_notion.sync_tasks()
    assert [t["task"] for t in db.get_tasks()] == ["Taxes"]


def test_sync_keeps_rows_skipped_for_their_level_and_refuses_an_empty_answer(monkeypatch):
    import notion_adapter
    import sync_notion
    goal = {"source": "notion", "source_goal_id": "g1", "goal": "Run", "frequency": None, "end_date": None,
            "required_time_hr": 1.0, "priority": None, "location": None, "days_of_week": None,
            "time_of_day_window": None, "status": "In-Progress", "last_modified": "x"}
    monkeypatch.setattr(notion_adapter, "fetch_tree", lambda: {"nodes": [], "goals": [goal], "tasks": [], "skipped": []})
    sync_notion.sync_notion()
    assert db.get_active_source_goal_ids("notion") == {"g1"}
    # Its Level cleared by mistake: skipped, but not deleted.
    monkeypatch.setattr(notion_adapter, "fetch_tree", lambda: {"nodes": [], "goals": [], "tasks": [],
                                                              "skipped": [{"id": "g1", "title": "Run", "level": None}]})
    assert sync_notion.sync_notion()["goals_deleted"] == 0
    assert db.get_active_source_goal_ids("notion") == {"g1"}
    # Nothing at all (a revoked share): an error, and nothing changes.
    monkeypatch.setattr(notion_adapter, "fetch_tree", lambda: {"nodes": [], "goals": [], "tasks": [], "skipped": []})
    with pytest.raises(RuntimeError):
        sync_notion.sync_notion()
    assert db.get_active_source_goal_ids("notion") == {"g1"}
