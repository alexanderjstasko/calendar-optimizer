"""schedule_config.py: parsing of the cadence settings and fallback on bad values."""

from __future__ import annotations

import pytest

import schedule_config as sc


def test_defaults_when_nothing_is_set():
    s = sc.load_schedule(env={})
    assert s.calendars.minutes == 5 and s.settle.minutes == 15
    assert (s.scout_fetch.enabled, s.scout_fetch.day_of_week, s.scout_fetch.hour, s.scout_fetch.minute) == (True, "mon", 6, 0)
    assert (s.scout_recommend.enabled, s.scout_recommend.day_of_week, s.scout_recommend.hour) == (True, None, 7)
    assert (s.optimizer.enabled, s.optimizer.day_of_week, s.optimizer.hour, s.optimizer.minute) == (True, "sun", 6, 30)
    assert s.notion_check.minutes == 60 and s.conflict_check.minutes == 5
    assert s.errors == []
    d = s.describe()
    assert d["calendar_sync"] == "every 5 min" and d["scout_recommend"] == "daily at 07:00 America/New_York"
    assert d["optimizer"] == "sun at 06:30 America/New_York" and d["scout_fetch"] == "mon at 06:00 America/New_York"
    assert d["notion_check"] == "every 60 min" and d["conflict_check"] == "every 5 min"
    off = sc.load_schedule(env={"NOTION_CHECK_EVERY_MIN": "off", "CONFLICT_CHECK_EVERY_MIN": "0"})
    assert off.notion_check.enabled is False and off.conflict_check.enabled is False


def test_custom_values():
    s = sc.load_schedule(env={
        "SYNC_CALENDARS_EVERY_MIN": "10", "SETTLE_EVERY_MIN": "off",
        "SCOUT_FETCH_DAYS": "mon,thu", "SCOUT_FETCH_TIME": "5:30",
        "SCOUT_RECOMMEND_DAYS": "mon-fri", "SCOUT_RECOMMEND_TIME": "07:15",
        "OPTIMIZER_RUN_DAYS": "sun", "OPTIMIZER_RUN_TIME": "18:00",
    })
    assert s.calendars.minutes == 10 and s.settle.enabled is False
    assert (s.scout_fetch.day_of_week, s.scout_fetch.hour, s.scout_fetch.minute) == ("mon,thu", 5, 30)
    assert s.scout_recommend.day_of_week == "mon-fri"
    assert (s.optimizer.enabled, s.optimizer.day_of_week, s.optimizer.hour) == (True, "sun", 18)
    assert s.describe()["settlement"] == "off"


def test_bad_values_fall_back_and_are_reported():
    s = sc.load_schedule(env={
        "SYNC_CALENDARS_EVERY_MIN": "-3", "SCOUT_FETCH_DAYS": "monday", "SCOUT_RECOMMEND_TIME": "25:00", "OPTIMIZER_RUN_DAYS": "daily",
    })
    assert s.calendars.minutes == 5
    assert s.scout_fetch.day_of_week == "mon"
    assert s.scout_recommend.hour == 7
    assert s.optimizer.enabled and s.optimizer.day_of_week is None
    assert len(s.errors) == 3 and all("using" in e for e in s.errors)


@pytest.mark.parametrize("raw,expected", [("off", None), ("0", None), ("15", 15), (" 7 ", 7)])
def test_parse_interval(raw, expected):
    assert sc.parse_interval(raw) == expected


@pytest.mark.parametrize("raw,expected", [("off", (False, None)), ("daily", (True, None)), ("*", (True, None)),
                                          ("Mon, Wed", (True, "mon,wed")), ("mon-fri", (True, "mon-fri"))])
def test_parse_days(raw, expected):
    assert sc.parse_days(raw) == expected


def test_parse_days_and_time_reject_garbage():
    with pytest.raises(ValueError):
        sc.parse_days("tuesday")
    with pytest.raises(ValueError):
        sc.parse_time("7pm")
    assert sc.parse_time("07:05") == (7, 5)
