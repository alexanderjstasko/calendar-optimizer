"""
db.py — SQLite persistence layer for the multi-source calendar aggregator.

Handles connection management, schema creation, upserts, deletes, and
queries for the unified `events` table plus per-source sync state.
"""

from __future__ import annotations

import sqlite3
import json
import datetime as _dt
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo
import re
import threading

DB_PATH = Path(__file__).parent / "events.db"

# Schedule-X uses event ids as DOM element ids / CSS selector targets
# (document.querySelector), which only safely support letters, digits,
# hyphens, and underscores. Source UIDs vary wildly across providers --
# email-like ("uid@domain.com"), Outlook's base64-ish "AAMkAG...==",
# ISO timestamps with colons (from our occurrence-key construction for
# recurring events) -- so every unsafe character is replaced with '-'
# here, once, rather than each adapter needing to know about this
# frontend-library constraint.
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_id(raw: str) -> str:
    return _UNSAFE_ID_CHARS.sub("-", raw)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,               -- Schedule-X `id` (generated as "{source}_{source_event_id}")
    calendar_id TEXT NOT NULL,         -- Schedule-X `calendarId`, e.g. 'google_primary'
    title TEXT,                        -- Schedule-X `title`
    description TEXT,                  -- Schedule-X `description`
    location TEXT,                     -- Schedule-X `location`
    people TEXT,                       -- Schedule-X `people` (string[]), stored as JSON text
    start TEXT NOT NULL,               -- 'YYYY-MM-DD' (all-day) or ISO datetime+offset (timed)
    end TEXT NOT NULL,                 -- same format as start
    all_day INTEGER DEFAULT 0,         -- 1 -> Temporal.PlainDate, 0 -> Temporal.ZonedDateTime
    timezone TEXT,                     -- IANA zone name, required when all_day = 0
    source TEXT NOT NULL,              -- 'google' | 'apple' | 'outlook' | 'partiful' (backend-only)
    source_event_id TEXT NOT NULL,     -- provider's raw event id (backend-only)
    status TEXT DEFAULT 'confirmed',   -- backend-only: confirmed | cancelled | tentative
    last_modified TEXT,                -- backend-only, ISO 8601
    deleted_at TEXT,                   -- backend-only, soft-delete marker
    raw_json TEXT,                     -- backend-only, original provider payload
    UNIQUE(source, source_event_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    calendar_id TEXT PRIMARY KEY,      -- matches events.calendar_id -- Google issues a sync token per calendar, not per source
    source TEXT NOT NULL,
    sync_token TEXT,
    last_synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
CREATE INDEX IF NOT EXISTS idx_events_calendar_id ON events(calendar_id);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,                -- generated as "{source}_{source_task_id}"
    task TEXT NOT NULL,
    time_to_complete_hr REAL,
    time_spent_hr REAL,                 -- Notion "Time Spent (hr)"; Notion owns it, time_tracker.py adds to it
    deadline TEXT,                      -- ISO 8601, as the source returns it
    priority TEXT,
    location TEXT,
    status TEXT,
    source TEXT NOT NULL,               -- 'notion' for now; room for other task sources later
    source_task_id TEXT NOT NULL,       -- provider's raw id (e.g. Notion page id)
    last_modified TEXT,
    deleted_at TEXT,                    -- soft-delete marker
    raw_json TEXT,                      -- original provider payload
    scheduled_event_ids TEXT,           -- JSON array of events.id -- written only by
                                         -- the future optimizer once it places this
                                         -- task on the calendar; the Notion sync never
                                         -- touches this column.
    UNIQUE(source, source_task_id)
);

CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,                -- generated as "{source}_{source_goal_id}"
    goal TEXT NOT NULL,
    frequency TEXT,                     -- stored raw ("2x Week", "1x Month") -- unit
                                         -- varies per goal, so this is parsed at read
                                         -- time (e.g. by a future parse_frequency()),
                                         -- not normalized to a bare integer here.
    end_date TEXT,
    required_time_hr REAL,
    priority TEXT,
    location TEXT,
    days_of_week TEXT,                  -- JSON array string, e.g. '["Monday","Wednesday"]'
    time_of_day_window TEXT,            -- raw value, e.g. "Morning" or "Afterwork"
    show_on_calendar INTEGER DEFAULT 1, -- 0 = context-only for the optimizer (Notion
                                         -- checkbox unchecked): never becomes events
    notes_for_llm TEXT,                 -- user's free-text guidance for the optimizer
    last_occurred TEXT,                 -- Notion "Last Occurred" (date); time_tracker.py moves it forward
    goal_type TEXT,                     -- Notion "Type": Routine | Flexible | Event Interest (routine.py)
    status TEXT,
    source TEXT NOT NULL,
    source_goal_id TEXT NOT NULL,
    last_modified TEXT,
    deleted_at TEXT,
    raw_json TEXT,
    scheduled_event_ids TEXT,           -- JSON array of events.id -- see tasks table note
    UNIQUE(source, source_goal_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);
CREATE INDEX IF NOT EXISTS idx_goals_end_date ON goals(end_date);

-- The Telos / Goal / Project rows of the one Notion database (2026-10-26):
-- the ancestry of every routine and task. Never scheduled themselves.
CREATE TABLE IF NOT EXISTS tree_nodes (
    id TEXT PRIMARY KEY,                -- "{source}_{source_node_id}", like tasks/goals
    title TEXT NOT NULL,
    level TEXT NOT NULL,                -- Telos | Goal | Project
    parent_id TEXT,                     -- another tree_nodes.id (NULL for a Telos)
    status TEXT,
    priority TEXT,
    location TEXT,
    notes_for_llm TEXT,
    target_date TEXT,                   -- "Target Date" (goals/projects)
    hours_per_week REAL,                -- optional: time the planner should give the goal/project itself
    time_spent_hr REAL,
    source TEXT NOT NULL,
    source_node_id TEXT NOT NULL,
    last_modified TEXT,
    deleted_at TEXT,
    raw_json TEXT,
    UNIQUE(source, source_node_id)
);

-- One row per optimizer run (see optimizer.py). Dry runs are not recorded.
CREATE TABLE IF NOT EXISTS optimizer_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',   -- running | succeeded | failed
    backend TEXT,
    model TEXT,
    horizon_start TEXT,
    horizon_end TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    proposed_count INTEGER,
    invalid_count INTEGER,
    notes TEXT,                               -- the model's free-text summary
    error TEXT,
    system_prompt TEXT,                       -- exactly what was sent, for audit/replay
    payload_json TEXT,                        -- the compact user message (structured input)
    kind TEXT DEFAULT 'optimizer'             -- optimizer | scout
);

-- Staging table: what the optimizer proposed. Nothing here is visible on the
-- calendar until promote_proposal() copies it into `events` (source='optimizer').
CREATE TABLE IF NOT EXISTS proposed_events (
    id TEXT PRIMARY KEY,                      -- "{run_id}_{n}"
    run_id TEXT NOT NULL,
    item_type TEXT NOT NULL,                  -- task | goal | travel
    item_id TEXT,                             -- tasks.id / goals.id; for travel, the destination event id
    title TEXT,
    description TEXT,
    location TEXT,
    start TEXT NOT NULL,                      -- same conventions as events.start/end
    end TEXT NOT NULL,
    all_day INTEGER DEFAULT 0,
    timezone TEXT,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | queued | approved | rejected | promoted | superseded | invalid | expired
    invalid_reason TEXT,
    created_at TEXT,
    decided_at TEXT,
    promoted_event_id TEXT,                   -- events.id once promoted
    settled_at TEXT,                          -- time_tracker: when this block's hours were written to Notion
    settled_hours REAL,                       -- time_tracker: hours added (0 when the event was cancelled)
    goal_id TEXT,                             -- event proposals: the goal they serve
    discovered_event_id TEXT,                 -- event proposals: discovered_events.id
    website_id TEXT,                          -- event proposals: websites.id
    url TEXT,                                 -- event proposals: source link
    cost TEXT,                                -- event proposals: price text
    original_start TEXT,                      -- the model's times, kept when the user rescheduled
    original_end TEXT,
    adjusted_at TEXT,
    ref TEXT,                                 -- the model's ref (travel item_id points at it)
    rank INTEGER,                             -- event options: the scout's order of appeal (1 = best)
    shown_at TEXT                             -- event options: first time it was on the page (NULL = unseen in the queue)
);

-- Small key/value store for bookkeeping (e.g. how many decisions the
-- preference learner has already digested).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposed_events_run ON proposed_events(run_id);
CREATE INDEX IF NOT EXISTS idx_proposed_events_status ON proposed_events(status);

-- Notion "Website List": sites the events scout reads. upcoming_event /
-- events_attended mirror the Notion columns that scout.py / time_tracker.py write.
CREATE TABLE IF NOT EXISTS websites (
    id TEXT PRIMARY KEY,                      -- "{source}_{source_website_id}"
    url TEXT NOT NULL,
    upcoming_event TEXT,
    events_attended REAL,
    source TEXT NOT NULL,
    source_website_id TEXT NOT NULL,
    last_modified TEXT,
    deleted_at TEXT,
    raw_json TEXT,
    last_fetched_at TEXT,                     -- scout bookkeeping
    last_fetch_method TEXT,                   -- api | server-data | json-ld | llm | unreadable | error
    last_fetch_count INTEGER,
    last_fetch_error TEXT,
    UNIQUE(source, source_website_id)
);

-- Public events the scout found (deduplicated by site + title + date).
CREATE TABLE IF NOT EXISTS discovered_events (
    id TEXT PRIMARY KEY,
    website_id TEXT,                          -- websites.id it came from
    site TEXT NOT NULL,
    title TEXT NOT NULL,
    start TEXT NOT NULL,                      -- same conventions as events.start
    end TEXT,
    all_day INTEGER DEFAULT 0,
    timezone TEXT,
    location TEXT,
    url TEXT,
    cost TEXT,
    category TEXT,
    summary TEXT,
    first_seen TEXT,
    last_seen TEXT,
    raw_json TEXT,
    recommended_at TEXT,                      -- last time the scout proposed it
    decision TEXT                             -- approved | rejected | attended (the preference signal)
);

CREATE INDEX IF NOT EXISTS idx_discovered_events_start ON discovered_events(start);

