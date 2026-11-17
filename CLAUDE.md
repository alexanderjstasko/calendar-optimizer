# calendar_optimizer — Project Context

Personal multi-source calendar aggregation and optimization system. Pulls events from
Google, Apple, Outlook and Partiful plus a Notion goal/task tree into one SQLite store,
renders them in a responsive calendar app, and uses Claude to plan tasks and goals as
calendar blocks that the person approves before anything lands on the calendar.

A personal `CLAUDE.local.md` next to this file (never published) may hold notes about one
person's own instance: hosts, accounts, history. This file covers the code only.

## Stack

- **Frontend:** Schedule-X 4 (`@schedule-x/calendar`) on Preact, Vite (port 5173),
  `temporal-polyfill`. Hash-routed tabs: Calendar / Events / Plan / Hours. PWA with a
  hand-written service worker (`frontend/src/sw.js`) for offline viewing.
- **Backend:** FastAPI (port 8000), APScheduler (`BackgroundScheduler`, in-process, so one
  uvicorn worker), SQLite (`backend/events.db`, WAL) as the single source of truth.
- **LLM:** `backend/llm_client.py` is the only module that talks to Claude. `LLM_BACKEND=api`
  (Anthropic SDK) or `cli` (the `claude` CLI on a subscription login).
- **Display timezone:** `America/New_York` throughout.
- **Target host:** a Raspberry Pi (Debian ARM64). Every dependency must have an ARM64 wheel
  or be pure Python/JS, and stay light on RAM.

## Layout

- `.env` lives at the project root (see `.env.example`); backend modules load it from one
  directory above their own.
- `backend/`: adapters per source (`Google_cal_pull.py`, `Apple_ics_pull.py`,
  `Outlook_ics_pull.py`, `Partiful_ics_pull.py`, `notion_adapter.py`), `db.py` (schema and
  persistence), `main.py` (thin routing only), `optimizer.py` (weekly plan), `routine.py`
  (routine goals placed in code), `triggers.py` (Notion and conflict checks between plans),
  `scout.py` + `event_sources.py` (public events), `time_tracker.py` (the only Notion
  writer), `notifier.py` (approval emails), `hours.py` (sleep/work/free hours).
- Prompt files at the root: `system_prompt.md`, `placement_prompt.md`, `scout_prompt.md`;
  `preferences.md` holds the person's stated preferences. Edit these, not code, to tune the model.
- `deploy/`: Raspberry Pi setup, deploy and backup scripts (`deploy/README.md`).
- `.claude/launch.json`: dev servers for Claude Code's preview tooling (`demo-screenshots` serves
  the made-up demo week on port 8002).
- `docs/`: the GitHub Pages site. Its screenshots come only from `tools/screenshots/`
  (`serve_demo.sh` + `capture.py`, fake data), never from a real instance.

## Running and testing

- Backend: `.venv/bin/python3 -m uvicorn --app-dir backend main:app --port 8000`.
  Startup syncs all calendars at once; `GET /health`, `GET /health/llm`, `GET /schedule`.
- Frontend dev: `cd frontend && npm run dev`. A production build (`npm run build`) is served
  by the backend at `/`.
- Tests: `cd backend && ../.venv/bin/python3 -m pytest tests -q` (throwaway databases, no
  network, no tokens spent).
- Job cadence lives in `.env` (`backend/schedule_config.py`): DAYS takes `mon,thu`,
  `mon-fri`, `daily` or `off`.

## Design decision: nothing reaches the calendar without approval

`sync_notion()` refreshes tasks/goals -> the runner builds one JSON payload (calendar,
hours, tasks, goals, preferences) -> `llm_client.complete_structured()` with
`optimizer_schema.PROPOSED_EVENTS_SCHEMA` -> `validate_proposals()` -> rows in
`proposed_events` -> the person approves in the Plan/Events tabs -> `db.promote_proposal()`
writes them to `events` with `source='optimizer'`. Plan runs never move approved blocks;
only `triggers.py` may propose a move, as a pending proposal. The app never writes to
Google/Apple/Outlook/Partiful.

## db.py principles

- Connections come from `get_conn()` (context manager). There is no `get_connection()`.
- Raw storage is ISO 8601 + an IANA `timezone` column; `to_schedule_x_event()` builds
  Temporal strings (`2026-07-15T10:00:00-04:00[America/New_York]`) at read time.
  All-day events store `YYYY-MM-DD` with an exclusive end.
- Event ids are `"{source}_{source_event_id}"`, sanitized for CSS (Schedule-X rejects `:`/`@`).
- Every sync is fetch -> diff -> upsert / soft-delete; never blind overwrites.
- Batches write in one transaction (`upsert_events`); the syncs start 20 s apart.

## Notion

One database, the Telos tree: a `Level` select routes rows (Telos/Goal/Project -> ancestry,
Routine -> goals, Task -> tasks). Property names are read with fallbacks in
`notion_adapter.get_first`. The README lists the properties. The integration must be shared
with each database (a missing share returns 404, not 401).

## Conventions

- Separation of concerns: normalization in the adapters, persistence in `db.py`, routing in
  `main.py`.
- Settings use the `LLM_` prefix, never `CLAUDE_` (Claude Code exports `CLAUDE_*` variables
  and python-dotenv never overrides an existing one).
- With the `cli` backend, never pass `--bare` (it drops the subscription login).
- No personal data in tracked files: hosts come from `TAILNET`, addresses from the Hours tab.
  `deploy/leak-check.sh` guards the public repo.
