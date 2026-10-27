#!/bin/bash
# Installs PostgreSQL to hold the Iceberg catalog, on the machine that runs
# the converter, instead of the SQLite file in the warehouse.
#
#   scripts/install_postgres.sh [OS_USER]
#
# Creates the role and database netmon, reachable only over the local Unix
# socket and with no password: peer authentication lets OS_USER (default
# netmon, which the converter runs as) log in as netmon. Then point the
# converter at it; the first run adopts the table already in the warehouse:
#
#   scripts/install_converter.sh WAREHOUSE RAW \
#     'postgresql+psycopg2://netmon@/netmon?host=/var/run/postgresql'
set -eu

if [ $# -gt 1 ]; then
  sed -n '2,13s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
os_user=${1:-netmon}
cd "$(dirname "$0")/.."
if [ "$os_user" = netmon ]; then
  scripts/add_netmon_user.sh
fi

echo "== Installing PostgreSQL"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql
sudo systemctl enable --now postgresql.service
version=$(pg_lsclusters -h | awk '$2 == "main" { print $1; exit }')
conf=/etc/postgresql/$version/main

echo "== Letting $os_user log in as netmon over the Unix socket"
ident="netmon $os_user netmon"
sudo grep -qxF "$ident" "$conf/pg_ident.conf" \
  || echo "$ident" | sudo tee -a "$conf/pg_ident.conf" >/dev/null
# The first matching line wins, so it goes above Debian's defaults.
hba="local netmon netmon peer map=netmon"
sudo grep -qxF "$hba" "$conf/pg_hba.conf" \
  || sudo sed -i "1i $hba" "$conf/pg_hba.conf"
sudo systemctl reload postgresql.service

echo "== Creating the netmon role and database"
psql_admin() { sudo -u postgres psql -v ON_ERROR_STOP=1 -tAc "$1"; }
[ "$(psql_admin "SELECT 1 FROM pg_roles WHERE rolname = 'netmon'")" = 1 ] \
  || sudo -u postgres createuser netmon
[ "$(psql_admin "SELECT 1 FROM pg_database WHERE datname = 'netmon'")" = 1 ] \
  || sudo -u postgres createdb -O netmon netmon
sudo -u "$os_user" psql -U netmon -d netmon -tAc 'SELECT current_user'
