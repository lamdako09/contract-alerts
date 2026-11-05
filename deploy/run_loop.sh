#!/usr/bin/env bash
# Runs both watchers forever on the server:
#   - SEC 8-K feed every ~60 seconds
#   - USAspending as soon as it publishes new data (one cheap "last updated" check per loop)
# Pulls code updates from GitHub about once an hour.
cd "$(dirname "$0")/.." || exit 1
i=0
while true; do
  if (( i % 60 == 0 )); then
    git pull -q --ff-only || echo "git pull failed; keeping current code"
  fi
  python3 announcement_alerts.py || echo "announcement_alerts.py failed"
  python3 contract_alerts.py --when-updated || echo "contract_alerts.py failed"
  i=$((i + 1))
  sleep 60
done
