"""
review_model.py on plain dicts (no database): series grouping, the cadence
copy, travel attachment (by ref and by the legacy heuristic) and the
/review/summary document. The series below are shaped like the live plan of
2026-10-08 (Read the News x24, Swim x8 + travel, Floss x8, Free time x18).
"""

from __future__ import annotations

import copy
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

import review_model as rm

NY = ZoneInfo("America/New_York")
RUN = "run_live"
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=NY)
_counter = iter(range(1, 100000))


def _at(day: str, hhmm: str) -> dt.datetime:
    """'10-12', '08:15' -> 2026-10-12 08:15 New York (DST-correct offset)."""
    return dt.datetime.fromisoformat(f"2026-{day}T{hhmm}:00").replace(tzinfo=NY)


def _row(item_type, item_id, title, start: dt.datetime, minutes, *, status="proposed", ref=None, run_id=RUN, **kw):
    n = next(_counter)
    return {
        "id": f"{run_id}_{n}", "run_id": run_id, "item_type": item_type, "item_id": item_id, "title": title,
        "description": "", "location": "", "start": start.isoformat(),
        "end": (start + dt.timedelta(minutes=minutes)).isoformat(), "all_day": 0,
        "timezone": "America/New_York", "rationale": "why", "status": status, "ref": ref, "rank": None,
        "shown_at": None, **kw,
    }


NEWS = ["10-12 08:15", "10-13 08:15", "10-14 08:00", "10-15 08:15", "10-16 08:15", "10-17 10:00", "10-18 10:00",
        "10-19 08:15", "10-20 08:15", "10-21 08:15", "10-22 08:15", "10-23 08:15", "10-24 10:00", "10-25 10:00",
        "10-26 08:15", "10-27 08:15", "10-28 08:15", "10-29 08:15", "11-02 08:15", "11-03 08:15", "11-04 08:15",
        "11-05 08:15", "11-06 08:15", "11-07 10:00"]
SWIM_DAYS = ["10-12", "10-14", "10-19", "10-21", "10-26", "10-28", "11-02", "11-04"]  # Mon & Wed
FLOSS = ["10-14 21:15", "10-17 10:30", "10-20 08:00", "10-23 08:00", "10-26 08:45", "10-29 08:00", "11-04 08:00",
         "11-07 10:30"]
FREE = [("10-09 18:00", 180), ("10-10 14:00", 180), ("10-13 18:00", 180), ("10-16 18:00", 180), ("10-17 14:00", 180),
        ("10-17 19:00", 60), ("10-23 17:00", 60), ("10-24 13:00", 180), ("10-24 19:00", 120), ("10-25 11:00", 120),
        ("10-25 18:30", 120), ("10-29 19:00", 120), ("10-31 11:00", 180), ("10-31 15:00", 180), ("11-03 18:00", 180),
        ("11-06 17:00", 60), ("11-07 13:00", 180), ("11-07 19:00", 120)]


def _news():
    return [_row("goal", "notion_news", "Read the News", _at(*d.split()), 30) for d in NEWS]


def _climbs(*, with_ref: bool):
    rows = []
    for i, day in enumerate(SWIM_DAYS, start=1):
        ref = f"r{i}"
        rows.append(_row("goal", "notion_swim", "Swim", _at(day, "17:30"), 180, ref=ref if with_ref else None))
        rows.append(_row("travel", ref, "Travel to Swim", _at(day, "17:00"), 30, ref=f"t{i}" if with_ref else None))
    return rows


def _by_title(series):
    return {s["title"]: s for s in series}


# --- cadence copy on live-shaped series ------------------------------------------------


def test_daily_series_across_weekdays_and_weekends():
    [news] = rm.group_series(_news())
    assert (news["count"], news["pending"], news["approved"]) == (24, 24, 0)
    assert news["cadence"] == "daily"
    assert news["time"] == "08:15"  # 17 of 24 at 08:15 (weekends at 10:00, one 08:00)
    assert news["duration_min"] == 30
    assert news["summary"] == "daily 8:15 · 30 min · 24×"
    assert news["first"] == "2026-10-12T08:15:00-04:00" and news["last"] == "2026-11-07T10:00:00-05:00"  # across DST
    assert news["key"] == "goal:notion_news" and news["item_type"] == "goal" and not news["has_travel"]
    assert [o["start"] for o in news["occurrences"]] == sorted(o["start"] for o in news["occurrences"])


@pytest.mark.parametrize("with_ref", [True, False], ids=["by-ref", "legacy-heuristic"])
def test_mon_wed_series_with_travel_attached(with_ref):
    rows = _climbs(with_ref=with_ref)
    series = rm.group_series(rows)
    assert len(series) == 1  # the eight travel blocks are not a series of their own
    swim = series[0]
    assert swim["cadence"] == "Mon & Wed" and swim["time"] == "17:30" and swim["duration_min"] == 180
    assert swim["has_travel"] is True and swim["count"] == 8
    assert swim["summary"] == "Mon & Wed 17:30 · 3 h · 8× · + travel"
    for occ in swim["occurrences"]:
        assert occ["travel"]["title"] == "Travel to Swim"
        assert occ["travel"]["end"] == occ["start"]


