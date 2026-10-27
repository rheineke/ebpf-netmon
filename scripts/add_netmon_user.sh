#!/bin/bash
# Creates the netmon user, which the sync and the converter run as, so that
# neither needs root, and gives it each DIR. Called by the installers.
#
#   [NETMON_UID=UID] scripts/add_netmon_user.sh [DIR...]
#
# On an NFS share that maps root to a guest (root squash), the NAS checks the
# uid a client sends, so give netmon the uid of the NAS user that owns the
# share. Without NETMON_UID, netmon gets a system uid. If netmon exists with
# another uid, this stops. Its group is users (gid 100), as for NAS users;
# the jobs write folders 755 and files 644, so the group can't write them.
#
# Each DIR is made if missing and handed to netmon while root still can (on
# local disk, or before the NAS squashes root). This stops unless netmon then
# owns everything in it.
set -eu

if id netmon >/dev/null 2>&1; then
  uid=$(id -u netmon)
  if [ -n "${NETMON_UID:-}" ] && [ "$NETMON_UID" != "$uid" ]; then
    echo "netmon has uid $uid, not NETMON_UID=$NETMON_UID: change it (usermod -u)" \
      "or unset NETMON_UID" >&2
    exit 1
  fi
else
  sudo useradd --system ${NETMON_UID:+--uid "$NETMON_UID"} --gid users \
    --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin netmon
  id netmon
fi

for dir in "$@"; do
  sudo install -d -o netmon -g users "$dir" 2>/dev/null \
    || sudo -u netmon mkdir -p "$dir"
  sudo chown -R netmon:users "$dir" 2>/dev/null || true
  other=$(sudo find "$dir" ! -user netmon -print -quit)
  if [ -n "$other" ] || ! sudo -u netmon test -w "$dir"; then
    echo "netmon (uid $(id -u netmon)) doesn't own ${other:-$dir}; see README.md" >&2
    exit 1
  fi
done
