#!/bin/bash
# Installs Grafana with the ClickHouse plugin and the netmon dashboard, on the
# machine running ClickHouse (scripts/install_clickhouse.sh).
#
#   scripts/install_grafana.sh
#
# Grafana listens on port 3000. The first login is admin/admin, and Grafana
# asks for a new password then. Re-run to update the dashboard.
set -eu
cd "$(dirname "$0")/.."

echo "== Installing Grafana from apt.grafana.com"
key=/usr/share/keyrings/grafana.gpg
curl -fsSL https://apt.grafana.com/gpg.key | sudo gpg --batch --yes --dearmor -o "$key"
echo "deb [signed-by=$key] https://apt.grafana.com stable main" \
  | sudo tee /etc/apt/sources.list.d/grafana.list >/dev/null
sudo apt-get update -qq
sudo apt-get install -y grafana
sudo grafana cli plugins install grafana-clickhouse-datasource
# grafana cli ran as root; the server installs its own plugins there too.
sudo chown -R grafana:grafana /var/lib/grafana/plugins

echo "== Provisioning the data source and dashboard"
sudo install -m 644 grafana/provisioning/datasources/netmon.yaml \
  /etc/grafana/provisioning/datasources/
sudo install -m 644 grafana/provisioning/dashboards/netmon.yaml \
  /etc/grafana/provisioning/dashboards/
sudo install -d /var/lib/grafana/dashboards/netmon
sudo install -m 644 grafana/dashboards/netmon.json /var/lib/grafana/dashboards/netmon/

sudo systemctl daemon-reload
sudo systemctl enable grafana-server
sudo systemctl restart grafana-server
for _ in $( # the first start migrates its database
  seq 90
); do
  curl -fs http://127.0.0.1:3000/api/health >/dev/null && break
  sleep 2
done
curl -fs http://127.0.0.1:3000/api/health
echo
echo "Open http://$(hostname -I | cut -d' ' -f1):3000/d/netmon-traffic"