def test_travel_ref_wins_over_title_and_timing():
    goal = _row("goal", "notion_swim", "Swim", _at("10-12", "17:30"), 180, ref="p1")
    commute = _row("travel", "p1", "Commute", _at("10-12", "16:45"), 30)  # ends 17:15, other title
    [only] = rm.group_series([goal, commute])
    assert only["occurrences"][0]["travel"]["id"] == commute["id"]


def test_travel_to_a_fixed_calendar_event_is_its_own_series():
    rows = [_row("travel", "google_tennis1", "Travel to Tennis", _at("10-18", "16:50"), 40),
            _row("travel", "google_tennis2", "Travel to Tennis", _at("10-25", "13:50"), 40),
            _row("travel", "r9", "Travel to Swim", _at("10-12", "17:00"), 30)]  # destination not in the list
    by_title = _by_title(rm.group_series(rows))
    assert by_title["Travel to Tennis"]["count"] == 2 and by_title["Travel to Tennis"]["key"] == "travel:travel to tennis"
    assert by_title["Travel to Swim"]["count"] == 1


def test_every_three_days_with_varying_times():
    rows = [_row("goal", "notion_floss", "Floss", _at(*d.split()), 15) for d in FLOSS]
    [floss] = rm.group_series(rows)
    assert floss["cadence"] == "every 3 days" and floss["time"] == "varies"
    assert floss["summary"] == "every 3 days · varies · 15 min · 8×"


def test_irregular_free_time_reads_as_a_weekly_rate():
    rows = [_row("free", "", "Free time", _at(*d.split()), minutes) for d, minutes in FREE]
    [free] = rm.group_series(rows)
    assert free["key"] == "free:free time"
    assert free["cadence"] == "4× a week" and free["duration_min"] == 180
    assert free["summary"] == "4× a week · varies · 3 h · 18×"


def test_whole_live_shaped_plan_groups_into_series_then_singles():
    repot = _row("goal", "notion_repot", "Repot", _at("10-24", "11:00"), 45)
    plants = [_row("goal", "notion_plants", "Water Plants", _at(d, "08:00"), 15) for d in ("10-19", "10-26")]
    rows = _news() + _climbs(with_ref=False) + [repot] + plants + \
        [_row("goal", "notion_floss", "Floss", _at(*d.split()), 15) for d in FLOSS]
    series = rm.group_series(rows)
    assert [(s["title"], s["count"]) for s in series] == [
        ("Read the News", 24), ("Swim", 8), ("Floss", 8), ("Water Plants", 2), ("Repot", 1)]
    by_title = _by_title(series)
    assert by_title["Water Plants"]["summary"] == "Mondays 8:00 · 15 min · 2×"
    assert by_title["Repot"]["cadence"] == "once" and by_title["Repot"]["summary"] == "Sat Oct 24 11:00 · 45 min"


def test_partial_approval_counts_and_entity_titles():
    rows = _climbs(with_ref=True)
    for r in rows[:6]:  # three climbs and their travel approved
        r["status"] = "promoted"
    talk = _row("goal", "notion_sci", "Sci&shy;ence &amp; Art", _at("10-20", "18:30"), 120)
    by_key = {s["key"]: s for s in rm.group_series(rows + [talk])}
    swim = by_key["goal:notion_swim"]
    assert (swim["pending"], swim["approved"], swim["count"]) == (5, 3, 8)
    assert by_key["goal:notion_sci"]["title"] == "Science & Art"


def test_group_series_does_not_modify_its_input():
    rows = _climbs(with_ref=True) + _news()
    before = copy.deepcopy(rows)
    rm.group_series(rows)
    assert rows == before


@pytest.mark.parametrize("days, expected", [
    (["10-12", "10-13", "10-14", "10-15", "10-16", "10-19", "10-20", "10-21", "10-22", "10-23"], "weekdays"),
    (["10-17", "10-18", "10-24", "10-25", "10-31", "11-01"], "weekends"),
    (["10-12", "10-19", "10-26", "11-02"], "Mondays"),
    (["10-13", "10-15", "10-17", "10-20", "10-22", "10-24"], "Tue, Thu & Sat"),
    (["10-12", "10-20", "10-27", "11-03"], "weekly"),  # Mon then Tuesdays: a week apart, no fixed day
    (["10-12", "10-20", "10-27", "11-05"], "every 8 days"),  # gaps 8, 7, 9
    (["10-09", "10-10", "10-13", "10-18", "10-19", "10-27", "10-30", "11-02"], "2× a week"),  # irregular -> a rate
    (["10-09", "10-10", "10-15", "10-16", "10-29", "11-07"], "weekly"),  # Thu/Fri/Sat but not weekly on each
    (["10-12", "10-14", "10-16", "10-18", "10-20", "10-22"], "every other day"),
    (["10-12", "10-26", "11-09"], "every other Mon"),
    (["10-12", "10-22", "11-01", "11-11"], "every 10 days"),
    (["10-12"], "once"),
])
def test_cadence_rules(days, expected):
    assert rm.cadence([_at(d, "09:00") for d in days]) == expected


