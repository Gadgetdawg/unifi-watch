#!/usr/bin/env bash
# Run FROM the Mac Studio. Pushes this repo to the Hermes Mac mini and (re)installs
# the watcher there. Everything is logged to deploy/last-deploy.log so you don't
# have to copy output anywhere.
#
#   ./deploy/deploy.sh                 # deploy to hermes@hermes-mini
#   HERMES_HOST=hermes@10.x.x.x ./deploy/deploy.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${HERMES_HOST:-hermes@hermes-mini}"
LOG="$HERE/deploy/last-deploy.log"

{
  echo "=== deploy $(date '+%F %T') -> $HOST"
  cd "$HERE"
  git pull --ff-only || echo "(git pull skipped)"
  # Ship code only. Never overwrite the remote config.env or its state/ history.
  rsync -az --delete \
    --exclude '.git' --exclude 'config.env' --exclude 'state/' --exclude 'deploy/last-deploy.log' \
    "$HERE/" "$HOST:unifi-watch/"
  ssh "$HOST" 'bash ~/unifi-watch/deploy/install.sh'
  echo "=== done $(date '+%F %T')"
} 2>&1 | tee "$LOG"
