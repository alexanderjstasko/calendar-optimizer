"""
scout.py against a throwaway database with the model, the site fetchers and
Notion replaced by fakes. Covers the schedule filter, recommendation
validation and storage, decisions, and settlement of attended events.
"""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

import db
import event_sources
import hours
import llm_client
import scout
import time_tracker
from optimizer_schema import DISPLAY_TIMEZONE

NY = ZoneInfo(DISPLAY_TIMEZONE)
NOW = dt.datetime(2026, 9, 20, 9, 0, tzinfo=NY)  # a Sunday morning
SITE_PAGE = "site-page-1"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.setenv("LLM_MODEL", "claude-opus-5")
    monkeypatch.setenv("LLM_HOME_LOCATION", "1 Home St")
    monkeypatch.setenv("LLM_FREE_TIME_HOURS_PER_WEEK", "10")
    monkeypatch.delenv("LLM_SCOUT_PROMPT_PATH", raising=False)
    # .env may set the old LLM_SCOUT_MAX_RECOMMENDATIONS; the tests use the code defaults (16 / 8)
    for name in ("LLM_SCOUT_MAX_RECOMMENDATIONS", "LLM_SCOUT_SHORTLIST", "LLM_SCOUT_PAGE_SIZE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_PREFERENCES_PATH", str(tmp_path / "preferences.md"))
    monkeypatch.setattr(scout.preferences, "refresh_learned", lambda **kw: {"refreshed": False})


def _seed():
    db.upsert_website({"source": "notion", "source_website_id": SITE_PAGE, "url": "https://www.nypl.org/events/calendar",
                       "upcoming_event": None, "events_attended": 2, "last_modified": "x"})
    db.upsert_goal({"source": "notion", "source_goal_id": "talks", "goal": "Talks & lectures", "frequency": "2x Month",
                    "end_date": None, "required_time_hr": 2.0, "priority": "Medium", "location": None,
                    "days_of_week": None, "time_of_day_window": "Afterwork", "show_on_calendar": 1,
                    "notes_for_llm": "Architecture, photography, city history.", "status": "In progress", "last_modified": "x"})
    db.upsert_event({"source": "google", "source_event_id": "dinner", "calendar_id": "google_primary", "title": "Dinner",
                     "start": "2026-09-25T19:00:00-04:00", "end": "2026-09-25T21:00:00-04:00", "all_day": False,
                     "timezone": DISPLAY_TIMEZONE, "last_modified": "x"})


def _ev(title, start, end=None, **kw):
    return event_sources.normalize("nypl.org", title, start, end, **kw)


def _discover(events, website_id="notion_site-page-1"):
    return db.upsert_discovered_events(events, website_id, "2026-09-20T12:00:00+00:00")


# --- discovery storage --------------------------------------------------------------


def test_discovered_events_upsert_dedupes_and_keeps_decisions():
    _seed()
    a = _ev("Talk A", "2026-10-01T19:00:00-04:00", "2026-10-01T20:30:00-04:00", url="https://a")
    assert _discover([a]) == (1, 0)
    db.set_discovered_decision(a["id"], "rejected")
    again = _ev("talk a", "2026-10-01T19:00:00-04:00", url="https://a2")  # same site/title/date -> same id
    assert _discover([again]) == (0, 1)
    row = db.get_discovered_event(a["id"])
    assert row["url"] == "https://a2" and row["decision"] == "rejected"  # refreshed, decision untouched
    assert db.get_discovered_events("2026-09-20", "2026-10-20", undecided_only=True) == []
    assert len(db.get_discovered_events("2026-09-20", "2026-10-20", undecided_only=False)) == 1
    assert db.get_decision_history()[0]["decision"] == "rejected"


# --- fetch --------------------------------------------------------------------------------


