#!/bin/bash
# Installs the hourly raw-to-Iceberg converter on a machine already running
# the netmon service (scripts/install_service.sh).
#
#   scripts/install_converter.sh WAREHOUSE
#
# WAREHOUSE is where the Iceberg table goes, e.g. an NFS mount on a NAS.
# Run one converter per warehouse. Re-run to upgrade or move it.
set -eu

if [ $# -ne 1 ]; then
  sed -n '2,8s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
warehouse=$1
cd "$(dirname "$0")/.."

echo "== Installing Python packages in /opt/netmon/venv"
sudo apt-get install -y --no-install-recommends python3-venv
sudo python3 -m venv /opt/netmon/venv
sudo /opt/netmon/venv/bin/pip install --quiet -r tools/requirements.txt
sudo install -m 644 tools/to_iceberg.py /opt/netmon/to_iceberg.py

echo "== Installing the hourly timer"
echo "WAREHOUSE=$warehouse" | sudo tee /etc/default/netmon-iceberg >/dev/null
sudo install -m 644 systemd/netmon-iceberg.service systemd/netmon-iceberg.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now netmon-iceberg.timer

echo "== First run"
sudo systemctl start netmon-iceberg.service
journalctl -u netmon-iceberg.service -n 3 --no-pager -o cat
