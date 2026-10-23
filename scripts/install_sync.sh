#!/bin/bash
# Copies this machine's raw netmon files to shared storage every minute, for
# a converter elsewhere to load (scripts/install_converter.sh).
#
#   scripts/install_sync.sh RAW_ROOT
#
# Files go to RAW_ROOT/$(hostname)/, so each host gets its own folder. RAW_ROOT
# is usually an NFS mount on a NAS; the timer skips runs while it is unmounted.
set -eu

if [ $# -ne 1 ]; then
  sed -n '2,9s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
dest=$(realpath "$1")/$(hostname)
cd "$(dirname "$0")/.."

sudo apt-get install -y --no-install-recommends rsync
echo "DEST=$dest" | sudo tee /etc/default/netmon-sync >/dev/null
sudo install -m 644 systemd/netmon-sync.service systemd/netmon-sync.timer \
  /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/netmon-sync.service.d
printf '[Unit]\nRequiresMountsFor=%s\n' "$dest" \
  | sudo tee /etc/systemd/system/netmon-sync.service.d/dest.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now netmon-sync.timer

echo "== First copy to $dest"
sudo systemctl start netmon-sync.service
sudo find "$dest" -name '*.jsonl' | wc -l
