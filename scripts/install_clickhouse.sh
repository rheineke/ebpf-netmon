#!/bin/bash
# Installs ClickHouse with views over the Iceberg tables that the converter
# writes (scripts/install_converter.sh) and over recent raw files.
#
#   scripts/install_clickhouse.sh WAREHOUSE [RAW]
#
# RAW defaults to /var/lib/netmon/raw. ClickHouse may read only under the
# deepest folder holding both, so keep them together (e.g. on one share).
# The converter makes the tables, so run scripts/install_converter.sh
# first (again, after an upgrade that adds one).
# With scripts/install_rest_catalog.sh installed, ClickHouse reads the tables
# through that catalog; otherwise it reads the newest metadata file in
# WAREHOUSE.
#
# ClickHouse releases need ARMv8.2 (Pi 5) and come from its apt repo. On
# ARMv8.0 (Pi 4) they die with SIGILL, so this installs the nightly ARMv8.0
# build instead; it has no releases, and re-running upgrades it. Re-run to
# move the warehouse.
set -eu

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
  sed -n '2,18s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
warehouse=$(realpath "$1")
raw=$(realpath "${2:-/var/lib/netmon/raw}")
# The paths go into SQL strings and a sed replacement below.
if [[ $warehouse$raw =~ [[:space:]\"\'\\\&\|%\<\>\*\?\[\{] ]]; then
  echo "WAREHOUSE and RAW can't contain spaces, quotes or any of \\&|%<>*?[{" >&2
  exit 2
fi
files=$(python3 -c 'import os, sys; print(os.path.commonpath(sys.argv[1:]))' \
  "$warehouse" "$raw")
if [ "$files" = / ]; then
  echo "WAREHOUSE and RAW share no folder below /" >&2
  exit 1
fi
cd "$(dirname "$0")/.."

if [ "$(uname -m)" != aarch64 ]; then
  echo "only aarch64 is supported" >&2
  exit 1
fi

if grep -qw asimddp /proc/cpuinfo && grep -qw atomics /proc/cpuinfo; then
  echo "== Installing the LTS release from packages.clickhouse.com"
  key=/usr/share/keyrings/clickhouse-keyring.gpg
  # Download, then convert: piped, a failed download would still replace
  # the key with an empty one.
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/clickhouse.key" \
    https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key
  sudo gpg --batch --yes --dearmor -o "$key" "$tmp/clickhouse.key"
  echo "deb [signed-by=$key arch=arm64] https://packages.clickhouse.com/deb lts main" \
    | sudo tee /etc/apt/sources.list.d/clickhouse.list >/dev/null
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y clickhouse-server clickhouse-client
else
  url=https://builds.clickhouse.com/master/aarch64v80compat/clickhouse
  echo "== Downloading the nightly ARMv8.0 build, $url"
  # /var/tmp, not /tmp: a Pi's /tmp is in RAM, and the binary unpacks to
  # about 800 MB.
  tmp=$(mktemp -d -p /var/tmp)
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/clickhouse" "$url"
  chmod +x "$tmp/clickhouse"
  echo "sha512 $(sha512sum "$tmp/clickhouse" | cut -d' ' -f1)"
  # Unpack and run it before stopping the running server.
  "$tmp/clickhouse" local -q 'SELECT version()'
  sudo systemctl stop clickhouse-server 2>/dev/null || true
  (cd "$tmp" && sudo ./clickhouse install --noninteractive >/dev/null)
  sudo install -m 644 systemd/clickhouse-server.service /etc/systemd/system/
fi
clickhouse-client --version

echo "== Configuring"
sudo install -m 644 clickhouse/config.d/netmon.xml /etc/clickhouse-server/config.d/
sudo install -m 644 clickhouse/users.d/grafana.xml /etc/clickhouse-server/users.d/
cat <<XML | sudo tee /etc/clickhouse-server/config.d/netmon-warehouse.xml >/dev/null
<!-- Written by install_clickhouse.sh: Iceberg tables and file() read only here. -->
<clickhouse>
    <user_files_path>$files/</user_files_path>
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
rest=/etc/default/netmon-iceberg-rest
lake=$(clickhouse-client -q \
  "SELECT engine FROM system.databases WHERE name = 'netmon_lake'")
# Drops only the tables' definitions: the warehouse's files stay.
drop_lake() {
  clickhouse-client -q 'DROP DATABASE IF EXISTS netmon_lake
    SETTINGS iceberg_delete_data_on_drop = 0'
}
if [ -f $rest ]; then
  served=$(sudo sed -n 's/^WAREHOUSE=//p' $rest)
  if [ "$served" != "$warehouse" ]; then
    echo "the REST catalog serves $served, not $warehouse" >&2
    exit 1
  fi
  echo "== Reading the tables through the REST catalog"
  drop_lake
  # Only S3 needs credentials, but ClickHouse asks for them unless the engine
  # has a second argument; for local files that is the format.
  clickhouse-client --allow_database_iceberg=1 -q "
    CREATE DATABASE netmon_lake
    ENGINE = DataLakeCatalog('http://127.0.0.1:8181/v1', 'auto')
    SETTINGS catalog_type = 'rest', warehouse = 'netmon'"
else
  echo "== Reading the tables' newest metadata files"
  if [ "$lake" = DataLakeCatalog ]; then
    drop_lake
  fi
  # Tables, not icebergLocal() in the views: Grafana's user may read a
  # table, but a table function would need FILE. Replaced one at a time, so
  # a table whose folder is missing (the share is down, or the converter
  # hasn't made it yet) keeps its old definition and works again later.
  clickhouse-client -q 'CREATE DATABASE IF NOT EXISTS netmon_lake'
  for table in traffic interfaces; do
    clickhouse-client -q "
      CREATE OR REPLACE TABLE netmon_lake.\`netmon.$table\`
      ENGINE = IcebergLocal('$warehouse/netmon/$table')"
  done
fi
# The views need the converter's columns: without one, replacing the live
# view would fail halfway and leave it broken.
for column in nfs_fetched nfs_sent; do
  if ! clickhouse-client -q \
    "SELECT $column FROM netmon_lake.\`netmon.traffic\` LIMIT 0" >/dev/null 2>&1; then
    echo "netmon.traffic has no $column yet: run scripts/install_converter.sh first" >&2
    exit 1
  fi
done
sed -e "s|@RAW@|$raw|g" clickhouse/views.sql | clickhouse-client --multiquery
clickhouse-client --user grafana -q \
  'SELECT host, count() AS rows, max(ts) AS latest FROM netmon.traffic_live GROUP BY host'
