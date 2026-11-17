# deploy/

Scripts for running calendar_optimizer on an always-on computer. How to use them:
[docs/always-on.md](../docs/always-on.md).

| File | What it does |
|---|---|
| `setup-mac.sh` | One-time setup on a Mac (LaunchAgent) |
| `setup-linux.sh` | One-time setup on Debian, Ubuntu or Raspberry Pi OS (systemd) |
| `deploy.sh` | Pull the newest version, rebuild what changed, restart |
| `calendar-optimizer.service` | systemd unit template (filled in by `setup-linux.sh`) |
| `backup-data.sh`, `restore-data.sh` | Back up `events.db` to a private git repo, and restore it |
| `calendar-backup.service`, `calendar-backup.timer` | Daily backup on Linux |
| `leak-check.sh`, `export-public.sh` | For maintainers: publish a copy of a private fork without personal data |
| `open-calendar.sh` | Open the app full-screen in Firefox (a wall display) |