-- geo.py's cache: one lookup per venue string (key = geo.place_key()), so the
-- Events tab can show a neighbourhood and a map pin. A miss is cached too
-- (lat NULL) and retried after geo.RETRY_MISS_DAYS.
CREATE TABLE IF NOT EXISTS places (
    key TEXT PRIMARY KEY,
    query TEXT,
    lat REAL,
    lon REAL,
    neighborhood TEXT,
    borough TEXT,
    looked_up_at TEXT
);
"""


# Inside db.batch() every get_conn() of that thread shares one connection and
# one commit (see batch()).
_shared = threading.local()


@contextmanager
def get_conn():
    shared = getattr(_shared, "conn", None)
    if shared is not None:
        yield shared  # the enclosing batch() commits
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL;")
    # 15 s: the four calendar syncs, a scout/optimizer run and the API can all
    # want the write lock at once on the Pi's SD card (5 s was not enough).
    conn.execute("PRAGMA busy_timeout=15000;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def batch():
    """
    Run many single-row writes as one transaction on one connection: one
    commit (one fsync on the Pi's SD card) instead of one per row, and all or
    nothing if something fails. Nested calls join the outer batch.
    """
    if getattr(_shared, "conn", None) is not None:
        yield
        return
    with get_conn() as conn:
        _shared.conn = conn
        try:
            yield
        finally:
            _shared.conn = None


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS never
# alters an existing table, so init_db() adds these with ALTER TABLE when absent.
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "goals": {
        "scheduled_event_ids": "TEXT",
        "show_on_calendar": "INTEGER DEFAULT 1",
        "notes_for_llm": "TEXT",
        "last_occurred": "TEXT",
        "goal_type": "TEXT",
        "times_per_week": "REAL",       # Notion "Times/Week" (2026-10-26); frequency keeps the text form
        "sessions": "REAL",             # Notion "Sessions": blocks that happened, written by time_tracker
        "level": "TEXT",                # "Routine", or "Goal"/"Project" for a node planned as sessions
        "parent_id": "TEXT",            # tree_nodes.id (or another item's id) this row hangs under
        "time_to_complete_hr": "REAL",  # a Goal/Project planned as sessions: total hours (caps the sessions)
        "time_spent_hr": "REAL",        # ... and the hours its blocks have logged (time_tracker writes Time Spent)
    },
    "optimizer_runs": {
        "system_prompt": "TEXT",
        "payload_json": "TEXT",
        "kind": "TEXT DEFAULT 'optimizer'",  # optimizer | scout
    },
    "tasks": {
        "scheduled_event_ids": "TEXT",   # live DB predates this column (found 2026-09-19)
        "time_spent_hr": "REAL",
        "level": "TEXT",                 # "Task"
        "parent_id": "TEXT",             # tree_nodes.id (or another item's id) this row hangs under
    },
    "proposed_events": {
        "settled_at": "TEXT",
        "settled_hours": "REAL",
        "goal_id": "TEXT",              # event proposals: the goal they serve
        "discovered_event_id": "TEXT",  # event proposals: discovered_events.id
        "website_id": "TEXT",           # event proposals: websites.id
        "url": "TEXT",                  # event proposals: source link
        "cost": "TEXT",                 # event proposals: price text, e.g. Free or $15.00
        "original_start": "TEXT",       # set when the user rescheduled before approving
        "original_end": "TEXT",
        "adjusted_at": "TEXT",
        "ref": "TEXT",                  # the model's ref; travel item_id names its destination's ref
        "rank": "INTEGER",              # event options: scout order of appeal, 1 = best
        "auto_approved": "INTEGER DEFAULT 0",  # legacy: routine blocks promoted by code before 2026-10-26
        "shown_at": "TEXT",             # event options: first shown on the page; NULL = unseen in the queue
        "replaces_event_id": "TEXT",    # a move: the approved block (events.id) this proposal takes the place of
        "replaced_by": "TEXT",          # set on the old block's proposal once the move was approved
        "conflict_event_id": "TEXT",    # an approved block that a synced event now overlaps (triggers.py)
        "conflict_found_at": "TEXT",
        "move_attempted_at": "TEXT",
        "settle_targets": "TEXT",       # time_tracker: JSON {key: value about to be written to Notion}, so a retry never adds twice    # when the conflict check last asked for a move for this block (once per clash)
        "notified_at": "TEXT",          # trigger proposals: when the hourly change email listed it
    },
}


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> list[str]:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    added = []
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            added.append(name)
    return added


def init_db():
    """Creates the database file and tables if they don't already exist, and adds any missing columns."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for table, columns in _COLUMN_MIGRATIONS.items():
            _ensure_columns(conn, table, columns)


_UPSERT_EVENT_SQL = """
    INSERT INTO events (
        id, calendar_id, title, description, location, people,
        start, end, all_day, timezone,
        source, source_event_id, status, last_modified,
        deleted_at, raw_json
    ) VALUES (
        :id, :calendar_id, :title, :description, :location, :people,
        :start, :end, :all_day, :timezone,
        :source, :source_event_id, :status, :last_modified,
        :deleted_at, :raw_json
    )
    ON CONFLICT(source, source_event_id) DO UPDATE SET
        calendar_id = excluded.calendar_id,
        title = excluded.title,
        description = excluded.description,
        location = excluded.location,
        people = excluded.people,
        start = excluded.start,
        end = excluded.end,
        all_day = excluded.all_day,
        timezone = excluded.timezone,
        status = excluded.status,
        last_modified = excluded.last_modified,
        deleted_at = excluded.deleted_at,
        raw_json = excluded.raw_json
"""


def _upsert_event(conn: sqlite3.Connection, event: dict) -> None:
    """One event upsert on an open connection (the caller owns the transaction)."""
    event_id = event.get("id") or f"{event['source']}_{event['source_event_id']}"
    conn.execute(
        _UPSERT_EVENT_SQL,
        {
            "id": sanitize_id(event_id),
            "calendar_id": event["calendar_id"],
            "title": event.get("title"),
            "description": event.get("description"),
            "location": event.get("location"),
            "people": json.dumps(event.get("people", [])),
            "start": event["start"],
            "end": event["end"],
            "all_day": int(event.get("all_day", False)),
            "timezone": event.get("timezone"),
            "source": event["source"],
            "source_event_id": event["source_event_id"],
            "status": event.get("status", "confirmed"),
            "last_modified": event.get("last_modified"),
            "deleted_at": event.get("deleted_at"),
            "raw_json": json.dumps(event.get("raw_json", {})),
        },
    )


def upsert_event(event: dict):
    """
    Insert a new event, or update it if (source, source_event_id) already
    exists. `event` should be a dict with:
        source, source_event_id, calendar_id, title, description, location,
        people (list[str]), start, end, all_day, timezone,
        status, last_modified, raw_json (original provider payload, any dict)

    `id` is auto-generated as "{source}_{source_event_id}" if not supplied,
    which keeps it globally unique across all four sources.
    """
    with get_conn() as conn:
        _upsert_event(conn, event)


def upsert_events(events: list[dict]):
    """
    Upsert a batch in one connection and one transaction: one commit (one
    fsync on the Pi's SD card) instead of one per row, so a 127-event
    Partiful sync holds the write lock once, briefly. All or nothing.
    """
    if not events:
        return
    with get_conn() as conn:
        for event in events:
            _upsert_event(conn, event)


def soft_delete_event(source: str, source_event_id: str, deleted_at: str):
    """Marks an event as deleted without removing the row (for audit/history)."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE events SET status = 'cancelled', deleted_at = ?
            WHERE source = ? AND source_event_id = ?
            """,
            (deleted_at, source, source_event_id),
        )


def get_events(calendar_id: str = None, source: str = None, include_deleted: bool = False) -> list[dict]:
    """
    Returns raw DB rows (backend shape -- includes source, raw_json, etc).
    Use get_schedule_x_events() instead when serving the frontend.
    """
    query = "SELECT * FROM events WHERE 1=1"
    params = []
    if calendar_id:
        query += " AND calendar_id = ?"
        params.append(calendar_id)
    if source:
        query += " AND source = ?"
        params.append(source)
    if not include_deleted:
        query += " AND deleted_at IS NULL"
    query += " ORDER BY start ASC"

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def _zoned(value: str, zone: str | None) -> str:
    """
    'ISO[Zone]' with the offset that zone really has at that instant. Temporal
    rejects a bracketed zone whose offset disagrees (an event written in one
    zone and labelled with another, a missing label), and one such event
    would stop the whole calendar from loading.
    """
    try:
        tz = ZoneInfo(zone) if zone else ZoneInfo(DISPLAY_TZ)
    except Exception:  # noqa: BLE001 -- an unknown zone name: show it in the display zone
        tz, zone = ZoneInfo(DISPLAY_TZ), None
    try:
        moment = _dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return f"{value}[{zone or DISPLAY_TZ}]"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)  # floating time: wall clock in that zone
    return f"{moment.astimezone(tz).isoformat()}[{zone or DISPLAY_TZ}]"


def to_schedule_x_event(row: dict) -> dict:
    """
    Converts one raw DB row into the exact shape Schedule-X's `events` array
    expects (https://schedule-x.dev/docs/calendar/events), with start/end
    pre-formatted as Temporal-parseable strings. Drops backend-only fields
    (source, source_event_id, status, raw_json, etc).

    All-day  -> 'YYYY-MM-DD'                                 (Temporal.PlainDate.from)
    Timed    -> '2023-11-04T10:05:00+01:00[Europe/Berlin]'   (Temporal.ZonedDateTime.from)

    All-day ends: every source stores the iCalendar/Google convention, an
    *exclusive* end (a one-day event on Oct 6 ends "2026-10-07"), but
    Schedule-X treats the end date as the last day shown. Converted here, at
    display time only, so a one-day event no longer spills into the next day.
    """
    if row["all_day"]:
        start, end = row["start"], row["end"]
        if end and end > start:
            end = (_dt.date.fromisoformat(end[:10]) - _dt.timedelta(days=1)).isoformat()
    else:
        start, end = _zoned(row["start"], row["timezone"]), _zoned(row["end"], row["timezone"])

    return {
        "id": row["id"],
        "calendarId": row["calendar_id"],
        "title": row["title"],
        "description": row["description"],
        "location": row["location"],
        "people": json.loads(row["people"]) if row["people"] else [],
        "start": start,
        "end": end,
    }


def get_schedule_x_events(calendar_id: str = None) -> list[dict]:
    """
    Returns events pre-shaped for Schedule-X, excluding soft-deleted and
    cancelled events. This is what your FastAPI /events endpoint should
    json.dumps() and return directly to the frontend. An approved block that
    a synced event now overlaps (triggers.py) carries `conflict` {event_id,
    title} and the CSS class `has-conflict`, so the calendar can mark it.
    """
    # Only what the calendar draws: raw_json (whole provider payloads) stays in the database.
    query = ("SELECT id, calendar_id, title, description, location, people, start, end, all_day, timezone "
             "FROM events WHERE deleted_at IS NULL AND status != 'cancelled'")
    params: list = []
    if calendar_id:
        query += " AND calendar_id = ?"
        params.append(calendar_id)
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(query + " ORDER BY start ASC", params).fetchall()]
    conflicts = get_conflicts()
    out = []
    for row in rows:
        shaped = to_schedule_x_event(row)
        clash = conflicts.get(row["id"])
        if clash:
            shaped["conflict"] = clash
            shaped["_options"] = {"additionalClasses": ["has-conflict"]}
        out.append(shaped)
    return out


def get_conflicts() -> dict[str, dict]:
    """{approved block's events.id: {event_id, title, found_at}} for every flagged conflict still on the calendar."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.promoted_event_id AS block_id, p.conflict_event_id, p.conflict_found_at, e.title
            FROM proposed_events p LEFT JOIN events e ON e.id = p.conflict_event_id
            WHERE p.status = 'promoted' AND p.conflict_event_id IS NOT NULL AND p.promoted_event_id IS NOT NULL
            """
        ).fetchall()
    return {r["block_id"]: {"event_id": r["conflict_event_id"], "title": r["title"] or "", "found_at": r["conflict_found_at"]}
            for r in rows}


def get_sync_token(calendar_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sync_token FROM sync_state WHERE calendar_id = ?", (calendar_id,)
        ).fetchone()
        return row["sync_token"] if row else None


