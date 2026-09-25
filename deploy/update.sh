#!/usr/bin/env bash
# Pull the latest code and restart everything.
#   ./deploy/update.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

before="$(git rev-parse --short HEAD)"
if ! git pull --ff-only; then
  echo
  echo "git pull failed. If it mentions local changes: 'git status' shows them;"
  echo "'git stash' sets them aside. config.toml is never affected."
  exit 1
fi
after="$(git rev-parse --short HEAD)"
echo "code: $before -> $after"

"$REPO/deploy/install.sh" --quick "$@"

# Restart the browser so it loads the new UI (kiosk.sh relaunches it).
pkill -f beacon-kiosk-profile || true
