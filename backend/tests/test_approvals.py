"""Approve-all by section (db.PROPOSAL_GROUPS), called on the route function directly."""

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


def _proposals():
    start = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)

    def row(item_type, hour):
        s = (start.replace(hour=hour, minute=0, second=0, microsecond=0)).isoformat()
        e = (start.replace(hour=hour, minute=30, second=0, microsecond=0)).isoformat()
        return {"item_type": item_type, "item_id": "", "title": f"{item_type} {hour}", "description": "",
                "location": "", "start": s, "end": e, "all_day": False, "timezone": "UTC",
                "rationale": "", "status": "proposed"}

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.create_optimizer_run("r1", now, "api", "m", "a", "b")
    return db.insert_proposals("r1", [row("goal", 9), row("task", 10), row("travel", 11), row("free", 12),
                                      row("event", 18)], now)


def _status():
    return {p["title"]: p["status"] for p in db.get_proposals(run_id="r1")}


def test_approve_all_plan_leaves_suggested_events_pending(monkeypatch):
    _proposals()
    out = main.approve_all_pending(group="plan")
    assert out["group"] == "plan" and len(out["promoted"]) == 4
    status = _status()
    assert status.pop("event 18") == "proposed"
    assert set(status.values()) == {"promoted"}


def test_approve_all_events_uses_the_scout_decision_path(monkeypatch):
    _proposals()
    recorded = []
    import scout
    monkeypatch.setattr(scout, "record_decision", lambda p, d: recorded.append((p["item_type"], d)))
    out = main.approve_all_pending(group="events")
    assert len(out["promoted"]) == 1 and recorded == [("event", "approved")]
    assert [t for t, s in _status().items() if s == "promoted"] == ["event 18"]


def test_approve_all_without_group_and_bad_group():
    _proposals()
    with pytest.raises(HTTPException) as err:
        main.approve_all_pending(group="everything")
    assert err.value.status_code == 422
    assert set(_status().values()) == {"proposed"}  # nothing touched by the bad request


def test_groups_cover_every_proposal_kind():
    kinds = [k for g in db.PROPOSAL_GROUPS.values() for k in g["item_types"]]
    assert sorted(kinds) == sorted(["event", "goal", "task", "travel", "free"])
    assert db.proposal_group("event") == "events" and db.proposal_group("travel") == "plan"


# --- review API: batch decisions, option overlaps, preview, summary, redirect -----------

import asyncio  # noqa: E402
import json  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pydantic  # noqa: E402

import event_sources  # noqa: E402
import schedule_config  # noqa: E402

NY = "America/New_York"


def _at(days: int, hhmm: str) -> str:
    """An ISO New York time `days` from today (always in the future for days >= 1)."""
    zone = ZoneInfo(NY)
    day = dt.datetime.now(zone).date() + dt.timedelta(days=days)
    hour, minute = map(int, hhmm.split(":"))
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).isoformat()


def _insert(run_id: str, rows: list[dict]) -> list[str]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if db.get_optimizer_run(run_id) is None:
        db.create_optimizer_run(run_id, now, "api", "m", "a", "b", kind="scout" if run_id.startswith("scout") else "optimizer")
    base = {"description": "", "location": "", "all_day": False, "timezone": NY, "rationale": "because", "status": "proposed"}
    return db.insert_proposals(run_id, [{**base, **r} for r in rows], now)


def _option(title: str, days: int, start: str, end: str, *, status="proposed", rank=1, site_event=None) -> dict:
    return {"item_type": "event", "item_id": site_event or title, "discovered_event_id": site_event, "title": title,
            "start": _at(days, start), "end": _at(days, end), "status": status, "rank": rank,
            "website_id": "notion_site" if site_event else None}


def test_batch_approve_reject_revoke_use_the_single_route_paths():
    goal, task, travel, free, event = _proposals()
    out = main.proposals_batch(main.Batch(action="approve", ids=[goal, task, "missing", goal]))
    assert [d["id"] for d in out["done"]] == [goal, task]  # duplicates collapsed, order kept
    assert out["done"][0] == {"id": goal, "status": "promoted", "event_id": f"optimizer_{goal}"}
    assert out["failed"] == [{"id": "missing", "detail": "proposal not found"}]
    assert out["superseded_overlaps"] == []

    out = main.proposals_batch(main.Batch(action="reject", ids=[travel, goal]))
    assert out["done"] == [{"id": travel, "status": "rejected", "event_id": None}]
    assert out["failed"] == [{"id": goal, "detail": "proposal is already promoted"}]
    assert db.count_decisions() == 3  # two approvals + one rejection: each occurrence is a decision

    out = main.proposals_batch(main.Batch(action="revoke", ids=[goal, free]))
    assert [d["id"] for d in out["done"]] == [goal] and "not promoted" in out["failed"][0]["detail"]
    assert _status() == {"goal 9": "proposed", "task 10": "promoted", "travel 11": "rejected", "free 12": "proposed",
                         "event 18": "proposed"}
    assert [e["title"] for e in db.get_schedule_x_events(calendar_id="optimizer")] == ["task 10"]


