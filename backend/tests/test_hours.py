"""hours.py: validation, storage, the derived views and the /hours routes."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

import db
import hours
import main


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()


def _hours(**overrides):
    data = json.loads(json.dumps(hours.DEFAULT_HOURS))
    for dotted, value in overrides.items():
        if "." in dotted:
            profile, rng = dotted.split(".")
            data[profile][rng] = value
        else:
            data[dotted] = value
    return data


def test_defaults_until_something_is_saved():
    st = hours.status()
    assert st["saved"] is False and st["saved_at"] is None
    assert st["hours"] == hours.DEFAULT_HOURS
    assert hours.load() is not hours.DEFAULT_HOURS  # a copy: callers can't corrupt the defaults
    assert hours.DEFAULT_HOURS["weekends"]["work"] is None
    assert hours.DEFAULT_HOURS["free_hours_per_week"] == 10 and hours.DEFAULT_HOURS["calendar_review_minutes"] == 20


def test_save_round_trips_and_drops_unknown_and_legacy_keys():
    data = _hours(**{"weekdays.work": {"start": "09:30", "end": "17:30", "location": "3 WTC", "office_days": ["tue", "Thursday", "Tue"]}})
    data["extra"] = 1
    data["weekdays"]["free"] = {"start": "17:00", "end": "23:00"}  # the pre-2026-10-26 stored range: ignored
    data["weekends"]["work"] = {"start": "10:00", "end": "12:00"}  # weekends never work
    st = hours.save(data, now=dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.timezone.utc))
    assert st["saved"] is True and st["saved_at"] == "2026-10-18T12:00:00+00:00"
    assert st["hours"]["weekdays"]["work"] == {"start": "09:30", "end": "17:30", "location": "3 WTC", "office_days": ["Tue", "Thu"]}
    assert "free" not in st["hours"]["weekdays"] and st["hours"]["weekends"]["work"] is None
    assert "extra" not in st["hours"]
    assert hours.load() == st["hours"]


@pytest.mark.parametrize("bad, message", [
    ({"weekdays.sleep": {"start": "9:00", "end": "17:00"}}, "weekdays.sleep.start must be a time"),
    ({"weekdays.work": {"start": "17:00", "end": "17:00"}}, "start and end are the same"),
    ({"weekdays.work": {"start": "17:00", "end": "09:00"}}, "weekdays.work: the end (09:00) is before the start (17:00)"),
    ({"weekdays.sleep": None}, "weekdays.sleep is required"),
    ({"weekdays.work": {"start": "01:00", "end": "05:00"}}, "weekdays.work lies entirely inside sleep"),
    ({"weekdays.work": {"start": "09:00", "end": "17:00", "office_days": ["Sat"]}}, "Sat is not a weekday"),
    ({"weekdays.work": {"start": "09:00", "end": "17:00", "office_days": ["someday"]}}, "unknown day 'someday'"),
    ({"free_hours_per_week": -1}, "free_hours_per_week must be between 0 and 100"),
    ({"free_hours_per_week": "lots"}, "free_hours_per_week must be a number"),
    ({"calendar_review_minutes": 2}, "calendar_review_minutes must be between 5 and 180"),
    ({"calendar_review_minutes": 12.5}, "calendar_review_minutes must be a whole number"),
    ({"goals_review_every_months": 0}, "goals_review_every_months must be between 1 and 12"),
    ({"goals_review_minutes": 300}, "goals_review_minutes must be between 5 and 240"),
    ({"event_keywords": [1]}, "event_keywords must contain text only"),
    ({"event_keywords": ["x" * 41]}, "longer than 40 characters"),
])
def test_validation_names_the_field(bad, message):
    with pytest.raises(hours.HoursError, match=message.replace("(", "\\(").replace(")", "\\)")):
        hours.validate(_hours(**bad))


def test_sleep_may_cross_midnight_and_work_may_be_off():
    data = _hours(**{"weekends.sleep": {"start": "01:30", "end": "10:00"}, "weekdays.work": None})
    clean = hours.validate(data)
    assert clean["weekends"]["sleep"] == {"start": "01:30", "end": "10:00"}
    assert clean["weekdays"]["work"] is None


def test_keywords_accept_a_list_or_comma_separated_text():
    assert hours.parse_keywords("jazz, AI talks ,jazz,, climbing ") == ["jazz", "AI talks", "climbing"]
    assert hours.parse_keywords(["Jazz", " jazz "]) == ["Jazz"]
    assert hours.parse_keywords(None) == []
    clean = hours.validate(_hours(event_keywords="opera, book launch"))
    assert clean["event_keywords"] == ["opera", "book launch"]
    assert hours.event_keywords(clean) == ["opera", "book launch"]


def test_unreadable_saved_value_falls_back_to_defaults():
    db.set_setting(hours.SETTING_KEY, "{not json", "x")
    assert hours.load() == hours.DEFAULT_HOURS
    db.set_setting(hours.SETTING_KEY, json.dumps({"weekdays": {}}), "x")
    assert hours.load() == hours.DEFAULT_HOURS


def test_free_time_is_derived_from_work_and_sleep():
    h = hours.validate(_hours(**{"weekends.sleep": {"start": "00:30", "end": "09:00"}}))
    # Weekday: after work (17:00) until bed (23:00); weekend: from waking (09:00) to a bedtime after midnight = 24:00.
    assert hours.free_range(h["weekdays"]) == {"start": "17:00", "end": "23:00"}
    assert hours.free_range(h["weekends"]) == {"start": "09:00", "end": "24:00"}
    assert hours.ranges_for(dt.date(2026, 10, 19), h)["free"] == {"start": "17:00", "end": "23:00"}  # a Monday
    assert hours.ranges_for(dt.date(2026, 10, 24), h)["free"]["end"] == "24:00"  # a Saturday
    # No work on weekdays: free from waking.
    off = hours.validate(_hours(**{"weekdays.work": None}))
    assert hours.free_range(off["weekdays"]) == {"start": "07:00", "end": "23:00"}
    # Work until bedtime: an empty window, never a negative one.
    late = hours.validate(_hours(**{"weekdays.work": {"start": "09:00", "end": "23:00"}}))
    assert hours.free_range(late["weekdays"]) == {"start": "23:00", "end": "23:00"}
    assert hours.in_range(dt.time(23, 30), h["weekdays"]["sleep"]) is True   # wraps midnight
    assert hours.in_range(dt.time(7, 0), h["weekdays"]["sleep"]) is False     # end is exclusive
    assert hours.in_range(dt.time(12, 0), None) is False


def test_for_model_spells_out_office_and_home_days():
    h = hours.validate(_hours(**{"weekdays.work": {"start": "09:00", "end": "17:00", "location": "3 WTC", "office_days": ["Tue", "Wed", "Thu"]}}))
    model = hours.for_model(h)
    assert model["weekdays"]["awake"] == {"start": "07:00", "end": "23:00"}
    assert model["weekdays"]["free"] == {"start": "17:00", "end": "23:00"}
    assert model["weekdays"]["work"]["office_days"] == ["Tue", "Wed", "Thu"]
    assert model["weekdays"]["work"]["home_days"] == ["Mon", "Fri"]
    assert model["weekends"]["work"] is None and model["weekends"]["free"] == {"start": "08:00", "end": "23:00"}
    assert hours.is_office_day(dt.date(2026, 10, 20), h) is True   # Tuesday
    assert hours.is_office_day(dt.date(2026, 10, 19), h) is False  # Monday: home
    assert hours.is_office_day(dt.date(2026, 10, 24), h) is False  # Saturday
    assert hours.free_hours_per_week(h) == 10.0 and hours.calendar_review_minutes(h) == 20
    assert hours.goals_review_every_months(h) == 1 and hours.goals_review_minutes(h) == 30


def test_routes_get_put_and_422():
    client = TestClient(main.app)
    assert client.get("/hours").json()["saved"] is False
    body = _hours(free_hours_per_week=12, calendar_review_minutes=30, event_keywords="jazz, talks")
    r = client.put("/hours", json=body)
    assert r.status_code == 200
    saved = r.json()["hours"]
    assert saved["free_hours_per_week"] == 12 and saved["calendar_review_minutes"] == 30 and saved["event_keywords"] == ["jazz", "talks"]
    assert client.get("/hours").json()["saved"] is True
    r = client.put("/hours", json=_hours(**{"weekdays.work": {"start": "22:00", "end": "12:00"}}))
    assert r.status_code == 422 and "before the start" in r.json()["detail"]
    r = client.put("/hours", json={"weekdays": {}})
    assert r.status_code == 422
    assert client.get("/hours").json()["hours"]["free_hours_per_week"] == 12  # the saved value is untouched


def test_home_and_work_location_are_free_text():
    data = _hours()
    data["home"] = "  1 Home St,\n New York "
    data["weekdays"]["work"]["location"] = "100 Office Plaza"
    clean = hours.validate(data)
    assert clean["home"] == "1 Home St, New York"
    assert clean["weekdays"]["work"]["location"] == "100 Office Plaza"
    assert hours.validate(_hours())["home"] == "" and hours.validate(_hours())["weekdays"]["work"]["location"] == ""
    with pytest.raises(hours.HoursError, match="home is longer"):
        hours.validate({**_hours(), "home": "x" * 201})
    with pytest.raises(hours.HoursError, match="work.location must be text"):
        bad = _hours(); bad["weekdays"]["work"]["location"] = 5
        hours.validate(bad)
    assert hours.home_location(clean, "fallback") == "1 Home St, New York"
    assert hours.home_location(hours.validate(_hours()), "fallback") == "fallback"
    client = TestClient(main.app)
    r = client.put("/hours", json=data)
    assert r.status_code == 200 and r.json()["hours"]["home"] == "1 Home St, New York"
    assert hours.for_model(hours.load())["weekdays"]["work"]["location"] == "100 Office Plaza"