def test_scout_fetch_records_per_site_status_and_only_window_events(monkeypatch):
    _seed()
    db.upsert_website({"source": "notion", "source_website_id": "p2", "url": "https://www.92ny.org/whats-on/calendar",
                       "last_modified": "x"})
    db.upsert_website({"source": "notion", "source_website_id": "p3", "url": "https://broken.example/x", "last_modified": "x"})
    monkeypatch.setattr(scout, "sync_websites", lambda: {"upserted": 3, "soft_deleted": 0})

    def fake_fetch(url, llm_extract=None):
        if "nypl" in url:
            return [_ev("In window", (dt.date.today() + dt.timedelta(days=3)).isoformat()),
                    _ev("Too far", (dt.date.today() + dt.timedelta(days=90)).isoformat())], "api"
        if "92ny" in url:
            return [], "unreadable"
        raise ConnectionError("boom")

    monkeypatch.setattr(scout.event_sources, "fetch_for_url", fake_fetch)
    summary = scout.scout_fetch()
    assert summary["new"] == 1 and summary["errors"] == 1
    by_url = {s["url"]: s for s in summary["sites"]}
    assert by_url["https://www.nypl.org/events/calendar"]["in_window"] == 1
    assert by_url["https://www.92ny.org/whats-on/calendar"]["method"] == "unreadable"
    assert "ConnectionError" in by_url["https://broken.example/x"]["error"]
    sites = {w["url"]: w for w in db.get_websites()}
    assert sites["https://www.nypl.org/events/calendar"]["last_fetch_method"] == "api"
    assert sites["https://broken.example/x"]["last_fetch_method"] == "error"


# --- schedule filter ----------------------------------------------------------------------


def test_filter_candidates_keeps_free_evenings_and_weekends():
    _seed()
    ws, we = scout._candidate_window(NOW, 30)
    busy = [(dt.datetime(2026, 9, 25, 19, 0, tzinfo=NY), dt.datetime(2026, 9, 25, 21, 0, tzinfo=NY))]
    events = [
        _ev("Weekday evening ok", "2026-09-22T19:00:00-04:00", "2026-09-22T20:30:00-04:00"),
        _ev("Weekday afternoon", "2026-09-22T14:00:00-04:00"),
        _ev("Saturday afternoon ok", "2026-09-26T14:00:00-04:00"),
        _ev("Late night", "2026-09-26T23:00:00-04:00"),
        _ev("Clashes with dinner", "2026-09-25T18:45:00-04:00", "2026-09-25T20:00:00-04:00"),
        _ev("All day fair", "2026-10-03"),
        _ev("Outside window", "2026-11-30T19:00:00-05:00"),
        _ev("Already past", "2026-09-19T19:00:00-04:00"),
    ]
    kept, dropped = scout.filter_candidates(events, busy, ws, we)
    assert [e["title"] for e in kept] == ["Weekday evening ok", "Saturday afternoon ok", "All day fair"]
    assert dropped == {"past_or_outside": 2, "before_free": 1, "after_free": 1, "busy": 1, "unparseable": 0}


def test_filter_candidates_follows_the_saved_free_hours():
    _seed()
    ws, we = scout._candidate_window(NOW, 30)
    # Free time is derived: weekdays from the end of work (15:00) to bed (23:00),
    # weekends from waking (12:00) to bed (01:00, i.e. midnight on the clock).
    saved = hours.validate({
        "weekdays": {"sleep": {"start": "23:00", "end": "07:00"}, "work": {"start": "09:00", "end": "15:00"}},
        "weekends": {"sleep": {"start": "01:00", "end": "12:00"}, "work": None},
    })
    events = [
        _ev("Weekday late afternoon now ok", "2026-09-22T16:00:00-04:00", "2026-09-22T17:00:00-04:00"),
        _ev("Weekday 23:30 too late", "2026-09-22T23:30:00-04:00"),
        _ev("Saturday morning too early", "2026-09-26T11:00:00-04:00"),
        _ev("Saturday evening ok", "2026-09-26T21:30:00-04:00"),
    ]
    kept, dropped = scout.filter_candidates(events, [], ws, we, free_hours=saved)
    assert [e["title"] for e in kept] == ["Weekday late afternoon now ok", "Saturday evening ok"]
    assert dropped["before_free"] == 1 and dropped["after_free"] == 1
    # Without the argument the filter reads what the Hours tab saved.
    hours.save(saved)
    kept, _ = scout.filter_candidates(events, [], ws, we)
    assert len(kept) == 2