def test_formatting_helpers():
    assert rm.format_duration(15) == "15 min" and rm.format_duration(180) == "3 h" and rm.format_duration(90) == "1 h 30 min"
    assert rm.format_time("08:15") == "8:15" and rm.format_time("varies") == "varies"


# --- /review/summary --------------------------------------------------------------------


def _option(day, hhmm, *, status="proposed", shown=True, rank=1):
    return _row("event", f"disc_{day}_{hhmm}", f"Talk {day} {hhmm}", _at(day, hhmm), 90, run_id="scout_1",
                status=status, rank=rank, shown_at="2026-10-08T11:00:00+00:00" if shown else None)


def test_build_summary_counts_and_filters():
    now = dt.datetime(2026, 10, 12, 12, 0, tzinfo=NY)
    past_option = _option("10-10", "19:00")
    shown = [_option("10-13", "19:00", rank=1), _option("10-14", "19:00", rank=2)]
    queued_unseen = [_option("10-15", "19:00", status="queued", shown=False, rank=r) for r in (3, 4, 5)]
    queued_seen = _option("10-16", "19:00", status="queued", rank=9)
    approved_event = _option("10-20", "19:00", status="promoted")
    plan = _news()
    plan[0]["status"] = "promoted"  # 10-12 08:15 -- over by noon, dropped
    plan[1]["status"] = "promoted"  # 10-13 -- upcoming approved
    held = [_row("goal", "notion_x", "Ghost", _at("10-20", "18:00"), 60, status="invalid", invalid_reason="unknown goal id"),
            _row("goal", "notion_x", "Old ghost", _at("10-01", "18:00"), 60, status="invalid")]
    runs = {"optimizer": {"run_id": RUN, "kind": "optimizer", "status": "succeeded", "started_at": "s",
                          "finished_at": "f", "proposed_count": 24, "invalid_count": 2, "notes": "n",
                          "backend": "cli", "error": None},
            "scout": None}
    rows = [past_option, *shown, *queued_unseen, queued_seen, approved_event, *plan]
    out = rm.build_summary(rows, held, runs, now, page_size=8)

    ev = out["events"]
    assert (ev["pending"], ev["queued"], ev["approved_upcoming"], ev["page_size"], ev["reload"]) == (2, 3, 1, 8, "queue")
    assert [i["status"] for i in ev["items"]] == ["proposed", "proposed", "promoted"]  # queued options are not listed
    p = out["plan"]
    assert (p["pending"], p["approved_upcoming"], p["pending_series"]) == (22, 1, 1)
    assert p["series"][0]["summary"].endswith("23×") and p["series"][0]["approved"] == 1
    assert [h["title"] for h in p["held_back"]] == ["Ghost"]
    assert out["runs"]["optimizer"] == {k: runs["optimizer"][k] for k in rm.RUN_SUMMARY_FIELDS}
    assert out["runs"]["scout"] is None
    assert out["generated_at"].startswith("2026-10-12T12:00:00")

    no_queue = rm.build_summary(shown, [], {"optimizer": None, "scout": None}, now, page_size=8)
    assert no_queue["events"]["queued"] == 0 and no_queue["events"]["reload"] == "model"


def test_is_past_matches_expiry_rules():
    now = dt.datetime(2026, 10, 12, 9, 0, tzinfo=NY)
    assert rm.is_past({"all_day": 0, "end": "2026-10-12T08:59:00-04:00"}, now)
    assert not rm.is_past({"all_day": 0, "end": "2026-10-12T13:30:00+00:00"}, now)  # 09:30 New York
    assert not rm.is_past({"all_day": 1, "end": "2026-10-12"}, now)
    assert rm.is_past({"all_day": 1, "end": "2026-10-11"}, now)
    assert not rm.is_past({"all_day": 0, "end": "garbage"}, now)


def test_occurrences_are_ordered_by_time_not_by_their_stored_text():
    a = {**_row("goal", "notion_g", "G", _at("10-12", "07:00"), 30)}                       # 07:00 New York
    b = {**_row("goal", "notion_g", "G", _at("10-13", "07:00"), 30),
         "start": "2026-10-12T10:30:00+00:00", "end": "2026-10-12T11:00:00+00:00"}       # 06:30 New York, stored in UTC
    [series] = rm.group_series([a, b])
    assert [o["id"] for o in series["occurrences"]] == [b["id"], a["id"]]
    assert series["first"] == b["start"] and series["last"] == a["start"]
