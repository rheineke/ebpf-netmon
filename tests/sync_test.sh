#!/bin/bash
# Installs the sync timer (scripts/install_sync.sh) on this machine and checks
# that raw files reach RAW_ROOT/$(hostname)/, new and grown, without deletes,
# copied by the netmon user.
# Needs systemd and sudo; CI runs it.
#
#   tests/sync_test.sh
set -eu
cd "$(dirname "$0")/.."

raw=/var/lib/netmon/raw
share=/srv/netmon-sync-test
dest=$share/$(hostname)
sudo mkdir -p "$raw/2026-01-01" "$share"
echo '{"ts":1}' | sudo tee "$raw/2026-01-01/00.jsonl" >/dev/null

# The uid a NAS would know the share's owner by.
NETMON_UID=4321 scripts/install_sync.sh "$share" >/dev/null
check() {
  if ! sudo cmp -s "$raw/2026-01-01/$1" "$dest/2026-01-01/$1"; then
    echo "FAIL: $2" >&2
    exit 1
  fi
  echo "ok  : $2"
}
check 00.jsonl "first copy"
if [ "$(systemctl show -p User --value netmon-sync.service)" != netmon ]; then
  echo "FAIL: the sync doesn't run as netmon" >&2
  exit 1
fi
if [ -n "$(sudo find "$dest" ! -user netmon)" ]; then
  echo "FAIL: copies not owned by netmon" >&2
  exit 1
fi
if [ "$(id -u netmon)" != 4321 ]; then
  echo "FAIL: netmon didn't get NETMON_UID" >&2
  exit 1
fi
echo "ok  : runs as netmon, with NETMON_UID"

echo '{"ts":2}' | sudo tee -a "$raw/2026-01-01/00.jsonl" >/dev/null
echo '{"ts":3}' | sudo tee "$raw/2026-01-01/01.jsonl" >/dev/null
sudo systemctl start netmon-sync.service
check 00.jsonl "grown file"
check 01.jsonl "new file"

sudo rm "$raw/2026-01-01/01.jsonl"
sudo systemctl start netmon-sync.service
if ! sudo test -f "$dest/2026-01-01/01.jsonl"; then
  echo "FAIL: a local delete removed the shared copy" >&2
  exit 1
fi
echo "ok  : never deletes"
if ! systemctl is-active --quiet netmon-sync.timer; then
  echo "FAIL: the timer isn't active" >&2
  exit 1
fi
echo "ok  : timer active"
echo PASS
