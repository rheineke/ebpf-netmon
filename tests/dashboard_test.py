"""Checks the Grafana dashboard's queries against ClickHouse.

  uv run tests/dashboard_test.py load WAREHOUSE   # as root, or with write access
  scripts/install_clickhouse.sh WAREHOUSE
  python3 tests/dashboard_test.py check

load writes two hosts of known traffic into a new Iceberg table. check runs
every panel of grafana/dashboards/netmon.json through clickhouse-client, as
Grafana's user, with Grafana's macros filled in, and checks the totals.
"""

import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
HOURS, ROWS = 3, 6  # per host, rows per hour
COUNTERS = {"tcp_tx": 1, "tcp_rx": 2, "udp_tx": 3, "udp_rx": 4, "nfs_read": 5}
COUNTERS["nfs_write"] = 6
SCALE = 2.0


def load(warehouse):
    sys.path.insert(0, str(ROOT / "tools"))
    import to_iceberg

    with tempfile.TemporaryDirectory() as raw:
        for host in ("pi-a", "pi-b"):
            for h in range(HOURS):
                start = START + dt.timedelta(hours=h)
                path = Path(raw, host, f"{start:%Y-%m-%d}", f"{start:%H}.jsonl")
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w") as f:
                    for i in range(ROWS):
                        row = {"ts": int(start.timestamp()) + 600 * i, "host": host}
                        row |= {"interval_s": 10, "pid": 100 + i % 2, "uid": i % 2}
                        row |= {"comm": f"proc{i % 2}", "scale": SCALE}
                        f.write(json.dumps(row | COUNTERS) + "\n")
        now = START + dt.timedelta(days=1)
        n = to_iceberg.convert(raw, warehouse, now, days=2)
    print(f"loaded {n} rows")


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
        ["clickhouse-client", "--user", "grafana", "--format", "JSONEachRow"],
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


def check():
    dash = json.loads((ROOT / "grafana/dashboards/netmon.json").read_text())
    per_host = HOURS * ROWS * SCALE  # each counter's weight per host
    hosts = [r["host"] for r in query(dash["templating"]["list"][0]["query"])]
    check_eq("host variable", hosts, ["pi-a", "pi-b"])
    for panel in dash["panels"]:
        rows = query(panel["targets"][0]["rawSql"])
        title = panel["title"]
        if panel["type"] == "timeseries":
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
    print("PASS")


if __name__ == "__main__":
    if sys.argv[1:2] == ["load"] and len(sys.argv) == 3:
        load(sys.argv[2])
    elif sys.argv[1:] == ["check"]:
        check()
    else:
        sys.exit(__doc__)
