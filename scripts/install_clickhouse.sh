#!/bin/bash
# Installs ClickHouse with a netmon.traffic view over the Iceberg table that
# the converter writes (scripts/install_converter.sh).
#
#   scripts/install_clickhouse.sh WAREHOUSE
#
# ClickHouse releases need ARMv8.2 (Pi 5) and come from its apt repo. On
# ARMv8.0 (Pi 4) they die with SIGILL, so this installs the nightly ARMv8.0
# build instead; it has no releases, and re-running upgrades it. Re-run to
# move the warehouse.
set -eu

if [ $# -ne 1 ]; then
  sed -n '2,10s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
warehouse=$(realpath "$1")
cd "$(dirname "$0")/.."

if [ "$(uname -m)" != aarch64 ]; then
  echo "only aarch64 is supported" >&2
  exit 1
fi

if grep -qw asimddp /proc/cpuinfo && grep -qw atomics /proc/cpuinfo; then
  echo "== Installing the LTS release from packages.clickhouse.com"
  key=/usr/share/keyrings/clickhouse-keyring.gpg
  curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key \
    | sudo gpg --batch --yes --dearmor -o "$key"
  echo "deb [signed-by=$key arch=arm64] https://packages.clickhouse.com/deb lts main" \
    | sudo tee /etc/apt/sources.list.d/clickhouse.list >/dev/null
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y clickhouse-server clickhouse-client
else
  url=https://builds.clickhouse.com/master/aarch64v80compat/clickhouse
  echo "== Downloading the nightly ARMv8.0 build, $url"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/clickhouse" "$url"
  chmod +x "$tmp/clickhouse"
  echo "sha512 $(sha512sum "$tmp/clickhouse" | cut -d' ' -f1)"
  sudo systemctl stop clickhouse-server 2>/dev/null || true
  (cd "$tmp" && sudo ./clickhouse install --noninteractive >/dev/null)
  sudo install -m 644 systemd/clickhouse-server.service /etc/systemd/system/
fi
clickhouse-client --version

echo "== Configuring"
sudo install -m 644 clickhouse/config.d/netmon.xml /etc/clickhouse-server/config.d/
sudo install -m 644 clickhouse/users.d/grafana.xml /etc/clickhouse-server/users.d/
cat <<XML | sudo tee /etc/clickhouse-server/config.d/netmon-warehouse.xml >/dev/null
<!-- Written by install_clickhouse.sh: icebergLocal() reads only here. -->
<clickhouse>
    <user_files_path>$warehouse/</user_files_path>
</clickhouse>
XML

echo "== Starting the service"
sudo systemctl daemon-reload
sudo systemctl enable clickhouse-server
sudo systemctl restart clickhouse-server
for _ in $(seq 30); do
  clickhouse-client -q 'SELECT 1' >/dev/null 2>&1 && break
  sleep 2
done
# netmon.traffic reads the Iceberg table, so each query sees its latest
# snapshot.
clickhouse-client --multiquery <<SQL
CREATE DATABASE IF NOT EXISTS netmon;
CREATE OR REPLACE VIEW netmon.traffic AS
SELECT * FROM icebergLocal('$warehouse/netmon/traffic');
SQL
clickhouse-client --user grafana -q \
  'SELECT host, count() AS rows, max(ts) AS latest FROM netmon.traffic GROUP BY host'
