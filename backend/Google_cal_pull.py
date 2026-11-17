"""
Google_cal_pull.py

Sync for Google's "primary" calendar via the Calendar API.

Uses Google's real incremental sync (nextSyncToken), not a full refetch
every run: the first sync scopes to upcoming events and Google hands back
a token; every sync after that asks "what changed since this token" and
gets back only the delta -- including cancelled events, which arrive as
minimal stubs with status='cancelled' rather than full event bodies. Those
get soft-deleted via db.soft_delete_event() instead of upserted, so
deleting/cancelling something on Google's calendar actually removes it
locally instead of leaving a stale row forever.

Credential handling is split in two:
  - get_credentials(interactive=False) -- used by fetch_and_sync(), safe
    to call from an unattended background scheduler. Silently refreshes an
    expired-but-refreshable token. RAISES instead of opening a browser if
    full re-authentication is required (the invalid_grant case) -- a
    background thread with no display has nowhere to send that browser
    interaction, so failing fast and loud beats hanging silently.
  - get_credentials(interactive=True) -- used only by main() (running this
    file directly). Falls back to the interactive OAuth consent flow if
    silent refresh isn't possible. This is how you recover from
    invalid_grant: just run `python3 Google_cal_pull.py` by hand.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import db

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

CALENDAR_ID = "primary"
SOURCE = "google"
INTERNAL_CALENDAR_ID = "google_primary"

# Anchored to this file's location, not cwd -- same reasoning as the .env
# loading in the ICS scripts: this needs to work whether the script is run
# directly, with a path prefix from the project root, or imported by
# main.py from wherever uvicorn happened to be launched.
TOKEN_PATH = Path(__file__).parent / "google_token.json"
CREDENTIALS_PATH = Path(__file__).parent / "google_credentials.json"


def normalize_event(event: dict) -> dict:
    """Maps a raw Google Calendar API event onto db.py's normalized schema."""
    start_info = event.get("start", {})
    end_info = event.get("end", {})
    all_day = "date" in start_info

    if all_day:
        start = start_info.get("date")
        end = end_info.get("date")
        timezone = None
    else:
        start = start_info.get("dateTime")
        end = end_info.get("dateTime")
        timezone = start_info.get("timeZone")

    people = [
        attendee.get("displayName") or attendee.get("email")
        for attendee in event.get("attendees", [])
        if attendee.get("displayName") or attendee.get("email")
    ]

    return {
        "source": SOURCE,
        "source_event_id": event["id"],
        "calendar_id": INTERNAL_CALENDAR_ID,
        "title": event.get("summary"),
        "description": event.get("description"),
        "location": event.get("location"),
        "people": people,
        "start": start,
        "end": end,
        "all_day": all_day,
        "timezone": timezone,
        "status": event.get("status", "confirmed"),
        "last_modified": event.get("updated"),
        "raw_json": event,
    }


def partition_events(all_events: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Split a batch of raw Google events into (normalized-active, cancelled-ids).
    Kept separate from fetch_and_sync() so the diff decision is unit-testable
    without a live API call.
    """
    active, cancelled_ids = [], []
    for event in all_events:
        if event.get("status") == "cancelled":
            cancelled_ids.append(event["id"])
        else:
            active.append(normalize_event(event))
    return active, cancelled_ids


def get_credentials(interactive: bool = False) -> Credentials:
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
        return creds

    if not interactive:
        raise RuntimeError(
            "Google credentials are missing or can't be silently refreshed "
            "(likely invalid_grant). Background sync will keep failing safely "
            "-- run `python3 Google_cal_pull.py` by hand to re-authenticate."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    return creds


def fetch_and_sync() -> None:
    """Non-interactive sync entry point -- safe to call from APScheduler."""
    creds = get_credentials(interactive=False)
    service = build("calendar", "v3", credentials=creds)

    sync_token = db.get_sync_token(INTERNAL_CALENDAR_ID)
    now = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()

    all_events = []
    page_token = None
    next_sync_token = None

    try:
        while True:
            request_kwargs = {
                "calendarId": CALENDAR_ID,
                "singleEvents": True,
                "pageToken": page_token,
            }
            if sync_token:
                request_kwargs["syncToken"] = sync_token
            else:
                # orderBy/timeMin are only allowed WITHOUT a syncToken --
                # this branch is first-sync or recovering from a 410.
                request_kwargs["timeMin"] = now
                request_kwargs["orderBy"] = "startTime"

            events_result = service.events().list(**request_kwargs).execute()
            all_events.extend(events_result.get("items", []))
            next_sync_token = events_result.get("nextSyncToken", next_sync_token)

            page_token = events_result.get("nextPageToken")
            if not page_token:
                break
    except HttpError as error:
        if error.resp.status == 410:
            db.set_sync_token(INTERNAL_CALENDAR_ID, SOURCE, None, now)
            print("[google] sync token expired (410 Gone) -- cleared; next run does a full resync.")
            return
        raise

    active, cancelled_ids = partition_events(all_events)

    if active:
        db.upsert_events(active)

    with db.batch():  # one commit for all removals
        for event_id in cancelled_ids:
            db.soft_delete_event(SOURCE, event_id, now)

    if next_sync_token:
        db.set_sync_token(INTERNAL_CALENDAR_ID, SOURCE, next_sync_token, now)

    print(f"[google] synced {len(active)} events, soft-deleted {len(cancelled_ids)}")


def main():
    """Manual/interactive entry point: first-time setup, or recovering
    from invalid_grant. Not called by the scheduler."""
    db.init_db()
    get_credentials(interactive=True)
    fetch_and_sync()


if __name__ == "__main__":
    main()