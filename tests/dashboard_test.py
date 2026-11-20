"""Checks the Grafana dashboard's queries against ClickHouse.

  uv run tests/dashboard_test.py load DIR      # as root, or with write access
  scripts/install_clickhouse.sh DIR/warehouse DIR/raw
  python3 tests/dashboard_test.py check DIR
  uv run tests/dashboard_test.py convert DIR   # then check again
  uv run tests/dashboard_test.py maintain DIR  # and again
  uv run tests/dashboard_test.py stray DIR     # REST catalog only; again

load writes four hours of known traffic from two hosts, ending in the
last finished hour, with their interfaces' counters, to DIR/raw, converts
the first three into DIR/warehouse, and then spoils one of them, which the
live views must not open. The converter's own next run would load the fourth.
check runs every panel of grafana/dashboards/netmon.json through
clickhouse-client, as Grafana's user, with Grafana's macros filled in, and
checks the totals, so the live view must add the last hour from the raw
files. It also runs every alert rule's query, and the high-traffic rule's
over rows made for its window too, which needs the dev dependencies (uv
sync). convert loads that hour too, and maintain compacts
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
COUNTERS["nfs_fetched"] = 7  # but in OLD_HOUR
OLD_HOUR = ("pi-b", 0)  # written by a netmon before nfs_fetched
IFACE_COUNTERS = ("rx_bytes", "rx_packets", "tx_bytes", "tx_packets")
SCALE = 2.0
# Each report's interface rows: (interface, rx_bytes, rx_packets, tx_bytes,
# tx_packets). Less the headers' 66 bytes a packet, pi-a's interfaces carry
# the 12 bytes received and 8 sent of its processes' rows (with SCALE), and
# pi-b's twice that. A veth pair's bytes, which also crossed a physical
# interface, don't count.
INTERFACES = {
    "pi-a": [("eth0", 66 * 3 + 12, 3, 66 * 2 + 8, 2), ("lo", 66, 1, 66, 1)],
    "pi-b": [("eth0", 66 * 3 + 24, 3, 66 * 2 + 16, 2), ("lo", 66, 1, 66, 1)],
}
for counts in INTERFACES.values():
    counts.append(("veth1a2b3c", 10_000, 9, 10_000, 9))
COVERAGE = {"pi-a": 1.0, "pi-b": 0.5}
START = None  # the first hour; load picks it, the others read it from DIR
CATALOG = os.environ.get("NETMON_TEST_CATALOG")


def convert(test_dir, hours):
    sys.path.insert(0, str(ROOT / "tools"))
    import to_iceberg

    now = START + dt.timedelta(hours=hours)
    warehouse = test_dir / "warehouse"
    for kind in to_iceberg.KINDS:
        n = to_iceberg.convert(
            test_dir / "raw", warehouse, now, days=2, catalog_uri=CATALOG, kind=kind
        )
        print(f"loaded {n} rows to {kind.table}")
        table = to_iceberg.open_table(warehouse, CATALOG, kind)
        rows = len(table.scan().to_arrow())
        (test_dir / f"iceberg_rows.{kind.name}").write_text(str(rows))


def maintain(test_dir):
    sys.path.insert(0, str(ROOT / "tools"))
    import to_iceberg

    month = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
    for kind in to_iceberg.KINDS:
        done = to_iceberg.maintain(
            test_dir / "warehouse", month, catalog_uri=CATALOG, kind=kind
        )
        print(kind.table, done)


def load(test_dir):
    if CATALOG:  # a previous run's table, whose files are gone
        sys.path.insert(0, str(ROOT / "tools"))
        import to_iceberg
        from pyiceberg.exceptions import NoSuchTableError

        for kind in to_iceberg.KINDS:
            try:
                to_iceberg.open_catalog(test_dir, CATALOG).drop_table(kind.table)
            except NoSuchTableError:
                pass
    this_hour = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    start = this_hour - dt.timedelta(hours=HOURS)
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
                    row |= COUNTERS
                    if (host, h) == OLD_HOUR:
                        del row["nfs_fetched"]
                    f.write(json.dumps(row) + "\n")
            with open(path.with_name(f"{hour:%H}.interfaces.jsonl"), "w") as f:
                for i in range(ROWS):
                    for name, *counts in INTERFACES[host]:
                        row = {"ts": int(hour.timestamp()) + 600 * i, "host": host}
                        row |= {"interval_s": 10, "interface": name}
                        row |= dict(zip(IFACE_COUNTERS, counts, strict=True))
                        f.write(json.dumps(row) + "\n")
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
    for name in (f"{START:%H}.jsonl", f"{START:%H}.interfaces.jsonl"):
        path = test_dir / "raw" / "pi-a" / f"{START:%Y-%m-%d}" / name
        with open(path, "a") as f:
            f.write("not json\n" * 200)


def stray(test_dir):
    """A stale metadata file numbered after the current one, as a crashed or
    second writer could leave: reading the folder, ClickHouse would take it."""
    folder = test_dir / "warehouse" / "netmon" / "traffic" / "metadata"
    first = min(folder.glob("00000-*.metadata.json"))
    (folder / "99999-stray.metadata.json").write_bytes(first.read_bytes())


def fill_macros(sql, interval=3600):
    """Does what Grafana and its ClickHouse plugin do before sending SQL."""
    end = START + dt.timedelta(hours=HOURS)
    sql = sql.replace("${host:singlequote}", "'pi-a', 'pi-b'")
    sql = re.sub(
        r"\$__timeFilter\((\w+)\)",
        rf"\1 >= toDateTime({int(START.timestamp())}) AND "
        rf"\1 <= toDateTime({int(end.timestamp())})",
        sql,
    )
    return sql.replace("$__interval_s", str(interval))


def query(sql, interval=3600):
    out = subprocess.run(
        [
            "clickhouse-client",
            "--user",
            "grafana",
            "--format",
            "JSONEachRow",
            "--output_format_json_quote_64bit_integers=0",
        ],
        input=fill_macros(sql, interval),
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
    want = int((test_dir / "iceberg_rows.traffic").read_text())
    check_eq("Iceberg rows, as the converter left them", int(iceberg), want)
    live = query("SELECT count() AS n FROM netmon.traffic_live")[0]["n"]
    check_eq("live rows", int(live), HOURS * ROWS * 2)
    iceberg = query("SELECT count() AS n FROM netmon.interfaces")[0]["n"]
    want = int((test_dir / "iceberg_rows.interfaces").read_text())
    check_eq("Iceberg interface rows, as the converter left them", int(iceberg), want)
    live = query("SELECT count() AS n FROM netmon.interfaces_live")[0]["n"]
    check_eq("live interface rows", int(live), HOURS * ROWS * 2 * 3)
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
                    "NFS fetched by process": 7,
                    "Network by user id": 10,
                }[title]
                * 2
                * per_host
            )
            if title == "NFS fetched by process":
                want -= 7 * ROWS * SCALE  # null in OLD_HOUR
            check_eq(f"{title}: bytes", round(total), want)
            check_eq(f"{title}: buckets", len({r["time"] for r in rows}), HOURS)
        elif panel["type"] == "heatmap":
            check_eq(f"{title}: format", panel["targets"][0]["format"], 0)
            got = {(r["time"], r["row"]): r["accounted"] for r in rows}
            want = {
                (time, f"{host} {direction}"): COVERAGE[host]
                for time in {r["time"] for r in rows}
                for host in INTERFACES
                for direction in ("received", "sent")
            }
            check_eq(f"{title}: cells", got, want)
            check_eq(f"{title}: buckets", len({r["time"] for r in rows}), HOURS)
        elif title == "Totals in range":
            got = {k: round(float(v)) for k, v in rows[0].items()}
            check_eq(title, got["TCP received"], 2 * 2 * per_host)
            check_eq(f"{title}: NFS written", got["NFS written"], 6 * 2 * per_host)
            fetched = 7 * (2 * per_host - ROWS * SCALE)  # null in OLD_HOUR
            check_eq(f"{title}: NFS fetched", got["NFS fetched"], fetched)
        else:
            check_eq(f"{title}: rows", len(rows), 4)  # 2 hosts x 2 comms
            check_eq(f"{title}: pids", {r["pids"] for r in rows}, {1})
    check_bucket_rates(dash)
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
        ("DROP VIEW netmon.interfaces_live", denied),
        ("SELECT 1 SETTINGS max_memory_usage = 0", limited),
        ("SELECT 1 SETTINGS max_execution_time = 0", limited),
        ("SELECT 1 SETTINGS max_execution_time = 600", limited),
    ]
    if CATALOG:
        probes.append(("SELECT count() FROM netmon_lake.`netmon.traffic`", denied))
        probes.append(("SELECT count() FROM netmon_lake.`netmon.interfaces`", denied))
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


def check_high_traffic(title, sql, limit):
    """The fixture has no rows in this rule's window, the last few minutes,
    so run its query over rows made for it: a subquery in the view's place.
    """
    rows = [  # seconds ago, comm, bytes sent, bytes received
        (180, "big", 2_000_000_000, 0),
        (120, "big", 1_100_000_000, 5),  # 3.1 GB in 5 minutes: over 10 MB/s
        (180, "small", 1_000_000, 2_000_000),  # 2 MB in: 6.7 kB/s
        (30, "late", 9_000_000_000, 0),  # in the last minute, maybe not synced
        (420, "early", 9_000_000_000, 0),  # before the window
    ]
    fake = " UNION ALL ".join(
        f"SELECT now() - {ago} AS ts, 'pi-a' AS host, '{comm}' AS comm,"
        f" toInt64({tx}) AS tcp_tx, toInt64({rx}) AS tcp_rx,"
        " toInt64(0) AS udp_tx, toInt64(0) AS udp_rx, 1.0 AS scale"
        for ago, comm, tx, rx in rows
    )
    view = "netmon.traffic_live"
    check_eq(f"{title}: reads {view} once", sql.count(view), 1)
    rates = {r["comm"]: r["bytes_per_s"] for r in query(sql.replace(view, f"({fake})"))}
    check_eq(f"{title}: programs in its window", sorted(rates), ["big", "small"])
    check_eq(f"{title}: rate", round(rates["big"]), round(3_100_000_000 / 300))
    check_eq(f"{title}: fires", rates["big"] > limit >= rates["small"], True)


def check_bucket_rates(dash):
    """Grafana picks 15 s buckets for a 6-hour view on many screens. Over a
    steady rate reported every 10 s, each rate panel must draw a flat line
    at every bucket width, not alternate between 2/3 and 4/3 of it."""
    start = int(START.timestamp())
    steady = (  # an hour of reports, 1,000 bytes per counter each
        f"SELECT toDateTime64({start} + 10 * (i + 1), 6, 'UTC') AS ts,"
        " 'pi-a' AS host, 'p' AS comm, 0 AS uid, 1.0 AS scale,"
        + ", ".join(f"toInt64(1000) AS {c}" for c in COUNTERS)
        + " FROM (SELECT arrayJoin(range(360)) AS i)"
    )
    for panel in dash["panels"]:
        if panel["type"] != "timeseries":
            continue
        sql = panel["targets"][0]["rawSql"].replace(
            "netmon.traffic_live", f"({steady})"
        )
        title = panel["title"]
        seen = set()
        for interval in (10, 15, 20, 60):
            rates = [r["bytes_per_s"] for r in query(sql, interval)][1:-1]  # whole
            check_eq(f"{title}: flat at {interval} s buckets", len(set(rates)), 1)
            seen |= set(rates)
        check_eq(f"{title}: the same rate at every width", len(seen), 1)


def check_alerts():
    """Each alert query must give Grafana one number per row, labelled by
    its string columns, or the rule cannot evaluate."""
    import yaml

    path = ROOT / "grafana/provisioning/alerting/netmon-rules.yaml"
    groups = yaml.safe_load(path.read_text())["groups"]
    for rule in (rule for group in groups for rule in group["rules"]):
        query_step, threshold = rule["data"]
        rows = query(query_step["model"]["rawSql"])
        title = rule["title"]
        if rule["uid"] == "netmon-host-silent":
            check_eq(
                f"{title}: hosts", sorted(r["host"] for r in rows), ["pi-a", "pi-b"]
            )
            # Pending 8m at 2m a check: it fires on the 5th failed check.
            group = next(g for g in groups if rule in g["rules"])
            minutes = int(rule["for"].removesuffix("m"))
            every = int(group["interval"].removesuffix("m"))
            check_eq(f"{title}: checks before it fires", minutes // every + 1, 5)
        if rule["uid"] == "netmon-converter-behind":
            check_eq(f"{title}: rows", len(rows), 1)
        if rule["uid"] == "netmon-high-traffic":
            limit = threshold["model"]["conditions"][0]["evaluator"]["params"][0]
            check_high_traffic(title, query_step["model"]["rawSql"], limit)
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
