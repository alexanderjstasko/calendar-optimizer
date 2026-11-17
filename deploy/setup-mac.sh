#!/usr/bin/env bash
# One-time setup on an always-on Mac: installs the backend and the built app,
# then a LaunchAgent that starts calendar_optimizer at login and restarts it if
# it stops. Run from the repo root after filling in .env:
#
#     ./deploy/setup-mac.sh
#
# Safe to re-run. Day-to-day updates use deploy/deploy.sh. Logs go to
# ~/Library/Logs/calendar-optimizer.log. The Mac must not sleep (System
# Settings -> Energy: "Prevent automatic sleeping when the display is off").
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
LABEL="com.calendar-optimizer"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/calendar-optimizer.log"
say() { printf '\n== %s\n' "$*"; }

say "Python and Node"
python3 - <<'EOF'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11+ is required, found {sys.version.split()[0]} (install it from python.org or with Homebrew)")
print(f"Python {sys.version.split()[0]} OK")
EOF
node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=12)||(a===20&&b>=19)?0:1)' 2>/dev/null \
  || { echo "Node 20.19+ or 22.12+ is required (nodejs.org or 'brew install node')" >&2; exit 1; }
echo "Node $(node --version) OK"

[ -f .env ] || { echo "MISSING .env -- copy .env.example to .env and fill it in (see README.md), then re-run." >&2; exit 1; }

say "Python venv + backend dependencies"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r backend/requirements.txt

say "Calendar app (production build)"
(cd frontend && npm ci --no-audit --no-fund && npm run build)
git rev-parse HEAD:frontend > frontend/dist/.tree

say "LaunchAgent ($LABEL)"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO/.venv/bin/python3</string>
    <string>-m</string><string>uvicorn</string>
    <string>--app-dir</string><string>backend</string>
    <string>main:app</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

say "Health check"
for _ in $(seq 1 30); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo "backend is up: http://localhost:8000/"
    exit 0
  fi
  sleep 1
done
echo "backend did not answer on :8000 -- check: tail -50 $LOG" >&2
exit 1
