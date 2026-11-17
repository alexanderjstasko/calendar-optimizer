# calendar_optimizer

A self-hosted calendar that merges Google, Apple, Outlook and Partiful into one view,
reads your goals and tasks from Notion, and has Claude plan them into your week. Every
planned block is a proposal: you approve it, move it or reject it before it lands on
your calendar. An optional scout suggests events that fit your free evenings.

It runs on your own computer; your data stays there. Overview with screenshots:
https://alexanderjstasko.github.io/calendar-optimizer/

---

## 1. Requirements

**A computer that stays on.** A Mac or a Linux machine (Debian, Ubuntu, Raspberry Pi
OS; x86 or ARM) that does not sleep: the syncs and the weekly plan run on a schedule.
A laptop works while it is awake. Windows is not supported (WSL may work, untested).

**Software on it:**
- [Python](https://www.python.org/downloads/) 3.11 or newer: `python3 --version`
- [Node.js](https://nodejs.org/) 20.19+ or 22.12+: `node --version`
- [git](https://git-scm.com/downloads): `git --version`

**Accounts:**
- **Notion** (the free plan is enough): your goals, routines and tasks live there.
- **Claude**, one of:
  - an **Anthropic API key** from [console.anthropic.com](https://console.anthropic.com)
    (pay per use; see "Costs" below), or
  - a **Claude Pro or Max subscription**, used through the `claude` command-line tool.

  Only Claude is supported; other AI models are not.
- **Your calendars**, any you use: Google (free Google Cloud project), Apple iCloud,
  Outlook, Partiful. All optional, and none are ever changed by the app.
- **Optional:** [Tailscale](https://tailscale.com) (free) to use it from your phone;
  a Gmail account with an app password for the review emails.

**Costs:** the software is free. Claude is the only running cost: a weekly plan with
the default model is roughly $0.30-$1 on the API, and the daily events scout costs
more than the plan over a week. To spend less, set a smaller model (`LLM_MODEL` in
`.env`) or leave the scout off. With a subscription there is no extra charge.

---

## 2. Download and set up

Run these in a terminal on the computer that will host it.

### 2.1 Get the code and install it

```bash
git clone https://github.com/alexanderjstasko/calendar-optimizer.git
cd calendar-optimizer
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
(cd frontend && npm ci && npm run build)
cp .env.example .env
```

This downloads the app, installs what it needs, builds the calendar app and creates
your settings file, `.env`. Open `.env` in any text editor for the next steps.

### 2.2 Time zone

Set `TIMEZONE` in `.env` to your zone, for example `Europe/London` or
`America/Chicago` ([list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)).
If you leave it out, the app uses **America/New_York**.

### 2.3 Notion

1. Open the **[Notion template](https://parallel-orangutan-22c.notion.site/Calendar-Optimizer-template-41b18723089b809287e2fe52965eee5b)** and click **Duplicate**. You get
   two databases: **Telos tree** (your goals, routines and tasks) and **Website List**
   (sites for the events scout).
2. Create an integration at [notion.so/profile/integrations](https://www.notion.so/profile/integrations)
   → **New integration** (type *Internal*). Copy its secret into `.env` as `NOTION_TOKEN`.
3. Open each of the two databases → **•••** → **Connections** → add your integration.
   (Skipping this gives a "404 not found" error, not a permission error.)
4. Copy each database's ID into `.env`: open the database as a full page; the ID is the
   32 characters in the link before `?v=`.
   - Telos tree → `NOTION_GOALS_DB_ID`
   - Website List → `NOTION_WEBSITES_DB_ID` (leave empty to keep the events scout off)

Replace the example rows with your own. What each column does: [docs/notion.md](docs/notion.md).

### 2.4 Claude

**With an API key:** in `.env` set `LLM_BACKEND=api` and `ANTHROPIC_API_KEY=` your key.

**Or with a Pro/Max subscription:**
```bash
curl -fsSL https://claude.ai/install.sh | bash
claude setup-token
```
Put the token it prints in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`, and set `LLM_BACKEND=cli`.

Check either way (spends nothing):
```bash
.venv/bin/python3 backend/llm_client.py --check
```

### 2.5 Calendars

Set only the ones you use; the rest are skipped.

- **Apple iCloud:** Calendar app → right-click the calendar → **Share Calendar** → tick
  **Public Calendar** → copy the link, change `webcal://` to `https://`, and put it in
  `APPLE_ICS_URL`.
- **Outlook:** Outlook on the web → Settings → Calendar → **Shared calendars** →
  **Publish a calendar** → copy the **ICS** link into `OUTLOOK_ICS_URL`.
- **Partiful:** copy your calendar subscription link into `PARTIFUL_ICS_URL`.
- **Google:** in [Google Cloud](https://console.cloud.google.com/): create a project,
  enable the **Google Calendar API**, set up the OAuth consent screen (add yourself as
  a user, then **Publish app** so the sign-in does not expire weekly), and create an
  **OAuth client ID** of type *Desktop app*. Download its JSON as
  `backend/google_credentials.json`, then sign in once:
  ```bash
  .venv/bin/python3 backend/Google_cal_pull.py
  ```

These links are private: anyone with one can read that calendar. They stay in `.env`.

### 2.6 First run

```bash
.venv/bin/python3 -m uvicorn --app-dir backend main:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000**. Your calendars appear within a minute. Then:

1. **Hours** tab: set when you sleep and work, your office days, your home address and
   how much free time to keep each week. The planner never books over these.
2. **Plan** tab → **Re-plan**: Claude plans the rest of this week (a few minutes).
3. Approve what you like. Approved blocks turn green on the calendar.

Stop it with `Ctrl+C` once that works, and set it up to run on its own.

### 2.7 Keep it running

```bash
./deploy/setup-mac.sh      # on a Mac
./deploy/setup-linux.sh    # on Linux (asks for your password for sudo)
```

Either one starts the app now and again after every restart. On a Mac, also open
**System Settings → Energy** and turn on **Prevent automatic sleeping when the display
is off**. To update to the newest version later: `./deploy/deploy.sh`.
Details, logs and backups: [docs/always-on.md](docs/always-on.md).

### 2.8 On your phone

**Same Wi-Fi:** open `http://<the computer's local IP>:8000` (on a Mac: System Settings
→ Wi-Fi → Details shows it).

**Anywhere, as a home-screen app** (needs Tailscale):
1. Install Tailscale on the computer and on your phone and sign in to both with the
   same account.
2. In the [Tailscale admin console](https://login.tailscale.com/admin/dns), enable
   **MagicDNS** and **HTTPS Certificates**.
3. On the computer: `tailscale serve --bg 8000`
4. On your phone, open `https://<computer name>.<your tailnet>.ts.net` and use
   **Share → Add to Home Screen**. The app then also works offline with the last data
   it saw.
5. In `.env`, set `NOTIFY_APP_URL` to that address (email links) and `TAILNET` to the
   `<your tailnet>.ts.net` part.

### 2.9 Optional extras

- **Review emails** when a new plan is ready: fill in the `NOTIFY_*` settings in `.env`
  (for Gmail: `smtp.gmail.com`, port 587 and an
  [app password](https://myaccount.google.com/apppasswords)).
- **Events scout:** add event listing pages for your city to the Website List (one URL
  per row) and set `NOTION_WEBSITES_DB_ID`. See [docs/events-scout.md](docs/events-scout.md).
- **When jobs run:** the `*_DAYS`, `*_TIME` and `*_EVERY_MIN` settings
  ([docs/settings.md](docs/settings.md)).

After changing `.env`, restart: run `./deploy/deploy.sh`, or press `Ctrl+C` and start
it again. After changing `TIMEZONE`, also rebuild: `(cd frontend && npm run build)`.

---

## 3. Using it

- **The weekly rhythm.** Every Sunday morning Claude plans the coming Monday to Sunday
  around your calendars, hours and free-time floor. You get an email (if set up) and
  review the plan in the **Plan** tab: a recurring item is one card with one switch;
  open it to move or skip a single day. Nothing is booked until you approve it.
- **During the week.** A new item in Notion is placed within the hour, and a meeting
  that lands on an approved block produces a suggested move. Both wait for your approval.
  **Re-plan** in the Plan tab plans the rest of the week at any time.
- **The four tabs.** **Calendar**: everything in one place; tap a block to move or
  delete it, or drag it. **Events**: the scout's suggestions. **Plan**: what to approve.
  **Hours**: sleep, work and planning settings.
- **Tuning.** Write standing wishes in `preferences.md` ("Keep Friday evenings free.").
  The app also learns from what you approve and reject.
- **Notion stays the source.** Edit goals, routines and tasks there. After a block has
  passed, the app writes the time spent and the last session back to Notion.

Something not working? [docs/troubleshooting.md](docs/troubleshooting.md).

---

Using Claude Code on this project? `CLAUDE.md` describes the architecture; put notes
about your own setup in `CLAUDE.local.md`. MIT licensed, see [LICENSE](LICENSE).
