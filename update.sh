#!/bin/bash
# Update Tip Tracker to the latest version on GitHub and restart it.
# Usage (on the server):  bash /mnt/user/appdata/tiptracker/update.sh
# Your data/ folder and .env are never touched.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
# Files copied in over the network are owned by "nobody", which makes git refuse
# to work as root. Trust this one folder (passed each run, so it survives reboots).
git() { command git -c safe.directory="$DIR" "$@"; }

echo "== Fetching the latest version"
git fetch origin
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "!! App files were changed on the server. Not updating, so nothing gets lost."
  git status --short --untracked-files=no
  echo "   To throw those changes away and update anyway:  git -c safe.directory=$DIR reset --hard origin/main"
  exit 1
fi
OLD=$(git rev-parse --short HEAD)
git merge --ff-only origin/main
NEW=$(git rev-parse --short HEAD)
if [ "$OLD" = "$NEW" ] && [ "${1:-}" != "--force" ]; then
  echo "== Already up to date ($NEW). Nothing to do."
  exit 0
fi
echo "== Changes $OLD -> $NEW:"
git log --format='   %s' "$OLD..$NEW" || true
echo "== Rebuilding and restarting"
docker compose up -d --build
sleep 3
docker compose ps
echo "== Done."
