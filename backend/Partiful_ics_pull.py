"""
Partiful_ics_pull.py

Full-fetch-and-diff sync for Partiful's ICS calendar feed.

Partiful has no API — this hits the public .ics link, parses every VEVENT,
normalizes it into the same schema Google_cal_pull.py writes to, and diffs
against what's currently active in the DB to produce soft-deletes for
events that dropped out of the feed (cancelled/removed on Partiful's side).

Run on a schedule (every ~5 min) via APScheduler in main.py — see the
wiring snippet in the accompanying notes.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from icalendar import Calendar

import db
import display_zone

# Anchored to this file's location, not cwd. .env lives at the project
# root (one level above backend/), not inside backend/ itself — so this
# is `.parent.parent`, not `.parent`.
load_dotenv(Path(__file__).parent.parent / ".env")

SOURCE = "partiful"
CALENDAR_ID = "partiful_default"  # Partiful exposes one feed per account, no sub-calendars
PARTIFUL_ICS_URL = os.environ.get("PARTIFUL_ICS_URL")

# Project-wide display convention -- used only as the fallback for the rare
# floating (timezone-less) event. Per RFC 5545, floating time means
# "whatever zone the viewer considers local," which for this project is
# always America/New_York, not UTC.
DISPLAY_TIMEZONE = display_zone.NAME
DISPLAY_ZONEINFO = ZoneInfo(DISPLAY_TIMEZONE)


def _isoformat(dt) -> str:
    """ISO 8601 with UTC offset. Floating (naive) datetimes have no fixed
    relationship to UTC per RFC 5545 -- treated as already being in the
    display zone (TIMEZONE, default America/New_York), not UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DISPLAY_ZONEINFO)
    return dt.isoformat()


def normalize_event(vevent) -> dict:
    """Map a VEVENT component to the internal events schema."""
    uid = str(vevent.get("uid"))
    dtstart = vevent.get("dtstart").dt
    dtend = vevent.get("dtend").dt if vevent.get("dtend") else dtstart

    all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)

    if all_day:
        start = dtstart.isoformat()  # YYYY-MM-DD
        end = dtend.isoformat()
        tz_name = None
    else:
        start = _isoformat(dtstart)
        end = _isoformat(dtend)
        # Floating events fall back to the display zone, matching
        # _isoformat's fallback above -- label and offset must always
        # change together, or Temporal rejects the pair as invalid.
        tz_name = str(dtstart.tzinfo) if dtstart.tzinfo else DISPLAY_TIMEZONE

    # LAST-MODIFIED falls back to DTSTAMP if the feed doesn't set it
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
        "id": f"{SOURCE}_{uid}",
        "calendar_id": CALENDAR_ID,
        "title": str(vevent.get("summary", "") or ""),
        "description": str(vevent.get("description", "") or ""),
        "location": str(vevent.get("location", "") or ""),
        "people": people,
        "start": start,
        "end": end,
        "all_day": all_day,
        "timezone": tz_name,
        "source": SOURCE,
        "source_event_id": uid,
        "status": str(vevent.get("status", "confirmed") or "confirmed").lower(),
        "last_modified": last_modified,
        "deleted_at": None,
        "raw_json": {k: str(v) for k, v in vevent.items()},  # dict; db.py handles json.dumps
    }


def fetch_and_sync() -> None:
    if not PARTIFUL_ICS_URL:
        raise RuntimeError("PARTIFUL_ICS_URL not set in environment/.env")

    resp = requests.get(PARTIFUL_ICS_URL, timeout=15)
    resp.raise_for_status()

    cal = Calendar.from_ical(resp.content)
    events = [
        normalize_event(component)
        for component in cal.walk()
        if component.name == "VEVENT"
    ]

    current_ids = {e["source_event_id"] for e in events}
    active_ids = db.get_active_source_event_ids(SOURCE, CALENDAR_ID)

    removed_ids = active_ids - current_ids
    now = datetime.now(timezone.utc).isoformat()
    with db.batch():  # one commit for all removals
        for uid in removed_ids:
            db.soft_delete_event(SOURCE, uid, now)

    if events:
        db.upsert_events(events)

    db.set_sync_token(
        calendar_id=CALENDAR_ID,
        source=SOURCE,
        sync_token=None,  # no incremental token for ICS feeds
        last_synced_at=now,
    )

    print(
        f"[partiful] synced {len(events)} events, "
        f"soft-deleted {len(removed_ids)}"
    )


if __name__ == "__main__":
    fetch_and_sync()