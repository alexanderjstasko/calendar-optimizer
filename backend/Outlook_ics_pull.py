"""
Outlook_ics_pull.py

Full-fetch-and-diff sync for a published (public, read-only) Outlook/
Office365 calendar ICS feed. Same shape as Apple_ics_pull.py: no API, no
OAuth -- just a public .ics link (Outlook.com/OWA: Settings -> Calendar ->
Shared calendars -> Publish a calendar -> ICS link).

Structurally this is nearly identical to Apple's sync (RRULE expansion via
recurring_ical_events, windowed diff to avoid mistaking "out of window" for
"cancelled") -- see Apple_ics_pull.py's module docstring for that reasoning.

One real difference: Outlook/Exchange commonly labels timezones with
Windows-style names (e.g. "Eastern Standard Time") rather than IANA names
(e.g. "America/New_York"), inside an embedded VTIMEZONE block. `icalendar`
resolves the actual UTC offset correctly either way, but when the TZID
isn't a recognized IANA name it falls back to a wrapper object whose
str() is NOT a clean, Temporal-parseable zone identifier -- verified with
a synthetic Outlook-style feed before this was written. Since this
project's convention is everything displays in the display zone (TIMEZONE)
regardless of source, the timezone label is hardcoded rather than derived
from tzinfo -- the offset itself is already correct, only the label needs
fixing.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
from dotenv import load_dotenv
from icalendar import Calendar

import db
import display_zone

# .env lives at the project root, one level above backend/.
load_dotenv(Path(__file__).parent.parent / ".env")

SOURCE = "outlook"
CALENDAR_ID = "outlook_default"  # single published calendar assumption, same as Apple/Partiful
OUTLOOK_ICS_URL = os.environ.get("OUTLOOK_ICS_URL")

# Project-wide display convention -- see project notes. Outlook/Exchange
# feeds can legitimately contain events in OTHER zones (e.g. a Pacific-time
# meeting on a work calendar) -- these are actually converted into Eastern
# time below (astimezone), not just relabeled, so the stored offset and
# the stored label stay consistent. Relabeling without converting was the
# original bug here: a Pacific event kept its real -07:00 offset but got
# stamped "America/New_York", which Temporal correctly rejects as an
# inconsistent pair.
DISPLAY_TIMEZONE = display_zone.NAME
DISPLAY_ZONEINFO = ZoneInfo(DISPLAY_TIMEZONE)

# Same window as Apple -- kept in sync intentionally so all recurring-event
# sources behave identically. If you ever change one, change both.
WINDOW_PAST_DAYS = 30
WINDOW_FUTURE_DAYS = 183  # ~6 months


def _isoformat(dt) -> str:
    """ISO 8601 with UTC offset. Naive datetimes (floating time) treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def normalize_occurrence(vevent, calendar_id: str) -> dict:
    """
    Map a single expanded occurrence to the internal events schema.
    Mirrors Apple_ics_pull.py's normalize_occurrence, with DISPLAY_TIMEZONE
    used in place of str(dtstart.tzinfo) -- see module docstring.
    """
    uid = str(vevent.get("uid"))
    dtstart = vevent.get("dtstart").dt
    dtend = vevent.get("dtend").dt if vevent.get("dtend") else dtstart

    all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)

    if all_day:
        start = dtstart.isoformat()
        end = dtend.isoformat()
        tz_name = None
        occurrence_key = start
    else:
        # Convert (not just relabel) into the display zone. A Pacific-time
        # meeting's actual instant is preserved -- astimezone() re-expresses
        # the same moment in America/New_York's offset for that date, so the
        # stored offset and stored label are always mutually consistent.
        dtstart_display = dtstart.astimezone(DISPLAY_ZONEINFO)
        dtend_display = dtend.astimezone(DISPLAY_ZONEINFO)
        start = dtstart_display.isoformat()
        end = dtend_display.isoformat()
        tz_name = DISPLAY_TIMEZONE
        occurrence_key = start

    source_event_id = f"{uid}_{occurrence_key}"

    last_modified_prop = vevent.get("last-modified") or vevent.get("dtstamp")
    last_modified = (
        _isoformat(last_modified_prop.dt)
        if last_modified_prop
        else datetime.now(timezone.utc).isoformat()
    )

    attendees = vevent.get("attendee")
    if attendees is None:
        people = []
    else:
        if not isinstance(attendees, list):
            attendees = [attendees]
        people = [str(a).replace("mailto:", "") for a in attendees]

    return {
        "id": f"{SOURCE}_{source_event_id}",
        "calendar_id": calendar_id,
        "title": str(vevent.get("summary", "") or ""),
        "description": str(vevent.get("description", "") or ""),
        "location": str(vevent.get("location", "") or ""),
        "people": people,
        "start": start,
        "end": end,
        "all_day": all_day,
        "timezone": tz_name,
        "source": SOURCE,
        "source_event_id": source_event_id,
        "status": str(vevent.get("status", "confirmed") or "confirmed").lower(),
        "last_modified": last_modified,
        "deleted_at": None,
        "raw_json": {k: str(v) for k, v in vevent.items()},
    }


def fetch_and_sync() -> None:
    if not OUTLOOK_ICS_URL:
        raise RuntimeError("OUTLOOK_ICS_URL not set in environment/.env")

    resp = requests.get(OUTLOOK_ICS_URL, timeout=15)
    resp.raise_for_status()

    cal = Calendar.from_ical(resp.content)

    window_start = date.today() - timedelta(days=WINDOW_PAST_DAYS)
    window_end = date.today() + timedelta(days=WINDOW_FUTURE_DAYS)

    occurrences = recurring_ical_events.of(cal).between(window_start, window_end)
    events = [normalize_occurrence(occ, CALENDAR_ID) for occ in occurrences]

    current_ids = {e["source_event_id"] for e in events}
    active_ids_in_window = db.get_active_source_event_ids_in_window(
        SOURCE,
        CALENDAR_ID,
        window_start.isoformat(),
        window_end.isoformat(),
    )

    removed_ids = active_ids_in_window - current_ids
    now = datetime.now(timezone.utc).isoformat()
    with db.batch():  # one commit for all removals
        for source_event_id in removed_ids:
            db.soft_delete_event(SOURCE, source_event_id, now)

    if events:
        db.upsert_events(events)

    db.set_sync_token(
        calendar_id=CALENDAR_ID,
        source=SOURCE,
        sync_token=None,
        last_synced_at=now,
    )

    print(
        f"[outlook] window {window_start} -> {window_end} | "
        f"synced {len(events)} occurrences, soft-deleted {len(removed_ids)}"
    )


if __name__ == "__main__":
    fetch_and_sync()