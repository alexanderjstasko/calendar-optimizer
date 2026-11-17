# calendar_optimizer

A self-hosted calendar that merges Google, Apple, Outlook and Partiful into one view,
reads your goals and tasks from Notion, and has Claude plan them into your week. Every
planned block is a proposal: you approve it, move it or reject it in the app before it
lands on your calendar. A scout also suggests public events that fit your free evenings.

Runs on a Mac for development and on a Raspberry Pi 4 for real use, reached from your
phone over [Tailscale](https://tailscale.com). Nothing is hosted for you; your data stays
on your machine.

## What you need

- Python 3.11+ and Node 20.19+ (or 22.12+)
- A Notion account and an internal integration token
- Claude: an Anthropic API key, or a Claude subscription and the `claude` CLI
- Optional: a Google Cloud OAuth client (Google Calendar), the secret iCal links of your
  Apple / Outlook / Partiful calendars, a Gmail app password (approval emails), Tailscale

## Quick start (local)

```bash
git clone https://github.com/<owner>/calendar_optimizer.git
cd calendar_optimizer
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cp .env.example .env      # then fill it in, see below
(cd frontend && npm install && npm run build)
.venv/bin/python3 -m uvicorn --app-dir backend main:app --port 8000
```

Open http://localhost:8000/. Run the tests (no network, no tokens spent):

```bash
cd backend && ../.venv/bin/python3 -m pytest tests -q
```

For frontend work, run `npm run dev` in `frontend/` (port 5173) next to the backend.

## Configuration

Everything secret or personal goes in `.env`, which git ignores. `.env.example` lists
every setting with its default. The important ones:

| Setting | What it is |
|---|---|
| `NOTION_TOKEN`, `NOTION_GOALS_DB_ID` | Your integration token and the Telos tree database (required) |
| `NOTION_WEBSITES_DB_ID` | Website List database for the events scout (optional) |
| `LLM_BACKEND` | `api` (needs `ANTHROPIC_API_KEY`) or `cli` (uses your `claude` login) |
| `APPLE_ICS_URL`, `OUTLOOK_ICS_URL`, `PARTIFUL_ICS_URL` | Secret iCal feed links; leave empty to skip a source |
| `TAILNET` | Your MagicDNS suffix (`example-tailnet.ts.net`), so phones on the tailnet can use the app |
| `NOTIFY_EMAIL_TO` + `NOTIFY_SMTP_*` | Approval emails after each plan; unset = off |
| `*_DAYS`, `*_TIME`, `*_EVERY_MIN` | When each background job runs; `off` disables one |

**Google Calendar:** create an OAuth client (Desktop app) in Google Cloud with the
Calendar API enabled, save it as `backend/google_credentials.json`, then run
`.venv/bin/python3 backend/Google_cal_pull.py` once to sign in. Delete
`backend/google_token.json` to sign in again.

**Your hours:** sleep, work, office days, home address and the free-time floor are set
in the app's Hours tab, not in files.

## Notion databases

Share each database with your integration (`•••` → Connections), or the API answers 404.

**Telos tree** (`NOTION_GOALS_DB_ID`), with sub-items turned on:

| Property | Type | Used for |
|---|---|---|
| (title) | title | The name |
| `Level` | select | `Telos`, `Goal`, `Project` (structure), `Routine` (recurring), `Task` (one-off) |
| `Parent item` | relation | The tree |
| `Status` | status/select | `Not started`, `In-Progress`, `On Hold` (pauses a subtree), `Done`, `Dropped` |
| `Priority` | select | `Critical`, `High`, `Medium`, `Low` (inherited from ancestors) |
| `Type` | select | Routines: `Routine` (placed by code), `Flexible` (placed by Claude), `Event Interest` (scout only) |
| `Times/Week` | number | Routine pace (7 = daily, 0.5 = every other week) |
| `Session Time (hr)` | number | Length of one session |
| `Time to Complete` | number | Hours a task (or project) needs |
| `Time Spent` | number | Written by the app after a block passes |
| `Target Date` | date | Deadline / target |
| `Display` | checkbox | Unchecked = context for the planner, never scheduled |
| `Day of Week`, `Time of Day` | select / text | Allowed days and a time window |
| `Location` | text | Where it happens (travel is planned around it) |
| `Notes for LLM` | text | Free-text guidance Claude follows |
| `Last Occurred`, `Sessions` | date / number | Written by the app |

**Website List** (optional, `NOTION_WEBSITES_DB_ID`): `URL` (title), `Upcoming Event`
(text), `Events Attended` (number).

The app writes to Notion only from `backend/time_tracker.py` (time spent, last occurred,
sessions) and the scout (`Upcoming Event`), and only after you approved the block.

## Tuning the planner

The prompts are plain files: `system_prompt.md` (weekly plan), `placement_prompt.md`
(fitting one new item), `scout_prompt.md` (events). Write your standing wishes in
`preferences.md` ("Keep Friday evenings free."). The app learns from your approvals and
rejections into `preferences.learned.md` (git-ignored).

## Running it on a Raspberry Pi

See [`deploy/README.md`](deploy/README.md): one systemd service, `deploy/deploy.sh` to
update, an optional daily data backup to a private repo of your own
(`CALENDAR_DATA_REMOTE=git@github.com:you/calendar_optimizer-data.git ./deploy/backup-data.sh`
the first time). Run the backend on one host at a time, or jobs and emails happen twice.

## Using it with Claude Code

`CLAUDE.md` describes the architecture for Claude Code. Put notes about your own setup
(hosts, accounts) in `CLAUDE.local.md`; Claude Code reads it too.

## License

MIT, see [LICENSE](LICENSE).