def test_filter_candidates_leaves_time_to_get_there_after_work():
    _seed()
    ws, we = scout._candidate_window(NOW, 30)
    saved = hours.validate({
        "weekdays": {"sleep": {"start": "23:00", "end": "07:00"}, "work": {"start": "09:00", "end": "18:00"}},
        "weekends": {"sleep": {"start": "23:00", "end": "09:00"}, "work": None},
    })
    events = [
        _ev("Right as work ends", "2026-09-22T18:00:00-04:00", "2026-09-22T19:00:00-04:00"),
        _ev("Twenty minutes after", "2026-09-22T18:20:00-04:00", "2026-09-22T19:30:00-04:00"),
        _ev("Half an hour after ok", "2026-09-22T18:30:00-04:00", "2026-09-22T19:30:00-04:00"),
        _ev("Saturday at six ok", "2026-09-26T18:00:00-04:00", "2026-09-26T19:00:00-04:00"),
    ]
    kept, dropped = scout.filter_candidates(events, [], ws, we, free_hours=saved)
    assert [e["title"] for e in kept] == ["Half an hour after ok", "Saturday at six ok"]
    assert dropped["before_free"] == 2


def test_too_soon_after_work_reads_the_work_day_only():
    saved = hours.validate({
        "weekdays": {"sleep": {"start": "23:00", "end": "07:00"}, "work": {"start": "09:00", "end": "17:00"}},
        "weekends": {"sleep": {"start": "23:00", "end": "09:00"}, "work": None},
    })
    tue = lambda hh, mm: dt.datetime(2026, 9, 22, hh, mm, tzinfo=NY)  # noqa: E731
    assert hours.too_soon_after_work(tue(17, 0), saved)
    assert hours.too_soon_after_work(tue(17, 29), saved)
    assert not hours.too_soon_after_work(tue(17, 30), saved)
    assert not hours.too_soon_after_work(dt.datetime(2026, 9, 26, 17, 0, tzinfo=NY), saved)  # Saturday
    assert hours.too_soon_after_work(dt.datetime(2026, 9, 22, 21, 0, tzinfo=dt.timezone.utc), saved)  # 17:00 in New York


# --- recommend + validate -----------------------------------------------------------------


def _fake_llm(monkeypatch, data, captured=None):
    def fake_complete(system, user, schema, **kw):
        if captured is not None:
            captured.update(system=system, user=json.loads(user), schema=schema, kw=kw)
        return llm_client.LLMResult(data=data, backend="api", model="claude-opus-5",
                                    usage={"input_tokens": 500, "output_tokens": 80}, cost_usd=0.02)
    monkeypatch.setattr(scout.llm_client, "complete_structured", fake_complete)


