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
# On a line of its own, so set -e stops at a missing RAW_ROOT (an unmounted
# share) rather than sync to /HOSTNAME. Symlinks stay for add_netmon_user.sh
# to refuse.
root=$(realpath -e -s -- "$1")
dest=$root/$(hostname)
# RequiresMountsFor= below splits on spaces and expands %.
case "$dest" in
  *[[:space:]%]*)
    echo "RAW_ROOT can't contain spaces or %" >&2
    exit 2
    ;;
esac
cd "$(dirname "$0")/.."

# The sync runs as netmon, which must own DEST (README.md). Pause it, even
# across a reboot, and let a copy in progress finish, so nothing writes
# there as root during the handover; the timer starts again below.
sudo systemctl disable --now netmon-sync.timer 2>/dev/null || true
while systemctl show -p ActiveState --value netmon-sync.service \
  | grep -qxE 'activating|deactivating'; do
  echo "waiting for netmon-sync.service to finish"
  sleep 2
done
scripts/add_netmon_user.sh "$dest"

# Two machines with one host name (Raspberry Pi OS calls every Pi
# raspberrypi) would overwrite each other's files. Each keeps an id, and
# DEST keeps the first one's.
id_file=/var/lib/netmon/sync-id
if ! sudo test -s $id_file; then
  sudo install -d /var/lib/netmon
  cat /proc/sys/kernel/random/uuid | sudo tee $id_file >/dev/null
fi
id=$(sudo cat $id_file)
mark=$dest/.netmon-sync-id
theirs=$(sudo -u netmon cat "$mark" 2>/dev/null || true)
if [ -n "$theirs" ] && [ "$theirs" != "$id" ]; then
  echo "another machine already syncs to $dest: give this one its own host name" >&2
  exit 1
fi
echo "$id" | sudo -u netmon tee "$mark" >/dev/null

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