def set_sync_token(calendar_id: str, source: str, sync_token: str, last_synced_at: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sync_state (calendar_id, source, sync_token, last_synced_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(calendar_id) DO UPDATE SET
                sync_token = excluded.sync_token,
                last_synced_at = excluded.last_synced_at
            """,
            (calendar_id, source, sync_token, last_synced_at),
        )

# addition necessary for partiful integration

def get_active_source_event_ids(source: str, calendar_id: str) -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source_event_id FROM events WHERE source = ? AND calendar_id = ? AND deleted_at IS NULL",
            (source, calendar_id),
        ).fetchall()
    return {row["source_event_id"] for row in rows}


def get_active_source_event_ids_in_window(
    source: str, calendar_id: str, window_start: str, window_end: str
) -> set:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT source_event_id FROM events
            WHERE source = ? AND calendar_id = ? AND deleted_at IS NULL
              AND start >= ? AND start < ?
            """,
            (source, calendar_id, window_start, window_end),
        ).fetchall()
    return {row["source_event_id"] for row in rows}


# --- tasks & goals (Notion-sourced, non-recurring, no windowed diffing) ---

def _parent_id(row: dict) -> str | None:
    """A normalized row's parent as our id: `parent_id` as given, else built from `parent_source_id`."""
    if row.get("parent_id"):
        return row["parent_id"]
    if row.get("parent_source_id"):
        return sanitize_id(f"{row['source']}_{row['parent_source_id']}")
    return None


def upsert_tree_node(node: dict) -> None:
    """Insert or update one Telos / Goal / Project row (see notion_adapter.normalize_node)."""
    node_id = sanitize_id(f"{node['source']}_{node['source_node_id']}")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO tree_nodes (
                id, title, level, parent_id, status, priority, location, notes_for_llm,
                target_date, hours_per_week, time_spent_hr, source, source_node_id,
                last_modified, deleted_at, raw_json
            ) VALUES (
                :id, :title, :level, :parent_id, :status, :priority, :location, :notes_for_llm,
                :target_date, :hours_per_week, :time_spent_hr, :source, :source_node_id,
                :last_modified, :deleted_at, :raw_json
            )
            ON CONFLICT(source, source_node_id) DO UPDATE SET
                title = excluded.title, level = excluded.level, parent_id = excluded.parent_id,
                status = excluded.status, priority = excluded.priority, location = excluded.location,
                notes_for_llm = excluded.notes_for_llm, target_date = excluded.target_date,
                hours_per_week = excluded.hours_per_week, time_spent_hr = excluded.time_spent_hr,
                last_modified = excluded.last_modified, deleted_at = excluded.deleted_at,
                raw_json = excluded.raw_json
            """,
            {
                "id": node_id, "title": node.get("title"), "level": node.get("level"), "parent_id": _parent_id(node),
                "status": node.get("status"), "priority": node.get("priority"), "location": node.get("location"),
                "notes_for_llm": node.get("notes_for_llm"), "target_date": node.get("target_date"),
                "hours_per_week": node.get("hours_per_week"), "time_spent_hr": node.get("time_spent_hr"),
                "source": node["source"], "source_node_id": node["source_node_id"],
                "last_modified": node.get("last_modified"), "deleted_at": node.get("deleted_at"),
                "raw_json": json.dumps(node.get("raw_json", {})),
            },
        )


def upsert_tree_nodes(nodes: list[dict]) -> None:
    with batch():
        for node in nodes:
            upsert_tree_node(node)


def get_active_source_node_ids(source: str) -> set[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT source_node_id FROM tree_nodes WHERE source = ? AND deleted_at IS NULL", (source,)).fetchall()
        return {row["source_node_id"] for row in rows}


def soft_delete_tree_node(source: str, source_node_id: str, deleted_at: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE tree_nodes SET deleted_at = ? WHERE source = ? AND source_node_id = ? AND deleted_at IS NULL",
                     (deleted_at, source, source_node_id))


def get_tree_nodes(include_deleted: bool = False) -> list[dict]:
    query = "SELECT * FROM tree_nodes" + ("" if include_deleted else " WHERE deleted_at IS NULL") + " ORDER BY level, title"
    with get_conn() as conn:
        return [dict(row) for row in conn.execute(query).fetchall()]


def upsert_task(task: dict):
    """
    Insert a new task, or update it if (source, source_task_id) already
    exists. `task` should be a dict with:
        source, source_task_id, task, time_to_complete_hr, time_spent_hr (optional),
        deadline, priority, location, status, last_modified, raw_json (optional)

    `id` is auto-generated as "{source}_{source_task_id}", matching the
    events table's id convention.
    """
    task_id = sanitize_id(f"{task['source']}_{task['source_task_id']}")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, task, time_to_complete_hr, time_spent_hr, deadline, priority,
                location, status, level, parent_id, source, source_task_id, last_modified,
                deleted_at, raw_json
            ) VALUES (
                :id, :task, :time_to_complete_hr, :time_spent_hr, :deadline, :priority,
                :location, :status, :level, :parent_id, :source, :source_task_id, :last_modified,
                :deleted_at, :raw_json
            )
            ON CONFLICT(source, source_task_id) DO UPDATE SET
                task = excluded.task,
                time_to_complete_hr = excluded.time_to_complete_hr,
                time_spent_hr = excluded.time_spent_hr,
                deadline = excluded.deadline,
                priority = excluded.priority,
                location = excluded.location,
                status = excluded.status,
                level = excluded.level,
                parent_id = excluded.parent_id,
                last_modified = excluded.last_modified,
                deleted_at = excluded.deleted_at,
                raw_json = excluded.raw_json
            """,
            {
                "id": task_id,
                "level": task.get("level"),
                "parent_id": _parent_id(task),
                "task": task.get("task"),
                "time_to_complete_hr": task.get("time_to_complete_hr"),
                "time_spent_hr": task.get("time_spent_hr"),
                "deadline": task.get("deadline"),
                "priority": task.get("priority"),
                "location": task.get("location"),
                "status": task.get("status"),
                "source": task["source"],
                "source_task_id": task["source_task_id"],
                "last_modified": task.get("last_modified"),
                "deleted_at": task.get("deleted_at"),
                "raw_json": json.dumps(task.get("raw_json", {})),
            },
        )


def upsert_tasks(tasks: list[dict]):
    """Upsert a batch of tasks in one transaction."""
    with batch():
        for task in tasks:
            upsert_task(task)


def upsert_goal(goal: dict):
    """
    Insert a new goal, or update it if (source, source_goal_id) already
    exists. `goal` should be a dict with:
        source, source_goal_id, goal, frequency, end_date,
        required_time_hr, priority, location, days_of_week (JSON string),
        time_of_day_window, show_on_calendar (0/1, optional, default 1),
        notes_for_llm (optional), last_occurred (optional, 'YYYY-MM-DD'),
        status, last_modified, raw_json (optional)
    """
    goal_id = sanitize_id(f"{goal['source']}_{goal['source_goal_id']}")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO goals (
                id, goal, frequency, end_date, required_time_hr, priority,
                location, days_of_week, time_of_day_window, show_on_calendar,
                notes_for_llm, last_occurred, goal_type, status,
                times_per_week, sessions, level, parent_id,
                time_to_complete_hr, time_spent_hr,
                source, source_goal_id, last_modified, deleted_at, raw_json
            ) VALUES (
                :id, :goal, :frequency, :end_date, :required_time_hr, :priority,
                :location, :days_of_week, :time_of_day_window, :show_on_calendar,
                :notes_for_llm, :last_occurred, :goal_type, :status,
                :times_per_week, :sessions, :level, :parent_id,
                :time_to_complete_hr, :time_spent_hr,
                :source, :source_goal_id, :last_modified, :deleted_at, :raw_json
            )
            ON CONFLICT(source, source_goal_id) DO UPDATE SET
                goal = excluded.goal,
                frequency = excluded.frequency,
                end_date = excluded.end_date,
                required_time_hr = excluded.required_time_hr,
                priority = excluded.priority,
                location = excluded.location,
                days_of_week = excluded.days_of_week,
                time_of_day_window = excluded.time_of_day_window,
                show_on_calendar = excluded.show_on_calendar,
                notes_for_llm = excluded.notes_for_llm,
                last_occurred = excluded.last_occurred,
                goal_type = excluded.goal_type,
                status = excluded.status,
                times_per_week = excluded.times_per_week,
                sessions = excluded.sessions,
                level = excluded.level,
                parent_id = excluded.parent_id,
                time_to_complete_hr = excluded.time_to_complete_hr,
                time_spent_hr = excluded.time_spent_hr,
                last_modified = excluded.last_modified,
                deleted_at = excluded.deleted_at,
                raw_json = excluded.raw_json
            """,
            {
                "id": goal_id,
                "times_per_week": goal.get("times_per_week"),
                "sessions": goal.get("sessions"),
                "level": goal.get("level"),
                "parent_id": _parent_id(goal),
                "time_to_complete_hr": goal.get("time_to_complete_hr"),
                "time_spent_hr": goal.get("time_spent_hr"),
                "goal": goal.get("goal"),
                "frequency": goal.get("frequency"),
                "end_date": goal.get("end_date"),
                "required_time_hr": goal.get("required_time_hr"),
                "priority": goal.get("priority"),
                "location": goal.get("location"),
                "days_of_week": goal.get("days_of_week"),
                "time_of_day_window": goal.get("time_of_day_window"),
                "show_on_calendar": int(bool(goal.get("show_on_calendar", True))),
                "notes_for_llm": goal.get("notes_for_llm"),
                "last_occurred": goal.get("last_occurred"),
                "goal_type": goal.get("goal_type"),
                "status": goal.get("status"),
                "source": goal["source"],
                "source_goal_id": goal["source_goal_id"],
                "last_modified": goal.get("last_modified"),
                "deleted_at": goal.get("deleted_at"),
                "raw_json": json.dumps(goal.get("raw_json", {})),
            },
        )


def upsert_goals(goals: list[dict]):
    """Upsert a batch of goals in one transaction."""
    with batch():
        for goal in goals:
            upsert_goal(goal)


def get_active_source_task_ids(source: str) -> set[str]:
    """
    For full-fetch-and-diff syncing (no windowing needed -- tasks aren't
    recurring occurrences): compare this against the ids in a fresh fetch
    to find which existing rows are now gone and should be soft-deleted.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source_task_id FROM tasks WHERE source = ? AND deleted_at IS NULL",
            (source,),
        ).fetchall()
    return {row["source_task_id"] for row in rows}


def get_active_source_goal_ids(source: str) -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source_goal_id FROM goals WHERE source = ? AND deleted_at IS NULL",
            (source,),
        ).fetchall()
    return {row["source_goal_id"] for row in rows}


def soft_delete_task(source: str, source_task_id: str, deleted_at: str):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE tasks SET status = 'deleted', deleted_at = ?
            WHERE source = ? AND source_task_id = ?
            """,
            (deleted_at, source, source_task_id),
        )


def soft_delete_goal(source: str, source_goal_id: str, deleted_at: str):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE goals SET status = 'deleted', deleted_at = ?
            WHERE source = ? AND source_goal_id = ?
            """,
            (deleted_at, source, source_goal_id),
        )


def get_tasks(include_deleted: bool = False) -> list[dict]:
    query = "SELECT * FROM tasks WHERE 1=1"
    if not include_deleted:
        query += " AND deleted_at IS NULL"
    query += " ORDER BY deadline ASC"
    with get_conn() as conn:
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]


def get_goals(include_deleted: bool = False) -> list[dict]:
    query = "SELECT * FROM goals WHERE 1=1"
    if not include_deleted:
        query += " AND deleted_at IS NULL"
    query += " ORDER BY end_date ASC"
    with get_conn() as conn:
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]


# --- optimizer: calendar window, schedulable items, runs, proposals ----------

