#!/usr/bin/env bash
# Serves the app on http://127.0.0.1:8002 from a throwaway copy filled with a
# made-up week (seed_demo.py), for the docs/ screenshots. The copy has its own
# events.db and an .env with no tokens and every job off, so nothing reaches
# Notion, Claude, your calendars or email. Rebuilt from the committed code and
# the current frontend/dist on every start. Stop it with Ctrl-C.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DEMO="$REPO/tools/screenshots/.demo"
PY="$REPO/.venv/bin/python3"

[ -f "$REPO/frontend/dist/index.html" ] || { echo "build the frontend first: cd frontend && npm run build" >&2; exit 1; }

rm -rf "$DEMO" && mkdir -p "$DEMO"
git -C "$REPO" archive HEAD backend frontend/package.json | tar -x -C "$DEMO"
cp -R "$REPO/frontend/dist" "$DEMO/frontend/dist"
cat > "$DEMO/.env" <<'ENV'
SYNC_CALENDARS_EVERY_MIN=off
SETTLE_EVERY_MIN=off
NOTION_CHECK_EVERY_MIN=off
CONFLICT_CHECK_EVERY_MIN=off
SCOUT_FETCH_DAYS=off
SCOUT_RECOMMEND_DAYS=off
OPTIMIZER_RUN_DAYS=off
NOTION_TOKEN=demo-invalid
NOTION_GOALS_DB_ID=demo
LLM_BACKEND=api
ANTHROPIC_API_KEY=
NOTIFY_EMAIL_TO=
NOTIFY_PUSHOVER=0
GEO_LOOKUPS=0
ENV

# A clean environment: nothing from your shell (tokens, CLAUDE_*) reaches the copy.
run() { env -i HOME="$HOME" PATH=/usr/bin:/bin TZ=America/New_York "$@"; }
(cd "$DEMO/backend" && run "$PY" -I "$REPO/tools/screenshots/seed_demo.py" "$DEMO/backend")
cd "$DEMO"
exec env -i HOME="$HOME" PATH=/usr/bin:/bin TZ=America/New_York \
  "$PY" -m uvicorn --app-dir backend main:app --host 127.0.0.1 --port 8002
