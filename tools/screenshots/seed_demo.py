"""
Fill a demo copy's events.db with a made-up week for the docs/ screenshots.

The week is the coming Monday to Sunday, so every block is still ahead and
nothing expires. Every person, place and link is invented; never seed a demo
from real data. Run by serve_demo.sh, inside the demo copy:

    python3 seed_demo.py <demo>/backend
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

demo_backend = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(demo_backend))

import db  # noqa: E402
import hours  # noqa: E402

if db.DB_PATH.parent != demo_backend:
    sys.exit(f"refusing to seed {db.DB_PATH}: not inside {demo_backend}")
if db.DB_PATH.exists():
    db.DB_PATH.unlink()
db.init_db()

TZ = "America/New_York"
ZONE = ZoneInfo(TZ)
NOW_DT = dt.datetime.now(ZONE).replace(microsecond=0)
NOW = NOW_DT.isoformat()
MONDAY = NOW_DT.date() + dt.timedelta(days=7 - NOW_DT.weekday())  # the coming Monday
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def at(day: int, hm: str) -> str:
    h, m = map(int, hm.split(":"))
    return dt.datetime.combine(MONDAY + dt.timedelta(days=day), dt.time(h, m), ZONE).isoformat()


def date(day: int) -> str:
    return (MONDAY + dt.timedelta(days=day)).isoformat()


# --- Hours tab ------------------------------------------------------------
hours.save({
    **hours.DEFAULT_HOURS,
    "home": "1 Home St, New York",
    "weekdays": {"sleep": {"start": "23:00", "end": "07:00"},
                 "work": {"start": "09:00", "end": "17:00", "location": "100 Office Plaza",
                          "office_days": ["Tue", "Wed", "Thu"]}},
    "weekends": {"sleep": {"start": "23:30", "end": "08:30"}, "work": None},
    "free_hours_per_week": 10,
    "calendar_review_minutes": 20,
    "event_keywords": ["jazz", "urban design", "film"],
})

# --- Synced calendar events ----------------------------------------------
SOURCES = {"google": "google_primary", "apple": "apple_default",
           "outlook": "outlook_default", "partiful": "partiful_default"}
events: list[dict] = []


def ev(source, sid, title, day, start, end, location=""):
    events.append({"source": source, "source_event_id": sid, "calendar_id": SOURCES[source],
                   "title": title, "start": at(day, start), "end": at(day, end), "timezone": TZ,
                   "location": location, "description": "", "all_day": False,
                   "status": "confirmed", "last_modified": NOW})


for d in range(MON, SAT):
    ev("outlook", f"standup-{d}", "Team standup", d, "09:30", "09:45", "Video call")
ev("outlook", "planning", "Sprint planning", MON, "11:00", "12:00", "Room 4B")
ev("apple", "bookclub", "Book club", TUE, "19:30", "21:00", "Corner Café")
ev("google", "dentist", "Dentist", WED, "08:00", "08:45", "Smile Dental, 20 Park Ave")
ev("outlook", "oneonone", "1:1 with Jordan", WED, "14:00", "14:30", "Video call")
ev("outlook", "review", "Design review", THU, "11:00", "12:00", "Room 2A")
ev("google", "dinner", "Dinner with Sam", THU, "19:00", "21:00", "Lucia's Trattoria")
ev("apple", "yoga", "Yoga class", SAT, "09:30", "10:30", "Studio Flow")
ev("partiful", "party", "Friendsgiving potluck", SAT, "19:00", "22:30", "Alex's place")
ev("google", "brunch", "Brunch with Priya", SUN, "11:00", "12:30", "Sunny Side Diner")
events.append({"source": "google", "source_event_id": "bday", "calendar_id": "google_primary",
               "title": "Mom's birthday", "start": date(FRI), "end": date(SAT), "timezone": TZ,
               "location": "", "description": "", "all_day": True, "status": "confirmed",
               "last_modified": NOW})
db.upsert_events(events)


# --- Notion goals and tasks ------------------------------------------------
def goal(gid, title, gtype, tpw, session_hr, window=None, days=None, priority="Medium", notes=None, location=None):
    db.upsert_goal({"source": "notion", "source_goal_id": gid, "goal": title, "frequency": None,
                    "times_per_week": tpw, "end_date": None, "required_time_hr": session_hr,
                    "priority": priority, "location": location,
                    "days_of_week": json.dumps(days) if days else None, "time_of_day_window": window,
                    "show_on_calendar": 1, "notes_for_llm": notes, "last_occurred": None,
                    "goal_type": gtype, "status": "In-Progress", "level": "Routine",
                    "last_modified": NOW})
    return f"notion_{gid}"


RUN = goal("run", "Run", "Flexible", 3, 1.0, "Morning", priority="High", notes="Training for a spring half marathon.")
STRETCH = goal("stretch", "Stretch", "Routine", 7, 0.25, "Evening")
READ = goal("read", "Read", "Flexible", 3, 0.75, "Evening")
CLIMB = goal("climb", "Bouldering", "Flexible", 1, 2.0, "Evening", location="Summit Climbing Gym")
LAUNDRY = goal("laundry", "Laundry", "Routine", 1, 1.5, "Morning", days=["Sunday"])
goal("culture", "Culture event", "Event Interest", 1, 2.0)


def task(tid, title, hours_needed, deadline, priority="Medium"):
    db.upsert_task({"source": "notion", "source_task_id": tid, "task": title,
                    "time_to_complete_hr": hours_needed, "time_spent_hr": 0, "deadline": deadline,
                    "priority": priority, "location": None, "status": "Not started", "level": "Task",
                    "last_modified": NOW})
    return f"notion_{tid}"


README = task("readme", "Write the side project's README", 4, date(FRI), "High")
TRIP = task("trip", "Plan the Lisbon trip", 1.5, date(SUN + 8))

# --- The weekly plan (an optimizer run) -------------------------------------
db.create_optimizer_run("run_demo_plan", NOW, "api", "claude", at(MON, "00:00"), at(SUN, "23:59"))
plan: list[dict] = []
approved: set[int] = set()


def block(item_type, item_id, title, day, start, end, why, location="", ref=None, ok=False):
    if ok:
        approved.add(len(plan))
    plan.append({"item_type": item_type, "item_id": item_id, "title": title, "location": location,
                 "description": "", "start": at(day, start), "end": at(day, end), "all_day": False,
                 "timezone": TZ, "rationale": why, "status": "proposed", "ref": ref or f"r{len(plan)}"})


block("goal", RUN, "Run", MON, "07:00", "08:00", "Morning slot before standup; first of three runs.", ok=True)
block("goal", READ, "Read", MON, "20:30", "21:15", "Quiet evening after work.", ok=True)
block("goal", RUN, "Run", WED, "07:00", "08:00", "Morning run, a day after the last one.", ok=True)
block("goal", RUN, "Run", FRI, "07:00", "08:00", "Morning run, a day after the last one.")
block("goal", RUN, "Run", SUN, "08:45", "09:45", "Weekend long run before brunch.")
for d in range(TUE, SUN + 1):
    block("goal", STRETCH, "Stretch", d, "22:15", "22:30", "Daily routine before bed.")
block("goal", READ, "Read", WED, "20:30", "21:15", "Free evening; no plans after work.")
block("goal", READ, "Read", SUN, "20:00", "20:45", "Sunday wind-down.")
block("task", README, "Write the side project's README", TUE, "17:30", "19:00",
      "Due Friday; first of two sessions, before book club.", ok=True)
block("task", README, "Write the side project's README", WED, "17:30", "20:00",
      "Due Friday; finishes the 4 hours.", ok=True)
block("travel", "climb_ref", "Travel to Bouldering", FRI, "18:00", "18:30", "Subway to the gym.",
      "Summit Climbing Gym", ref="travel1")
block("goal", CLIMB, "Bouldering", FRI, "18:30", "20:30", "Friday evening is free and the gym is quieter.",
      "Summit Climbing Gym", ref="climb_ref")
block("task", TRIP, "Plan the Lisbon trip", SAT, "14:00", "15:30", "Saturday afternoon, between yoga and the potluck.")
block("goal", LAUNDRY, "Laundry", SUN, "13:30", "15:00", "Sunday routine, after brunch.")
block("goal", "builtin_calendar_review", "Calendar Review", SUN, "17:00", "17:20", "Review next week's plan.")
ids = db.insert_proposals("run_demo_plan", plan, NOW)
db.finish_optimizer_run("run_demo_plan", status="succeeded", finished_at=NOW, proposed_count=len(ids),
                        invalid_count=0,
                        notes="Three runs spaced out, the README finished before Friday, Saturday kept light.")
for n in sorted(approved):
    db.promote_proposal(ids[n], NOW)

# --- Scouted events (the Events tab) -----------------------------------------
found = [
    ("heat", "Talk: How cities keep cool in a heat wave", TUE, "18:30", "19:30", "Central Library, Auditorium",
     "Free", "Urban design talk, free, near the office on an office day."),
    ("film", "Indie film night + director Q&A", WED, "19:00", "21:00", "Riverside Cinema", "$12",
     "Film key word; fits Wednesday evening if you move reading."),
    ("quiz", "Science pub quiz", THU, "21:15", "22:45", "The Lab Bar", "Free",
     "Right after dinner with Sam, a short walk away."),
    ("jazz", "Late-night jazz trio", FRI, "21:00", "22:30", "The Blue Room", "$15",
     "Matches your key word jazz; starts after your bouldering block."),
    ("photo", "Photo walk along the waterfront", SAT, "15:30", "17:30", "Pier 3 entrance", "Free",
     "Saturday afternoon is open after the trip planning."),
]
db.upsert_discovered_events([
    {"id": f"demo_{k}", "site": "Example Events", "title": t, "start": at(d, s), "end": at(d, e),
     "timezone": TZ, "location": loc, "url": f"https://example.org/events/{k}", "cost": cost,
     "category": "talk", "summary": ""}
    for k, t, d, s, e, loc, cost, _ in found
], None, NOW)
db.create_optimizer_run("run_demo_scout", NOW, "api", "claude", at(MON, "00:00"), at(SUN, "23:59"), kind="scout")
opts = [{"item_type": "event", "item_id": f"demo_{k}", "title": t, "location": loc, "description": "",
         "start": at(d, s), "end": at(d, e), "all_day": False, "timezone": TZ, "rationale": why,
         "status": "proposed", "discovered_event_id": f"demo_{k}", "url": f"https://example.org/events/{k}",
         "cost": cost, "rank": n, "shown_at": NOW}
        for n, (k, t, d, s, e, loc, cost, why) in enumerate(found, start=1)]
db.insert_proposals("run_demo_scout", opts, NOW)
db.finish_optimizer_run("run_demo_scout", status="succeeded", finished_at=NOW, proposed_count=len(opts),
                        invalid_count=0, notes="")
print(f"seeded the week of {MONDAY}: {len(events)} events, {len(plan)} plan blocks, {len(opts)} event options")
