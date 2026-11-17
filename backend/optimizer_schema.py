"""
optimizer_schema.py — the JSON contract between the optimizer runner and Claude.

The runner sends a structured payload (planning window, preferences, the
existing calendar, tasks, goals) and asks for calendar events back. Claude's
reply is constrained to PROPOSED_EVENTS_SCHEMA via
llm_client.complete_structured(), so every item in `events` already carries
the fields db.upsert_event() needs. The runner validates each event, stores
it in the proposed_events staging table, and db.promote_proposal() copies
approved ones into `events` with source='optimizer'.

Five kinds of proposed event:
  task    a block of work for a tasks row (item_id = tasks.id)
  goal    one occurrence of a goals row (item_id = goals.id)
  travel  a travel block ending when the destination starts (item_id = the
          destination: an existing events.id, or the `ref` of another event
          in the same response)
  free    a protected free-time block (item_id = "") the optimizer reserves
          to meet the weekly free-time target
  event   a public event the scout recommends (item_id = discovered_events.id);
          only scout.py produces these, via RECOMMENDATION_SCHEMA

Datetime convention matches db.py: timed events carry ISO 8601 with a UTC
offset (the IANA zone goes in `timezone`); all-day events carry plain
YYYY-MM-DD in `start`/`end` with all_day = true.

Kept deliberately strict -- every property required, no additional
properties, no nullable types -- because that is the shape both the API's
strict tool mode and the CLI's --json-schema validator handle most reliably.
"""

from __future__ import annotations

DISPLAY_TIMEZONE = "America/New_York"

ITEM_TYPES = ("task", "goal", "travel", "free", "event")
OPTIMIZER_ITEM_TYPES = ("task", "goal", "travel", "free")

PROPOSED_EVENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "ref": {
            "type": "string",
            "description": "Short label unique within this response, e.g. p1, p2. Travel events use it to point at their destination.",
        },
        "item_type": {"type": "string", "enum": list(ITEM_TYPES)},
        "item_id": {
            "type": "string",
            "description": (
                "For task/goal: the tasks.id or goals.id copied verbatim from the input. "
                "For travel: the destination, either an existing calendar event id or the ref of a proposed event. "
                "For free: an empty string."
            ),
        },
        "title": {"type": "string"},
        "description": {"type": "string"},
        "location": {
            "type": "string",
            "description": "Where it happens, copied from the task/goal or the destination; empty string when none applies.",
        },
        "start": {
            "type": "string",
            "description": (
                "ISO 8601 with UTC offset, e.g. 2026-09-21T09:00:00-04:00, "
                "or YYYY-MM-DD when all_day is true."
            ),
        },
        "end": {"type": "string", "description": "Same format as start."},
        "all_day": {"type": "boolean"},
        "timezone": {"type": "string", "enum": [DISPLAY_TIMEZONE]},
        "rationale": {"type": "string", "description": "One sentence on why this slot was chosen."},
        "replaces_event_id": {
            "type": "string",
            "description": (
                "Only when re-evaluating one item (movable_blocks in the input): the calendar id of the approved "
                "block this event takes the place of. Empty string otherwise."
            ),
        },
    },
    "required": [
        "ref", "item_type", "item_id", "title", "description", "location",
        "start", "end", "all_day", "timezone", "rationale", "replaces_event_id",
    ],
    "additionalProperties": False,
}

UNSCHEDULED_ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "item_type": {"type": "string", "enum": ["task", "goal"]},
        "item_id": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["item_type", "item_id", "reason"],
    "additionalProperties": False,
}

PROPOSED_EVENTS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": PROPOSED_EVENT_SCHEMA},
        "unscheduled": {
            "type": "array",
            "description": "Tasks or goals that could not be (fully) placed in the planning window, with the reason.",
            "items": UNSCHEDULED_ITEM_SCHEMA,
        },
        "notes": {
            "type": "string",
            "description": "Short free-text summary of the plan, or an empty string.",
        },
    },
    "required": ["events", "unscheduled", "notes"],
    "additionalProperties": False,
}

# Name and description of the tool Claude calls to hand the plan back (api
# backend, tool mode). The cli backend uses the same schema via --json-schema.
SUBMIT_TOOL_NAME = "submit_schedule"
SUBMIT_TOOL_DESCRIPTION = (
    "Submit the proposed calendar events for the planning window. Call this exactly once "
    "with the complete plan: every task block, goal occurrence and travel block you want "
    "added, plus anything you could not place under `unscheduled`."
)

# The scout's output contract (scout.py): a ranked menu of public events. Options
# may overlap each other (alternatives); scout.validate_recommendations() only
# checks them against the fixed calendar.
RECOMMENDATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "description": "The options, ranked by appeal (best first), each candidate at most once.",
            "items": {
                "type": "object",
                "properties": {
                    "discovered_event_id": {"type": "string", "description": "The candidate's id, copied verbatim."},
                    "start": {"type": "string", "description": "ISO 8601 with UTC offset in America/New_York, e.g. 2026-10-02T18:00:00-04:00."},
                    "end": {"type": "string", "description": "Same format; the candidate's end, or start plus a sensible duration when unknown."},
                    "goal_id": {"type": "string", "description": "The goals.id this event serves, or an empty string (fine for a strong option)."},
                    "why": {"type": "string", "description": "One or two sentences on why this matches the person's interests and history."},
                    "schedule_fit": {"type": "string", "description": "The slot it fills, including travel from where the person will be."},
                    "confidence": {"type": "number", "description": "0 to 1."},
                },
                "required": ["discovered_event_id", "start", "end", "goal_id", "why", "schedule_fit", "confidence"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string", "description": "Two or three sentences on the menu as a whole, or what was thin this time."},
    },
    "required": ["recommendations", "notes"],
    "additionalProperties": False,
}

SCOUT_TOOL_NAME = "submit_recommendations"
SCOUT_TOOL_DESCRIPTION = (
    "Submit the menu of public events for the coming weeks. Call exactly once with the "
    "complete list of options, ranked by appeal (best first), not by date."
)

# Extraction contract for sites whose pages are plain text (scout.py).
EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "start_time": {"type": "string", "description": "HH:MM 24-hour local time, or empty when unknown"},
                    "end_time": {"type": "string", "description": "HH:MM or empty"},
                    "location": {"type": "string"},
                    "url": {"type": "string"},
                    "cost": {"type": "string"},
                    "category": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["title", "date", "start_time", "end_time", "location", "url", "cost", "category", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}
EXTRACT_TOOL_NAME = "submit_events"

