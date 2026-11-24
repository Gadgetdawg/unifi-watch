#!/usr/bin/env bash
# Runs ON the watcher host (Hermes Mac mini). Installs/refreshes the launchd job.
# Idempotent: safe to rerun after every deploy.
set -euo pipefail
APP="$HOME/unifi-watch"
PY="$(command -v python3)"
PLIST="$HOME/Library/LaunchAgents/com.herrin.unifi-watch.plist"
LABEL="com.herrin.unifi-watch"

cd "$APP"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs" state

if [[ ! -f config.env ]]; then
  cp config.example.env config.env
  chmod 600 config.env
  echo "!! Created $APP/config.env from the template. Fill in UI_API_KEY and the Telegram"
  echo "!! values, then rerun this script. The watcher is NOT started yet."
  exit 2
fi

echo "== offline tests"
"$PY" -m unittest discover -s tests

echo "== one live poll (read-only)"
"$PY" unifi_watch.py --once

sed -e "s#__HOME__#$HOME#g" -e "s#__PY__#$PY#g" deploy/com.herrin.unifi-watch.plist > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
echo "== installed and running: $LABEL"
echo "   logs:      $HOME/Library/Logs/unifi-watch.log"
echo "   dashboard: $APP/state/status.html"