def test_recommend_stores_event_proposals_and_supersedes_only_events(monkeypatch):
    _seed()
    good = _ev("Calvert Vaux lecture", "2026-09-27T14:00:00-04:00", "2026-09-27T16:00:00-04:00", url="https://nypl/x", cost="Free")
    other = _ev("Photo talk", "2026-10-02T18:00:00-04:00", "2026-10-02T19:30:00-04:00")
    _discover([good, other])
    # an unrelated pending optimizer proposal must survive a scout run
    db.create_optimizer_run("run_opt", "t", "api", "m", "a", "b")
    db.insert_proposals("run_opt", [{"item_type": "goal", "item_id": "notion_talks", "title": "x", "start": "2026-09-28T18:00:00-04:00",
                                     "end": "2026-09-28T20:00:00-04:00", "all_day": False, "timezone": DISPLAY_TIMEZONE, "status": "proposed"}], "t")
    captured = {}
    _fake_llm(monkeypatch, {"recommendations": [
        {"discovered_event_id": good["id"], "start": "2026-09-27T14:00:00-04:00", "end": "2026-09-27T16:00:00-04:00",
         "goal_id": "notion_talks", "why": "Architecture, free.", "schedule_fit": "Sunday, clear.", "confidence": 0.8},
        {"discovered_event_id": "nope", "start": "2026-09-28T19:00:00-04:00", "end": "2026-09-28T20:00:00-04:00",
         "goal_id": "", "why": "?", "schedule_fit": "?", "confidence": 0.1},
        {"discovered_event_id": other["id"], "start": "2026-10-03T18:00:00-04:00", "end": "2026-10-03T19:30:00-04:00",
         "goal_id": "notion_ghost", "why": "x", "schedule_fit": "x", "confidence": 0.5},
    ], "notes": "Two picks."}, captured)

    summary = scout.scout_recommend(now=NOW)
    assert summary["kind"] == "scout" and summary["proposed"] == 1 and summary["invalid"] == 2
    assert "unknown candidate id" in summary["invalid_reasons"][0]
    assert "differs from the candidate" in summary["invalid_reasons"][1] or "unknown goal id" in summary["invalid_reasons"][1]
    assert captured["kw"]["tool_name"] == "submit_recommendations"
    payload = captured["user"]
    assert [c["id"] for c in payload["candidates"]] == [good["id"], other["id"]]
    assert payload["preferences"]["free_time_hours_per_week"] == 10 and payload["preferences"]["shortlist_size"] == 16
    assert payload["goals"][0]["notes_for_llm"].startswith("Architecture")
    assert "submit_recommendations" in captured["system"] and "history" in captured["system"]
    assert payload["person"] == {"stated_preferences": "", "learned_preferences": ""}

    rows = db.get_proposals(run_id=summary["run_id"])
    ok = next(r for r in rows if r["status"] == "proposed")
    assert (ok["item_type"], ok["item_id"], ok["goal_id"], ok["url"], ok["website_id"]) == ("event", good["id"], "notion_talks", "https://nypl/x", "notion_site-page-1")
    assert ok["title"] == "Calvert Vaux lecture" and "Free" in ok["description"] and "Fit:" in ok["rationale"]
    assert ok["cost"] == "Free"
    assert db.get_discovered_event(good["id"])["recommended_at"] is not None
    assert db.get_proposals(run_id="run_opt")[0]["status"] == "proposed"  # optimizer proposal untouched
    run = db.get_optimizer_run(summary["run_id"])
    assert run["kind"] == "scout" and json.loads(run["payload_json"])["window"]["start"] == summary["window_start"]

    # a second scout run supersedes the first run's pending event
    second = scout.scout_recommend(now=NOW + dt.timedelta(days=1))
    assert second["superseded"] == 1
    assert db.get_proposals(run_id=summary["run_id"])[0]["status"] in ("superseded", "invalid")


def test_recommend_with_no_candidates_makes_no_call(monkeypatch):
    _seed()
    monkeypatch.setattr(scout.llm_client, "complete_structured", lambda *a, **k: pytest.fail("called"))
    summary = scout.scout_recommend(now=NOW)
    assert summary["proposed"] == 0 and summary["run_id"] is None


def test_dry_run_estimates_only(monkeypatch):
    _seed()
    _discover([_ev("T", "2026-09-27T14:00:00-04:00")])
    monkeypatch.setattr(scout.llm_client, "complete_structured", lambda *a, **k: pytest.fail("called"))
    monkeypatch.setattr(scout.llm_client, "estimate_tokens", lambda *a, **k: (777, "count_tokens"))
    summary = scout.scout_recommend(now=NOW, dry_run=True)
    assert summary["dry_run"] and summary["input_tokens_estimate"] == 777 and db.get_optimizer_runs() == []


# --- decisions + settlement -----------------------------------------------------------------


class FakeNotion:
    def __init__(self):
        self.numbers = {SITE_PAGE: 2}
        self.texts = {SITE_PAGE: ""}
        self.dates = {"talks": None}
        self.writes = []

    def read_number(self, page, name): return self.numbers[page]
    def write_number(self, page, name, v): self.writes.append((name, v)); self.numbers[page] = v; return v
    def read_text(self, page, name): return self.texts[page]
    def write_text(self, page, name, v): self.writes.append((name, v)); self.texts[page] = v; return v
    def read_date(self, page, name): return self.dates[page]
    def write_date(self, page, name, v): self.writes.append((name, v)); self.dates[page] = v; return v


@pytest.fixture
def notion(monkeypatch):
    fake = FakeNotion()
    for mod in (scout.notion_adapter, time_tracker.notion_adapter):
        monkeypatch.setattr(mod, "read_number_property", fake.read_number)
        monkeypatch.setattr(mod, "update_number_property", fake.write_number)
        monkeypatch.setattr(mod, "read_text_property", fake.read_text)
        monkeypatch.setattr(mod, "update_text_property", fake.write_text)
        monkeypatch.setattr(mod, "read_date_property", fake.read_date)
        monkeypatch.setattr(mod, "update_date_property", fake.write_date)
    return fake


