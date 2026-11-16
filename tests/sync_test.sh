#!/bin/bash
# Installs the sync timer (scripts/install_sync.sh) on this machine and checks
# that raw files reach RAW_ROOT/$(hostname)/, new and grown, without deletes,
# copied by the netmon user.
# Needs systemd and sudo; CI runs it.
#
#   tests/sync_test.sh
set -eu
cd "$(dirname "$0")/.."

fail() {
  echo "FAIL: $*" >&2
  exit 1
}
host=$(hostname)
row() { printf '{"ts":%s,"host":"%s"}\n' "$1" "${2:-$host}"; }
raw=/var/lib/netmon/raw
share=/srv/netmon-sync-test
dest=$share/$host
sudo mkdir -p "$raw/2026-01-01" "$dest/2026-01-01"
row 1 | sudo tee "$raw/2026-01-01/00.jsonl" >/dev/null
# A shorter copy a root sync left, as before the sync ran as netmon, which
# must take it over to replace it.
echo '{}' | sudo tee "$dest/2026-01-01/00.jsonl" >/dev/null

# The uid a NAS would know the share's owner by.
NETMON_UID=4321 scripts/install_sync.sh "$share" >/dev/null
check() {
  sudo cmp -s "$raw/2026-01-01/$1" "$dest/2026-01-01/$1" || fail "$2"
  echo "ok  : $2"
}
check 00.jsonl "first copy"
[ "$(systemctl show -p User --value netmon-sync.service)" = netmon ] \
  || fail "the sync doesn't run as netmon"
[ -z "$(sudo find "$dest" ! -user netmon)" ] || fail "copies not owned by netmon"
[ "$(id -u netmon)" = 4321 ] || fail "netmon didn't get NETMON_UID"
echo "ok  : runs as netmon, with NETMON_UID"
if NETMON_UID=4322 scripts/add_netmon_user.sh 2>/dev/null; then
  fail "netmon kept its uid for another NETMON_UID, unnoticed"
fi
echo "ok  : stops for another NETMON_UID"
sudo mkdir -p "/srv/netmon sync"
if scripts/install_sync.sh "/srv/netmon sync/raw" 2>/dev/null; then
  fail "took a RAW_ROOT with a space, which systemd would split"
fi
echo "ok  : stops for a RAW_ROOT with a space"

# Links someone else planted on the share, to local files: the installer runs
# as root, so it must change the links, never what they point at.
victim=/srv/netmon-victim
sudo install -d -m 755 "$victim.d"
echo secret | sudo tee "$victim" >/dev/null
sudo chmod 640 "$victim"
sudo ln -s "$victim" "$dest/2026-01-01/planted.jsonl"
sudo ln -s "$victim.d" "$dest/planted"
sudo chown -h 5555:5555 "$dest/2026-01-01/planted.jsonl" "$dest/planted"
status=0
NETMON_UID=4321 scripts/install_sync.sh "$share" >/dev/null 2>&1 || status=$?
[ "$(sudo stat -c %U:%G "$victim" "$victim.d" | sort -u)" = root:root ] \
  || fail "the installer changed the owner of a link's target"
[ $status = 0 ] && [ "$(sudo stat -c %U "$dest/planted")" = netmon ] \
  || fail "the installer left a link netmon doesn't own"
sudo rm "$dest/2026-01-01/planted.jsonl" "$dest/planted"
echo "ok  : leaves the targets of links on the share alone"
linked=/srv/netmon-linked
sudo mkdir -p "$linked"
sudo ln -s "$victim.d" "$linked/$host"
if scripts/install_sync.sh "$linked" 2>/dev/null; then
  fail "took a DEST that is a symlink"
fi
[ "$(sudo stat -c %U "$victim.d")" = root ] || fail "handed a symlinked DEST's target to netmon"
sudo rm -r "$linked" "$victim" "$victim.d"
echo "ok  : stops for a DEST that is a symlink"
# A link further up, the share's netmon folder swapped for a link to a local
# one, which neither the installer nor add_netmon_user.sh may follow.
localdir=/srv/netmon-local
sudo install -d "$localdir/raw/$host" /srv/netmon-lt
echo secret | sudo tee "$localdir/raw/$host/key" >/dev/null
sudo ln -s "$localdir" /srv/netmon-lt/netmon
if scripts/install_sync.sh /srv/netmon-lt/netmon/raw 2>/dev/null; then
  fail "took a RAW_ROOT through a symlink"
fi
[ "$(sudo stat -c %U "$localdir/raw/$host" "$localdir/raw/$host/key" | sort -u)" = root ] \
  || fail "handed a local folder behind a symlink to netmon"
sudo rm -r "$localdir" /srv/netmon-lt
echo "ok  : stops for a RAW_ROOT through a symlink"

