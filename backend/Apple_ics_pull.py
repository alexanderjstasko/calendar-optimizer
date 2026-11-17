"""
Apple_ics_pull.py

Full-fetch-and-diff sync for a published (public, read-only) Apple/iCloud
calendar ICS feed.

Like Partiful, Apple exposes no API for a shared public calendar — just an
ICS link (Calendar.app: Share Calendar -> Public Calendar -> Copy Link,
then swap webcal:// for https://). Unlike Partiful, Apple calendars
routinely contain RECURRING events (RRULE), which a naive VEVENT-by-VEVENT
read would only capture as a single row for the first occurrence.

This uses `recurring_ical_events` to expand all recurrence rules (respecting
EXDATE/RECURRENCE-ID overrides) into concrete occurrences within a rolling
date window, then diffs against the DB scoped to that same window.

Window scoping matters: an occurrence that has simply aged out of the
window (e.g. last week's recurring meeting) is NOT the same as an occurrence
that's been cancelled. Comparing against ALL active Apple events (not just
ones inside the window) would incorrectly soft-delete valid past events on
every sync. See db.get_active_source_event_ids_in_window().

Run on a schedule via APScheduler in main.py, same pattern as Partiful.
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

# .env lives at the project root, one level above backend/.
load_dotenv(Path(__file__).parent.parent / ".env")

SOURCE = "apple"
CALENDAR_ID = "apple_default"  # single published calendar assumption, same as Partiful
APPLE_ICS_URL = os.environ.get("APPLE_ICS_URL")

# Project-wide display convention -- used only as the fallback for floating
# (timezone-less) events. Per RFC 5545, floating time means "whatever zone
# the viewer considers local" -- for this project that's always
# America/New_York, not UTC. Events that DO carry a real TZID keep their
# original zone (str(dtstart.tzinfo)) rather than being converted, since
# Apple/iCloud feeds use clean IANA names and Schedule-X's ZonedDateTime
# handles arbitrary zones correctly -- this fallback only matters for the
# rare event with no timezone info at all.
DISPLAY_TIMEZONE = "America/New_York"
DISPLAY_ZONEINFO = ZoneInfo(DISPLAY_TIMEZONE)

# Rolling fetch/diff window. Adjust here if you want a wider or narrower
# lookback/lookahead — this is the only place window size is defined.
WINDOW_PAST_DAYS = 30
WINDOW_FUTURE_DAYS = 183  # ~6 months


def _isoformat(dt) -> str:
    """ISO 8601 with UTC offset. Floating (naive) datetimes have no fixed
    relationship to UTC per RFC 5545 -- treated as already being in the
    display zone (America/New_York), not UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DISPLAY_ZONEINFO)
    return dt.isoformat()


def normalize_occurrence(vevent, calendar_id: str) -> dict:
    """
    Map a single expanded occurrence (already resolved by
    recurring_ical_events -- DTSTART/DTEND are the real occurrence times,
    not the series' first occurrence) to the internal events schema.
    """
    uid = str(vevent.get("uid"))
    dtstart = vevent.get("dtstart").dt
    dtend = vevent.get("dtend").dt if vevent.get("dtend") else dtstart

    all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)

    if all_day:
        start = dtstart.isoformat()
        end = dtend.isoformat()
        tz_name = None
        # Distinct per-occurrence id even though all-day events don't carry
        # a time component -- the date itself is the disambiguator.
        occurrence_key = start
    else:
        start = _isoformat(dtstart)
        end = _isoformat(dtend)
        # Floating events (no tzinfo at all) fall back to the display zone,
        # matching _isoformat's fallback above -- label and offset must
        # always change together, or Temporal rejects the pair as invalid.
        tz_name = str(dtstart.tzinfo) if dtstart.tzinfo else DISPLAY_TIMEZONE
        occurrence_key = start

    # One VEVENT UID recurs across many rows here, so the occurrence's own
    # start time is folded into source_event_id to keep each instance unique.
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
    if not APPLE_ICS_URL:
        raise RuntimeError("APPLE_ICS_URL not set in environment/.env")

    resp = requests.get(APPLE_ICS_URL, timeout=15)
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
        sync_token=None,  # no incremental token for ICS feeds
        last_synced_at=now,
    )

    print(
        f"[apple] window {window_start} -> {window_end} | "
        f"synced {len(events)} occurrences, soft-deleted {len(removed_ids)}"
    )


if __name__ == "__main__":
    fetch_and_sync()