"""preferences.py and the decision inputs in db.py, against a temp database and a temp preferences file."""

from __future__ import annotations

import datetime as dt
import json

import pytest

import db
import llm_client
import preferences


@pytest.fixture(autouse=True)
def temp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()
    monkeypatch.setenv("LLM_PREFERENCES_PATH", str(tmp_path / "preferences.md"))
    monkeypatch.delenv("LLM_LEARNED_PREFERENCES_PATH", raising=False)
    monkeypatch.setenv("LLM_LEARN_EVERY_DECISIONS", "3")
    monkeypatch.setenv("LLM_BACKEND", "api")


def _proposal(title, start, end, item_type="task", status="proposed"):
    return {"item_type": item_type, "item_id": "x", "title": title, "start": start, "end": end, "all_day": False,
            "timezone": "America/New_York", "status": status, "description": "", "location": "", "rationale": ""}


def _run(proposals):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    run_id = f"run_{len(db.get_optimizer_runs())}"
    db.create_optimizer_run(run_id, now, "api", "m", "a", "b")
    return db.insert_proposals(run_id, proposals, now)


def test_preferences_file_is_created_and_parsed():
    prefs = preferences.load()
    assert prefs == {"stated": "", "learned": ""}
    path = preferences.path()
    text = path.read_text()
    text = text.replace("(Nothing yet. Examples: \"Keep Friday evenings free.\" \"Deep work is best\nbefore noon.\" \"I prefer two long task blocks to many short ones.\")",
                        "Keep Friday evenings free.")
    path.write_text(text)
    assert preferences.load()["stated"] == "Keep Friday evenings free."
    preferences.write_learned("- Evenings are approved, mornings rejected.")
    assert preferences.load() == {"stated": "Keep Friday evenings free.", "learned": "- Evenings are approved, mornings rejected."}
    assert path.read_text() == text  # the person's file is never written
    assert preferences.learned_path() == path.with_name("preferences.learned.md")
    assert "- Evenings are approved" in preferences.learned_path().read_text()


def test_legacy_learned_section_is_read_until_the_first_rewrite():
    path = preferences.path()
    path.write_text("# Preferences\n\n## Stated\n\nNo Mondays.\n\n## Learned from your decisions\n\n"
                    f"{preferences.START_MARK}\n- old bullet\n{preferences.END_MARK}\n")
    assert preferences.load() == {"stated": "No Mondays.", "learned": "- old bullet"}
    preferences.write_learned("- new bullet")
    assert preferences.load() == {"stated": "No Mondays.", "learned": "- new bullet"}  # learned file wins
    assert "- old bullet" in path.read_text()  # untouched; the person can delete the old section


def test_hand_edits_to_the_learned_file_are_read_without_the_header():
    preferences.write_learned("- first")
    preferences.learned_path().write_text(preferences.LEARNED_HEADER + "\n\n- corrected by hand\n")
    assert preferences.load()["learned"] == "- corrected by hand"


def test_reschedule_keeps_original_times_and_feeds_history():
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    (pid,) = _run([_proposal("Deep work", "2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00")])
    assert db.reschedule_proposal(pid, "2026-09-22T19:30", "2026-09-22T21:00", now) is True
    p = db.get_proposal(pid)
    assert (p["start"], p["end"]) == ("2026-09-22T19:30:00-04:00", "2026-09-22T21:00:00-04:00")
    assert (p["original_start"], p["original_end"]) == ("2026-09-22T17:00:00-04:00", "2026-09-22T19:00:00-04:00")
    assert db.reschedule_proposal(pid, "2026-09-22T20:00", "2026-09-22T21:30", now) is True
    assert db.get_proposal(pid)["original_start"] == "2026-09-22T17:00:00-04:00"  # first original kept
    with pytest.raises(ValueError):
        db.reschedule_proposal(pid, "2026-09-22T21:00", "2026-09-22T20:00", now)
    assert db.promote_proposal(pid, now)
    assert db.reschedule_proposal(pid, "2026-09-23T10:00", "2026-09-23T11:00", now) is False  # decided
    (eid,) = _run([_proposal("Concert", "2026-10-03T20:00:00-04:00", "2026-10-03T22:00:00-04:00", item_type="event")])
    with pytest.raises(ValueError, match="cannot be moved"):
        db.reschedule_proposal(eid, "2026-10-03T15:00", "2026-10-03T17:00", now)
    assert db.get_proposal(eid)["start"] == "2026-10-03T20:00:00-04:00"
    hist = db.get_plan_decision_history()
    assert hist[0]["status"] == "promoted" and hist[0]["original_start"] == "2026-09-22T17:00:00-04:00"
    assert db.get_schedule_x_events(calendar_id="optimizer")[0]["start"].startswith("2026-09-22T20:00:00-04:00")


