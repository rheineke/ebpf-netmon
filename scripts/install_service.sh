#!/bin/bash
# Installs build/netmon and the systemd service that captures its reports.
# Run scripts/build_on_pi.sh first. Re-run to upgrade.
set -eu

cd "$(dirname "$0")/.."
if [ ! -x build/netmon ]; then
  echo "build/netmon not found; run scripts/build_on_pi.sh first" >&2
  exit 1
fi

sudo install -m 755 build/netmon /usr/local/bin/netmon
sudo install -m 644 systemd/netmon.service /etc/systemd/system/netmon.service
sudo systemctl daemon-reload
sudo systemctl enable netmon.service
sudo systemctl restart netmon.service
systemctl --no-pager status netmon.service | head -5
echo "Reports go to /var/lib/netmon/raw/YYYY-MM-DD/HH.jsonl (UTC)."
