#!/usr/bin/env bash
# Pull the latest main from GitHub and restart the service. Run on the machine
# that hosts it (set up with setup-linux.sh or setup-mac.sh):
#
#     ./deploy/deploy.sh          # pull, install what changed, build, restart
#     ./deploy/deploy.sh --test   # same, but run the backend tests before restarting
#
# Refuses to run with local changes to tracked files (pull would conflict);
# machine state (.env, events.db, tokens, preferences.learned.md) is
# git-ignored and never touched.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
RUN_TESTS=0
[ "${1:-}" = "--test" ] && RUN_TESTS=1

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Local changes to tracked files -- commit (or stash) them first:" >&2
  git status --short --untracked-files=no >&2
  exit 1
fi

before="$(git rev-parse HEAD)"
git pull --ff-only --quiet
after="$(git rev-parse HEAD)"
if [ "$before" = "$after" ]; then
  echo "Already up to date ($(git log -1 --format='%h %s'))."
else
  echo "Updated $(git rev-parse --short "$before")..$(git rev-parse --short "$after"):"
  git log --format='  %h %s' "$before..$after"
fi
# True when the pull changed anything under the given paths.
changed() { [ "$before" != "$after" ] && ! git diff --quiet "$before" "$after" -- "$@"; }

if changed backend/requirements.txt || [ ! -d .venv ]; then
  echo "Installing backend dependencies..."
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q -r backend/requirements.txt
fi

if changed frontend/package.json frontend/package-lock.json || [ ! -d frontend/node_modules ]; then
  echo "Installing frontend dependencies..."
  (cd frontend && npm ci --no-audit --no-fund --silent)
fi
# Rebuild when the committed frontend/ tree differs from the one the current
# build came from (dist/.tree), not only when the pull changed it: commits made
# on the Pi itself make the pull a no-op (2026-10-18: the service restarted on
# a stale build that way).
tree="$(git rev-parse HEAD:frontend)"
if [ ! -d frontend/dist ] || [ "$(cat frontend/dist/.tree 2>/dev/null)" != "$tree" ]; then
  echo "Building the calendar app..."
  (cd frontend && npm run build --silent)
  echo "$tree" > frontend/dist/.tree
fi

if [ "$RUN_TESTS" = 1 ]; then
  echo "Running backend tests..."
  (cd backend && ../.venv/bin/python3 -m pip install -q -r requirements-dev.txt && ../.venv/bin/python3 -m pytest tests -q)
fi

echo "Restarting calendar-optimizer..."
if [ "$(uname)" = "Darwin" ]; then
  launchctl kickstart -k "gui/$(id -u)/com.calendar-optimizer"
  logs="tail -50 ~/Library/Logs/calendar-optimizer.log"
else
  sudo systemctl restart calendar-optimizer
  logs="journalctl -u calendar-optimizer -n 50"
fi
for _ in $(seq 1 30); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo "OK: backend is up at $(git log -1 --format='%h %s')"
    exit 0
  fi
  sleep 1
done
echo "backend did not come back -- check: $logs" >&2
exit 1
