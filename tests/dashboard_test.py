"""Checks the Grafana dashboard's queries against ClickHouse.

  uv run tests/dashboard_test.py load DIR      # as root, or with write access
  scripts/install_clickhouse.sh DIR/warehouse DIR/raw
  python3 tests/dashboard_test.py check DIR
  uv run tests/dashboard_test.py convert DIR   # then check again
  uv run tests/dashboard_test.py maintain DIR  # and again
  uv run tests/dashboard_test.py stray DIR     # REST catalog only; again

load writes four hours of known traffic from two hosts, ending in the
current hour, to DIR/raw, converts the first three into DIR/warehouse,
and then spoils one of them, which the live view must not open.
check runs every panel of grafana/dashboards/netmon.json through
clickhouse-client, as Grafana's user, with Grafana's macros filled in, and
checks the totals, so the live view must add the last hour from the raw
files. It also runs every alert rule's query, which needs the dev
dependencies (uv sync). convert loads that hour too, and maintain compacts
the table and expires its old snapshots, as a month later; the totals must
not change. stray adds a copy of the table's first metadata file, numbered
after the rest, which only a catalog read ignores.

With $NETMON_TEST_CATALOG (a SQLAlchemy URI), the table is in that catalog,
and check also expects ClickHouse to read it through the REST catalog
(scripts/install_rest_catalog.sh).
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOURS, ROWS = 4, 6  # per host, rows per hour
COUNTERS = {"tcp_tx": 1, "tcp_rx": 2, "udp_tx": 3, "udp_rx": 4, "nfs_read": 5}
COUNTERS["nfs_write"] = 6
SCALE = 2.0
START = None  # the first hour; load picks it, the others read it from DIR
CATALOG = os.environ.get("NETMON_TEST_CATALOG")


def convert(test_dir, hours):
    sys.path.insert(0, str(ROOT / "tools"))
    import to_iceberg

    now = START + dt.timedelta(hours=hours)
    warehouse = test_dir / "warehouse"
    n = to_iceberg.convert(
        test_dir / "raw", warehouse, now, days=2, catalog_uri=CATALOG
    )
    print(f"loaded {n} rows")
    rows = len(to_iceberg.open_table(warehouse, CATALOG).scan().to_arrow())
    (test_dir / "iceberg_rows").write_text(str(rows))


def maintain(test_dir):
    sys.path.insert(0, str(ROOT / "tools"))
    import to_iceberg

    month = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
    print(to_iceberg.maintain(test_dir / "warehouse", month, catalog_uri=CATALOG))


def load(test_dir):
    if CATALOG:  # a previous run's table, whose files are gone
        sys.path.insert(0, str(ROOT / "tools"))
        import to_iceberg
        from pyiceberg.exceptions import NoSuchTableError

        try:
            to_iceberg.open_catalog(test_dir, CATALOG).drop_table(to_iceberg.TABLE)
        except NoSuchTableError:
            pass
    this_hour = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    start = this_hour - dt.timedelta(hours=HOURS - 1)
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "start").write_text(start.isoformat())
    for host in ("pi-a", "pi-b"):
        for h in range(HOURS):
            hour = start + dt.timedelta(hours=h)
            path = test_dir / "raw" / host / f"{hour:%Y-%m-%d}" / f"{hour:%H}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                for i in range(ROWS):
                    row = {"ts": int(hour.timestamp()) + 600 * i, "host": host}
                    row |= {"interval_s": 10, "pid": 100 + i % 2, "uid": i % 2}
                    row |= {"comm": f"proc{i % 2}", "scale": SCALE}
                    f.write(json.dumps(row | COUNTERS) + "\n")
    # Copies of pi-a's last hour, still only in raw files: a backup, which
    # must count once, and folders to skip, each with a row of its own.
    path = test_dir / "raw" / "pi-a" / f"{hour:%Y-%m-%d}" / f"{hour:%H}.jsonl"
    for folder in ("pi-a.bak", "#recycle/pi-a", "@eaDir/pi-a", ".snapshot/pi-a"):
        copy = test_dir / "raw" / folder / path.relative_to(path.parents[1])
        copy.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text()
        if folder != "pi-a.bak":
            text += json.dumps(json.loads(text.splitlines()[0]) | {"pid": 999}) + "\n"
        copy.write_text(text)
    # A file the converter ignores, which the live view must ignore too.
    odd = path.parent / "notes.jsonl"
    row = json.loads(path.read_text().splitlines()[0]) | {"pid": 998}
    odd.write_text(json.dumps(row) + "\n")


def spoil_converted(test_dir):
    """Appends garbage to an hour already in Iceberg. The live view must skip
    that file without opening it: over 100 bad lines would fail its query."""
    path = test_dir / "raw" / "pi-a" / f"{START:%Y-%m-%d}" / f"{START:%H}.jsonl"
    with open(path, "a") as f:
        f.write("not json\n" * 200)


def stray(test_dir):
    """A stale metadata file numbered after the current one, as a crashed or
    second writer could leave: reading the folder, ClickHouse would take it."""
    folder = test_dir / "warehouse" / "netmon" / "traffic" / "metadata"
    first = min(folder.glob("00000-*.metadata.json"))
    (folder / "99999-stray.metadata.json").write_bytes(first.read_bytes())


def fill_macros(sql):
    """Does what Grafana and its ClickHouse plugin do before sending SQL."""
    end = START + dt.timedelta(hours=HOURS)
    sql = sql.replace("${host:singlequote}", "'pi-a', 'pi-b'")
    sql = re.sub(
        r"\$__timeFilter\((\w+)\)",
        rf"\1 >= toDateTime({int(START.timestamp())}) AND "
        rf"\1 <= toDateTime({int(end.timestamp())})",
        sql,
    )
    sql = re.sub(
        r"\$__timeInterval\((\w+)\)",
        r"toStartOfInterval(toDateTime(\1), INTERVAL 3600 second)",
        sql,
    )
    return sql.replace("$__interval_s", "3600")


def query(sql):
    out = subprocess.run(
        [
            "clickhouse-client",
            "--user",
            "grafana",
            "--format",
            "JSONEachRow",
            "--output_format_json_quote_64bit_integers=0",
        ],
        input=fill_macros(sql),
        capture_output=True,
        text=True,
    )
    if out.returncode:
        sys.exit(f"FAIL: {out.stderr.strip()}\n{sql}")
    return [json.loads(line) for line in out.stdout.splitlines()]


def check_eq(name, got, want):
    if got != want:
        sys.exit(f"FAIL: {name}: got {got}, want {want}")
    print(f"ok  : {name}")


def check(test_dir):
    dash = json.loads((ROOT / "grafana/dashboards/netmon.json").read_text())
    per_host = HOURS * ROWS * SCALE  # each counter's weight per host
    iceberg = query("SELECT count() AS n FROM netmon.traffic")[0]["n"]
    # The live view would hide a stale table, so check it by itself too.
    if CATALOG:
        sql = "SELECT create_table_query AS q FROM system.tables"
        sql += " WHERE database = 'netmon' AND name = 'traffic'"
        check_eq(
            "read through the REST catalog", "netmon_lake" in query(sql)[0]["q"], True
        )
    want = int((test_dir / "iceberg_rows").read_text())
    check_eq("Iceberg rows, as the converter left them", int(iceberg), want)
    live = query("SELECT count() AS n FROM netmon.traffic_live")[0]["n"]
    check_eq("live rows", int(live), HOURS * ROWS * 2)
    hosts = [r["host"] for r in query(dash["templating"]["list"][0]["query"])]
    check_eq("host variable", hosts, ["pi-a", "pi-b"])
    for panel in dash["panels"]:
        rows = query(panel["targets"][0]["rawSql"])
        title = panel["title"]
        if panel["type"] == "timeseries":
            # 0 is a time series, one line per series; 1, a table, is one line.
            check_eq(f"{title}: format", panel["targets"][0]["format"], 0)
            # bytes_per_s over hour buckets, times 3600, is the bytes in range.
            total = sum(r["bytes_per_s"] for r in rows) * 3600
            want = (
                {
                    "Network by process (top 10)": 10,
                    "Sent by process": 4,
                    "Received by process": 6,
                    "NFS read by process": 5,
                    "NFS written by process": 6,
                    "Network by user id": 10,
                }[title]
                * 2
                * per_host
            )
            check_eq(f"{title}: bytes", round(total), want)
            check_eq(f"{title}: buckets", len({r["time"] for r in rows}), HOURS)
        elif title == "Totals in range":
            got = {k: round(float(v)) for k, v in rows[0].items()}
            check_eq(title, got["TCP received"], 2 * 2 * per_host)
            check_eq(f"{title}: NFS written", got["NFS written"], 6 * 2 * per_host)
        else:
            check_eq(f"{title}: rows", len(rows), 4)  # 2 hosts x 2 comms
            check_eq(f"{title}: pids", {r["pids"] for r in rows}, {1})
    check_alerts()
    check_locked_down()
    print("PASS")


def check_locked_down():
    """Grafana's user can't reach files, URLs or its limits but by the views,
    and can set the time limits Grafana's plugin asks for."""
    denied, limited = "ACCESS_DENIED", "SETTING_CONSTRAINT_VIOLATION"
    probes = [
        ("SELECT count() FROM file('**/*.jsonl', 'LineAsString')", denied),
        ("INSERT INTO FUNCTION file('grafana.txt', 'LineAsString') SELECT 'x'", denied),
        ("SELECT * FROM url('http://127.0.0.1:8123/', 'LineAsString')", denied),
        ("DROP VIEW netmon.traffic_live", denied),
        ("SELECT 1 SETTINGS max_memory_usage = 0", limited),
        ("SELECT 1 SETTINGS max_execution_time = 0", limited),
        ("SELECT 1 SETTINGS max_execution_time = 600", limited),
    ]
    if CATALOG:
        probes.append(("SELECT count() FROM netmon_lake.`netmon.traffic`", denied))
    for sql, error in probes:
        out = subprocess.run(
            ["clickhouse-client", "--user", "grafana", "-q", sql],
            capture_output=True,
            text=True,
        )
        check_eq(f"refused: {sql}", f"({error})" in out.stderr, True)
    # The plugin sends its query timeout plus 5 s: 64 for panels, 34 for alerts.
    for seconds in (64, 34):
        sql = "SELECT count() AS n FROM netmon.traffic_live"
        sql += f" SETTINGS max_execution_time = {seconds}"
        check_eq(f"max_execution_time {seconds}", query(sql)[0]["n"], HOURS * ROWS * 2)


