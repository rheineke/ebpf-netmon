#!/bin/bash
# Sends Grafana's netmon alerts by email. Run it on the Grafana machine, in a
# terminal: it asks for the address and SMTP login, and stores them in
# /etc/grafana/netmon-alerts.env, and the password in
# /etc/grafana/netmon-smtp-password, both readable only by root and Grafana.
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
read -rp "Send from [$user]: " from
from=${from:-$user}
IFS= read -rsp "SMTP password: " password
echo
case $password in
  "" | [[:space:]]* | *[[:space:]])
    echo "the password can't be empty or start or end with a space," \
      "which Grafana would drop" >&2
    exit 2
    ;;
esac
case $host in
  *:[0-9]*) ;;
  *)
    echo "give the SMTP server as host:port, e.g. smtp.gmail.com:587" >&2
    exit 2
    ;;
esac
# systemd reads the settings file unquoted, so keep them plain. The
# password, which may hold anything, goes in a file of its own, which
# Grafana reads through $__file{} (the apt package has no __FILE variables).
for value in "$to" "$host" "$user" "$from"; do
  case $value in
    "" | *[[:space:]\\\"\']*)
      echo "addresses and servers can't be empty or hold spaces, quotes or \\" >&2
      exit 2
      ;;
  esac
done

secret=/etc/grafana/netmon-smtp-password
sudo install -m 640 -o root -g grafana /dev/null "$secret"
printf '%s' "$password" | sudo tee "$secret" >/dev/null
unset password
env=/etc/grafana/netmon-alerts.env
sudo install -m 640 -o root -g grafana /dev/null "$env"
printf '%s\n' \
  GF_SMTP_ENABLED=true \
  "GF_SMTP_HOST=$host" \
  "GF_SMTP_USER=$user" \
  "GF_SMTP_PASSWORD=\$__file{$secret}" \
  "GF_SMTP_FROM_ADDRESS=$from" \
  "GF_SMTP_FROM_NAME=netmon" \
  "NETMON_ALERT_EMAIL=$to" | sudo tee "$env" >/dev/null

sudo mkdir -p /etc/systemd/system/grafana-server.service.d
printf '[Service]\nEnvironmentFile=%s\n' "$env" \
  | sudo tee /etc/systemd/system/grafana-server.service.d/netmon-alerts.conf >/dev/null
sudo install -m 640 -o root -g grafana grafana/alerting/netmon-email.yaml \
  /etc/grafana/provisioning/alerting/
sudo systemctl daemon-reload
sudo systemctl restart grafana-server
echo "Done. To send a test, open Alerting > Contact points > netmon email > Test."
