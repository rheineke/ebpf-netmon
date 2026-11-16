#!/bin/bash
# Prints this machine's sync id, or checks it against the one in DEST.
#
#   scripts/sync_id.sh [DEST]
#
# Two machines with one host name would sync to one folder and overwrite
# each other's files. So DEST keeps the id of the machine that syncs there,
# in DEST/.netmon-sync-id: scripts/install_sync.sh writes it, and
# netmon-sync.service runs this before each copy. With DEST, this exits 0
# if DEST's id is this machine's, 3 if DEST has none, and 1 otherwise.
#
# The id is a hash of the Pi's board serial number, so a Pi with a new SD
# card keeps its folder while a copy of its card on another Pi doesn't; on
# other machines it hashes /etc/machine-id. The share never sees the serial.
set -eu

serial=/sys/firmware/devicetree/base/serial-number
if [ -e $serial ]; then
  board=$(tr -d '\0' <$serial)
else
  board=$(cat /etc/machine-id)
fi
id=$(printf 'netmon-sync %s' "$board" | sha256sum | cut -c1-32)

if [ $# -eq 0 ]; then
  echo "$id"
  exit 0
fi
if [ $# -ne 1 ]; then
  sed -n '2,14s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
mark=$1/.netmon-sync-id
if [ ! -e "$mark" ] && [ ! -L "$mark" ]; then
  echo "$1 has no sync id; run scripts/install_sync.sh" >&2
  exit 3
fi
# Whoever can write the share could plant a link or a FIFO there.
theirs=
if [ -f "$mark" ] && [ ! -L "$mark" ]; then
  theirs=$(timeout 10 head -c 64 -- "$mark" || true)
fi
if [ "$theirs" != "$id" ]; then
  echo "another machine syncs to $1: give this one its own host name" \
    "(README.md, Several Pis). If the folder is this machine's (on a new" \
    "board, or with an id from an older installer), remove its" \
    ".netmon-sync-id as netmon and re-run scripts/install_sync.sh" >&2
  exit 1
fi