DISPLAY_TZ = "America/New_York"
OPTIMIZER_SOURCE = "optimizer"
OPTIMIZER_CALENDAR_ID = "optimizer"
# `queued` = a scout option ranked below the shown page, waiting for POST
# /scout/reload. Like superseded/expired it is not a decision: never emailed,
# never approved by approve-all, ignored by the preference learner.
# `retired` = was approved (a decision the learner keeps) but taken off the
# calendar wholesale by reset_plan.py, not by the person one by one.
PROPOSAL_STATUSES = ("proposed", "queued", "approved", "rejected", "promoted", "superseded", "invalid", "expired", "retired")
APPROVED_STATUSES = ("promoted", "retired")
UNDECIDED_STATUSES = ("proposed", "queued")
# How the approvals page and the email split proposals: public events the scout
# suggested vs. everything the optimizer planned (goals, tasks, travel, free time).
PROPOSAL_GROUPS = {
    "events": {"title": "Suggested events", "item_types": ("event",)},
    "plan": {"title": "Your plan", "item_types": ("goal", "task", "travel", "free")},
}


def proposal_group(item_type: str) -> str:
    return "events" if item_type in PROPOSAL_GROUPS["events"]["item_types"] else "plan"
# Notion status values (lower-cased) that take a task/goal out of scheduling.
_DONE_STATUSES = {"done", "complete", "completed", "cancelled", "canceled", "archived", "deleted", "dropped"}
# A goal/project/telos on hold pauses everything beneath it in the planner (Notion is untouched).
_PAUSED_STATUSES = {"on hold", "on-hold", "paused"}
PRIORITY_ORDER = ("critical", "high", "medium", "low")


def _is_done(status: str | None) -> bool:
    return (status or "").strip().lower() in _DONE_STATUSES


def _is_paused(status: str | None) -> bool:
    return (status or "").strip().lower() in _PAUSED_STATUSES


def ancestry(parent_id: str | None, nodes_by_id: dict[str, dict]) -> list[dict]:
    """The chain of tree nodes above an item, Telos first. A missing parent ends the chain; cycles stop."""
    chain: list[dict] = []
    seen: set[str] = set()
    current = parent_id
    while current and current in nodes_by_id and current not in seen:
        seen.add(current)
        node = nodes_by_id[current]
        chain.append(node)
        current = node.get("parent_id")
    chain.reverse()
    return chain


def effective_priority(own: str | None, chain: list[dict]) -> str | None:
    """A row's own priority wins; otherwise the nearest ancestor's."""
    if (own or "").strip():
        return own
    for node in reversed(chain):
        if (node.get("priority") or "").strip():
            return node["priority"]
    return None


def _with_tree(item: dict, nodes_by_id: dict[str, dict]) -> dict | None:
    """Attach ancestry (path, telos, effective priority) to a task/goal; None when a node above it ends or pauses it."""
    chain = ancestry(item.get("parent_id"), nodes_by_id)
    if any(_is_done(n.get("status")) or _is_paused(n.get("status")) for n in chain):
        return None
    telos = next((n["title"] for n in chain if (n.get("level") or "") == "Telos"), None)
    return {
        **item,
        "own_priority": item.get("priority"),
        "priority": effective_priority(item.get("priority"), chain),
        "path": [n["title"] for n in chain],
        "telos": telos,
        "parent_title": chain[-1]["title"] if chain else None,
        "ancestor_notes": [n["notes_for_llm"] for n in chain if (n.get("notes_for_llm") or "").strip()],
    }


def parse_event_time(value: str, all_day: int | bool, tz: ZoneInfo | None = None) -> _dt.datetime:
    """
    Turn a stored start/end string into an aware datetime in the display zone.
    All-day values are 'YYYY-MM-DD' (midnight in the display zone); timed values
    are ISO 8601 with whatever offset the source used, normalized here.
    """
    tz = tz or ZoneInfo(DISPLAY_TZ)
    if all_day or "T" not in value:
        return _dt.datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=tz)
    parsed = _dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:  # floating time: treated as display zone (RFC 5545)
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def get_events_between(window_start: _dt.datetime, window_end: _dt.datetime) -> list[dict]:
    """
    Active, non-cancelled events overlapping [window_start, window_end): every
    row with start < window_end and end > window_start. Stored offsets vary by
    source, so SQL only prefilters by date prefix and Python does the exact
    comparison. Each returned row gains aware `start_dt` / `end_dt` fields in
    the display zone.
    """
    tz = ZoneInfo(DISPLAY_TZ)
    prefix = (window_start - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    # Date prefixes with a day of slack each side (stored offsets vary by source).
    upper = (window_end + _dt.timedelta(days=2)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE deleted_at IS NULL AND status != 'cancelled' AND end >= ? AND start < ?
            ORDER BY start ASC
            """,
            (prefix, upper),
        ).fetchall()

    selected: list[dict] = []
    for row in rows:
        event = dict(row)
        try:
            event["start_dt"] = parse_event_time(event["start"], event["all_day"], tz)
            event["end_dt"] = parse_event_time(event["end"], event["all_day"], tz)
        except ValueError:
            continue
        if event["all_day"] and event["end_dt"] <= event["start_dt"]:
            event["end_dt"] = event["start_dt"] + _dt.timedelta(days=1)
        if event["start_dt"] < window_end and event["end_dt"] > window_start:
            selected.append(event)
    return selected


def get_schedulable_items() -> list[dict]:
    """
    Active tasks and goals in one list, each with a `type` field ('task' or
    'goal'), excluding rows whose Notion status marks them finished. This is
    the optimizer's input; the calendar events are fetched separately.
    """
    nodes_by_id = {n["id"]: n for n in get_tree_nodes()}
    items: list[dict] = []
    for task in get_tasks():
        if not _is_done(task.get("status")) and not _is_paused(task.get("status")):
            row = _with_tree({"type": "task", **task}, nodes_by_id)
            if row:
                items.append(row)
    for goal in get_goals():
        if not _is_done(goal.get("status")) and not _is_paused(goal.get("status")):
            if _exhausted(goal):
                continue  # a Goal/Project planned as sessions whose Time to Complete is all logged
            row = _with_tree({"type": "goal", **goal}, nodes_by_id)
            if row:
                items.append(row)
    return items


def _exhausted(goal: dict) -> bool:
    total = goal.get("time_to_complete_hr")
    if total is None:
        return False
    try:
        return float(total) - float(goal.get("time_spent_hr") or 0.0) <= 1e-9
    except (TypeError, ValueError):
        return False


def get_tree() -> list[dict]:
    """
    The Telos tree for the planner: every active node with its children nested
    (`children`), each routine/task that hangs under a node listed by title and
    kind. Orphans (no parent, or a parent that is not a node) are collected
    under a synthetic "(no telos)" entry so nothing is invisible.
    """
    nodes = get_tree_nodes()
    by_id = {n["id"]: dict(n, children=[], items=[]) for n in nodes}
    roots: list[dict] = []
    for node in by_id.values():
        parent = by_id.get(node.get("parent_id"))
        (parent["children"] if parent else roots).append(node)
    orphan_items: list[dict] = []
    for item in get_schedulable_items():
        entry = {"id": item["id"], "kind": item["type"], "title": item.get("task") or item.get("goal"),
                 "status": item.get("status"), "priority": item.get("priority")}
        if item["type"] == "goal":
            entry["goal_type"] = item.get("goal_type")
            entry["frequency"] = item.get("frequency")
            entry["required_time_hr"] = item.get("required_time_hr")
            entry["time_to_complete_hr"] = item.get("time_to_complete_hr")
            entry["time_spent_hr"] = item.get("time_spent_hr")
        else:
            entry["deadline"] = item.get("deadline")
            entry["time_to_complete_hr"] = item.get("time_to_complete_hr")
            entry["time_spent_hr"] = item.get("time_spent_hr")
        own = by_id.get(item["id"])
        if own is not None:
            # A Goal/Project planned as sessions or as one piece of work: the node's own plan,
            # not a child of it (notion_adapter.node_plan_kind).
            own["planned"] = entry
            continue
        parent = by_id.get(item.get("parent_id"))
        (parent["items"] if parent else orphan_items).append(entry)
    if orphan_items:
        roots.append({"id": "", "title": "(no telos)", "level": "Telos", "status": None, "priority": None,
                      "notes_for_llm": None, "target_date": None, "hours_per_week": None, "children": [], "items": orphan_items})
    return roots


def mark_item_scheduled(item_type: str, item_id: str, event_id: str) -> None:
    """Record `event_id` on the task/goal row keyed by its own id."""
    with get_conn() as conn:
        _mark_item_scheduled(conn, item_type, item_id, event_id)


def _mark_item_scheduled(conn: sqlite3.Connection, item_type: str, item_id: str, event_id: str) -> None:
    table = {"task": "tasks", "goal": "goals"}[item_type]
    row = conn.execute(f"SELECT scheduled_event_ids FROM {table} WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return
    existing = json.loads(row["scheduled_event_ids"]) if row["scheduled_event_ids"] else []
    if event_id not in existing:
        existing.append(event_id)
        conn.execute(f"UPDATE {table} SET scheduled_event_ids = ? WHERE id = ?", (json.dumps(existing), item_id))


def create_optimizer_run(
    run_id: str, started_at: str, backend: str, model: str, horizon_start: str, horizon_end: str,
    *, system_prompt: str | None = None, payload_json: str | None = None, kind: str = "optimizer",
) -> None:
    """Record a run before the model call; system_prompt/payload_json keep the exact request."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO optimizer_runs (
                run_id, started_at, status, backend, model, horizon_start, horizon_end,
                system_prompt, payload_json, kind
            ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, started_at, backend, model, horizon_start, horizon_end, system_prompt, payload_json, kind),
        )


def finish_optimizer_run(run_id: str, *, status: str, finished_at: str, **fields) -> None:
    """fields may include input_tokens, output_tokens, cost_usd, proposed_count, invalid_count, notes, error."""
    allowed = {"input_tokens", "output_tokens", "cost_usd", "proposed_count", "invalid_count", "notes", "error"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown optimizer_runs fields: {sorted(unknown)}")
    sql = "UPDATE optimizer_runs SET status = :status, finished_at = :finished_at"
    if fields:
        sql += ", " + ", ".join(f"{name} = :{name}" for name in fields)
    sql += " WHERE run_id = :run_id"
    with get_conn() as conn:
        conn.execute(sql, {"status": status, "finished_at": finished_at, "run_id": run_id, **fields})


_RUN_REQUEST_COLUMNS = ("system_prompt", "payload_json")


def _run_summary_columns(conn: sqlite3.Connection) -> str:
    """optimizer_runs' columns minus the stored request (tens of kB per run), for the polled summaries."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(optimizer_runs)") if r["name"] not in _RUN_REQUEST_COLUMNS]
    return ", ".join(cols)


def get_optimizer_runs(limit: int = 20, kind: str | None = None) -> list[dict]:
    """Run summaries, newest first, without the (large) stored request; see get_optimizer_run()."""
    with get_conn() as conn:
        cols = _run_summary_columns(conn)
        if kind:
            rows = conn.execute(
                f"SELECT {cols} FROM optimizer_runs WHERE kind = ? ORDER BY started_at DESC LIMIT ?", (kind, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM optimizer_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


def get_optimizer_run(run_id: str) -> dict | None:
    """One run including system_prompt and payload_json, exactly as sent to the model."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM optimizer_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def supersede_open_proposals(now_iso: str, item_types: tuple[str, ...] | None = None,
                             exclude_run_ids: tuple[str, ...] = (), created_before: str | None = None) -> int:
    """
    A new run replaces the still-undecided proposals (shown or queued) of its
    own kind: the optimizer passes its item types, the scout passes
    ('event',). None = all. `exclude_run_ids` keeps the proposals of runs made
    as part of the same pass (the routine expansion that precedes a plan).
    """
    sql = "UPDATE proposed_events SET status = 'superseded', decided_at = ? WHERE status IN ('proposed', 'queued')"
    params: list = [now_iso]
    if item_types:
        sql += f" AND item_type IN ({','.join('?' for _ in item_types)})"
        params += list(item_types)
    if exclude_run_ids:
        sql += f" AND run_id NOT IN ({','.join('?' for _ in exclude_run_ids)})"
        params += list(exclude_run_ids)
    if created_before:
        sql += " AND created_at < ?"
        params.append(created_before)
    with get_conn() as conn:
        return conn.execute(sql, params).rowcount