def _recommended_proposal(monkeypatch, start="2026-09-27T14:00:00-04:00", end="2026-09-27T16:00:00-04:00"):
    ev = _ev("Calvert Vaux lecture", start, end, url="https://nypl/x")
    _discover([ev])
    _fake_llm(monkeypatch, {"recommendations": [{"discovered_event_id": ev["id"], "start": start, "end": end,
                                                 "goal_id": "notion_talks", "why": "w", "schedule_fit": "f", "confidence": 0.9}],
                            "notes": ""})
    run_id = scout.scout_recommend(now=NOW)["run_id"]
    return ev, db.get_proposals(run_id=run_id)[0]


def test_approve_writes_upcoming_event_and_marks_goal(monkeypatch, notion):
    _seed()
    ev, proposal = _recommended_proposal(monkeypatch)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    event_id = db.promote_proposal(proposal["id"], now)
    out = scout.record_decision(db.get_proposal(proposal["id"]), "approved")
    assert out["upcoming_event"] == "Calvert Vaux lecture — 2026-09-27"
    assert notion.writes == [("Upcoming Event", "Calvert Vaux lecture — 2026-09-27")]
    assert db.get_website("notion_site-page-1")["upcoming_event"] == "Calvert Vaux lecture — 2026-09-27"
    assert db.get_discovered_event(ev["id"])["decision"] == "approved"
    assert json.loads(db.get_goals()[0]["scheduled_event_ids"]) == [event_id]
    shown = db.get_schedule_x_events(calendar_id="optimizer")
    assert shown[0]["title"] == "Calvert Vaux lecture"

    # revoke: event leaves the calendar, decision flips to rejected, goal link removed
    assert db.revoke_proposal(proposal["id"], now) == event_id
    assert db.get_discovered_event(ev["id"])["decision"] is None  # undecided again
    assert db.get_proposal(proposal["id"])["status"] == "proposed"
    assert json.loads(db.get_goals()[0]["scheduled_event_ids"]) == []


def test_revoke_clears_upcoming_event_only_if_it_still_names_this_event(monkeypatch, notion):
    _seed()
    _, proposal = _recommended_proposal(monkeypatch)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.promote_proposal(proposal["id"], now)
    scout.record_decision(db.get_proposal(proposal["id"]), "approved")
    db.revoke_proposal(proposal["id"], now)

    out = scout.clear_upcoming_event(proposal)
    assert out == {"upcoming_event_cleared": "Calvert Vaux lecture — 2026-09-27"}
    assert notion.texts[SITE_PAGE] == "" and db.get_website("notion_site-page-1")["upcoming_event"] == ""

    # The cell names something else (later approval or a hand edit): leave it alone.
    notion.texts[SITE_PAGE] = "Something I typed"
    notion.writes.clear()
    assert scout.clear_upcoming_event(proposal) == {"upcoming_event_kept": "Something I typed"}
    assert notion.writes == []


def test_reject_records_decision_without_notion(monkeypatch, notion):
    _seed()
    ev, proposal = _recommended_proposal(monkeypatch)
    db.set_proposal_status(proposal["id"], "rejected", "t")
    out = scout.record_decision(db.get_proposal(proposal["id"]), "rejected")
    assert out == {"decision": "rejected"} and notion.writes == []
    assert db.get_discovered_event(ev["id"])["decision"] == "rejected"
    assert db.get_decision_history()[0]["title"] == "Calvert Vaux lecture"


def test_attended_event_settles_site_count_upcoming_and_goal(monkeypatch, notion):
    _seed()
    ev, proposal = _recommended_proposal(monkeypatch)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.promote_proposal(proposal["id"], now)
    scout.record_decision(db.get_proposal(proposal["id"]), "approved")
    notion.writes.clear()

    before = time_tracker.settle_task_time(now=dt.datetime(2026, 9, 27, 15, 0, tzinfo=NY))
    assert before["pending"] == 1 and before["settled"] == []

    after = time_tracker.settle_task_time(now=dt.datetime(2026, 9, 27, 17, 0, tzinfo=NY))
    entry = after["settled"][0]
    assert entry["item_type"] == "event" and entry["attended_before"] == 2 and entry["attended_after"] == 3
    assert entry["clears_upcoming"] is True and entry["goal_last_occurred"] == "2026-09-27"
    assert notion.writes == [("Events Attended", 3), ("Upcoming Event", ""), ("Last Occurred", "2026-09-27")]
    site = db.get_website("notion_site-page-1")
    assert site["events_attended"] == 3 and site["upcoming_event"] == ""
    assert db.get_goals()[0]["last_occurred"] == "2026-09-27"
    assert db.get_discovered_event(ev["id"])["decision"] == "attended"
    assert db.get_proposal(proposal["id"])["settled_hours"] == 2.0

    again = time_tracker.settle_task_time(now=dt.datetime(2026, 9, 28, 9, 0, tzinfo=NY))
    assert again["settled"] == [] and len(notion.writes) == 3  # once only


