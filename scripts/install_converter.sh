#!/bin/bash
# Installs the hourly raw-to-Iceberg converter on a machine already running
# the netmon service (scripts/install_service.sh).
#
#   scripts/install_converter.sh WAREHOUSE [RAW [CATALOG]]
#
# WAREHOUSE is where the Iceberg table goes, e.g. an NFS mount on a NAS.
# RAW holds the raw files, by default this machine's /var/lib/netmon/raw; to
# load several hosts, point it at the folder scripts/install_sync.sh copies
# them to. CATALOG is a SQLAlchemy URI for the catalog, by default a SQLite
# file in WAREHOUSE (see scripts/install_postgres.sh). Run one converter per
# warehouse. Re-run to upgrade or move it.
set -eu

if [ $# -lt 1 ] || [ $# -gt 3 ]; then
  sed -n '2,13s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
warehouse=$1
raw=${2:-/var/lib/netmon/raw}
catalog=${3:-}
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

echo "== Installing the hourly timer"
printf 'WAREHOUSE=%s\nRAW=%s\nCATALOG=%s\n' "$warehouse" "$raw" "$catalog" \
  | sudo tee /etc/default/netmon-iceberg >/dev/null
sudo install -m 644 systemd/netmon-iceberg.service systemd/netmon-iceberg.timer \
  /etc/systemd/system/
# If WAREHOUSE or RAW is on a mount (a NAS), skip runs while it is unmounted
# rather than write a second warehouse under the empty mount point.
sudo mkdir -p /etc/systemd/system/netmon-iceberg.service.d
printf '[Unit]\nRequiresMountsFor=%s %s\n' "$warehouse" "$raw" \
  | sudo tee /etc/systemd/system/netmon-iceberg.service.d/warehouse.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now netmon-iceberg.timer

echo "== First run"
sudo systemctl start netmon-iceberg.service
journalctl -u netmon-iceberg.service -n 3 --no-pager -o cat
