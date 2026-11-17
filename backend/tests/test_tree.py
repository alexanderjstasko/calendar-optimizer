"""
The one-database Telos tree (2026-10-26): pages split by Level, ancestry on
tasks and routines, paused subtrees, effective priority, the goals review
and the Sessions write-back. No network: the Notion fetch is faked.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

import db
import hours
import notion_adapter as na
import optimizer
import slot
import sync_notion
import time_tracker

NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()


# --- canned pages in the live database's shape ---------------------------------


def _page(page_id, title, level, *, parent=None, status="In-Progress", priority=None, **extra):
    props = {
        "Title": {"type": "title", "title": [{"plain_text": title}] if title else []},
        "Level": {"type": "select", "select": {"name": level} if level else None},
        "Status": {"type": "status", "status": {"name": status} if status else None},
        "Priority": {"type": "select", "select": {"name": priority} if priority else None},
        "Parent item": {"type": "relation", "relation": [{"id": parent}] if parent else []},
        "Sub-item": {"type": "relation", "relation": []},
        "Notes for LLM": {"type": "rich_text", "rich_text": [{"plain_text": extra.pop("notes", "")}] if extra.get("notes") else []},
        "Location": {"type": "rich_text", "rich_text": []},
        "Display": {"type": "checkbox", "checkbox": extra.pop("display", True)},
        "Target Date": {"type": "date", "date": {"start": extra["target"]} if extra.get("target") else None},
        "Time to Complete": {"type": "number", "number": extra.get("ttc")},
        "Time Spent": {"type": "number", "number": extra.get("spent")},
        "Times/Week": {"type": "number", "number": extra.get("tpw")},
        "Session Time (hr)": {"type": "number", "number": extra.get("session")},
        "Sessions": {"type": "number", "number": extra.get("sessions")},
        "Type": {"type": "select", "select": {"name": extra["type"]} if extra.get("type") else None},
        "Day of Week": {"type": "multi_select", "multi_select": [{"name": d} for d in extra.get("days", [])]},
        "Time of Day": {"type": "rich_text", "rich_text": [{"plain_text": extra["tod"]}] if extra.get("tod") else []},
        "Last Occurred": {"type": "date", "date": {"start": extra["last"]} if extra.get("last") else None},
    }
    return {"id": page_id, "last_edited_time": "2026-10-26T12:00:00.000Z", "properties": props}


def _tree_pages():
    return [
        _page("telos-health", "Health and Energy", "Telos", status="Telos", notes="Body first."),
        _page("telos-peace", "Craft and Career", "Telos", status="Telos"),
        _page("goal-piano", "Play Piano", "Goal", parent="telos-health", priority="High", target="2027-06-01"),
        _page("proj-opt", "Calendar Optimizer", "Project", parent="telos-peace", status="On Hold"),
        _page("r-piano", "Learn piano", "Routine", parent="goal-piano", type="Flexible", tpw=3, session=1),
        _page("r-floss", "Floss", "Routine", parent="telos-peace", type="Routine", tpw=3, session=0.25, priority="High", last="2026-10-25"),
        _page("r-paused", "Write docs", "Routine", parent="proj-opt", type="Flexible", tpw=1, session=2),
        _page("r-culture", "Culture Event", "Routine", parent="telos-peace", type="Event Interest", tpw=0.5),
        _page("t-pt", "Call the plumber", "Task", parent="telos-health", target="2026-11-07", ttc=0.5, status="Not started"),
        _page("t-done", "Basketcase", "Task", status="Done", ttc=1),
        _page("t-dropped", "Old idea", "Task", parent="telos-health", status="Dropped", ttc=1),
        _page("t-orphan", "Buy lotion", "Task", ttc=0.5, status="Not started", priority="Low"),
    ]


def _sync(pages):
    tree = na.split_tree(pages)
    return sync_notion._apply_nodes(tree["nodes"]), sync_notion._apply_goals(tree["goals"]), sync_notion._apply_tasks(tree["tasks"])


# --- adapter ----------------------------------------------------------------------


def test_split_tree_sorts_pages_by_level_and_reads_the_renamed_properties():
    tree = na.split_tree(_tree_pages())
    assert [n["title"] for n in tree["nodes"]] == ["Health and Energy", "Craft and Career", "Play Piano", "Calendar Optimizer"]
    assert [g["goal"] for g in tree["goals"]] == ["Learn piano", "Floss", "Write docs", "Culture Event"]
    assert [t["task"] for t in tree["tasks"]] == ["Call the plumber", "Basketcase", "Old idea", "Buy lotion"]
    assert tree["skipped"] == []

    floss = tree["goals"][1]
    assert floss["frequency"] == "3x Week" and floss["times_per_week"] == 3 and floss["required_time_hr"] == 0.25
    assert floss["parent_source_id"] == "telos-peace" and floss["level"] == "Routine" and floss["last_occurred"] == "2026-10-25"
    culture = tree["goals"][3]
    assert culture["frequency"] == "1x Every Other Week" and culture["goal_type"] == "Event Interest"

    pt = tree["tasks"][0]
    assert pt["deadline"] == "2026-11-07" and pt["time_to_complete_hr"] == 0.5 and pt["parent_source_id"] == "telos-health"
    node = tree["nodes"][2]
    assert node["level"] == "Goal" and node["parent_source_id"] == "telos-health" and node["target_date"] == "2027-06-01"


@pytest.mark.parametrize("tpw, text", [(7, "Daily"), (2, "2x Week"), (1, "1x Week"), (0.5, "1x Every Other Week"),
                                       (0.25, "1x Month"), (3.5, "Once every 2 days"), (0, None), (None, None)])
def test_times_per_week_becomes_the_frequency_text_the_parser_knows(tpw, text):
    import routine
    assert na.frequency_text(tpw) == text
    if text:
        assert routine.parse_frequency(text) is not None


def test_a_page_without_a_level_is_read_the_old_way():
    old_goal = _page("g-old", "Old goal", None, tpw=1, session=1)
    del old_goal["properties"]["Level"]  # the old Goals database had no Level at all
    unset = _page("g-unset", None, None, tpw=1)  # a tree row whose Level was never picked
    old_task = {"id": "t-old", "last_edited_time": "2026-10-26T12:00:00.000Z", "properties": {
        "Task": {"type": "title", "title": [{"plain_text": "Old task"}]},
        "Deadline": {"type": "date", "date": {"start": "2026-11-01"}},
        "Time to Complete (hr)": {"type": "number", "number": 2},
        "Time Spent (hr)": {"type": "number", "number": 0.5},
    }}
    tree = na.split_tree([old_goal, old_task, unset])
    assert [g["goal"] for g in tree["goals"]] == ["Old goal"]
    assert tree["skipped"] == [{"id": "g-unset", "title": None, "level": None}]
    assert tree["tasks"][0] == {**tree["tasks"][0], "task": "Old task", "deadline": "2026-11-01",
                                "time_to_complete_hr": 2, "time_spent_hr": 0.5, "parent_source_id": None}


# --- sync -------------------------------------------------------------------------


def test_sync_lands_in_three_tables_and_diffs_each(monkeypatch):
    monkeypatch.setattr(sync_notion.notion_adapter, "fetch_tree", lambda: na.split_tree(_tree_pages()))
    summary = sync_notion.sync_notion()
    assert summary == {"nodes": 4, "nodes_deleted": 0, "goals": 4, "goals_deleted": 0, "tasks": 4, "tasks_deleted": 0, "skipped": 0}
    assert {n["id"]: n["parent_id"] for n in db.get_tree_nodes()}["notion_goal-piano"] == "notion_telos-health"
    assert {g["goal"]: g["parent_id"] for g in db.get_goals()}["Floss"] == "notion_telos-peace"

    pages = [p for p in _tree_pages() if p["id"] not in ("proj-opt", "r-paused", "t-orphan")]
    monkeypatch.setattr(sync_notion.notion_adapter, "fetch_tree", lambda: na.split_tree(pages))
    summary = sync_notion.sync_notion()
    assert (summary["nodes_deleted"], summary["goals_deleted"], summary["tasks_deleted"]) == (1, 1, 1)
    assert "Calendar Optimizer" not in [n["title"] for n in db.get_tree_nodes()]
    assert [n["title"] for n in db.get_tree_nodes(include_deleted=True) if n["deleted_at"]] == ["Calendar Optimizer"]


# --- what the planner sees --------------------------------------------------------


def test_schedulable_items_carry_ancestry_and_skip_done_dropped_and_paused_subtrees():
    _sync(_tree_pages())
    items = {i.get("task") or i.get("goal"): i for i in db.get_schedulable_items()}
    assert sorted(items) == ["Buy lotion", "Call the plumber", "Culture Event", "Floss", "Learn piano"]
    # Done and Dropped rows are out; "Write docs" hangs under the On Hold project.
    piano = items["Learn piano"]
    assert piano["path"] == ["Health and Energy", "Play Piano"] and piano["telos"] == "Health and Energy"
    assert piano["priority"] == "High" and piano["own_priority"] is None  # the goal's priority, inherited
    assert piano["ancestor_notes"] == ["Body first."]
    assert items["Floss"]["priority"] == "High" and items["Floss"]["own_priority"] == "High"
    assert items["Buy lotion"]["path"] == [] and items["Buy lotion"]["telos"] is None and items["Buy lotion"]["priority"] == "Low"


def test_get_tree_nests_nodes_and_lists_orphans():
    _sync(_tree_pages())
    tree = {n["title"]: n for n in db.get_tree()}
    assert sorted(tree) == ["(no telos)", "Craft and Career", "Health and Energy"]
    health = tree["Health and Energy"]
    assert [c["title"] for c in health["children"]] == ["Play Piano"]
    assert [i["title"] for i in health["children"][0]["items"]] == ["Learn piano"]
    assert [i["title"] for i in health["items"]] == ["Call the plumber"]
    assert [i["title"] for i in tree["(no telos)"]["items"]] == ["Buy lotion"]
    assert "Write docs" not in str(tree)  # paused subtree: not an item anywhere


def test_payload_sends_the_tree_and_the_path_on_every_item():
    _sync(_tree_pages())
    start = dt.datetime(2026, 10, 26, 8, tzinfo=NY)
    payload, _ = optimizer.build_payload(start, start + dt.timedelta(days=7))
    assert [t["title"] for t in payload["tree"]] == ["Craft and Career", "Health and Energy", "(no telos)"]
    pt = next(t for t in payload["tasks"] if t["title"] == "Call the plumber")
    assert pt["telos"] == "Health and Energy" and pt["path"] == ["Health and Energy"]
    piano = next(g for g in payload["goals"] if g["title"] == "Learn piano")
    assert piano["priority"] == "High" and piano["times_per_week"] == 3 and piano["frequency"] == "3x Week"
    assert optimizer.GOALS_REVIEW_TITLE in [g["title"] for g in payload["goals"]]


def test_goals_review_is_due_once_per_cadence(monkeypatch):
    start = dt.datetime(2026, 10, 26, 0, tzinfo=NY)
    end = start + dt.timedelta(days=7)
    h = {**hours.DEFAULT_HOURS, "goals_review_every_months": 2, "goals_review_minutes": 45}
    due = optimizer.goals_review_goal(start, end, h)
    assert due and due["required_time_hr"] == 0.75 and due["last_occurred"] is None

    run_id = "run-review"
    db.create_optimizer_run(run_id, "2026-10-20T00:00:00+00:00", "api", "m", "2026-10-19T00:00:00-04:00", "2026-10-26T00:00:00-04:00")
    db.insert_proposals(run_id, [{"item_type": "goal", "item_id": optimizer.GOALS_REVIEW_ID, "title": optimizer.GOALS_REVIEW_TITLE,
                                  "description": "", "location": "", "start": "2026-10-24T10:00:00-04:00", "end": "2026-10-24T10:45:00-04:00",
                                  "all_day": False, "timezone": "America/New_York", "rationale": "", "status": "proposed",
                                  "invalid_reason": None, "ref": "r1", "replaces_event_id": ""}], "2026-10-20T00:00:00+00:00")
    assert optimizer.goals_review_goal(start, end, h) is None  # covered two days ago, even while still pending
    later = start + dt.timedelta(days=70)
    again = optimizer.goals_review_goal(later, later + dt.timedelta(days=7), h)
    assert again and again["last_occurred"] == "2026-10-24"


def test_critical_sorts_first_in_the_slot_sheet():
    assert slot._priority("Critical") < slot._priority("High") < slot._priority("Medium") < slot._priority("Low") < slot._priority("")


# --- write-back -------------------------------------------------------------------


class FakeNotion:
    def __init__(self):
        self.numbers = {("r-floss", "Sessions"): 4, ("t-pt", "Time Spent"): 1.0}
        self.dates = {("r-floss", "Last Occurred"): "2026-10-20"}
        self.writes = []

    def resolve(self, page_id, names, ptype):
        return "Time Spent"

    def read_number(self, page_id, name):
        return self.numbers[(page_id, name)]

    def write_number(self, page_id, name, value):
        self.writes.append((page_id, name, value))
        self.numbers[(page_id, name)] = value
        return value

    def read_date(self, page_id, name):
        return self.dates[(page_id, name)]

    def write_date(self, page_id, name, value):
        self.writes.append((page_id, name, value))
        self.dates[(page_id, name)] = value
        return value


def test_settling_a_routine_block_moves_last_occurred_and_adds_a_session(monkeypatch):
    _sync(_tree_pages())
    fake = FakeNotion()
    monkeypatch.setattr(time_tracker.notion_adapter, "resolve_property_name", fake.resolve)
    monkeypatch.setattr(time_tracker.notion_adapter, "read_number_property", fake.read_number)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_number_property", fake.write_number)
    monkeypatch.setattr(time_tracker.notion_adapter, "read_date_property", fake.read_date)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_date_property", fake.write_date)

    run_id = "run-settle"
    db.create_optimizer_run(run_id, "2026-10-20T00:00:00+00:00", "api", "m", "2026-10-19T00:00:00-04:00", "2026-10-26T00:00:00-04:00")
    ids = db.insert_proposals(run_id, [
        {"item_type": "goal", "item_id": "notion_r-floss", "title": "Floss", "description": "", "location": "", "start": "2026-10-22T18:00:00-04:00",
         "end": "2026-10-22T18:15:00-04:00", "all_day": False, "timezone": "America/New_York", "rationale": "", "status": "proposed",
         "invalid_reason": None, "ref": "a", "replaces_event_id": ""},
        {"item_type": "task", "item_id": "notion_t-pt", "title": "Call the plumber", "description": "", "location": "", "start": "2026-10-22T12:00:00-04:00",
         "end": "2026-10-22T12:30:00-04:00", "all_day": False, "timezone": "America/New_York", "rationale": "", "status": "proposed",
         "invalid_reason": None, "ref": "b", "replaces_event_id": ""},
    ], "2026-10-20T00:00:00+00:00")
    for pid in ids:
        db.promote_proposal(pid, "2026-10-21T00:00:00+00:00")
    summary = time_tracker.settle_task_time(now=dt.datetime(2026, 10, 23, 9, tzinfo=NY))
    assert summary["errors"] == []
    assert ("r-floss", "Last Occurred", "2026-10-22") in fake.writes
    assert ("r-floss", "Sessions", 5) in fake.writes
    assert ("t-pt", "Time Spent", 1.5) in fake.writes  # the renamed property, resolved on the page
    goals = {g["goal"]: g for g in db.get_goals()}
    assert goals["Floss"]["sessions"] == 5 and goals["Floss"]["last_occurred"] == "2026-10-22"


# --- a Goal/Project planned as sessions or as one piece of work (2026-10-27) -------


def _planned_pages():
    return _tree_pages() + [
        _page("proj-ashtray", "Make Skatepark Ashtray", "Project", parent="telos-peace", priority="Low", type="Flexible",
              tpw=2, session=3, ttc=12, spent=3, tod="Afterwork", target="2026-12-05"),
        _page("goal-dash", "Build out Daily Dash", "Goal", parent="telos-peace", priority="High", ttc=80, tod="Afterwork",
              target="2026-11-30"),
        _page("goal-swim", "Swim v8", "Goal", parent="telos-health", display=False, session=1, tpw=1),
        _page("proj-done", "Finished project", "Project", parent="telos-peace", session=1, tpw=1, ttc=4, spent=4),
    ]


def test_a_goal_or_project_with_session_time_is_a_node_and_a_planner_goal():
    tree = na.split_tree(_planned_pages())
    assert [n["title"] for n in tree["nodes"]][-4:] == ["Make Skatepark Ashtray", "Build out Daily Dash", "Swim v8", "Finished project"]
    goals = {g["goal"]: g for g in tree["goals"]}
    tasks = {t["task"]: t for t in tree["tasks"]}
    ashtray = goals["Make Skatepark Ashtray"]
    assert ashtray["level"] == "Project" and ashtray["goal_type"] == "Flexible" and ashtray["frequency"] == "2x Week"
    assert ashtray["required_time_hr"] == 3 and ashtray["time_to_complete_hr"] == 12 and ashtray["time_spent_hr"] == 3
    assert ashtray["parent_source_id"] == "telos-peace" and ashtray["end_date"] == "2026-12-05"
    assert goals["Finished project"]["goal_type"] == "Flexible"  # untyped node sessions default to Flexible, never the scout
    dash = tasks["Build out Daily Dash"]  # Time to Complete without a Session Time: one piece of work
    assert dash["level"] == "Goal" and dash["time_to_complete_hr"] == 80 and dash["deadline"] == "2026-11-30"
    assert "Swim v8" not in goals and "Swim v8" not in tasks  # Display off: ancestry only
    assert "Build out Daily Dash" not in goals and "Make Skatepark Ashtray" not in tasks
    assert tree["skipped"] == []


def test_planned_nodes_reach_the_planner_under_their_own_node():
    _sync(_planned_pages())
    items = {i.get("task") or i.get("goal"): i for i in db.get_schedulable_items()}
    ashtray = items["Make Skatepark Ashtray"]
    assert ashtray["type"] == "goal" and ashtray["path"] == ["Craft and Career"] and ashtray["priority"] == "Low"
    assert items["Build out Daily Dash"]["type"] == "task"
    assert "Finished project" not in items  # Time Spent has reached Time to Complete
    assert "Swim v8" not in items

    peace = next(n for n in db.get_tree() if n["title"] == "Craft and Career")
    node = next(c for c in peace["children"] if c["title"] == "Make Skatepark Ashtray")
    assert node["planned"]["kind"] == "goal" and node["items"] == []
    assert "Make Skatepark Ashtray" not in [i["title"] for i in peace["items"]]

    start = dt.datetime(2026, 10, 26, 8, tzinfo=NY)
    payload, _ = optimizer.build_payload(start, start + dt.timedelta(days=7))
    goal = next(g for g in payload["goals"] if g["title"] == "Make Skatepark Ashtray")
    assert goal["level"] == "Project" and goal["hours_remaining"] == 9 and goal["frequency"] == "2x Week"
    assert next(g for g in payload["goals"] if g["title"] == "Learn piano")["level"] == "Routine"
    assert "hours_remaining" not in next(g for g in payload["goals"] if g["title"] == "Learn piano")
    tree_peace = next(t for t in payload["tree"] if t["title"] == "Craft and Career")
    planned = {c["title"]: c.get("planned_as") for c in tree_peace["children"]}
    assert planned["Make Skatepark Ashtray"] == {"kind": "goal", "frequency": "2x Week", "session_hr": 3, "hours_remaining": 9}
    assert planned["Build out Daily Dash"] == {"kind": "task", "deadline": "2026-11-30", "hours_remaining": 80}
    assert planned["Finished project"] is None


def _promote_session(item_id, title, start, end):
    run_id = f"run-{item_id}"
    db.create_optimizer_run(run_id, "2026-10-20T00:00:00+00:00", "api", "m", "2026-10-19T00:00:00-04:00", "2026-10-26T00:00:00-04:00")
    ids = db.insert_proposals(run_id, [
        {"item_type": "goal", "item_id": item_id, "title": title, "description": "", "location": "", "start": start,
         "end": end, "all_day": False, "timezone": "America/New_York", "rationale": "", "status": "proposed",
         "invalid_reason": None, "ref": "a", "replaces_event_id": ""},
    ], "2026-10-20T00:00:00+00:00")
    db.promote_proposal(ids[0], "2026-10-21T00:00:00+00:00")
    return ids[0]


def test_settling_a_project_session_also_logs_its_hours(monkeypatch):
    _sync(_planned_pages())
    fake = FakeNotion()
    fake.numbers[("proj-ashtray", "Time Spent")] = 3.0
    fake.numbers[("proj-ashtray", "Sessions")] = 1
    fake.dates[("proj-ashtray", "Last Occurred")] = None
    for name in ("resolve_property_name", "read_number_property", "update_number_property", "read_date_property", "update_date_property"):
        monkeypatch.setattr(time_tracker.notion_adapter, name, getattr(fake, {"resolve_property_name": "resolve", "read_number_property": "read_number",
                            "update_number_property": "write_number", "read_date_property": "read_date", "update_date_property": "write_date"}[name]))
    pid = _promote_session("notion_proj-ashtray", "Make Skatepark Ashtray", "2026-10-22T18:00:00-04:00", "2026-10-22T20:00:00-04:00")
    summary = time_tracker.settle_task_time(now=dt.datetime(2026, 10, 23, 9, tzinfo=NY))
    assert summary["errors"] == []
    assert ("proj-ashtray", "Last Occurred", "2026-10-22") in fake.writes
    assert ("proj-ashtray", "Time Spent", 5.0) in fake.writes
    assert ("proj-ashtray", "Sessions", 2) in fake.writes
    row = {g["goal"]: g for g in db.get_goals()}["Make Skatepark Ashtray"]
    assert row["time_spent_hr"] == 5.0 and row["sessions"] == 2 and row["last_occurred"] == "2026-10-22"
    settled = {p["id"]: p for p in db.get_proposals()}[pid]
    assert settled["settled_at"] and settled["settled_hours"] == 2.0


def test_a_failed_time_spent_write_leaves_the_session_unsettled(monkeypatch):
    _sync(_planned_pages())
    fake = FakeNotion()
    fake.numbers[("proj-ashtray", "Time Spent")] = 3.0
    fake.numbers[("proj-ashtray", "Sessions")] = 1
    fake.dates[("proj-ashtray", "Last Occurred")] = None

    def write_number(page_id, name, value):
        if name == "Time Spent":
            raise RuntimeError("Notion is down")
        return fake.write_number(page_id, name, value)

    monkeypatch.setattr(time_tracker.notion_adapter, "resolve_property_name", fake.resolve)
    monkeypatch.setattr(time_tracker.notion_adapter, "read_number_property", fake.read_number)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_number_property", write_number)
    monkeypatch.setattr(time_tracker.notion_adapter, "read_date_property", fake.read_date)
    monkeypatch.setattr(time_tracker.notion_adapter, "update_date_property", fake.write_date)
    pid = _promote_session("notion_proj-ashtray", "Make Skatepark Ashtray", "2026-10-22T18:00:00-04:00", "2026-10-22T20:00:00-04:00")
    summary = time_tracker.settle_task_time(now=dt.datetime(2026, 10, 23, 9, tzinfo=NY))
    assert len(summary["errors"]) == 1 and summary["settled"] == []
    assert ("proj-ashtray", "Last Occurred", "2026-10-22") in fake.writes  # harmless to redo: never moves backwards
    assert not any(w[1] == "Sessions" for w in fake.writes)  # no count before the hours are safe
    assert {p["id"]: p for p in db.get_proposals()}[pid]["settled_at"] is None