def test_cancelled_attended_event_is_not_counted(monkeypatch, notion):
    _seed()
    ev, proposal = _recommended_proposal(monkeypatch)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.promote_proposal(proposal["id"], now)
    db.revoke_proposal(proposal["id"], now)  # back to pending + cancelled: no longer promoted, never settled
    summary = time_tracker.settle_task_time(now=dt.datetime(2026, 9, 28, 9, 0, tzinfo=NY))
    assert summary["checked"] == 0 and notion.writes == []


def test_spread_sample_keeps_coverage_across_the_window():
    events = [{"i": i} for i in range(300)]
    picked = scout.spread_sample(events, 150)
    assert len(picked) == 150 and picked[0]["i"] == 0 and picked[-1]["i"] == 298
    assert scout.spread_sample(events[:10], 150) == events[:10]



# --- the menu: overlapping options, a shown page, a queue, reload -------------------------


def _evening(day: str, title: str | None = None, start="19:00", end="20:30", **kw):
    """A candidate on 2026-<day> (NOW is Sunday 2026-09-20; the dinner blocks 09-25 evening)."""
    offset = "-04:00"
    return _ev(title or f"Talk {day} {start}", f"2026-{day}T{start}:00{offset}", f"2026-{day}T{end}:00{offset}", **kw)


def _rec(ev, goal_id="", why="w"):
    return {"discovered_event_id": ev["id"], "start": ev["start"], "end": ev["end"], "goal_id": goal_id,
            "why": why, "schedule_fit": "free evening", "confidence": 0.7}


ELEVEN_DAYS = ["09-21", "09-22", "09-23", "09-24", "09-26", "09-27", "09-28", "09-29", "09-30", "10-01", "10-02"]


def test_options_may_overlap_each_other_only_the_fixed_calendar_invalidates():
    _seed()
    a, b = _evening("09-22", "A"), _evening("09-22", "B", "19:30", "21:00")  # same evening, overlapping
    clash = _evening("09-25", "Clashes with dinner", "19:30", "21:00")
    _discover([a, b, clash])
    ws, we = scout._candidate_window(NOW, 30)
    _, fixed = scout.optimizer.build_payload(ws, we)
    rows = scout.validate_recommendations([_rec(a), _rec(b), _rec(clash), _rec(a)], [a, b, clash], fixed, [], ws, we)
    assert [(r["title"], r["status"], r["rank"]) for r in rows] == [
        ("A", "proposed", 1), ("B", "proposed", 2), ("Clashes with dinner", "invalid", 3), ("A", "invalid", 4)]
    assert rows[2]["invalid_reason"] == "overlaps existing event 'Dinner'"
    assert rows[3]["invalid_reason"] == "repeats an earlier option"


def _menu_run(monkeypatch, days=ELEVEN_DAYS):
    _seed()
    events = [_evening(d) for d in days]
    _discover(events)
    _fake_llm(monkeypatch, {"recommendations": [_rec(e) for e in events], "notes": "A long menu."})
    return events, scout.scout_recommend(now=NOW)


def test_recommend_shows_the_best_page_and_queues_the_rest(monkeypatch):
    events, summary = _menu_run(monkeypatch)
    assert (summary["proposed"], summary["queued"], summary["invalid"]) == (8, 3, 0)
    rows = sorted(db.get_proposals(run_id=summary["run_id"]), key=lambda r: r["rank"])
    assert [r["status"] for r in rows] == ["proposed"] * 8 + ["queued"] * 3
    assert all(r["shown_at"] for r in rows[:8]) and not any(r["shown_at"] for r in rows[8:])
    # only what is on the page counts as recommended; queued ones stay eligible later
    assert [db.get_discovered_event(e["id"])["recommended_at"] is not None for e in events] == [True] * 8 + [False] * 3
    run = db.get_optimizer_run(summary["run_id"])
    assert (run["proposed_count"], run["invalid_count"]) == (11, 0)
    assert db.count_unseen_queued_options() == 3


