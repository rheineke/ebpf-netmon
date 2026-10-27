#!/bin/bash
# Installs the hourly raw-to-Iceberg converter on a machine already running
# the netmon service (scripts/install_service.sh).
#
#   scripts/install_converter.sh WAREHOUSE [RAW [CATALOG]]
#
# WAREHOUSE is where the Iceberg table goes, e.g. an NFS mount on a NAS.
# RAW holds the raw files, by default this machine's /var/lib/netmon/raw; to
# load several hosts, point it at the folder scripts/install_sync.sh copies
# them to. CATALOG is a SQLAlchemy URI for the catalog (see
# scripts/install_postgres.sh), and '' is a SQLite file in WAREHOUSE. Left
# out, RAW and CATALOG stay as installed. Run one converter per warehouse.
# Re-run to upgrade or move it.
set -eu

if [ $# -lt 1 ] || [ $# -gt 3 ]; then
  sed -n '2,13s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
installed() { sed -n "s/^$1=//p" /etc/default/netmon-iceberg 2>/dev/null || true; }
warehouse=$1
if [ $# -ge 2 ] && [ -n "$2" ]; then
  raw=$2
else
  raw=$(installed RAW)
  raw=${raw:-/var/lib/netmon/raw}
fi
if [ $# -eq 3 ]; then
  catalog=$3
else
  catalog=$(installed CATALOG)
fi
echo "== RAW=$raw CATALOG=${catalog:-(SQLite in WAREHOUSE)}"

# The converter runs as netmon, which must be able to write WAREHOUSE. Root
# can hand it over on local disk or a share without root squash; on a
# root-squashed share, the NAS user with netmon's uid must own it already
# (README.md).
"$(dirname "$0")/add_netmon_user.sh"
sudo install -d -o netmon -g users "$warehouse" 2>/dev/null \
  || sudo -u netmon mkdir -p "$warehouse"
sudo chown -R netmon:users "$warehouse" 2>/dev/null || true
if ! sudo -u netmon test -w "$warehouse"; then
  echo "netmon (uid $(id -u netmon)) can't write $warehouse; see README.md" >&2
  exit 1
fi
if ! sudo -u netmon test -r "$raw" -a -x "$raw"; then
  echo "netmon (uid $(id -u netmon)) can't read $raw" >&2
  exit 1
fi
# Earlier versions ran as root and kept the run marker in /var/lib/netmon.
old_marker=/var/lib/netmon/iceberg-run.json
if sudo test -f $old_marker; then
  sudo install -d -o netmon -g users /var/lib/netmon-iceberg
  sudo install -o netmon -g users -m 644 $old_marker /var/lib/netmon-iceberg/
  sudo rm $old_marker
fi
cd "$(dirname "$0")/.."

uv_version=0.13.5
if ! uv --version 2>/dev/null | grep -q "^uv $uv_version"; then
  echo "== Installing uv $uv_version to /usr/local/bin"
  tarball=uv-$(uname -m)-unknown-linux-gnu.tar.gz
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  base=https://github.com/astral-sh/uv/releases/download/$uv_version
  curl -fsSL -o "$tmp/$tarball" "$base/$tarball"
  curl -fsSL -o "$tmp/$tarball.sha256" "$base/$tarball.sha256"
  (cd "$tmp" && sha256sum -c "$tarball.sha256")
  tar -xzf "$tmp/$tarball" -C "$tmp"
  sudo install -m 755 "$tmp/${tarball%.tar.gz}/uv" /usr/local/bin/uv
fi

echo "== Installing Python packages from uv.lock in /opt/netmon/venv"
# The system Python, so apt keeps it patched.
sudo UV_PROJECT_ENVIRONMENT=/opt/netmon/venv UV_PYTHON_DOWNLOADS=never \
  /usr/local/bin/uv sync --locked --no-dev --no-install-project \
  --python /usr/bin/python3
sudo install -m 644 tools/to_iceberg.py /opt/netmon/to_iceberg.py

echo "== Installing the hourly and weekly (maintenance) timers"
printf 'WAREHOUSE=%s\nRAW=%s\nCATALOG=%s\n' "$warehouse" "$raw" "$catalog" \
  | sudo tee /etc/default/netmon-iceberg >/dev/null
for unit in netmon-iceberg netmon-iceberg-maintain; do
  sudo install -m 644 "systemd/$unit.service" "systemd/$unit.timer" \
    /etc/systemd/system/
  # If WAREHOUSE or RAW is on a mount (a NAS), skip runs while it is
  # unmounted rather than write a second warehouse under the mount point.
  sudo mkdir -p "/etc/systemd/system/$unit.service.d"
  printf '[Unit]\nRequiresMountsFor=%s %s\n' "$warehouse" "$raw" \
    | sudo tee "/etc/systemd/system/$unit.service.d/warehouse.conf" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now netmon-iceberg.timer netmon-iceberg-maintain.timer

echo "== First run"
sudo systemctl start netmon-iceberg.service
journalctl -u netmon-iceberg.service -n 3 --no-pager -o cat
