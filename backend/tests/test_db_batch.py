"""db.batch(): many single-row writes, one transaction."""
from __future__ import annotations

import pytest

import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()


def _goal(gid):
    return {"source": "notion", "source_goal_id": gid, "goal": gid, "frequency": None, "end_date": None,
            "required_time_hr": None, "priority": None, "location": None, "days_of_week": None,
            "time_of_day_window": None, "status": "In-Progress", "last_modified": "x"}


def test_batch_commits_once_and_rolls_back_on_error():
    db.upsert_goals([_goal("a"), _goal("b")])
    assert db.get_active_source_goal_ids("notion") == {"a", "b"}
    with pytest.raises(RuntimeError):
        with db.batch():
            db.upsert_goal(_goal("c"))
            with db.batch():  # nested: joins the outer one
                db.upsert_goal(_goal("d"))
            raise RuntimeError("Notion page malformed")
    assert db.get_active_source_goal_ids("notion") == {"a", "b"}