def expire_past_proposals(now: _dt.datetime) -> int:
    """
    Undecided proposals (shown or queued) whose end has passed can no longer
    happen: mark them 'expired' so no view lists them and approve-all can
    never promote one (time_tracker would then write it to Notion as done).
    Not a decision, so the preference learner ignores them. All-day rows
    expire once their end date is before today in the display zone.
    """
    now = now.astimezone(ZoneInfo(DISPLAY_TZ))
    today = now.date().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, end, all_day FROM proposed_events WHERE status IN ('proposed', 'queued')"
        ).fetchall()
        past = []
        for r in rows:
            try:
                if r["end"][:10] < today if r["all_day"] else parse_event_time(r["end"], False) <= now:
                    past.append(r["id"])
            except (ValueError, TypeError):
                continue  # unparseable end: leave it for a human, never crash a view
        for proposal_id in past:
            conn.execute(
                "UPDATE proposed_events SET status = 'expired', decided_at = ? "
                "WHERE id = ? AND status IN ('proposed', 'queued')",
                (now.isoformat(), proposal_id),
            )
        return len(past)


def insert_proposals(run_id: str, proposals: list[dict], created_at: str) -> list[str]:
    """
    Store one run's proposals. Each dict carries the schema fields (item_type,
    item_id, title, description, location, start, end, all_day, timezone,
    rationale, and the model's `ref`) plus `status` ('proposed', 'queued' or
    'invalid'), optional `invalid_reason`, and for scout options `rank` and
    `shown_at`. Returns the generated ids, in order.
    """
    ids: list[str] = []
    with get_conn() as conn:
        for n, p in enumerate(proposals, start=1):
            proposal_id = sanitize_id(f"{run_id}_{n}")
            ids.append(proposal_id)
            conn.execute(
                """
                INSERT INTO proposed_events (
                    id, run_id, item_type, item_id, title, description, location,
                    start, end, all_day, timezone, rationale, status, invalid_reason, created_at,
                    goal_id, discovered_event_id, website_id, url, cost, ref, rank, shown_at, auto_approved,
                    replaces_event_id
                ) VALUES (
                    :id, :run_id, :item_type, :item_id, :title, :description, :location,
                    :start, :end, :all_day, :timezone, :rationale, :status, :invalid_reason, :created_at,
                    :goal_id, :discovered_event_id, :website_id, :url, :cost, :ref, :rank, :shown_at, :auto_approved,
                    :replaces_event_id
                )
                """,
                {
                    "id": proposal_id,
                    "run_id": run_id,
                    "item_type": p["item_type"],
                    "item_id": p.get("item_id"),
                    "title": p.get("title"),
                    "description": p.get("description"),
                    "location": p.get("location"),
                    "start": p["start"],
                    "end": p["end"],
                    "all_day": int(bool(p.get("all_day", False))),
                    "timezone": p.get("timezone"),
                    "rationale": p.get("rationale"),
                    "status": p.get("status", "proposed"),
                    "invalid_reason": p.get("invalid_reason"),
                    "created_at": created_at,
                    "goal_id": p.get("goal_id") or None,
                    "discovered_event_id": p.get("discovered_event_id") or None,
                    "website_id": p.get("website_id") or None,
                    "url": p.get("url") or None,
                    "cost": p.get("cost") or None,
                    "ref": p.get("ref") or None,
                    "rank": p.get("rank"),
                    "shown_at": p.get("shown_at"),
                    "auto_approved": int(bool(p.get("auto_approved"))),
                    "replaces_event_id": p.get("replaces_event_id") or None,
                },
            )
    return ids


def get_proposals(run_id: str | None = None, status: str | None = None, item_type: str | None = None) -> list[dict]:
    query = "SELECT * FROM proposed_events WHERE 1=1"
    params: list = []
    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    if item_type:
        query += " AND item_type = ?"
        params.append(item_type)
    query += " ORDER BY start ASC"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


# --- review API: open proposals, preview feed, option queue --------------------------

PREVIEW_CALENDAR_ID = "proposed"


def get_open_proposals(now: _dt.datetime) -> list[dict]:
    """
    What the review tabs work from: undecided proposals (shown and queued)
    plus promoted ones that may not have ended yet, oldest first. Promoted
    rows are prefiltered by end date with a day of slack (offsets vary);
    review_model.is_past() makes the exact cut.
    """
    since = (now.astimezone(ZoneInfo(DISPLAY_TZ)) - _dt.timedelta(days=1)).date().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM proposed_events WHERE status IN ('proposed', 'queued') "
            "OR (status = 'promoted' AND substr(end, 1, 10) >= ?) ORDER BY start ASC",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_run(kind: str) -> dict | None:
    """
    The newest succeeded run of a kind ('optimizer' | 'scout') -- the one whose
    proposals are under review -- else the newest run of any status. Without
    the stored request (see get_optimizer_run()).
    """
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_run_summary_columns(conn)} FROM optimizer_runs WHERE COALESCE(kind, 'optimizer') = ? "
            "ORDER BY status != 'succeeded', started_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
    return dict(row) if row else None


def proposal_to_schedule_x_event(row: dict) -> dict:
    """
    A pending proposal shaped for Schedule-X, for the calendar's "if I accept
    everything" preview. Same conventions as to_schedule_x_event() (bracket
    zoned strings, inclusive all-day end); timed values are normalized to the
    display zone so the offset always agrees with the bracketed zone (Temporal
    rejects a mismatch). calendarId 'proposed', id 'proposal_<id>', the
    rationale as description; `proposalId`/`itemType` and an extra CSS class
    (`_options.additionalClasses`) let the frontend style and link them.
    """
    if row["all_day"]:
        shaped = to_schedule_x_event({**row, "calendar_id": PREVIEW_CALENDAR_ID, "people": None})
        start, end = shaped["start"], shaped["end"]
    else:
        start = f"{parse_event_time(row['start'], False).isoformat()}[{DISPLAY_TZ}]"
        end = f"{parse_event_time(row['end'], False).isoformat()}[{DISPLAY_TZ}]"
    return {
        "id": sanitize_id(f"proposal_{row['id']}"),
        "calendarId": PREVIEW_CALENDAR_ID,
        "title": row["title"],
        "description": row.get("rationale") or "",
        "location": row.get("location") or "",
        "people": [],
        "start": start,
        "end": end,
        "proposalId": row["id"],
        "itemType": row["item_type"],
        "_options": {"additionalClasses": ["sx-proposal", f"sx-proposal-{row['item_type']}"]},
    }


def get_preview_events() -> list[dict]:
    """Every status='proposed' row as a Schedule-X event (queued options are not on the page, so not previewed)."""
    events = []
    for row in get_proposals(status="proposed"):
        try:
            events.append(proposal_to_schedule_x_event(row))
        except (ValueError, TypeError, KeyError):
            continue  # an unparseable row must not break the whole preview
    return events


def _span(row) -> tuple[_dt.datetime, _dt.datetime]:
    start = parse_event_time(row["start"], row["all_day"])
    end = parse_event_time(row["end"], row["all_day"])
    if row["all_day"] and end <= start:
        end = start + _dt.timedelta(days=1)
    return start, end


def supersede_overlapping_options(proposal_id: str, now_iso: str) -> list[str]:
    """
    After an event option is approved, the other undecided options (shown or
    queued) that overlap it in time are alternatives that can no longer be
    attended: they become 'superseded' (not a decision; the learner ignores
    them). Returns their ids; [] for anything but an event proposal.
    """
    chosen = get_proposal(proposal_id)
    if chosen is None or chosen["item_type"] != "event":
        return []
    try:
        c_start, c_end = _span(chosen)
    except (ValueError, TypeError):
        return []
    overlapping: list[str] = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, start, end, all_day FROM proposed_events "
            "WHERE item_type = 'event' AND status IN ('proposed', 'queued') AND id != ?",
            (proposal_id,),
        ).fetchall()
        for r in rows:
            try:
                start, end = _span(r)
            except (ValueError, TypeError):
                continue
            if start < c_end and end > c_start:
                overlapping.append(r["id"])
        for other in overlapping:
            conn.execute(
                "UPDATE proposed_events SET status = 'superseded', decided_at = ? "
                "WHERE id = ? AND status IN ('proposed', 'queued')",
                (now_iso, other),
            )
    return overlapping


def count_unseen_queued_options() -> int:
    """Queued event options never shown yet: what an instant reload can bring."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM proposed_events WHERE item_type = 'event' AND status = 'queued' AND shown_at IS NULL"
        ).fetchone()
        return int(row["n"])


def rotate_event_options(page_size: int, now_iso: str) -> dict | None:
    """
    Reload from the queue, without the model: the shown, still-undecided
    options go back to the queue behind everything else (rank bumped past the
    current maximum, their order kept), and the next page is the unseen
    queued options by rank, topped up with already-seen ones (best rank
    first) when fewer than page_size unseen remain. Decided rows are never
    touched and nothing is rejected. Newly shown options get shown_at and
    their discovered event's recommended_at.

    Returns {"shown": [ids], "unseen_left": n}, or None (nothing changed)
    when no unseen option is queued -- then a reload needs the model.
    """
    with get_conn() as conn:
        open_rows = [dict(r) for r in conn.execute(
            "SELECT id, status, rank, shown_at, discovered_event_id FROM proposed_events "
            "WHERE item_type = 'event' AND status IN ('proposed', 'queued') "
            "ORDER BY rank IS NULL, rank, start"
        ).fetchall()]
        unseen = [r for r in open_rows if r["status"] == "queued" and not r["shown_at"]]
        if not unseen:
            return None
        top = max((r["rank"] or 0 for r in open_rows), default=0)
        for i, r in enumerate((r for r in open_rows if r["status"] == "proposed"), start=1):
            conn.execute(
                "UPDATE proposed_events SET status = 'queued', rank = ?, shown_at = COALESCE(shown_at, ?) "
                "WHERE id = ? AND status = 'proposed'",
                (top + i, now_iso, r["id"]),
            )
        page = unseen[:page_size]
        if len(page) < page_size:
            seen = [dict(r) for r in conn.execute(
                "SELECT id, discovered_event_id, shown_at FROM proposed_events "
                "WHERE item_type = 'event' AND status = 'queued' AND shown_at IS NOT NULL "
                "ORDER BY rank IS NULL, rank, start"
            ).fetchall()]
            page += seen[: page_size - len(page)]
        for r in page:
            conn.execute(
                "UPDATE proposed_events SET status = 'proposed', shown_at = COALESCE(shown_at, ?) "
                "WHERE id = ? AND status = 'queued'",
                (now_iso, r["id"]),
            )
        conn.executemany(
            "UPDATE discovered_events SET recommended_at = ? WHERE id = ?",
            [(now_iso, r["discovered_event_id"]) for r in page if r["discovered_event_id"] and not r["shown_at"]],
        )
    return {"shown": [r["id"] for r in page], "unseen_left": len(unseen) - min(len(unseen), page_size)}


def get_proposal(proposal_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM proposed_events WHERE id = ?", (proposal_id,)).fetchone()
        return dict(row) if row else None


def set_proposal_status(proposal_id: str, status: str, decided_at: str) -> bool:
    if status not in PROPOSAL_STATUSES:
        raise ValueError(f"bad proposal status {status!r}")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE proposed_events SET status = ?, decided_at = ? WHERE id = ?",
            (status, decided_at, proposal_id),
        )
        return cur.rowcount == 1


def reject_proposal(proposal_id: str, decided_at: str) -> bool:
    """Reject a pending (shown) proposal -- a decision the learner reads. False when missing or not pending."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE proposed_events SET status = 'rejected', decided_at = ? "
            "WHERE id = ? AND status IN ('proposed', 'approved')",
            (decided_at, proposal_id),
        )
        return cur.rowcount == 1


