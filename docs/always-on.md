# Keeping it running

The app is one process: a web server on port 8000 that also runs the scheduled jobs
(calendar syncs, the weekly plan, the scout, writing time back to Notion). Run it on
**one** computer only; two copies would send every email and Notion write twice.

## Setup

From the project folder, after `.env` is filled in:

| Computer | Command | What it installs |
|---|---|---|
| Mac | `./deploy/setup-mac.sh` | A LaunchAgent: starts at login, restarts if it stops |
| Linux (Debian, Ubuntu, Raspberry Pi OS) | `./deploy/setup-linux.sh` | A systemd service: starts at boot, restarts if it stops |

Both are safe to run again. The Linux script also installs Python, git and Node 22 if
they are missing (it uses `sudo`).

**A Mac must stay awake:** System Settings → Energy → **Prevent automatic sleeping when
the display is off**. A laptop with the lid closed sleeps unless it is plugged into a
display.

## Updating

```bash
./deploy/deploy.sh          # add --test to run the tests before restarting
```

It pulls the newest version, reinstalls dependencies only when they changed, rebuilds
the calendar app when needed, restarts the service and waits until it answers. It
refuses to run if you edited tracked files (your `.env` and data are never touched).

## Checking on it

| | Mac | Linux |
|---|---|---|
| Is it running? | `launchctl print gui/$(id -u)/com.calendar-optimizer \| head` | `systemctl status calendar-optimizer` |
| Logs | `tail -f ~/Library/Logs/calendar-optimizer.log` | `journalctl -u calendar-optimizer -f` |
| Restart | `launchctl kickstart -k gui/$(id -u)/com.calendar-optimizer` | `sudo systemctl restart calendar-optimizer` |

From any machine: `curl -s localhost:8000/health` answers `{"status":"ok"}`, and
`curl -s localhost:8000/schedule` shows what runs when.

## Backups

Everything the app learns lives in `backend/events.db` (and `preferences.learned.md`).
`deploy/backup-data.sh` exports both as text to a **private** git repository of your
own, committing only when something changed. No secrets are included.

```bash
CALENDAR_DATA_REMOTE=git@github.com:<you>/calendar_optimizer-data.git ./deploy/backup-data.sh   # first time
./deploy/backup-data.sh             # afterwards
./deploy/restore-data.sh            # restore the latest backup
./deploy/restore-data.sh <commit>   # restore an older one
```

On Linux, `setup-linux.sh` installs a daily 08:00 timer for it when
`CALENDAR_DATA_REMOTE` is set (or a backup clone exists). On a Mac, run it by hand or
add it to your crontab (`crontab -e`, then
`0 8 * * * cd /path/to/calendar-optimizer && ./deploy/backup-data.sh`).

## Notes for Linux

- `MemoryMax=1G` in the service keeps a stuck Claude call from taking the machine
  down; raise it in `deploy/calendar-optimizer.service` if you need to, then re-run
  `setup-linux.sh`.
- With `LLM_BACKEND=cli`, put the `claude` tool in `~/.local/bin` (the installer's
  default); the service looks there.