def check_alerts():
    """Each alert query must give Grafana one number per row, labelled by
    its string columns, or the rule cannot evaluate."""
    import yaml

    path = ROOT / "grafana/provisioning/alerting/netmon-rules.yaml"
    for rule in yaml.safe_load(path.read_text())["groups"][0]["rules"]:
        query_step, threshold = rule["data"]
        rows = query(query_step["model"]["rawSql"])
        title = rule["title"]
        if rule["uid"] == "netmon-host-silent":
            check_eq(
                f"{title}: hosts", sorted(r["host"] for r in rows), ["pi-a", "pi-b"]
            )
        if rule["uid"] == "netmon-converter-behind":
            check_eq(f"{title}: rows", len(rows), 1)
        for row in rows:
            kinds = sorted(type(v).__name__ for v in row.values())
            numbers = [k for k in kinds if k in ("int", "float")]
            if len(numbers) != 1 or set(kinds) - {"int", "float", "str"}:
                sys.exit(f"FAIL: {title}: row {row} is not one number and labels")
        check_eq(f"{title}: threshold step", threshold["model"]["expression"], "A")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in (
        "load",
        "check",
        "convert",
        "maintain",
        "stray",
    ):
        sys.exit(__doc__)
    test_dir = Path(sys.argv[2])
    if sys.argv[1] == "load":
        load(test_dir)
    START = dt.datetime.fromisoformat((test_dir / "start").read_text())
    if sys.argv[1] == "load":
        convert(test_dir, HOURS - 1)  # the last hour stays raw
        spoil_converted(test_dir)
    elif sys.argv[1] == "convert":
        convert(test_dir, HOURS)
    elif sys.argv[1] == "maintain":
        maintain(test_dir)
    elif sys.argv[1] == "stray":
        stray(test_dir)
    else:
        check(test_dir)