def promote_proposal(proposal_id: str, now_iso: str) -> str | None:
    """
    Copy an undecided/approved proposal into `events` (source='optimizer',
    calendar_id='optimizer'), record the event id on the task/goal it fulfils,
    and mark the proposal promoted. Returns the events.id, or None when the
    proposal does not exist or was already decided.

    One transaction, claimed by a guarded UPDATE first: a proposal that a
    scheduler job superseded a moment ago is never promoted anyway, and a power
    cut never leaves an event on the calendar behind an undecided proposal.
    """
    event_id = sanitize_id(f"{OPTIMIZER_SOURCE}_{proposal_id}")
    with get_conn() as conn:
        claimed = conn.execute(
            "UPDATE proposed_events SET status = 'promoted', decided_at = ?, promoted_event_id = ? "
            "WHERE id = ? AND status IN ('proposed', 'approved')",
            (now_iso, event_id, proposal_id),
        ).rowcount
        if not claimed:
            return None
        proposal = dict(conn.execute("SELECT * FROM proposed_events WHERE id = ?", (proposal_id,)).fetchone())
        _upsert_event(
            conn,
            {
                "source": OPTIMIZER_SOURCE,
                "source_event_id": proposal_id,
                "calendar_id": OPTIMIZER_CALENDAR_ID,
                "title": proposal["title"],
                "description": proposal["description"],
                "location": proposal["location"],
                "people": [],
                "start": proposal["start"],
                "end": proposal["end"],
                "all_day": bool(proposal["all_day"]),
                "timezone": proposal["timezone"],
                "status": "confirmed",
                "last_modified": now_iso,
                "raw_json": {
                    "proposal_id": proposal_id,
                    "run_id": proposal["run_id"],
                    "item_type": proposal["item_type"],
                    "item_id": proposal["item_id"],
                    "goal_id": proposal.get("goal_id"),
                    "url": proposal.get("url"),
                    "rationale": proposal["rationale"],
                },
            },
        )
        if proposal["item_type"] in ("task", "goal") and proposal["item_id"]:
            _mark_item_scheduled(conn, proposal["item_type"], proposal["item_id"], event_id)
        elif proposal["item_type"] == "event":
            if proposal.get("goal_id"):
                _mark_item_scheduled(conn, "goal", proposal["goal_id"], event_id)
            if proposal.get("discovered_event_id"):
                conn.execute("UPDATE discovered_events SET decision = 'approved' WHERE id = ?", (proposal["discovered_event_id"],))
    return event_id


# --- time tracking: optimizer task blocks -> Notion "Time Spent (hr)" --------


def get_goal_block_dates(goal_id: str, first: _dt.date, last: _dt.date, *, include_pending: bool = True) -> set[_dt.date]:
    """
    Local dates in [first, last] that already hold a block for the goal:
    approved, promoted or rejected goal proposals, plus undecided ones when
    `include_pending` (superseded, expired, retired and invalid rows never
    count). The routine expander leaves these dates alone.
    """
    tz = ZoneInfo(DISPLAY_TZ)
    statuses = ("proposed", "queued", "approved", "promoted", "rejected") if include_pending else ("approved", "promoted", "rejected")
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT start, all_day FROM proposed_events
            WHERE item_type = 'goal' AND item_id = ?
              AND status IN ({",".join("?" for _ in statuses)})
              AND start >= ? AND start < ?
            """,
            (goal_id, *statuses, (first - _dt.timedelta(days=1)).isoformat(), (last + _dt.timedelta(days=2)).isoformat()),
        ).fetchall()
    out: set[_dt.date] = set()
    for r in rows:
        try:
            day = parse_event_time(r["start"], r["all_day"], tz).astimezone(tz).date()
        except ValueError:
            continue
        if first <= day <= last:
            out.add(day)
    return out


def get_unsettled_task_proposals() -> list[dict]:
    """
    Promoted task proposals whose hours have not been written back to Notion
    yet, joined with the task's Notion ids and the live calendar row (so a
    cancelled/soft-deleted event can be recognised). Ordered by end time.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   t.source AS task_source, t.source_task_id, t.task AS task_title,
                   t.time_spent_hr AS task_time_spent_hr,
                   e.deleted_at AS event_deleted_at, e.status AS event_status
            FROM proposed_events p
            JOIN tasks t ON t.id = p.item_id
            LEFT JOIN events e ON e.id = p.promoted_event_id
            WHERE p.status = 'promoted' AND p.item_type = 'task' AND p.settled_at IS NULL
            ORDER BY p.end ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def get_unsettled_goal_proposals() -> list[dict]:
    """
    Promoted goal proposals whose occurrence has not been recorded in Notion
    yet, joined with the goal's Notion ids and the live calendar row.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   g.source AS goal_source, g.source_goal_id, g.goal AS goal_title,
                   g.last_occurred AS goal_last_occurred, g.level AS goal_level,
                   e.deleted_at AS event_deleted_at, e.status AS event_status
            FROM proposed_events p
            JOIN goals g ON g.id = p.item_id
            LEFT JOIN events e ON e.id = p.promoted_event_id
            WHERE p.status = 'promoted' AND p.item_type = 'goal' AND p.settled_at IS NULL
            ORDER BY p.end ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def set_goal_last_occurred(goal_id: str, date_str: str | None) -> None:
    """Mirror the value Notion now holds; the next Notion sync overwrites it anyway."""
    with get_conn() as conn:
        conn.execute("UPDATE goals SET last_occurred = ? WHERE id = ?", (date_str, goal_id))


def set_goal_sessions(goal_id: str, value: float | None) -> None:
    """Mirror the Sessions count Notion now holds; the next Notion sync overwrites it anyway."""
    with get_conn() as conn:
        conn.execute("UPDATE goals SET sessions = ? WHERE id = ?", (value, goal_id))


def set_goal_time_spent(goal_id: str, hours: float | None) -> None:
    """Mirror the Time Spent a Goal/Project planned as sessions now holds in Notion."""
    with get_conn() as conn:
        conn.execute("UPDATE goals SET time_spent_hr = ? WHERE id = ?", (hours, goal_id))


def get_settle_target(proposal_id: str, key: str):
    with get_conn() as conn:
        row = conn.execute("SELECT settle_targets FROM proposed_events WHERE id = ?", (proposal_id,)).fetchone()
    return (json.loads(row["settle_targets"]) if row and row["settle_targets"] else {}).get(key)


def set_settle_target(proposal_id: str, key: str, value) -> None:
    """Remember the value a settle pass is about to write for this block (see time_tracker._add_once)."""
    with get_conn() as conn:
        row = conn.execute("SELECT settle_targets FROM proposed_events WHERE id = ?", (proposal_id,)).fetchone()
        targets = json.loads(row["settle_targets"]) if row and row["settle_targets"] else {}
        targets[key] = value
        conn.execute("UPDATE proposed_events SET settle_targets = ? WHERE id = ?", (json.dumps(targets), proposal_id))


def mark_proposal_settled(proposal_id: str, settled_at: str, hours: float) -> bool:
    """Record that this block's hours were written (or that nothing was owed). Idempotent."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE proposed_events SET settled_at = ?, settled_hours = ? WHERE id = ? AND settled_at IS NULL",
            (settled_at, hours, proposal_id),
        )
        return cur.rowcount == 1


def set_task_time_spent(task_id: str, hours: float | None) -> None:
    """Mirror the value Notion now holds; the next Notion sync overwrites it anyway."""
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET time_spent_hr = ? WHERE id = ?", (hours, task_id))


def revoke_proposal(proposal_id: str, now_iso: str) -> str | None:
    """
    Undo a promotion: soft-delete (cancel) the optimizer event so it leaves
    the calendar, drop its id from the task/goal bookkeeping, and return the
    proposal to `proposed` so it can be approved again or rejected
    (promoted_event_id is kept for the audit trail; a re-approval revives the
    same event row). A scout event's recorded decision is cleared. Returns
    the event id, or None if the proposal was not promoted or was already
    settled to Notion.
    """
    proposal = get_proposal(proposal_id)
    if proposal is None or proposal["status"] != "promoted" or not proposal["promoted_event_id"]:
        return None
    if proposal.get("settled_at"):
        return None  # already written to Notion (time spent, sessions): it happened
    event_id = proposal["promoted_event_id"]
    soft_delete_event(OPTIMIZER_SOURCE, proposal_id, now_iso)
    _unlink_scheduled(proposal, event_id)
    if proposal["item_type"] == "event" and proposal.get("discovered_event_id"):
        set_discovered_decision(proposal["discovered_event_id"], None)
    with get_conn() as conn:
        if proposal.get("auto_approved"):
            # A routine block (placed by code, never reviewed): taking it off the
            # calendar is the person's decision, so it becomes a rejection that
            # the learner sees and the expander will not refill.
            conn.execute(
                "UPDATE proposed_events SET status = 'rejected', decided_at = ?, auto_approved = 0 WHERE id = ?",
                (now_iso, proposal_id),
            )
        else:
            conn.execute(
                "UPDATE proposed_events SET status = 'proposed', decided_at = NULL WHERE id = ?",
                (proposal_id,),
            )
    return event_id


# --- events scout: websites, discovered events ------------------------------------


