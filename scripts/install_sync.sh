#!/bin/bash
# Copies this machine's raw netmon files to shared storage every minute, for
# a converter elsewhere to load (scripts/install_converter.sh).
#
#   scripts/install_sync.sh RAW_ROOT
#
# Files go to RAW_ROOT/$(hostname)/, so each host gets its own folder. RAW_ROOT
# is usually an NFS mount on a NAS; the timer skips runs while it is unmounted.
# RAW_ROOT's parent folder must exist, and no part of the path be a symlink.
set -eu

if [ $# -ne 1 ]; then
  sed -n '2,9s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
# The parent must exist, so that an unmounted share stops this rather than
# sync to the folder beneath it; add_netmon_user.sh makes the rest. -s keeps
# symlinks for add_netmon_user.sh to refuse.
parent=$(dirname -- "$1")
if ! root=$(realpath -e -s -- "$parent" 2>/dev/null); then
  echo "$parent doesn't exist; is the share mounted?" >&2
  exit 1
fi
host=$(hostname)
dest=${root%/}/$(basename -- "$1")/$host
# RequiresMountsFor= below splits on spaces and expands %.
case "$dest" in
  *[[:space:]%]*)
    echo "RAW_ROOT can't contain spaces or %" >&2
    exit 2
    ;;
esac
cd "$(dirname "$0")/.."

# Rows carry the host name netmon read when it started, and the converter
# goes by that, not by folder: files captured under another name would load
# as that host's. Only files it would load (its window is 7 days) matter,
# and a file's first row shows its name.
others=$(sudo find /var/lib/netmon/raw -name '*.jsonl' -mtime -8 -exec awk \
  -v h="\"host\":\"$host\"" 'FNR == 1 { if (!index($0, h)) print FILENAME; nextfile }' \
  {} + 2>/dev/null | head -3)
if [ -n "$others" ]; then
  echo "recent raw files captured under another host name, which would load as that host's:" >&2
  echo "$others" >&2
  echo "after renaming a Pi, restart netmon, move the old files out of" \
    "/var/lib/netmon/raw, and re-run this" >&2
  exit 1
fi

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

# Claim DEST for this machine (scripts/sync_id.sh), unless another one has.
sudo install -D -m 755 scripts/sync_id.sh /opt/netmon/sync_id.sh
sudo rm -f /var/lib/netmon/sync-id # an older installer's id, now unused
status=0
msg=$(sudo -u netmon /opt/netmon/sync_id.sh "$dest" 2>&1) || status=$?
case $status in
  0) ;;
  3)
    # Written beside it and renamed into place, which replaces a link
    # planted meanwhile rather than following it.
    tmp=$(sudo -u netmon mktemp "$dest/.netmon-sync-id.XXXXXX")
    /opt/netmon/sync_id.sh | sudo -u netmon tee "$tmp" >/dev/null
    sudo -u netmon mv -T -- "$tmp" "$dest/.netmon-sync-id"
    ;;
  *)
    echo "$msg" >&2
    exit 1
    ;;
esac

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
