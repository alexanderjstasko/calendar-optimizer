# Running on the Raspberry Pi

Changes are made on a laptop (or on the Pi itself), pushed to GitHub, and pulled by
the Pi. The Pi runs one systemd service: FastAPI on port 8000 serving the API,
the background jobs (calendar sync, scout, optimizer, time tracking) and the
built calendar app. There is no Vite dev server on the Pi.

| URL (over Tailscale)       | What                   |
|----------------------------|------------------------|
| `http://<pi>:8000/`        | calendar app           |
| `http://<pi>:8000/review`  | approvals page         |
| `http://<pi>:8000/schedule`| job cadence + next runs|

## What lives where

- **In git:** code, `system_prompt.md`, `scout_prompt.md`, `preferences.md`
  (your Stated preferences), these deploy files.
- **Only on the Pi, never in git:** `.env`, `backend/google_credentials.json`,
  `backend/google_token.json`, `backend/events.db`, `preferences.learned.md`
  (the system rewrites it from your decisions). `deploy.sh` never touches them.

## First-time setup

1. **Pi OS:** Raspberry Pi OS (64-bit, Bookworm or newer), Tailscale installed
   and logged in (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`).
   Turn off anything like Shields Up; use the MagicDNS name.

2. **Read-only access to the repo:** on the Pi,
   `ssh-keygen -t ed25519 -f ~/.ssh/calendar_deploy -N ""`, add the `.pub` file
   to the GitHub repo under *Settings → Deploy keys* (leave "write" off), then
   ```bash
   printf 'Host github.com\n  IdentityFile ~/.ssh/calendar_deploy\n' >> ~/.ssh/config
   git clone git@github.com:<you>/calendar_optimizer.git ~/calendar_optimizer
   ```

3. **Secrets, copied from the Mac** (from the Mac's repo root):
   ```bash
   scp .env backend/google_credentials.json backend/google_token.json <pi>:calendar_optimizer/
   ssh <pi> 'cd calendar_optimizer && mv google_credentials.json google_token.json backend/'
   ```
   Optional, to keep your history (approvals, learned preferences): stop the
   Mac's backend first, then also copy `backend/events.db` into `backend/` and
   `preferences.learned.md` into the repo root.

4. **Edit `.env` on the Pi** where it names the Mac:
   - `VITE_API_URL=http://<pi-magicdns-name>:8000` (the approval emails link to
     `$VITE_API_URL/review`; or set `NOTIFY_REVIEW_URL` directly)
   - `LLM_CLI_PATH` empty (the service puts `~/.local/bin` on PATH)

5. **Claude CLI** (for `LLM_BACKEND=cli`, the Max subscription): on the Pi run
   `curl -fsSL https://claude.ai/install.sh | bash`. Then, on any machine logged
   in to the subscription, run `claude setup-token` and put the token in the
   Pi's `.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`. Check: `.venv/bin/python3 backend/llm_client.py --check`
   after step 6.

6. **Install and start:**
   ```bash
   cd ~/calendar_optimizer && ./deploy/setup-pi.sh
   ```
   It installs system packages (and Node 22, which Vite needs), creates the
   venv, builds the calendar app, installs `calendar-optimizer.service`, starts
   it and waits for `/health`.

7. **Stop the Mac's backend**, so the two machines don't both run the
   scheduled jobs (double emails, double scout runs, two writers to Notion).

## Day to day

On the Mac: commit and push. On the Pi:

```bash
cd ~/calendar_optimizer && ./deploy/deploy.sh          # add --test to run the tests first
```

It pulls, reinstalls dependencies only if they changed, rebuilds the app if
`frontend/` changed, restarts the service and waits for `/health`. It refuses
to run if tracked files were edited on the Pi; make changes on the Mac.

Useful on the Pi:

```bash
systemctl status calendar-optimizer
journalctl -u calendar-optimizer -f            # live logs
curl -s localhost:8000/schedule                # what runs when
```

## Data backups (calendar_optimizer-data)

Every day at 08:00 `calendar-backup.timer` runs `deploy/backup-data.sh`: it exports
`backend/events.db` as SQL text (`events.sql`) plus `preferences.learned.md` into
`~/calendar_optimizer-data` and pushes to the private `calendar_optimizer-data`
repo, committing only when something changed. No secrets go there. It uses its own
deploy key (`~/.ssh/calendar_data_deploy`, SSH alias `github-data`, write access),
since GitHub allows a deploy key on only one repo.

```bash
./deploy/backup-data.sh                       # back up now
./deploy/restore-data.sh                      # restore the latest backup
./deploy/restore-data.sh <commit>             # restore an older day
systemctl list-timers calendar-backup.timer   # next run
```