def test_reload_pages_through_the_queue_without_making_decisions(monkeypatch):
    events, summary = _menu_run(monkeypatch)
    rows = sorted(db.get_proposals(run_id=summary["run_id"]), key=lambda r: r["rank"])
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.promote_proposal(rows[0]["id"], now)                   # decided: approved
    db.reject_proposal(rows[1]["id"], now)                    # decided: rejected
    decided_before = {r["id"]: (r["status"], r["decided_at"]) for r in db.get_proposals() if r["id"] in (rows[0]["id"], rows[1]["id"])}
    monkeypatch.setattr(scout.llm_client, "complete_structured", lambda *a, **k: pytest.fail("the queue needs no model"))

    out = scout.reload(now=NOW)
    assert out["source"] == "queue" and out["queued"] == 0
    shown = {r["id"] for r in out["shown"]}
    # the three unseen options first, topped up with the best of the page just shown (ranks 3-7)
    assert shown == {r["id"] for r in rows[8:11]} | {r["id"] for r in rows[2:7]}
    status = {r["id"]: r for r in db.get_proposals(run_id=summary["run_id"])}
    assert status[rows[7]["id"]]["status"] == "queued" and status[rows[7]["id"]]["rank"] > 11  # moved to the back
    assert db.get_proposals(status="rejected") and len(db.get_proposals(status="rejected")) == 1  # nothing new rejected
    assert {i: (status[i]["status"], status[i]["decided_at"]) for i in decided_before} == decided_before
    assert all(db.get_discovered_event(e["id"])["recommended_at"] for e in events)  # newly shown are now recommended

    # nothing unseen left: the next reload asks the model, skipping recently shown candidates when it can
    fresh = _evening("10-03", "Fresh pick")
    _discover([fresh])
    captured = {}
    _fake_llm(monkeypatch, {"recommendations": [_rec(fresh)], "notes": "One more."}, captured)
    out = scout.reload(now=NOW)
    assert out["source"] == "model" and out["run_id"] and out["queued"] == 0
    assert [r["title"] for r in out["shown"]] == ["Fresh pick"]
    assert "recently_recommended" in captured["user"]["filter_stats"]["dropped"]  # too few to exclude (< 24): kept
    old = {r["id"]: r["status"] for r in db.get_proposals(run_id=summary["run_id"])}
    assert old[rows[0]["id"]] == "promoted" and old[rows[1]["id"]] == "rejected"
    assert set(old.values()) == {"promoted", "rejected", "superseded"}
    assert len(db.get_proposals(status="rejected")) == 1


def test_model_reload_skips_recently_recommended_candidates_when_enough_remain():
    now = dt.datetime(2026, 10, 8, 12, 0, tzinfo=NY)
    recent = (now - dt.timedelta(days=3)).isoformat()
    old = (now - dt.timedelta(days=20)).isoformat()
    cands = [{"id": f"c{i}", "recommended_at": recent if i < 10 else (old if i < 15 else None)} for i in range(40)]
    kept, excluded = scout.drop_recently_recommended(cands, now, 14)
    assert excluded == 10 and [c["id"] for c in kept] == [f"c{i}" for i in range(10, 40)]
    few = cands[:30]  # 20 would remain: below the floor of 24, so nothing is excluded
    assert scout.drop_recently_recommended(few, now, 14) == (few, 0)


def test_reload_route_maps_model_errors_to_502(monkeypatch):
    import main
    from fastapi import HTTPException

    def boom(**kw):
        raise llm_client.LLMRateLimitError("slow down")

    monkeypatch.setattr(scout, "reload", boom)
    with pytest.raises(HTTPException) as err:
        main.scout_reload()
    assert err.value.status_code == 502 and "LLMRateLimitError" in err.value.detail