def test_batch_rejects_unknown_actions():
    with pytest.raises(pydantic.ValidationError):
        main.Batch(action="maybe", ids=["x"])


def test_batch_never_promotes_an_expired_block():
    past_start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).isoformat()
    past_end = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
    [old] = _insert("r_old", [{"item_type": "goal", "item_id": "", "title": "old", "start": past_start, "end": past_end}])
    out = main.proposals_batch(main.Batch(action="approve", ids=[old]))
    assert out["done"] == [] and out["failed"] == [{"id": old, "detail": "proposal is already expired"}]


def _site_and_candidates(*titles_days):
    db.upsert_website({"source": "notion", "source_website_id": "site", "url": "https://www.nypl.org/events", "last_modified": "x"})
    events = [event_sources.normalize("nypl.org", title, _at(days, "19:00"), _at(days, "20:30")) for title, days in titles_days]
    db.upsert_discovered_events(events, "notion_site", "2026-10-08T12:00:00+00:00")
    return events


def test_event_approval_survives_a_failing_notion_write(monkeypatch, caplog):
    import requests
    import scout

    [ev] = _site_and_candidates(("Octopus talk", 3))
    [pid] = _insert("scout_1", [_option("Octopus talk", 3, "19:00", "20:30", site_event=ev["id"])])

    def unauthorized(*a, **k):
        raise requests.HTTPError("401 Client Error: Unauthorized for url: https://api.notion.com/v1/pages/site")

    monkeypatch.setattr(scout.notion_adapter, "update_text_property", unauthorized)
    out = main.approve_proposal(pid)
    assert out["status"] == "promoted" and out["event_id"] == f"optimizer_{pid}"
    assert "401" in out["scout"]["upcoming_event_error"]
    assert db.get_discovered_event(ev["id"])["decision"] == "approved"
    assert "could not write Upcoming Event" in caplog.text


