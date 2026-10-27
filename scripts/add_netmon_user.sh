#!/bin/bash
# Creates the netmon user, which the sync and the converter run as, so that
# neither needs root. Called by the installers; does nothing if it exists.
#
#   [NETMON_UID=UID] scripts/add_netmon_user.sh
#
# On an NFS share that maps root to a guest (root squash), the NAS checks the
# uid a client sends, so give netmon the uid of the NAS user that owns the
# share. Without NETMON_UID, netmon gets a system uid.
set -eu

if [ $# -ne 0 ]; then
  sed -n '2,10s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
if id netmon >/dev/null 2>&1; then
  exit 0
fi
sudo useradd --system ${NETMON_UID:+--uid "$NETMON_UID"} --gid users \
  --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin netmon
id netmon