def test_queued_options_are_not_decisions_and_never_emailed(monkeypatch):
    import notifier

    soon = dt.datetime.now(NY) + dt.timedelta(days=2)
    def option(title, status, hour):
        start = soon.replace(hour=hour, minute=0, second=0, microsecond=0)
        return {"item_type": "event", "item_id": title, "title": title, "start": start.isoformat(),
                "end": (start + dt.timedelta(hours=1)).isoformat(), "all_day": False, "timezone": DISPLAY_TIMEZONE,
                "status": status, "rank": hour}
    db.create_optimizer_run("scout_q", "t", "api", "m", "a", "b", kind="scout")
    ids = db.insert_proposals("scout_q", [option("Shown one", "proposed", 17), option("Shown two", "proposed", 18),
                                          option("Approved one", "proposed", 19), option("Rejected one", "proposed", 20),
                                          option("Queued one", "queued", 21), option("Queued two", "queued", 22)], "t")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.promote_proposal(ids[2], now)
    db.reject_proposal(ids[3], now)

    assert sorted(h["title"] for h in db.get_plan_decision_history()) == ["Approved one", "Rejected one"]
    assert db.plan_decision_stats()["decisions"] == 2 and db.count_decisions() == 2

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)

    for name, value in {"NOTIFY_EMAIL_TO": "me@example.com", "NOTIFY_SMTP_USER": "bot@example.com",
                        "NOTIFY_SMTP_PASSWORD": "pw", "NOTIFY_EMAIL_FROM": "bot@example.com",
                        "NOTIFY_SMTP_PORT": "587"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    notifier.notify_run({"run_id": "scout_q", "kind": "scout", "proposed": 2}, "scout")
    assert len(sent) == 1
    text = sent[0].get_body(("plain",)).get_content()
    assert "Shown one" in text and "Shown two" in text
    assert "Queued" not in text and "Approved one" not in text


def test_expiry_covers_queued_options():
    db.create_optimizer_run("scout_x", "t", "api", "m", "a", "b", kind="scout")
    base = {"item_type": "event", "item_id": "x", "all_day": False, "timezone": DISPLAY_TIMEZONE}
    ids = db.insert_proposals("scout_x", [
        {**base, "title": "past queued", "start": "2026-09-19T19:00:00-04:00", "end": "2026-09-19T20:00:00-04:00", "status": "queued"},
        {**base, "title": "past shown", "start": "2026-09-19T19:00:00-04:00", "end": "2026-09-19T20:00:00-04:00", "status": "proposed"},
        {**base, "title": "future queued", "start": "2026-09-23T19:00:00-04:00", "end": "2026-09-23T20:00:00-04:00", "status": "queued"},
    ], "t")
    assert db.expire_past_proposals(NOW) == 2
    assert [db.get_proposal(i)["status"] for i in ids] == ["expired", "expired", "queued"]
    assert db.count_decisions() == 0


def test_shortlist_setting_and_its_old_name(monkeypatch):
    assert (scout.shortlist_size(), scout.page_size()) == (16, 8)
    monkeypatch.setenv("LLM_SCOUT_MAX_RECOMMENDATIONS", "12")
    assert scout.shortlist_size() == 12  # the old name still works...
    monkeypatch.setenv("LLM_SCOUT_SHORTLIST", "20")
    assert scout.shortlist_size() == 20  # ...but the new one wins
    monkeypatch.setenv("LLM_SCOUT_PAGE_SIZE", "5")
    assert scout.page_size() == 5


def test_extracted_titles_are_decoded_but_ids_stay_stable():
    raw = "How Art and Sci&shy;ence Can Honor Indigenous Histories &amp; Ecologies"
    ev = event_sources.normalize("thoughtgallery.org", raw, "2026-10-24T14:00:00-04:00", location="Caf&eacute; Room")
    assert ev["title"] == "How Art and Science Can Honor Indigenous Histories & Ecologies"
    assert ev["location"] == "Café Room"
    assert ev["id"] == event_sources.event_id("thoughtgallery.org", raw, "2026-10-24T14:00:00-04:00")  # same key as before
    # a row stored with the escaped title is fixed by the next fetch, decision untouched
    db.upsert_discovered_events([{**ev, "title": raw}], None, "t1")
    db.set_discovered_decision(ev["id"], "rejected")
    db.upsert_discovered_events([ev], None, "t2")
    row = db.get_discovered_event(ev["id"])
    assert row["title"] == ev["title"] and row["decision"] == "rejected"
