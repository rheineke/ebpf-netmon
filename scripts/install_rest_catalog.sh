#!/bin/bash
# Installs an Iceberg REST catalog that serves the PostgreSQL catalog
# (scripts/install_postgres.sh) to ClickHouse, read-only, on 127.0.0.1:8181.
#
#   scripts/install_rest_catalog.sh WAREHOUSE
#
# Run it on the machine with the converter's PostgreSQL catalog and
# ClickHouse, then re-run scripts/install_clickhouse.sh: ClickHouse then
# reads the table's current metadata from the catalog, not the newest file in
# WAREHOUSE. The server is Apache Iceberg's own (its REST test fixture),
# run with Debian's Java. It logs in to PostgreSQL as netmon_rest, which may
# only read the catalog, and can't write files, so it changes nothing.
set -eu

if [ $# -ne 1 ]; then
  sed -n '2,12s/^# \{0,1\}//p' "$0" >&2
  exit 2
fi
warehouse=$(realpath "$1")
cd "$(dirname "$0")/.."

echo "== Installing Java and PostgreSQL's JDBC driver"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  openjdk-21-jre-headless libpostgresql-jdbc-java

# Iceberg publishes the server with its dependencies only as a Docker image,
# apache/iceberg-rest-fixture:1.10.1. The jar is the image's last layer.
layer=sha256:b49ea7e256ec0b2d4be5476f00b44127b921f7e3c9c0131f4dfe1d2c403eeb9d
jar_sha256=49d01ec8e0995001a92b4c880adfe184d602541ba39d135f105511101da4db7d
jar=/opt/netmon/iceberg-rest/iceberg-rest-adapter.jar
if ! echo "$jar_sha256  $jar" | sha256sum -c --status 2>/dev/null; then
  echo "== Downloading the Iceberg REST server 1.10.1 from Docker Hub"
  repo=apache/iceberg-rest-fixture
  token=$(curl -fsSL \
    "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$repo:pull" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')
  # /var/tmp, not /tmp: a Pi's /tmp is in RAM, and the layer is 190 MB.
  tmp=$(mktemp -d -p /var/tmp)
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -H "Authorization: Bearer $token" -o "$tmp/layer.tar.gz" \
    "https://registry-1.docker.io/v2/$repo/blobs/$layer"
  echo "${layer#sha256:}  $tmp/layer.tar.gz" | sha256sum -c --quiet
  tar -xzf "$tmp/layer.tar.gz" -C "$tmp" usr/lib/iceberg-rest/iceberg-rest-adapter.jar
  echo "$jar_sha256  $tmp/usr/lib/iceberg-rest/iceberg-rest-adapter.jar" \
    | sha256sum -c --quiet
  sudo install -D -m 644 "$tmp/usr/lib/iceberg-rest/iceberg-rest-adapter.jar" "$jar"
fi

echo "== Creating the read-only role netmon_rest"
psql_admin() { sudo -u postgres psql -v ON_ERROR_STOP=1 -d netmon -qtA; }
[ "$(echo "SELECT count(*) FROM pg_tables WHERE tablename = 'iceberg_tables'" | psql_admin)" = 1 ] || {
  echo "no catalog in the netmon database yet: run the converter first" >&2
  exit 1
}
password=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
# On stdin, so the password is never in a command line.
psql_admin <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'netmon_rest') THEN
    CREATE ROLE netmon_rest LOGIN;
  END IF;
END \$\$;
ALTER ROLE netmon_rest PASSWORD '$password';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO netmon_rest;
ALTER DEFAULT PRIVILEGES FOR ROLE netmon IN SCHEMA public
  GRANT SELECT ON TABLES TO netmon_rest;
SQL

echo "== Installing netmon-iceberg-rest.service, as user netmon-rest"
id netmon-rest >/dev/null 2>&1 \
  || sudo useradd --system --no-create-home --shell /usr/sbin/nologin netmon-rest
sudo install -m 644 systemd/netmon-iceberg-rest.service /etc/systemd/system/
env=/etc/default/netmon-iceberg-rest
sudo install -m 600 /dev/null "$env"
printf 'WAREHOUSE=%s\nCATALOG_WAREHOUSE=file://%s\nCATALOG_JDBC_PASSWORD=%s\n' \
  "$warehouse" "$warehouse" "$password" | sudo tee "$env" >/dev/null
sudo mkdir -p /etc/systemd/system/netmon-iceberg-rest.service.d
printf '[Unit]\nRequiresMountsFor=%s\n' "$warehouse" \
  | sudo tee /etc/systemd/system/netmon-iceberg-rest.service.d/warehouse.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable netmon-iceberg-rest.service
sudo systemctl restart netmon-iceberg-rest.service
for _ in $(seq 60); do
  tables=$(curl -fs http://127.0.0.1:8181/v1/namespaces/netmon/tables) && break
  sleep 1
done
if [ -z "${tables:-}" ]; then
  journalctl -u netmon-iceberg-rest.service -n 20 --no-pager -o cat
  exit 1
fi
echo "$tables"
echo "Now re-run scripts/install_clickhouse.sh to read through it."
