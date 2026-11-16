"""Runs the dashboard's panels and alert rules through Grafana and its
ClickHouse plugin, on the data tests/dashboard_test.py loaded. This catches
what clickhouse-client can't: the plugin's own settings, macros and
timeouts. Needs Grafana from scripts/install_grafana.sh, still with its
first login; CI runs it:

  scripts/install_grafana.sh && python3 tests/grafana_test.py
"""

import base64
import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAFANA = "http://127.0.0.1:3000"
AUTH = "Basic " + base64.b64encode(b"admin:admin").decode()
DATASOURCE = "netmon-clickhouse"
SMTP = ("127.0.0.1", 2525)  # the server CI gives configure_alert_email.sh


class SMTPCapture(socketserver.StreamRequestHandler):
    """Just enough of an SMTP server to take a mail, recording the AUTH
    PLAIN login it was sent, as (user, password), in server.logins."""

    def handle(self):
        def send(line):
            self.wfile.write(line.encode() + b"\r\n")

        send("220 localhost ESMTP netmon test")
        in_data = False
        for raw in self.rfile:
            line = raw.decode(errors="replace").rstrip("\r\n")
            if in_data:
                if line == ".":
                    in_data = False
                    send("250 queued")
                continue
            verb, _, arg = line.partition(" ")
            verb = verb.upper()
            if verb in ("EHLO", "HELO"):
                send("250-localhost")
                send("250 AUTH PLAIN")
            elif verb == "AUTH":
                initial = arg.partition(" ")[2]
                if not initial:
                    send("334 ")
                    initial = self.rfile.readline().decode().strip()
                login = base64.b64decode(initial).split(b"\0")[1:]
                self.server.logins.append(tuple(x.decode() for x in login))
                send("235 accepted")
            elif verb == "DATA":
                in_data = True
                send("354 go on")
            elif verb == "QUIT":
                send("221 bye")
                return
            else:
                send("250 ok")


def api(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": AUTH, "Content-Type": "application/json"}
    request = urllib.request.Request(GRAFANA + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        sys.exit(f"FAIL: {path}: HTTP {e.code}: {e.read().decode()[:1000]}")


def check(name, ok, detail=""):
    if not ok:
        sys.exit(f"FAIL: {name} {detail}")
    print(f"ok  : {name}")


def run_query(target, sql):
    """Runs one query the way a panel does; returns its frames' columns."""
    query = target | {"rawSql": sql, "intervalMs": 60_000, "maxDataPoints": 1000}
    out = api("/api/ds/query", {"queries": [query], "from": "now-7d", "to": "now"})
    result = out["results"][target["refId"]]
    if "error" in result:
        sys.exit(f"FAIL: {result['error']}\n{sql}")
    return [frame["data"]["values"] for frame in result.get("frames", [])]


def check_panels():
    dash = api("/api/dashboards/uid/netmon-traffic")["dashboard"]
    variable = dash["templating"]["list"][0]
    target = {"refId": "A", "datasource": variable["datasource"]}
    target |= {"editorType": "sql", "format": 1, "queryType": "table"}
    hosts = run_query(target, variable["query"])[0][0]
    check(f"host variable lists {hosts}", len(hosts) >= 2)
    quoted = ", ".join(f"'{h}'" for h in hosts)
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            sql = target["rawSql"].replace("${host:singlequote}", quoted)
            columns = [c for frame in run_query(target, sql) for c in frame]
            check(f"panel {panel['title']!r}", any(columns), "returned no rows")


def check_alert_rules():
    """Every provisioned rule must evaluate without an error. Firing is fine:
    the test data stopped minutes ago, so "Host stopped reporting" fires."""
    path = ROOT / "grafana/provisioning/alerting/netmon-rules.yaml"
    import yaml

    groups = yaml.safe_load(path.read_text())["groups"]
    want = sorted(rule["title"] for group in groups for rule in group["rules"])
    deadline = time.monotonic() + 240  # the slowest group runs every 2 minutes
    while True:
        out = api("/api/prometheus/grafana/api/v1/rules")
        rules = [r for g in out["data"]["groups"] for r in g["rules"]]
        health = {r["name"]: (r["health"], r.get("lastError", "")) for r in rules}
        if sorted(health) == want and "unknown" not in {h for h, _ in health.values()}:
            break
        if time.monotonic() > deadline:
            sys.exit(f"FAIL: alert rules not all evaluated: {health}")
        time.sleep(10)
    for name, (state, error) in sorted(health.items()):
        check(f"alert rule {name!r} evaluates ({state})", state != "error", error)


def check_alert_email():
    """With scripts/configure_alert_email.sh run, the contact point has the
    address it was given, read from Grafana's environment."""
    want = os.environ.get("NETMON_TEST_ALERT_EMAIL")
    if not want:
        return
    points = api("/api/v1/provisioning/contact-points")
    email = [p for p in points if p["name"] == "netmon email"]
    got = email[0]["settings"].get("addresses") if email else None
    check(f"the alert contact point sends to {want}", got == want, got)
    # Grafana shows a set password as asterisks, and an unset one as "".
    smtp = api("/api/admin/settings")["smtp"]
    check("Grafana has the SMTP password", smtp.get("password", "") != "", smtp)
    password = os.environ.get("NETMON_TEST_SMTP_PASSWORD")
    if not password:
        return
    # Asterisks can hide the wrong password, so take a test mail and look.
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(SMTP, SMTPCapture) as server:
        server.logins = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config = {"name": "netmon test", "type": "email"}
        config["settings"] = {"addresses": want}
        receiver = {"name": "netmon test", "grafana_managed_receiver_configs": [config]}
        api(
            "/api/alertmanager/grafana/config/api/v1/receivers/test",
            {"receivers": [receiver]},
        )
        for _ in range(30):
            if server.logins:
                break
            time.sleep(1)
        server.shutdown()
    check(
        "Grafana logs in to SMTP with the configured password",
        server.logins[:1] == [(smtp["user"], password)],
        [user for user, _ in server.logins],  # never the password
    )


def main():
    check_alert_email()
    health = api(f"/api/datasources/uid/{DATASOURCE}/health")
    check("ClickHouse data source health", health.get("status") == "OK", health)
    check_panels()
    check_alert_rules()
    print("PASS")


if __name__ == "__main__":
    main()
