#!/usr/bin/env bash
# Copies this repo's committed code into a clone of the public repo, minus the
# paths in .public-exclude, and refuses if deploy/leak-check.sh finds anything.
# It never commits or pushes: review the diff in the clone, then commit there.
#
#     ./deploy/export-public.sh ../calendar_optimizer-public
#
# First time: create the empty public repo on GitHub, clone it next to this one,
# then run this. The clone gets a pre-push hook that runs the leak check again.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:?usage: deploy/export-public.sh <clone of the public repo>}"
[ -d "$DEST/.git" ] || { echo "$DEST is not a git clone" >&2; exit 2; }
DEST="$(cd "$DEST" && pwd)"
[ "$DEST" != "$REPO" ] || { echo "the destination is this repo" >&2; exit 2; }

if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
  echo "commit or stash your changes first: only committed code is exported" >&2
  exit 1
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
git -C "$REPO" archive HEAD | tar -x -C "$stage"
if [ -f "$REPO/.public-exclude" ]; then
  grep -vE '^\s*(#|$)' "$REPO/.public-exclude" | while IFS= read -r path; do
    rm -rf "${stage:?}/$path"
  done
fi

"$REPO/deploy/leak-check.sh" "$stage"

# Mirror the staged tree into the clone (its .git stays untouched).
rsync -a --delete --exclude .git "$stage"/ "$DEST"/

hook="$DEST/.git/hooks/pre-push"
cat > "$hook" <<HOOK
#!/usr/bin/env bash
# Installed by export-public.sh: no push while the leak check finds anything.
exec "$REPO/deploy/leak-check.sh" "$DEST"
HOOK
chmod +x "$hook"

echo
echo "Exported $(git -C "$REPO" rev-parse --short HEAD) to $DEST. Changes:"
git -C "$DEST" status --short | head -50
echo
echo "Review, then: cd $DEST && git add -A && git commit -m '...' && git push"
