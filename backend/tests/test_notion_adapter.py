"""
Unit tests for notion_adapter.normalize_goal / normalize_task using canned
Notion page payloads. No network: fetch_all_pages is never called.
"""

from __future__ import annotations

import json

import notion_adapter as na


def _page(props: dict, page_id: str = "abc-123") -> dict:
    return {"id": page_id, "last_edited_time": "2026-09-19T12:00:00.000Z", "properties": props}


def _title(text):
    return {"type": "title", "title": [{"plain_text": text}]}


def _rich(text):
    return {"type": "rich_text", "rich_text": [{"plain_text": text}] if text else []}


def _select(name):
    return {"type": "select", "select": {"name": name} if name else None}


def _checkbox(value):
    return {"type": "checkbox", "checkbox": value}


def test_context_goal_with_notes_is_imported():
    page = _page({
        "Goal": _title("Work"),
        "Frequency": _rich(""),
        "Required Time (hr)": {"type": "number", "number": None},
        "Priority": _select(None),
        "Location": _rich("100 Office Plaza"),
        "Day of Week": {"type": "multi_select", "multi_select": []},
        "Time of Day": _rich(""),
        "Show on Calendar": _checkbox(False),
        "Notes for LLM": _rich("I work from home Mondays and Fridays."),
        "Last Occurred": {"type": "date", "date": {"start": "2026-09-11", "end": None, "time_zone": None}},
        "Status": {"type": "status", "status": {"name": "In progress"}},
        "End Date": {"type": "date", "date": None},
    })
    goal = na.normalize_goal(page)
    assert goal["goal"] == "Work"
    assert goal["last_occurred"] == "2026-09-11"
    assert goal["show_on_calendar"] == 0
    assert goal["notes_for_llm"] == "I work from home Mondays and Fridays."
    assert goal["location"] == "100 Office Plaza"
    assert goal["days_of_week"] is None
    assert goal["source_goal_id"] == "abc-123"


def test_checked_goal_with_empty_notes():
    page = _page({
        "Goal": _title("Swim"),
        "Frequency": _rich("2x Week"),
        "Required Time (hr)": {"type": "number", "number": 2},
        "Priority": _select("High"),
        "Location": _rich(""),
        "Day of Week": {"type": "multi_select", "multi_select": [{"name": "Monday"}, {"name": "Wednesday"}]},
        "Time of Day": _rich("Afterwork"),
        "Show on Calendar": _checkbox(True),
        "Notes for LLM": _rich(""),
        "Status": {"type": "status", "status": {"name": "In progress"}},
        "End Date": {"type": "date", "date": None},
    })
    goal = na.normalize_goal(page)
    assert goal["show_on_calendar"] == 1
    assert goal["notes_for_llm"] is None
    assert goal["last_occurred"] is None  # property absent on this page
    assert json.loads(goal["days_of_week"]) == ["Monday", "Wednesday"]
    assert goal["time_of_day_window"] == "Afterwork"


def test_missing_checkbox_property_counts_as_unchecked():
    page = _page({
        "Goal": _title("Legacy"),
        "Status": {"type": "status", "status": {"name": "In progress"}},
    })
    goal = na.normalize_goal(page)
    assert goal["show_on_calendar"] == 0
    assert goal["notes_for_llm"] is None


# --- tasks + the one write-back -------------------------------------------------


def test_normalize_task_imports_time_spent():
    page = _page({
        "Task": _title("Calendar Optimizer"),
        "Time to Complete (hr)": {"type": "number", "number": 20},
        "Time Spent (hr)": {"type": "number", "number": 3.5},
        "Deadline": {"type": "date", "date": None},
        "Priority": _select("Low"),
        "Location": _rich(""),
        "Status": {"type": "status", "status": {"name": "Not started"}},
    })
    task = na.normalize_task(page)
    assert task["time_to_complete_hr"] == 20
    assert task["time_spent_hr"] == 3.5
    assert task["deadline"] is None


def test_update_number_property_sends_only_that_property(monkeypatch):
    page = {"id": "pg", "properties": {
        "Task": _title("X"),
        "Time Spent (hr)": {"type": "number", "number": 1.0},
    }}
    calls = {}

    class _Resp:
        def __init__(self, body): self._body = body
        def raise_for_status(self): pass
        def json(self): return self._body

    def fake_get(url, headers, timeout):
        calls["get"] = url
        return _Resp(page)

    def fake_patch(url, headers, json, timeout):
        calls["patch"] = (url, json)
        updated = {"id": "pg", "properties": {**page["properties"], "Time Spent (hr)": {"type": "number", "number": json["properties"]["Time Spent (hr)"]["number"]}}}
        return _Resp(updated)

    monkeypatch.setattr(na.requests, "get", fake_get)
    monkeypatch.setattr(na.requests, "patch", fake_patch)

    assert na.read_number_property("pg", "Time Spent (hr)") == 1.0
    assert na.update_number_property("pg", "Time Spent (hr)", 2.5) == 2.5
    assert calls["patch"] == (f"{na.BASE_URL}/pages/pg", {"properties": {"Time Spent (hr)": {"number": 2.5}}})