# An unmounted share: RAW_ROOT's parent isn't there.
if scripts/install_sync.sh /srv/no-share/netmon/raw 2>/dev/null \
  || sudo test -e "/$host"; then
  fail "synced to /$host for a share that isn't there"
fi
echo "ok  : stops for a RAW_ROOT whose parent doesn't exist"
# A new share, where RAW_ROOT isn't made yet.
sudo mkdir -p /srv/netmon-fresh
NETMON_UID=4321 scripts/install_sync.sh /srv/netmon-fresh/raw >/dev/null \
  || fail "stopped for a RAW_ROOT that isn't made yet"
sudo test -s "/srv/netmon-fresh/raw/$host/2026-01-01/00.jsonl" \
  || fail "no copy in a new RAW_ROOT"
sudo rm -r /srv/netmon-fresh
echo "ok  : makes RAW_ROOT"

# This Pi with a new SD card: no local state, but the same board.
sudo rm -f /etc/default/netmon-sync /var/lib/netmon/sync-id
NETMON_UID=4321 scripts/install_sync.sh "$share" >/dev/null \
  || fail "refused this machine's own DEST after a re-install"
echo "ok  : keeps its DEST after a re-install"

# Another machine with this host name already syncs here.
mark=$dest/.netmon-sync-id
sudo test -s "$mark" || fail "the installer left no sync id in DEST"
ours=$(sudo cat "$mark")
echo other-machine | sudo -u netmon tee "$mark" >/dev/null
if NETMON_UID=4321 scripts/install_sync.sh "$share" >/dev/null 2>&1; then
  fail "took a DEST another machine syncs to"
fi
# A copy of this SD card in another Pi, whose timer runs without the installer.
if sudo systemctl start netmon-sync.service 2>/dev/null; then
  fail "synced to a DEST another machine syncs to"
fi
# A FIFO planted there, which a read would wait on.
sudo -u netmon rm "$mark"
sudo -u netmon mkfifo "$mark"
status=0
NETMON_UID=4321 timeout 60 scripts/install_sync.sh "$share" >/dev/null 2>&1 || status=$?
[ $status = 1 ] || fail "with a FIFO for DEST's id, the installer exited $status"
sudo -u netmon rm "$mark"
echo "$ours" | sudo -u netmon tee "$mark" >/dev/null
echo "ok  : stops for a DEST another machine syncs to"

# Files from before a rename, under the old name (here one that only starts
# with this one), would load as that host's.
row 1 "$host-old" | sudo tee "$raw/2026-01-01/02.jsonl" >/dev/null
if NETMON_UID=4321 scripts/install_sync.sh "$share" >/dev/null 2>&1; then
  fail "took raw files captured under another host name"
fi
sudo rm "$raw/2026-01-01/02.jsonl"
echo "ok  : stops for raw files under another host name"
# Older ones are out of the converter's window.
row 1 "$host-old" | sudo tee "$raw/2026-01-01/02.jsonl" >/dev/null
sudo touch -d '30 days ago' "$raw/2026-01-01/02.jsonl"
NETMON_UID=4321 scripts/install_sync.sh "$share" >/dev/null \
  || fail "stopped for old raw files under another host name"
sudo rm "$raw/2026-01-01/02.jsonl"
echo "ok  : takes old raw files under another host name"

row 2 | sudo tee -a "$raw/2026-01-01/00.jsonl" >/dev/null
row 3 | sudo tee "$raw/2026-01-01/01.jsonl" >/dev/null
sudo systemctl start netmon-sync.service
check 00.jsonl "grown file"
check 01.jsonl "new file"

sudo rm "$raw/2026-01-01/01.jsonl"
sudo systemctl start netmon-sync.service
sudo test -f "$dest/2026-01-01/01.jsonl" || fail "a local delete removed the shared copy"
echo "ok  : never deletes"

# A file root can't hand over, as on a share that squashes root, stops the
# installer, and the sync stays paused until a re-run succeeds.
sudo touch "$dest/stuck"
sudo chattr +i "$dest/stuck"
if NETMON_UID=4321 scripts/install_sync.sh "$share" >/dev/null 2>&1; then
  sudo chattr -i "$dest/stuck"
  fail "installed with a file netmon doesn't own"
fi
sudo chattr -i "$dest/stuck"
if systemctl is-active --quiet netmon-sync.timer \
  || systemctl is-enabled --quiet netmon-sync.timer; then
  fail "the timer runs on, or would at boot, after a failed handover"
fi
echo "ok  : stops for a file netmon doesn't own"
sudo rm "$dest/stuck"
NETMON_UID=4321 scripts/install_sync.sh "$share" >/dev/null
systemctl is-active --quiet netmon-sync.timer || fail "the timer isn't active"
echo "ok  : timer active"
echo PASS
