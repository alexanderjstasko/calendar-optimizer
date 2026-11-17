# Settings (`.env`)

All settings live in `.env` in the project folder (start from `.env.example`). An empty
value means "use the default". Restart after a change (`./deploy/deploy.sh`); after
changing `TIMEZONE`, also rebuild the app (`cd frontend && npm run build`).
`GET /schedule` shows the job times in effect.

Sleep, work, office days, home address, free-time floor, review lengths and the
scout's key words are **not** here: set them in the app's **Hours** tab.

## Basics

| Setting | Default | |
|---|---|---|
| `TIMEZONE` | `America/New_York` | Zone everything is shown and planned in (IANA name) |
| `NOTION_TOKEN` | required | Your Notion integration's secret |
| `NOTION_GOALS_DB_ID` | required | The Telos tree database |
| `NOTION_WEBSITES_DB_ID` | empty = scout off | The Website List database |
| `APPLE_ICS_URL`, `OUTLOOK_ICS_URL`, `PARTIFUL_ICS_URL` | empty = skipped | Private calendar feed links |
| `TAILNET` | empty | Your Tailscale name, e.g. `example-tailnet.ts.net` |
| `NOTIFY_APP_URL` | `http://localhost:8000` | The address you open the app at; email links use it |

Google Calendar has no setting: it is on when `backend/google_credentials.json` exists.

## Claude

| Setting | Default | |
|---|---|---|
| `LLM_BACKEND` | `api` | `api` (API key) or `cli` (subscription through the `claude` tool) |
| `ANTHROPIC_API_KEY` | | For `api` |
| `CLAUDE_CODE_OAUTH_TOKEN` | | For `cli` on an always-on machine (`claude setup-token`) |
| `LLM_MODEL` | `claude-opus-5` | A smaller model (e.g. `claude-sonnet-5-5`) costs less |
| `LLM_EFFORT` | `high` | How hard the model thinks: `low`, `medium`, `high`, `xhigh`, `max` |
| `LLM_MAX_OUTPUT_TOKENS` | `16000` | Upper limit for one plan |
| `LLM_TIMEOUT_S` | `600` | Seconds before a call is given up |
| `LLM_CLI_PATH` | `claude` on PATH | Where the `claude` tool is |
| `LLM_CLI_MAX_BUDGET_USD` | `2` | Spending cap per `cli` call |
| `LLM_API_OUTPUT_MODE` | `tool` | Leave as is (`format` is the alternative) |

## Planning

| Setting | Default | |
|---|---|---|
| `LLM_HOME_LOCATION` | empty | Home address, used only when the Hours tab has none |
| `LLM_DEFAULT_TRAVEL_MIN` | `30` | Travel time to assume for a place away from home |
| `LLM_LEARN_EVERY_DECISIONS` | `5` | Rewrite the learned preferences after this many new decisions |
| `LLM_SYSTEM_PROMPT_PATH`, `LLM_PLACEMENT_PROMPT_PATH`, `LLM_SCOUT_PROMPT_PATH` | the `.md` files in the project folder | Use your own prompt files |

## When jobs run

`*_EVERY_MIN` takes minutes or `off`. `*_DAYS` takes day names (`mon,thu`), a range
(`mon-fri`), `daily` or `off`; `*_TIME` is `HH:MM` in your time zone.

| Setting | Default | Job |
|---|---|---|
| `SYNC_CALENDARS_EVERY_MIN` | `5` | Pull your calendars |
| `CONFLICT_CHECK_EVERY_MIN` | `5` | Spot meetings landing on approved blocks |
| `NOTION_CHECK_EVERY_MIN` | `60` | Place new Notion items |
| `SETTLE_EVERY_MIN` | `15` | Write time spent back to Notion |
| `OPTIMIZER_RUN_DAYS` / `_TIME` | `sun` / `06:30` | The weekly plan |
| `SCOUT_FETCH_DAYS` / `_TIME` | `mon` / `06:00` | Read the Website List sites |
| `SCOUT_RECOMMEND_DAYS` / `_TIME` | `daily` / `07:00` | Pick events |

## Events scout

| Setting | Default | |
|---|---|---|
| `LLM_SCOUT_WINDOW_DAYS` | `30` | How far ahead to look |
| `LLM_SCOUT_SHORTLIST` | `16` | Options ranked per run |
| `LLM_SCOUT_PAGE_SIZE` | `8` | Options shown at once (the rest wait for Reload) |
| `GEO_LOOKUPS` | `1` | `0` turns off the neighbourhood lookups |

## Notifications

| Setting | Default | |
|---|---|---|
| `NOTIFY_EMAIL_TO` | empty = no email | Where the review emails go |
| `NOTIFY_EMAIL_FROM` | `NOTIFY_SMTP_USER` | Sender |
| `NOTIFY_SMTP_HOST` / `_PORT` | `smtp.gmail.com` / `587` | Mail server (465 = SSL) |
| `NOTIFY_SMTP_USER` / `_PASSWORD` | | For Gmail, an [app password](https://myaccount.google.com/apppasswords) |
| `NOTIFY_REVIEW_URL`, `NOTIFY_CALENDAR_URL` | from `NOTIFY_APP_URL` | Override single links |
| `NOTIFY_PUSHOVER`, `PUSHOVER_USER`, `PUSHOVER_TOKEN` | off | Phone push notifications through [Pushover](https://pushover.net) |

## Development only

| Setting | |
|---|---|
| `VITE_API_URL` | Where the Vite dev server (`npm run dev`) calls the API |
| `NOTION_TASKS_DB_ID` | A separate, older Tasks database; read only while the tree has no Task rows |
