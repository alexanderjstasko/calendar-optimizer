#!/usr/bin/env bash
# Refuses to publish personal data. Scans every file under DIR (default: this
# repo's tracked files) for
#   - the words in .leak-denylist (repo root, git-ignored; one per line, case-
#     insensitive, # comments; or LEAK_DENYLIST=path): your tailnet, address,
#     email, GitHub user, Notion ids ...
#   - token-shaped strings (Anthropic/Notion/Google keys, private keys, iCloud
#     and Outlook calendar feed links).
# Exit 0 = clean, 1 = something matched (each hit is printed), 2 = usage.
#
#     ./deploy/leak-check.sh                 # this repo
#     ./deploy/leak-check.sh ../public-copy  # an export before it is pushed
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DENYLIST="${LEAK_DENYLIST:-$REPO/.leak-denylist}"
TARGET="${1:-}"
if [ -n "$TARGET" ]; then
  [ -d "$TARGET" ] || { echo "not a directory: $TARGET" >&2; exit 2; }
  TARGET="$(cd "$TARGET" && pwd)"
fi

PATTERNS='sk-ant-[A-Za-z0-9_-]{20,}|ntn_[A-Za-z0-9]{30,}|secret_[A-Za-z0-9]{30,}|AIza[0-9A-Za-z_-]{30,}|GOCSPX-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|webcal:/{2}[A-Za-z0-9-]+\.[A-Za-z0-9.-]+/|p[0-9]+-caldav\.icloud\.com|outlook\.office365\.com/owa/calendar/[A-Za-z0-9]'

files() {
  if [ -n "$TARGET" ]; then
    (cd "$TARGET" && find . -type f -not -path './.git/*' -not -path '*/node_modules/*' -print0)
  else
    (cd "$REPO" && git ls-files -z)
  fi
}
ROOT="${TARGET:-$REPO}"

hits=0
check() {  # grep arguments: how to match and what
  local out
  out="$(cd "$ROOT" && files | xargs -0 grep -nIH "$@" 2>/dev/null || true)"
  if [ -n "$out" ]; then
    echo "$out" | cut -c1-200
    hits=1
  fi
}

check -E -e "$PATTERNS"
if [ -f "$DENYLIST" ]; then
  words="$(grep -vE '^\s*(#|$)' "$DENYLIST" || true)"
  if [ -n "$words" ]; then
    tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
    printf '%s\n' "$words" > "$tmp"
    check -i -F -f "$tmp"
  fi
else
  echo "warning: no denylist at $DENYLIST; only token patterns were checked" >&2
fi

if [ "$hits" -ne 0 ]; then
  echo "leak-check: personal data or secrets found above; nothing should be published" >&2
  exit 1
fi
echo "leak-check: clean ($ROOT)"
