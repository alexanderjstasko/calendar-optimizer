#!/usr/bin/env bash
# One-time setup on an always-on Linux machine (Debian, Ubuntu, Raspberry Pi
# OS; x86 or ARM). Run from the repo root after cloning and filling in .env:
#
#     ./deploy/setup-linux.sh
#
# Safe to re-run: it only installs what is missing, then (re)installs and
# restarts the systemd service. Day-to-day updates use deploy/deploy.sh.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
say() { printf '\n== %s\n' "$*"; }

say "System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl sqlite3 python3 python3-venv python3-pip

python3 - <<'EOF'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11+ is required, found {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]} OK")
EOF

# Vite 8 needs Node 20.19+ or 22.12+; Debian's packaged node is older.
node_ok() { command -v node >/dev/null && node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=12)||(a===20&&b>=19)?0:1)'; }
if ! node_ok; then
  say "Node.js 22 (NodeSource)"
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y -qq nodejs
fi
# Debian ships npm as its own package; a node from apt (e.g. installed for
# MagicMirror) may come without it.
if ! command -v npm >/dev/null; then
  say "npm"
  sudo apt-get install -y -qq npm
fi
echo "Node $(node --version), npm $(npm --version) OK"

say "Secrets and config"
if [ ! -f .env ]; then
  echo "MISSING .env -- copy .env.example to .env and fill it in (see README.md), then re-run." >&2
  exit 1
fi
echo "found   .env"
for f in backend/google_credentials.json backend/google_token.json; do
  if [ -f "$f" ]; then echo "found   $f"; else echo "skip    $f (Google Calendar not set up; that is fine)"; fi
done
if ! command -v "$HOME/.local/bin/claude" >/dev/null && ! command -v claude >/dev/null; then
  echo "note: no 'claude' CLI found; LLM_BACKEND=cli needs it (see deploy/README.md)."
fi

say "Python venv + backend dependencies"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r backend/requirements.txt

say "Calendar app (production build)"
(cd frontend && npm ci --no-audit --no-fund && npm run build)

say "systemd service"
sed -e "s|@USER@|$USER|g" -e "s|@HOME@|$HOME|g" -e "s|@REPO@|$REPO|g" \
  deploy/calendar-optimizer.service | sudo tee /etc/systemd/system/calendar-optimizer.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable calendar-optimizer
sudo systemctl restart calendar-optimizer

# Optional: a nightly copy of the database to a private git repo of your own
# (deploy/backup-data.sh). Installed when CALENDAR_DATA_REMOTE is set for this
# run or a backup clone already exists.
if [ -n "${CALENDAR_DATA_REMOTE:-}" ] || [ -d "${CALENDAR_DATA_DIR:-$HOME/calendar_optimizer-data}/.git" ]; then
  say "Daily data backup"
  for unit in calendar-backup.service calendar-backup.timer; do
    sed -e "s|@USER@|$USER|g" -e "s|@HOME@|$HOME|g" -e "s|@REPO@|$REPO|g" \
      "deploy/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
  done
  sudo systemctl daemon-reload
  sudo systemctl enable --now calendar-backup.timer
  systemctl list-timers calendar-backup.timer --no-pager | head -2
fi

say "Health check"
for _ in $(seq 1 30); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo "backend is up: http://$(hostname):8000/"
    exit 0
  fi
  sleep 1
done
echo "backend did not answer on :8000 -- check: journalctl -u calendar-optimizer -n 50" >&2
exit 1