def upsert_website(site: dict) -> None:
    site_id = sanitize_id(f"{site['source']}_{site['source_website_id']}")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO websites (
                id, url, upcoming_event, events_attended, source, source_website_id,
                last_modified, deleted_at, raw_json
            ) VALUES (
                :id, :url, :upcoming_event, :events_attended, :source, :source_website_id,
                :last_modified, :deleted_at, :raw_json
            )
            ON CONFLICT(source, source_website_id) DO UPDATE SET
                url = excluded.url,
                upcoming_event = excluded.upcoming_event,
                events_attended = excluded.events_attended,
                last_modified = excluded.last_modified,
                deleted_at = excluded.deleted_at,
                raw_json = excluded.raw_json
            """,
            {
                "id": site_id,
                "url": site["url"],
                "upcoming_event": site.get("upcoming_event"),
                "events_attended": site.get("events_attended"),
                "source": site["source"],
                "source_website_id": site["source_website_id"],
                "last_modified": site.get("last_modified"),
                "deleted_at": site.get("deleted_at"),
                "raw_json": json.dumps(site.get("raw_json", {})),
            },
        )


def upsert_websites(sites: list[dict]) -> None:
    with batch():
        for site in sites:
            upsert_website(site)


def get_websites(include_deleted: bool = False) -> list[dict]:
    query = "SELECT * FROM websites" + ("" if include_deleted else " WHERE deleted_at IS NULL") + " ORDER BY url"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(query).fetchall()]


def get_website(website_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM websites WHERE id = ?", (website_id,)).fetchone()
        return dict(row) if row else None


def get_active_source_website_ids(source: str) -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source_website_id FROM websites WHERE source = ? AND deleted_at IS NULL", (source,)
        ).fetchall()
    return {r["source_website_id"] for r in rows}


def soft_delete_website(source: str, source_website_id: str, deleted_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE websites SET deleted_at = ? WHERE source = ? AND source_website_id = ?",
            (deleted_at, source, source_website_id),
        )


def set_website_fetch_status(website_id: str, fetched_at: str, method: str, count: int, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE websites SET last_fetched_at = ?, last_fetch_method = ?, last_fetch_count = ?, last_fetch_error = ? WHERE id = ?",
            (fetched_at, method, count, error, website_id),
        )


def set_website_notion_mirror(website_id: str, *, upcoming_event: str | None = None, events_attended: float | None = None) -> None:
    """Mirror values just written to Notion; the next Website List sync overwrites them anyway."""
    with get_conn() as conn:
        if upcoming_event is not None:
            conn.execute("UPDATE websites SET upcoming_event = ? WHERE id = ?", (upcoming_event, website_id))
        if events_attended is not None:
            conn.execute("UPDATE websites SET events_attended = ? WHERE id = ?", (events_attended, website_id))


def upsert_discovered_events(events: list[dict], website_id: str | None, seen_at: str) -> tuple[int, int]:
    """Insert new / refresh known discovered events. Returns (new, updated). Decisions are never touched."""
    new = updated = 0
    with get_conn() as conn:
        for e in events:
            exists = conn.execute("SELECT 1 FROM discovered_events WHERE id = ?", (e["id"],)).fetchone()
            conn.execute(
                """
                INSERT INTO discovered_events (
                    id, website_id, site, title, start, end, all_day, timezone, location, url,
                    cost, category, summary, first_seen, last_seen, raw_json
                ) VALUES (
                    :id, :website_id, :site, :title, :start, :end, :all_day, :timezone, :location, :url,
                    :cost, :category, :summary, :seen, :seen, :raw_json
                )
                ON CONFLICT(id) DO UPDATE SET
                    website_id = COALESCE(excluded.website_id, discovered_events.website_id),
                    title = excluded.title,
                    start = excluded.start, all_day = excluded.all_day,
                    end = excluded.end, location = excluded.location, url = excluded.url,
                    cost = excluded.cost, category = excluded.category, summary = excluded.summary,
                    last_seen = excluded.last_seen, raw_json = excluded.raw_json
                """,
                {
                    "id": e["id"], "website_id": website_id, "site": e["site"], "title": e["title"],
                    "start": e["start"], "end": e.get("end"), "all_day": int(bool(e.get("all_day"))),
                    "timezone": e.get("timezone"), "location": e.get("location"), "url": e.get("url"),
                    "cost": e.get("cost"), "category": e.get("category"), "summary": e.get("summary"),
                    "seen": seen_at, "raw_json": json.dumps(e.get("raw"), default=str) if e.get("raw") is not None else None,
                },
            )
            if exists:
                updated += 1
            else:
                new += 1
    return new, updated


def get_discovered_events(start_from: str, start_to: str, *, undecided_only: bool = True) -> list[dict]:
    """Discovered events starting in [start_from, start_to) by date prefix, oldest first."""
    query = "SELECT * FROM discovered_events WHERE substr(start, 1, 10) >= ? AND substr(start, 1, 10) < ?"
    params: list = [start_from[:10], start_to[:10]]
    if undecided_only:
        query += " AND decision IS NULL"
    query += " ORDER BY start ASC"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_discovered_event(event_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM discovered_events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None


def mark_discovered_recommended(event_ids: list[str], at: str) -> None:
    with get_conn() as conn:
        conn.executemany("UPDATE discovered_events SET recommended_at = ? WHERE id = ?", [(at, i) for i in event_ids])


def set_discovered_decision(event_id: str, decision: str | None) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE discovered_events SET decision = ? WHERE id = ?", (decision, event_id))


def get_decision_history(limit: int = 60) -> list[dict]:
    """What the person approved, rejected or attended, newest first: the scout's preference signal."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT title, site, category, substr(start, 1, 10) AS date, location, cost, decision
            FROM discovered_events WHERE decision IS NOT NULL
            ORDER BY start DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_unsettled_event_proposals() -> list[dict]:
    """Promoted event proposals not yet settled, joined with website + goal Notion ids and the live calendar row."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   w.source AS website_source, w.source_website_id, w.url AS website_url,
                   g.source AS goal_source, g.source_goal_id,
                   e.deleted_at AS event_deleted_at, e.status AS event_status
            FROM proposed_events p
            LEFT JOIN websites w ON w.id = p.website_id
            LEFT JOIN goals g ON g.id = p.goal_id
            LEFT JOIN events e ON e.id = p.promoted_event_id
            WHERE p.status = 'promoted' AND p.item_type = 'event' AND p.settled_at IS NULL
            ORDER BY p.end ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


# --- settings + preference learning inputs ---------------------------------------


def get_setting(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str, updated_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, updated_at),
        )


def reschedule_proposal(proposal_id: str, start: str, end: str, now_iso: str) -> bool:
    """
    Move a still-undecided optimizer block (task, goal, travel, free) to the
    user's own times before approval. Public events (item_type 'event') are
    refused: they happen when the listing says.
    Times are ISO 8601 (naive = display zone). The model's original times are
    kept the first time a proposal is moved, so the learner can see the delta.
    Returns False when the proposal is missing or already decided.
    """
    proposal = get_proposal(proposal_id)
    if proposal is None or proposal["status"] not in ("proposed", "approved"):
        return False
    if proposal["item_type"] == "event":
        raise ValueError("a public event happens at its listed time and cannot be moved")
    start_dt = parse_event_time(start, False)
    end_dt = parse_event_time(end, False)
    if end_dt <= start_dt:
        raise ValueError("end must be after start")
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE proposed_events
            SET start = ?, end = ?, all_day = 0, timezone = ?, adjusted_at = ?,
                original_start = COALESCE(original_start, start),
                original_end = COALESCE(original_end, end)
            WHERE id = ?
            """,
            (start_dt.isoformat(), end_dt.isoformat(), DISPLAY_TZ, now_iso, proposal_id),
        )
    return True


