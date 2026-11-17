"""
llm_smoke.py — one real, token-spending round trip through llm_client using
the optimizer's events schema. Run it to prove a backend end to end after
`python3 llm_client.py --check` passes.

    python3 llm_smoke.py                 # backend from .env (LLM_BACKEND)
    python3 llm_smoke.py --backend cli   # override for this run only

Sends a small fixed scenario (three-day window, a few busy blocks, two tasks,
one goal) and prints the events Claude proposes plus token usage, cost and
wall time. Nothing is written to events.db. Exit code 1 if the reply breaks
any of the basic scheduling rules checked at the bottom.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import llm_client
from optimizer_schema import DISPLAY_TIMEZONE, PROPOSED_EVENTS_SCHEMA

SYSTEM = f"""You are the scheduling engine for a personal calendar.

You receive a planning window, the user's fixed commitments, and a list of
tasks and goals. Propose calendar events that fit the tasks and goals into
free time inside the window.

Rules:
- Only place events inside the planning window and outside the busy blocks.
- Keep events inside working hours (09:00 to 18:00) unless the item says
  otherwise; goals marked "Afterwork" go after 18:00.
- Respect each task's deadline and its time_to_complete_hr.
- A goal with frequency "2x Week" and days_of_week listed should be placed
  on up to two of those days in the window, each for required_time_hr.
- Copy item_id verbatim from the input. Use timezone "{DISPLAY_TIMEZONE}".
- Timed events use ISO 8601 with the UTC offset, e.g. 2026-09-21T09:00:00-04:00,
  and all_day false. Do not overlap events with each other.
- If something cannot be placed, list it under unscheduled with a reason.
"""

SCENARIO = {
    "planning_window": {"start": "2026-09-21", "end": "2026-09-23", "timezone": DISPLAY_TIMEZONE},
    "busy": [
        {"title": "Team meetings", "start": "2026-09-21T09:00:00-04:00", "end": "2026-09-21T12:00:00-04:00"},
        {"title": "Dentist", "start": "2026-09-22T13:00:00-04:00", "end": "2026-09-22T15:00:00-04:00"},
        {"title": "Dinner with friends", "start": "2026-09-23T18:00:00-04:00", "end": "2026-09-23T20:00:00-04:00"},
    ],
    "tasks": [
        {
            "id": "notion_task_car_registration",
            "task": "Renew car registration",
            "time_to_complete_hr": 1.0,
            "deadline": "2026-09-23",
            "priority": "High",
            "location": "Online",
        },
        {
            "id": "notion_task_birthday_card",
            "task": "Write birthday card for Sam",
            "time_to_complete_hr": 0.5,
            "deadline": "2026-09-25",
            "priority": "Low",
            "location": "Home",
        },
    ],
    "goals": [
        {
            "id": "notion_goal_rockclimb",
            "goal": "Rockclimb",
            "frequency": "2x Week",
            "required_time_hr": 2.0,
            "days_of_week": ["Monday", "Wednesday"],
            "time_of_day_window": "Afterwork",
            "priority": "Medium",
            "location": "Brooklyn Boulders",
        }
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=llm_client.VALID_BACKENDS, help="override LLM_BACKEND for this run")
    parser.add_argument("--effort", choices=llm_client.VALID_EFFORTS, help="override LLM_EFFORT for this run")
    args = parser.parse_args()
    if args.backend:
        os.environ["LLM_BACKEND"] = args.backend

    backend = llm_client.get_backend()
    print(f"backend={backend} model={llm_client._env('LLM_MODEL', llm_client.DEFAULT_MODEL)} "
          f"effort={args.effort or llm_client._env('LLM_EFFORT', llm_client.DEFAULT_EFFORT)}")
    print("sending scenario...", flush=True)

    started = time.monotonic()
    try:
        result = llm_client.complete_structured(
            SYSTEM, json.dumps(SCENARIO, indent=2), PROPOSED_EVENTS_SCHEMA, effort=args.effort
        )
    except llm_client.LLMError as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    elapsed = time.monotonic() - started

    print(json.dumps(result.data, indent=2))

    known_ids = {t["id"] for t in SCENARIO["tasks"]} | {g["id"] for g in SCENARIO["goals"]}
    problems: list[str] = []
    events = result.data["events"]
    for i, ev in enumerate(events):
        if ev["item_id"] not in known_ids:
            problems.append(f"events[{i}] item_id {ev['item_id']!r} not in input")
        if ev["timezone"] != DISPLAY_TIMEZONE:
            problems.append(f"events[{i}] timezone {ev['timezone']!r}")
        if ev["start"] >= ev["end"]:
            problems.append(f"events[{i}] start >= end")
        if not (SCENARIO["planning_window"]["start"] <= ev["start"][:10] <= SCENARIO["planning_window"]["end"]):
            problems.append(f"events[{i}] outside planning window: {ev['start']}")
    timed = sorted((ev["start"], ev["end"], i) for i, ev in enumerate(events) if not ev["all_day"])
    for (s1, e1, a), (s2, e2, b) in zip(timed, timed[1:]):
        if s2 < e1:
            problems.append(f"events[{a}] and events[{b}] overlap")

    print()
    print(f"events={len(events)} unscheduled={len(result.data['unscheduled'])} "
          f"backend={result.backend} model={result.model}")
    print(f"usage={result.usage} cost_usd={result.cost_usd} session_id={result.session_id} "
          f"elapsed={elapsed:.1f}s")
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("sanity checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