def test_event_decisions_survive_the_scout_failing_entirely(monkeypatch):
    import scout

    a, b = _site_and_candidates(("Talk A", 3), ("Talk B", 4))
    pa, pb = _insert("scout_1", [_option("Talk A", 3, "19:00", "20:30", site_event=a["id"]),
                                 _option("Talk B", 4, "19:00", "20:30", rank=2, site_event=b["id"])])
    monkeypatch.setattr(scout, "record_decision", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scout broke")))
    out = main.proposals_batch(main.Batch(action="approve", ids=[pa]))
    assert out["done"][0]["status"] == "promoted"
    out = main.proposals_batch(main.Batch(action="reject", ids=[pb]))
    assert out["done"][0]["status"] == "rejected"
    assert db.get_discovered_event(a["id"])["decision"] == "approved"
    assert db.get_discovered_event(b["id"])["decision"] == "rejected"  # the learner's signal is kept anyway


def test_approving_an_option_supersedes_overlapping_alternatives(monkeypatch):
    import scout

    monkeypatch.setattr(scout, "record_decision", lambda p, d: {"decision": d})
    a, b, c, d = _insert("scout_1", [
        _option("A", 3, "19:00", "20:30", rank=1),
        _option("B overlaps A", 3, "19:30", "21:00", rank=2),
        _option("C queued, overlaps A", 3, "20:00", "21:00", status="queued", rank=9),
        _option("D next day", 4, "19:00", "20:30", rank=3),
    ])
    [plan] = _insert("r_plan", [{"item_type": "free", "item_id": "", "title": "Free time", "start": _at(3, "19:00"),
                                 "end": _at(3, "21:00")}])
    out = main.approve_proposal(a)
    assert sorted(out["superseded_overlaps"]) == sorted([b, c])
    status = {p["id"]: p["status"] for p in db.get_proposals()}
    assert (status[a], status[b], status[c], status[d], status[plan]) == ("promoted", "superseded", "superseded", "proposed", "proposed")
    assert db.count_decisions() == 1  # superseding an alternative is not a decision

    out = main.proposals_batch(main.Batch(action="approve", ids=[b, d]))
    assert [x["id"] for x in out["done"]] == [d] and out["failed"][0]["detail"] == "proposal is already superseded"


def test_approve_all_events_prefers_the_better_ranked_alternative_and_skips_queued(monkeypatch):
    import scout

    monkeypatch.setattr(scout, "record_decision", lambda p, d: {"decision": d})
    worse, better, queued = _insert("scout_1", [
        _option("Worse", 3, "19:00", "20:30", rank=2),
        _option("Better", 3, "19:30", "21:00", rank=1),
        _option("Queued", 5, "19:00", "20:30", status="queued", rank=9),
    ])
    out = main.approve_all_pending(group="events")
    assert [p["proposal_id"] for p in out["promoted"]] == [better] and out["superseded_overlaps"] == [worse]
    status = {p["id"]: p["status"] for p in db.get_proposals()}
    assert status[queued] == "queued"  # approve-all never includes queued options


def test_preview_feed_has_every_pending_proposal_as_a_schedule_x_event():
    start, end = (dt.datetime.fromisoformat(_at(7, t)) for t in ("17:30", "20:30"))
    utc = dt.timezone.utc
    day_off = dt.date.fromisoformat(_at(10, "00:00")[:10])
    rows = _insert("r_plan", [
        {"item_type": "goal", "item_id": "g", "title": "Climb", "start": start.astimezone(utc).isoformat(),
         "end": end.astimezone(utc).isoformat(), "rationale": "Mondays work best."},          # stored in UTC
        {"item_type": "free", "item_id": "", "title": "Day off", "start": day_off.isoformat(),
         "end": (day_off + dt.timedelta(days=1)).isoformat(), "all_day": True, "timezone": None},
        {"item_type": "goal", "item_id": "g", "title": "Approved", "start": _at(2, "08:00"), "end": _at(2, "09:00")},
    ])
    _insert("scout_1", [_option("Queued", 3, "19:00", "20:30", status="queued")])
    db.promote_proposal(rows[2], dt.datetime.now(dt.timezone.utc).isoformat())

    preview = {e["title"]: e for e in main.events_preview()}
    assert set(preview) == {"Climb", "Day off"}  # queued and already-approved rows are not previewed
    climb = preview["Climb"]
    assert climb["id"] == f"proposal_{rows[0]}" and climb["calendarId"] == "proposed"
    assert climb["start"] == f"{start.isoformat()}[America/New_York]"  # normalized: offset agrees with the zone
    assert climb["end"] == f"{end.isoformat()}[America/New_York]"
    assert climb["description"] == "Mondays work best." and climb["people"] == []
    assert climb["proposalId"] == rows[0] and climb["itemType"] == "goal"
    assert climb["_options"]["additionalClasses"] == ["sx-proposal", "sx-proposal-goal"]
    assert (preview["Day off"]["start"], preview["Day off"]["end"]) == (day_off.isoformat(),) * 2  # inclusive end


def test_review_summary_route_shape():
    _proposals()
    _insert("scout_1", [_option("Queued", 3, "19:00", "20:30", status="queued", rank=2)])
    out = main.review_summary()
    assert set(out) == {"generated_at", "events", "plan", "runs"}
    assert {"pending", "queued", "approved_upcoming", "items"} <= set(out["events"])
    assert {"pending", "approved_upcoming", "series", "held_back"} <= set(out["plan"])
    assert out["events"]["pending"] == 1 and out["events"]["queued"] == 1 and out["events"]["reload"] == "queue"
    assert [i["title"] for i in out["events"]["items"]] == ["event 18"]
    assert out["plan"]["pending"] == 4 and len(out["plan"]["series"]) == 4
    series = out["plan"]["series"][0]
    assert set(series) >= {"key", "item_type", "title", "count", "pending", "approved", "cadence", "time", "duration_min",
                           "first", "last", "summary", "has_travel", "occurrences"}
    assert {"ref", "rank", "travel"} <= set(series["occurrences"][0])
    assert out["runs"]["optimizer"]["run_id"] == "r1" and out["runs"]["scout"]["run_id"] == "scout_1"
    json.dumps(out)  # plain JSON all the way down


def test_old_review_page_redirects_to_the_plan_tab():
    response = main.review_page()
    assert response.status_code == 307 and response.headers["location"] == "/#/plan"


def test_calendar_syncs_start_twenty_seconds_apart(monkeypatch):
    class FakeScheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, func, trigger, **kw):
            self.jobs.append(kw)

        def start(self):
            pass

        def shutdown(self, wait=True):
            pass

        def get_jobs(self):
            return []

    fake = FakeScheduler()
    off = schedule_config.CronJob(False, None, 6, 0)
    monkeypatch.setattr(main, "scheduler", fake)
    monkeypatch.setattr(main.schedule_config, "load_schedule", lambda: schedule_config.Schedule(
        calendars=schedule_config.IntervalJob(5), settle=schedule_config.IntervalJob(None),
        scout_fetch=off, scout_recommend=off, optimizer=off,
        notion_check=schedule_config.IntervalJob(None), conflict_check=schedule_config.IntervalJob(None)))

    async def run():
        async with main.lifespan(main.app):
            pass

    asyncio.run(run())
    assert [j["id"] for j in fake.jobs] == ["google_sync", "apple_sync", "outlook_sync", "partiful_sync"]
    first = fake.jobs[0]["next_run_time"]
    assert [(j["next_run_time"] - first).total_seconds() for j in fake.jobs] == [0, 20, 40, 60]