def get_event(event_id: str) -> dict | None:
    """One raw events row by its Schedule-X id (soft-deleted rows included), or None."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None


def move_optimizer_event(event_id: str, start: str, end: str, now_iso: str) -> dict:
    """
    Move an approved optimizer block (an events row with source 'optimizer') to
    the user's own start/end, from the calendar's event details. Times are ISO
    8601 (naive = display zone). The proposal it was promoted from moves with
    it, keeping the model's first times in original_start/end plus adjusted_at,
    so the learner sees the move and time_tracker settles the block at its new
    time. Synced events (Google, Apple, Outlook, Partiful) are refused: the app
    never writes to those calendars, so the next sync would undo the move.
    Raises LookupError when the event is missing or off the calendar, and
    ValueError for a refused move.
    """
    event = get_event(event_id)
    if event is None or event["deleted_at"] or event["status"] == "cancelled":
        raise LookupError("event not found")
    if event["source"] != OPTIMIZER_SOURCE:
        raise ValueError(f"{event['source']} events belong to that calendar; move them there")
    start_dt = parse_event_time(start, False)
    end_dt = parse_event_time(end, False)
    if end_dt <= start_dt:
        raise ValueError("end must be after start")
    proposal = get_proposal(event["source_event_id"])
    if proposal and proposal.get("settled_at"):
        raise ValueError("this block has already been written to Notion and cannot be moved")
    with get_conn() as conn:
        conn.execute(
            "UPDATE events SET start = ?, end = ?, all_day = 0, timezone = ?, last_modified = ? WHERE id = ?",
            (start_dt.isoformat(), end_dt.isoformat(), DISPLAY_TZ, now_iso, event_id),
        )
        if proposal:
            conn.execute(
                """
                UPDATE proposed_events
                SET start = ?, end = ?, all_day = 0, timezone = ?, adjusted_at = ?,
                    original_start = COALESCE(original_start, start),
                    original_end = COALESCE(original_end, end)
                WHERE id = ?
                """,
                (start_dt.isoformat(), end_dt.isoformat(), DISPLAY_TZ, now_iso, proposal["id"]),
            )
    return get_event(event_id)


def get_plan_decision_history(limit: int = 40) -> list[dict]:
    """
    What the person did with the optimizer's and scout's proposals, newest
    first: approved (promoted), rejected, or moved before approving. Free of
    undecided/superseded rows. This is the optimizer's preference signal.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT item_type, title, start, end, status, original_start, original_end, adjusted_at,
                   decided_at, location, cost
            FROM proposed_events
            WHERE (status IN ('promoted', 'retired', 'rejected') OR adjusted_at IS NOT NULL) AND COALESCE(auto_approved, 0) = 0
            ORDER BY COALESCE(decided_at, adjusted_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_decisions() -> int:
    """Total decided proposals; the learner compares this with what it has already digested."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM proposed_events WHERE (status IN ('promoted', 'retired', 'rejected') OR adjusted_at IS NOT NULL) AND COALESCE(auto_approved, 0) = 0"
        ).fetchone()
        return int(row["n"])


def plan_decision_stats() -> dict:
    """
    Plain counts the model can trust without inference: approval rate per
    kind, per time-of-day bucket, and how blocks get moved when rescheduled.
    """
    tz = ZoneInfo(DISPLAY_TZ)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT item_type, start, end, all_day, status, original_start, adjusted_at FROM proposed_events "
            "WHERE (status IN ('promoted', 'retired', 'rejected') OR adjusted_at IS NOT NULL) AND COALESCE(auto_approved, 0) = 0"
        ).fetchall()
    by_kind: dict[str, dict[str, int]] = {}
    by_slot: dict[str, dict[str, int]] = {}
    moves: list[int] = []
    for r in rows:
        kind = by_kind.setdefault(r["item_type"], {"approved": 0, "rejected": 0, "moved": 0})
        try:
            start = parse_event_time(r["start"], r["all_day"], tz)
        except ValueError:
            continue
        if r["all_day"]:
            slot = "all-day"
        elif start.weekday() >= 5:
            slot = "weekend"
        elif start.hour < 12:
            slot = "weekday morning"
        elif start.hour < 17:
            slot = "weekday afternoon"
        else:
            slot = "weekday evening"
        bucket = by_slot.setdefault(slot, {"approved": 0, "rejected": 0})
        if r["status"] in APPROVED_STATUSES:
            kind["approved"] += 1
            bucket["approved"] += 1
        elif r["status"] == "rejected":
            kind["rejected"] += 1
            bucket["rejected"] += 1
        if r["adjusted_at"] and r["original_start"]:
            kind["moved"] += 1
            try:
                original = parse_event_time(r["original_start"], False, tz)
                moves.append(int((start - original).total_seconds() // 60))
            except ValueError:
                pass

    def rate(d: dict[str, int]) -> float | None:
        total = d["approved"] + d["rejected"]
        return round(d["approved"] / total, 2) if total else None

    moved_later = sum(1 for m in moves if m > 0)
    moved_earlier = sum(1 for m in moves if m < 0)
    median_move = sorted(moves)[len(moves) // 2] if moves else None
    return {
        "decisions": len(rows),
        "by_kind": {k: {**v, "approval_rate": rate(v)} for k, v in by_kind.items()},
        "by_slot": {k: {**v, "approval_rate": rate(v)} for k, v in by_slot.items()},
        "reschedules": {"count": len(moves), "moved_later": moved_later, "moved_earlier": moved_earlier,
                        "median_shift_minutes": median_move},
    }


# --- triggers (triggers.py): conflicts, moves, deleted Notion rows, the change digest ----------


def get_schedulable_item(item_type: str, item_id: str) -> dict | None:
    """One active task or goal by its id (with `type`), or None when missing, done or deleted."""
    for item in get_schedulable_items():
        if item["type"] == item_type and item["id"] == item_id:
            return item
    return None


def get_proposals_for_item(item_type: str, item_id: str, statuses: tuple[str, ...]) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM proposed_events WHERE item_type = ? AND item_id = ? AND status IN ({','.join('?' for _ in statuses)}) "
            "ORDER BY start ASC",
            (item_type, item_id, *statuses),
        ).fetchall()
    return [dict(r) for r in rows]


def get_promoted_plan_blocks(window_start: _dt.datetime, window_end: _dt.datetime) -> list[dict]:
    """
    Approved blocks still on the calendar whose event overlaps the window:
    the proposal row plus `start_dt`/`end_dt` from the live event (a move from
    the details sheet changes the event, and the proposal follows it).
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.*, e.start AS event_start, e.end AS event_end, e.all_day AS event_all_day, e.title AS event_title
            FROM proposed_events p JOIN events e ON e.id = p.promoted_event_id
            WHERE p.status = 'promoted' AND e.deleted_at IS NULL AND e.status != 'cancelled' AND e.end >= ?
            """,
            ((window_start - _dt.timedelta(days=1)).strftime("%Y-%m-%d"),),
        ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        try:
            row["start_dt"] = parse_event_time(row["event_start"], row["event_all_day"])
            row["end_dt"] = parse_event_time(row["event_end"], row["event_all_day"])
        except (ValueError, TypeError):
            continue
        if row["start_dt"] < window_end and row["end_dt"] > window_start:
            out.append(row)
    return out


def set_move_attempted(proposal_id: str, at: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE proposed_events SET move_attempted_at = ? WHERE id = ?", (at, proposal_id))


def get_moves_for(event_id: str) -> list[dict]:
    """The move proposals (any status) that would take the place of approved block `event_id`."""
    with get_conn() as conn:
        rows = conn.execute("SELECT id, status, created_at FROM proposed_events WHERE replaces_event_id = ?",
                            (event_id,)).fetchall()
    return [dict(r) for r in rows]


def set_conflict(proposal_id: str, conflict_event_id: str | None, found_at: str | None) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE proposed_events SET conflict_event_id = ?, conflict_found_at = ? WHERE id = ?",
                     (conflict_event_id, found_at, proposal_id))


def get_travel_blocks_for(proposal: dict, statuses: tuple[str, ...] = ("promoted",)) -> list[dict]:
    """Travel proposals attached to `proposal` (same run, item_id = its ref), in the given statuses."""
    if not proposal.get("ref"):
        return []
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM proposed_events WHERE item_type = 'travel' AND run_id = ? AND item_id = ? "
            f"AND status IN ({','.join('?' for _ in statuses)})",
            (proposal["run_id"], proposal["ref"], *statuses),
        ).fetchall()
    return [dict(r) for r in rows]


def _unlink_scheduled(proposal: dict, event_id: str) -> None:
    links = []
    if proposal["item_type"] in ("task", "goal") and proposal.get("item_id"):
        links.append((proposal["item_type"], proposal["item_id"]))
    if proposal["item_type"] == "event" and proposal.get("goal_id"):
        links.append(("goal", proposal["goal_id"]))
    for item_type, item_id in links:
        table = {"task": "tasks", "goal": "goals"}[item_type]
        with get_conn() as conn:
            row = conn.execute(f"SELECT scheduled_event_ids FROM {table} WHERE id = ?", (item_id,)).fetchone()
            if row and row["scheduled_event_ids"]:
                remaining = [e for e in json.loads(row["scheduled_event_ids"]) if e != event_id]
                conn.execute(f"UPDATE {table} SET scheduled_event_ids = ? WHERE id = ?", (json.dumps(remaining), item_id))


def _take_off_calendar(proposal: dict, now_iso: str, status: str, replaced_by: str | None = None) -> None:
    """Soft-delete the block's event, drop the task/goal link and set the proposal's status (not a decision)."""
    event_id = proposal.get("promoted_event_id")
    if event_id:
        soft_delete_event(OPTIMIZER_SOURCE, proposal["id"], now_iso)
        _unlink_scheduled(proposal, event_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE proposed_events SET status = ?, decided_at = ?, replaced_by = COALESCE(?, replaced_by), "
            "conflict_event_id = NULL, conflict_found_at = NULL WHERE id = ?",
            (status, now_iso, replaced_by, proposal["id"]),
        )


def delete_optimizer_event(event_id: str, now_iso: str) -> dict:
    """
    "Delete" from the calendar's event details: the approved block leaves the
    calendar and its proposal becomes `rejected` (a decision the learner reads
    and the routine expander respects: that date is not refilled). Attached
    travel goes with it as `superseded`. Synced events are refused (ValueError:
    the app never writes to those calendars), as is a block already written to
    Notion; a missing or already removed block raises LookupError. Returns the
    proposal row (for the scout bookkeeping) and the travel ids taken off.
    """
    event = get_event(event_id)
    if event is None or event["deleted_at"] or event["status"] == "cancelled":
        raise LookupError("event not found")
    if event["source"] != OPTIMIZER_SOURCE:
        raise ValueError(f"{event['source']} events belong to that calendar; delete them there")
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM proposed_events WHERE promoted_event_id = ? AND status = 'promoted'", (event_id,)).fetchone()
    if row is None:
        raise LookupError("event not found")
    proposal = dict(row)
    if proposal.get("settled_at"):
        raise ValueError("this block has already been written to Notion and cannot be deleted")
    travel_ids = []
    for travel in get_travel_blocks_for(proposal):
        _take_off_calendar(travel, now_iso, "superseded")
        travel_ids.append(travel["id"])
    _take_off_calendar(proposal, now_iso, "rejected")
    if proposal["item_type"] == "event" and proposal.get("discovered_event_id"):
        set_discovered_decision(proposal["discovered_event_id"], "rejected")
    return {"proposal": proposal, "travel": travel_ids}


def replace_block(event_id: str, now_iso: str, replaced_by: str) -> list[str]:
    """
    An approved move: the old block (and its attached travel) leave the
    calendar as `superseded` (replaced_by = the new proposal's id); the
    caller then promotes the new one. Returns the proposal ids taken off.
    Nothing happens when the block is already gone.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM proposed_events WHERE promoted_event_id = ? AND status = 'promoted'", (event_id,)).fetchone()
    if row is None:
        return []
    proposal = dict(row)
    gone = []
    for travel in get_travel_blocks_for(proposal):
        _take_off_calendar(travel, now_iso, "superseded", replaced_by)
        gone.append(travel["id"])
    _take_off_calendar(proposal, now_iso, "superseded", replaced_by)
    gone.append(proposal["id"])
    return gone


def restore_replaced(move_id: str, now: _dt.datetime, now_iso: str) -> list[str]:
    """
    Undo replace_block() when an approved move is revoked: the blocks it took
    off (the old block and its travel, replaced_by = move_id) come back onto
    the calendar, re-linked to their task/goal. Only while the old time is
    still ahead: a block in the past would be settled to Notion as if it had
    happened. Returns the proposal ids restored.
    """
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM proposed_events WHERE replaced_by = ? AND status = 'superseded' AND promoted_event_id IS NOT NULL",
            (move_id,)).fetchall()]
    restored = []
    with batch():
        for p in rows:
            try:
                end = parse_event_time(p["end"], p["all_day"])
            except (ValueError, TypeError):
                continue
            if end <= now:
                continue
            with get_conn() as conn:
                conn.execute("UPDATE events SET status = 'confirmed', deleted_at = NULL WHERE source = ? AND source_event_id = ?",
                             (OPTIMIZER_SOURCE, p["id"]))
                conn.execute("UPDATE proposed_events SET status = 'promoted', decided_at = ?, replaced_by = NULL WHERE id = ?",
                             (now_iso, p["id"]))
                if p["item_type"] in ("task", "goal") and p.get("item_id"):
                    _mark_item_scheduled(conn, p["item_type"], p["item_id"], p["promoted_event_id"])
            restored.append(p["id"])
    return restored


def remove_item_blocks(item_type: str, item_id: str, now_iso: str) -> dict[str, list[str]]:
    """
    A task or goal deleted in Notion: its pending proposals are superseded
    and its approved blocks (with their travel) leave the calendar, also as
    superseded -- not a decision by the person.
    """
    out = {"pending": [], "approved": []}
    for p in get_proposals_for_item(item_type, item_id, ("proposed", "queued", "approved")):
        with get_conn() as conn:
            conn.execute("UPDATE proposed_events SET status = 'superseded', decided_at = ? WHERE id = ?", (now_iso, p["id"]))
        out["pending"].append(p["id"])
    for p in get_proposals_for_item(item_type, item_id, ("promoted",)):
        for travel in get_travel_blocks_for(p):
            _take_off_calendar(travel, now_iso, "superseded")
        _take_off_calendar(p, now_iso, "superseded")
        out["approved"].append(p["id"])
    return out


def retire_open_plan(now: _dt.datetime) -> dict:
    """
    reset_plan.py: every undecided proposal becomes `superseded`; every
    approved block that has not ended yet comes off the calendar as `retired`
    (the approval stays in the decision history). A retired scouted event's
    discovered row loses its decision so the scout may offer it again.
    Returns counts and the retired event proposals (for clearing Notion).
    """
    now_iso = now.isoformat()
    superseded = supersede_open_proposals(now_iso)
    retired, events = [], []
    for p in get_proposals(status="promoted"):
        try:
            end = parse_event_time(p["end"], p["all_day"])
            if p["all_day"] and end <= parse_event_time(p["start"], True):
                end = end + _dt.timedelta(days=1)
        except (ValueError, TypeError):
            continue
        if end <= now:
            continue
        _take_off_calendar(p, now_iso, "retired")
        if p["item_type"] == "event":
            if p.get("discovered_event_id"):
                set_discovered_decision(p["discovered_event_id"], None)
            events.append(p)
        retired.append(p["id"])
    return {"superseded": superseded, "retired": retired, "retired_events": events}


def get_unnotified_trigger_proposals() -> list[dict]:
    """Pending proposals from trigger runs that no change email has listed yet."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.* FROM proposed_events p JOIN optimizer_runs r ON r.run_id = p.run_id
            WHERE r.kind = 'trigger' AND p.status = 'proposed' AND p.notified_at IS NULL
            ORDER BY p.start ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def mark_proposals_notified(ids: list[str], at: str) -> None:
    if not ids:
        return
    with get_conn() as conn:
        conn.execute(f"UPDATE proposed_events SET notified_at = ? WHERE id IN ({','.join('?' for _ in ids)})", (at, *ids))


# --- places (geo.py) -----------------------------------------------------------------


def get_places(keys: list[str]) -> dict[str, dict]:
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return {}
    with get_conn() as conn:
        rows = conn.execute(f"SELECT * FROM places WHERE key IN ({','.join('?' * len(keys))})", keys).fetchall()
    return {r["key"]: dict(r) for r in rows}


def upsert_place(key: str, query: str, lat: float | None, lon: float | None, neighborhood: str | None,
                 borough: str | None, looked_up_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO places (key, query, lat, lon, neighborhood, borough, looked_up_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET query=excluded.query, lat=excluded.lat, lon=excluded.lon, "
            "neighborhood=excluded.neighborhood, borough=excluded.borough, looked_up_at=excluded.looked_up_at",
            (key, query, lat, lon, neighborhood, borough, looked_up_at),
        )


def get_discovered_summaries(ids: list[str]) -> dict[str, str]:
    """discovered_events.summary by id: the event's own description, for the Events tab."""
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    with get_conn() as conn:
        rows = conn.execute(f"SELECT id, summary FROM discovered_events WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: r["summary"] or "" for r in rows}


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