def test_decision_stats_by_kind_and_slot():
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    ids = _run([
        _proposal("A", "2026-09-22T09:00:00-04:00", "2026-09-22T10:00:00-04:00"),           # weekday morning, reject
        _proposal("B", "2026-09-22T18:00:00-04:00", "2026-09-22T20:00:00-04:00"),           # weekday evening, approve
        _proposal("C", "2026-09-26T13:00:00-04:00", "2026-09-26T16:00:00-04:00"),           # weekend, approve
        _proposal("D", "2026-09-23T18:00:00-04:00", "2026-09-23T20:00:00-04:00", "goal"),   # goal, moved later then approved
        _proposal("E", "2026-09-24T18:00:00-04:00", "2026-09-24T20:00:00-04:00"),           # untouched
    ])
    db.set_proposal_status(ids[0], "rejected", now)
    db.promote_proposal(ids[1], now)
    db.promote_proposal(ids[2], now)
    db.reschedule_proposal(ids[3], "2026-09-23T19:00", "2026-09-23T21:00", now)
    db.promote_proposal(ids[3], now)
    stats = db.plan_decision_stats()
    assert stats["decisions"] == 4 and db.count_decisions() == 4
    assert stats["by_kind"]["task"] == {"approved": 2, "rejected": 1, "moved": 0, "approval_rate": 0.67}
    assert stats["by_kind"]["goal"] == {"approved": 1, "rejected": 0, "moved": 1, "approval_rate": 1.0}
    assert stats["by_slot"]["weekday morning"]["approval_rate"] == 0.0
    assert stats["by_slot"]["weekday evening"]["approval_rate"] == 1.0
    assert stats["reschedules"] == {"count": 1, "moved_later": 1, "moved_earlier": 0, "median_shift_minutes": 60}
    ctx = preferences.person_context()
    assert ctx["decision_stats"]["decisions"] == 4
    moved = next(d for d in ctx["recent_decisions"] if d["title"] == "D")
    assert moved["decision"] == "approved" and moved["moved_from"]["start"] == "2026-09-23T18:00:00-04:00"


def test_refresh_waits_for_enough_decisions_then_rewrites_note(monkeypatch):
    calls = []

    def fake(system, user, schema, **kw):
        calls.append(json.loads(user))
        return llm_client.LLMResult(data={"learned": "- Evening task blocks get approved (3 of 3).", "changed": "first note"},
                                    backend="api", model="m", usage={"input_tokens": 50, "output_tokens": 20})

    monkeypatch.setattr(preferences.llm_client, "complete_structured", fake)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    ids = _run([_proposal(t, f"2026-09-2{i}T18:00:00-04:00", f"2026-09-2{i}T20:00:00-04:00") for i, t in enumerate(["A", "B", "C"], start=2)])
    db.promote_proposal(ids[0], now)
    db.promote_proposal(ids[1], now)

    out = preferences.refresh_learned()
    assert out == {"refreshed": False, "new_decisions": 2, "threshold": 3} and calls == []

    db.promote_proposal(ids[2], now)
    out = preferences.refresh_learned()
    assert out["refreshed"] is True and out["new_decisions"] == 3 and out["changed"] == "first note"
    assert preferences.load()["learned"] == "- Evening task blocks get approved (3 of 3)."
    assert calls[0]["decision_stats"]["decisions"] == 3 and len(calls[0]["recent_decisions"]) == 3
    assert db.get_setting(preferences.SETTING_SEEN) == "3"

    again = preferences.refresh_learned()
    assert again["refreshed"] is False and again["new_decisions"] == 0 and len(calls) == 1
    forced = preferences.refresh_learned(force=True)
    assert forced["refreshed"] is True and len(calls) == 2
    assert calls[1]["previous_learned_note"] == "- Evening task blocks get approved (3 of 3)."


def test_refresh_survives_model_failure(monkeypatch):
    def boom(*a, **k):
        raise llm_client.LLMRateLimitError("slow down")

    monkeypatch.setattr(preferences.llm_client, "complete_structured", boom)
    preferences.write_learned("- old note")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    (pid,) = _run([_proposal("A", "2026-09-22T18:00:00-04:00", "2026-09-22T20:00:00-04:00")])
    db.promote_proposal(pid, now)
    out = preferences.refresh_learned(force=True)
    assert out["refreshed"] is False and "LLMRateLimitError" in out["error"]
    assert preferences.load()["learned"] == "- old note"
