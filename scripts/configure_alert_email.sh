#!/bin/bash
# Sends Grafana's netmon alerts by email. Run it on the Grafana machine, in a
# terminal: it asks for the address and SMTP login, and stores them in
# /etc/grafana/netmon-alerts.env, readable only by root and Grafana.
#
#   scripts/configure_alert_email.sh
#
# For Gmail, use smtp.gmail.com:587 and an app password
# (https://myaccount.google.com/apppasswords), not the account password.
set -eu
cd "$(dirname "$0")/.."

read -rp "Send alerts to: " to
read -rp "SMTP server [smtp.gmail.com:587]: " host
host=${host:-smtp.gmail.com:587}
read -rp "SMTP user [$to]: " user
user=${user:-$to}
read -rsp "SMTP password: " password
echo

env=/etc/grafana/netmon-alerts.env
sudo install -m 640 -o root -g grafana /dev/null "$env"
# systemd reads this file: no quotes, one setting per line.
printf '%s\n' \
  GF_SMTP_ENABLED=true \
  "GF_SMTP_HOST=$host" \
  "GF_SMTP_USER=$user" \
  "GF_SMTP_PASSWORD=$password" \
  "GF_SMTP_FROM_ADDRESS=$user" \
  "GF_SMTP_FROM_NAME=netmon" \
  "NETMON_ALERT_EMAIL=$to" | sudo tee "$env" >/dev/null
unset password

sudo mkdir -p /etc/systemd/system/grafana-server.service.d
printf '[Service]\nEnvironmentFile=%s\n' "$env" \
  | sudo tee /etc/systemd/system/grafana-server.service.d/netmon-alerts.conf >/dev/null
sudo install -m 640 -o root -g grafana grafana/alerting/netmon-email.yaml \
  /etc/grafana/provisioning/alerting/
sudo systemctl daemon-reload
sudo systemctl restart grafana-server
echo "Done. To send a test, open Alerting > Contact points > netmon email > Test."