def test_update_number_property_refuses_missing_or_mistyped_property(monkeypatch):
    class _Resp:
        def __init__(self, body): self._body = body
        def raise_for_status(self): pass
        def json(self): return self._body

    page = {"id": "pg", "properties": {"Notes": _rich("x")}}
    monkeypatch.setattr(na.requests, "get", lambda url, headers, timeout: _Resp(page))
    monkeypatch.setattr(na.requests, "patch", lambda *a, **k: pytest.fail("must not write"))
    import pytest
    with pytest.raises(ValueError, match="does not exist"):
        na.update_number_property("pg", "Time Spent (hr)", 1.0)
    page["properties"]["Time Spent (hr)"] = _rich("3")
    with pytest.raises(ValueError, match="not number"):
        na.update_number_property("pg", "Time Spent (hr)", 1.0)


def test_update_date_property_sends_only_that_property(monkeypatch):
    page = {"id": "pg", "properties": {
        "Goal": _title("Repot"),
        "Last Occurred": {"type": "date", "date": {"start": "2026-09-11", "end": None, "time_zone": None}},
    }}
    calls = {}

    class _Resp:
        def __init__(self, body): self._body = body
        def raise_for_status(self): pass
        def json(self): return self._body

    def fake_patch(url, headers, json, timeout):
        calls["patch"] = (url, json)
        start = json["properties"]["Last Occurred"]["date"]["start"]
        return _Resp({"id": "pg", "properties": {**page["properties"], "Last Occurred": {"type": "date", "date": {"start": start, "end": None}}}})

    monkeypatch.setattr(na.requests, "get", lambda url, headers, timeout: _Resp(page))
    monkeypatch.setattr(na.requests, "patch", fake_patch)
    assert na.read_date_property("pg", "Last Occurred") == "2026-09-11"
    assert na.update_date_property("pg", "Last Occurred", "2026-09-26") == "2026-09-26"
    assert calls["patch"] == (f"{na.BASE_URL}/pages/pg", {"properties": {"Last Occurred": {"date": {"start": "2026-09-26"}}}})

    page["properties"]["Last Occurred"] = _rich("x")
    import pytest
    monkeypatch.setattr(na.requests, "patch", lambda *a, **k: pytest.fail("must not write"))
    with pytest.raises(ValueError, match="not date"):
        na.update_date_property("pg", "Last Occurred", "2026-09-26")



# --- Website List -----------------------------------------------------------------


def test_normalize_website_and_text_write(monkeypatch):
    page = _page({
        "URL": _title("https://www.nypl.org/events/calendar"),
        "Upcoming Event": _rich(""),
        "Events Attended": {"type": "number", "number": None},
    }, page_id="site-1")
    site = na.normalize_website(page)
    assert site == {"source": "notion", "source_website_id": "site-1", "url": "https://www.nypl.org/events/calendar",
                    "upcoming_event": None, "events_attended": None, "last_modified": "2026-09-19T12:00:00.000Z", "raw_json": page}

    calls = {}

    class _Resp:
        def __init__(self, body): self._body = body
        def raise_for_status(self): pass
        def json(self): return self._body

    def fake_patch(url, headers, json, timeout):
        calls["patch"] = (url, json)
        text = "".join(c["text"]["content"] for c in json["properties"]["Upcoming Event"]["rich_text"])
        return _Resp({"id": "site-1", "properties": {**page["properties"], "Upcoming Event": _rich(text)}})

    monkeypatch.setattr(na.requests, "get", lambda url, headers, timeout: _Resp(page))
    monkeypatch.setattr(na.requests, "patch", fake_patch)
    assert na.read_text_property("site-1", "Upcoming Event") == ""
    assert na.update_text_property("site-1", "Upcoming Event", "Poets Out Loud — 2026-10-03") == "Poets Out Loud — 2026-10-03"
    assert calls["patch"] == (f"{na.BASE_URL}/pages/site-1", {"properties": {"Upcoming Event": {"rich_text": [
        {"type": "text", "text": {"content": "Poets Out Loud — 2026-10-03"}}]}}})
    assert na.update_text_property("site-1", "Upcoming Event", "") == ""
    assert calls["patch"][1] == {"properties": {"Upcoming Event": {"rich_text": []}}}
