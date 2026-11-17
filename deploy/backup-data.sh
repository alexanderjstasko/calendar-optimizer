#!/usr/bin/env bash
# Daily (08:00) backup of the Pi's machine state to the private calendar_optimizer-data
# repo on GitHub. Run by calendar-backup.timer; safe to run by hand any time:
#
#     ./deploy/backup-data.sh
#
# What goes in (and nothing else):
#   events.sql              backend/events.db as SQL text (sqlite3 .dump), so git
#                           shows exactly which rows changed and stores only deltas
#   preferences.learned.md  the learned preferences note
# Never: .env, Google credentials/token, the Claude token.
#
# Commits only when something changed, then pushes. Restore: deploy/restore-data.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${CALENDAR_DATA_DIR:-$HOME/calendar_optimizer-data}"
# Your own private repo, e.g. git@github.com:you/calendar_optimizer-data.git; only
# needed for the first clone (an existing clone keeps its origin).
REMOTE="${CALENDAR_DATA_REMOTE:-}"
DB="$REPO/backend/events.db"

[ -d "$DATA/.git" ] || [ -n "$REMOTE" ] || { echo "set CALENDAR_DATA_REMOTE to your private backup repo (first run only)" >&2; exit 1; }
if [ ! -d "$DATA/.git" ]; then
  git clone -q "$REMOTE" "$DATA" 2>/dev/null || { mkdir -p "$DATA" && git -C "$DATA" init -q -b main && git -C "$DATA" remote add origin "$REMOTE"; }
fi
cd "$DATA"
git pull -q --ff-only origin main 2>/dev/null || true  # empty remote on the first run

# .dump runs in one read transaction, so the export is consistent even while
# the backend is writing (WAL mode). Write to a temp file and check it before
# replacing the last good export.
# The backend may be mid-write (a sync every 5 min); wait for the lock like the app does.
sqlite3 -cmd ".timeout 30000" "$DB" ".dump" > events.sql.tmp
if ! grep -q "^COMMIT;" events.sql.tmp || ! grep -q "CREATE TABLE.*proposed_events" events.sql.tmp; then
  echo "export looks incomplete; keeping the previous events.sql" >&2
  rm -f events.sql.tmp
  exit 1
fi
mv events.sql.tmp events.sql
[ -f "$REPO/preferences.learned.md" ] && cp "$REPO/preferences.learned.md" preferences.learned.md

if [ ! -f README.md ]; then
  cat > README.md <<'EOF'
# calendar_optimizer-data

Daily (08:00) backup of the Raspberry Pi's calendar_optimizer state, written by
`deploy/backup-data.sh` in the code repo. No secrets live here.

- `events.sql` -- `backend/events.db` as SQL text (`sqlite3 .dump`)
- `preferences.learned.md` -- the learned scheduling preferences

Restore on the Pi: `~/calendar_optimizer/deploy/restore-data.sh` (optionally
with a commit id to go back to an older night).
EOF
fi

git add -A
if git diff --cached --quiet; then
  echo "backup: nothing changed since the last one"
  exit 0
fi
count() { sqlite3 -cmd ".timeout 30000" "$DB" "select count(*) from $1" 2>/dev/null || echo "?"; }
git commit -q -m "Backup $(date +%F): $(count events) events, $(count proposed_events) proposals, $(count discovered_events) discovered"
git push -q -u origin main
echo "backup: pushed $(git log -1 --format='%h %s')"
