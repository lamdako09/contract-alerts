#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu server (run as the ubuntu user). secrets.json must already be in ~/.
set -euo pipefail
sudo apt-get update -qq
sudo apt-get install -y -qq git python3
if [ -d ~/contract-alerts/.git ]; then
  git -C ~/contract-alerts pull -q --ff-only
else
  git clone -q https://github.com/lamdako09/contract-alerts.git ~/contract-alerts
fi
mv -f ~/secrets.json ~/contract-alerts/secrets.json 2>/dev/null || true
chmod 600 ~/contract-alerts/secrets.json
sudo cp ~/contract-alerts/deploy/contract-alerts.service /etc/systemd/system/contract-alerts.service
sudo systemctl daemon-reload
sudo systemctl enable --now contract-alerts
sudo systemctl restart contract-alerts
systemctl --no-pager status contract-alerts | head -5
