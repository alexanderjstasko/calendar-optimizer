#!/usr/bin/env bash
# Rebuild backend/events.db (and preferences.learned.md) from the
# calendar_optimizer-data backup repo. Run on the Pi:
#
#     ./deploy/restore-data.sh            # latest backup
#     ./deploy/restore-data.sh <commit>   # an older night (git log in ~/calendar_optimizer-data)
#
# The current database is kept as backend/events.db.before-restore-<time>.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${CALENDAR_DATA_DIR:-$HOME/calendar_optimizer-data}"
# Your own private repo, e.g. git@github.com:you/calendar_optimizer-data.git; only
# needed for the first clone (an existing clone keeps its origin).
REMOTE="${CALENDAR_DATA_REMOTE:-}"
REV="${1:-origin/main}"

[ -d "$DATA/.git" ] || [ -n "$REMOTE" ] || { echo "set CALENDAR_DATA_REMOTE to your private backup repo (first run only)" >&2; exit 1; }
[ -d "$DATA/.git" ] || git clone -q "$REMOTE" "$DATA"
git -C "$DATA" fetch -q origin
echo "Restoring from $(git -C "$DATA" log -1 --format='%h %s' "$REV")"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git -C "$DATA" show "$REV:events.sql" > "$tmp/events.sql"
sqlite3 "$tmp/events.db" < "$tmp/events.sql"
[ "$(sqlite3 "$tmp/events.db" 'pragma integrity_check')" = "ok" ] || { echo "rebuilt database failed integrity_check" >&2; exit 1; }
echo "Rebuilt: $(sqlite3 "$tmp/events.db" 'select count(*) from events') events, $(sqlite3 "$tmp/events.db" 'select count(*) from proposed_events') proposals"

sudo systemctl stop calendar-optimizer
stamp="$(date +%Y%m%d-%H%M%S)"
[ -f "$REPO/backend/events.db" ] && mv "$REPO/backend/events.db" "$REPO/backend/events.db.before-restore-$stamp"
rm -f "$REPO/backend/events.db-wal" "$REPO/backend/events.db-shm"
cp "$tmp/events.db" "$REPO/backend/events.db"
if git -C "$DATA" cat-file -e "$REV:preferences.learned.md" 2>/dev/null; then
  git -C "$DATA" show "$REV:preferences.learned.md" > "$REPO/preferences.learned.md"
fi
sudo systemctl start calendar-optimizer
echo "Restored. Previous database kept as backend/events.db.before-restore-$stamp"
