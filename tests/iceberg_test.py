"""Unit test for tools/to_iceberg.py. Needs no root.

uv run tests/iceberg_test.py
"""

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import to_iceberg  # noqa: E402

NOW = dt.datetime(2026, 10, 22, 12, 30, tzinfo=dt.UTC)


def write_hour(raw, host, start, rows, extra=""):
    path = raw / host / start.strftime("%Y-%m-%d") / start.strftime("%H.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for i in range(rows):
            ts = int(start.timestamp()) + 10 * i
            row = {
                "ts": ts,
                "host": host,
                "interval_s": 10,
                "pid": 100 + i,
                "uid": 0,
                "comm": "proc",
                "tcp_tx": 1,
                "tcp_rx": 2,
                "udp_tx": 3,
                "udp_rx": 4,
                "nfs_read": 5,
                "nfs_write": 6,
                "scale": 1,
            }
            f.write(json.dumps(row) + "\n")
        f.write(extra)


def check(name, got, want):
    if got != want:
        sys.exit(f"FAIL: {name}: got {got}, want {want}")
    print(f"ok  : {name}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        raw, warehouse = Path(tmp) / "raw", Path(tmp) / "warehouse"
        h10 = NOW.replace(hour=10, minute=0)
        h11 = NOW.replace(hour=11, minute=0)
        h12 = NOW.replace(hour=12, minute=0)
        write_hour(raw, "pi-a", h10, 5)
        write_hour(raw, "pi-a", h11, 7, extra='{"ts": 17926')  # cut off
        write_hour(raw, "pi-b", h11, 3)
        write_hour(raw, "pi-a", h12, 4)  # current hour: not yet
        write_hour(raw, "pi-a", h10 - dt.timedelta(days=8), 9)  # older than --days

        check(
            "first run appends finished hours",
            to_iceberg.convert(raw, warehouse, NOW),
            15,
        )
        check("second run appends nothing", to_iceberg.convert(raw, warehouse, NOW), 0)

        later = NOW + dt.timedelta(hours=1)
        check("next hour appends only it", to_iceberg.convert(raw, warehouse, later), 4)

        table = to_iceberg.open_table(warehouse).scan().to_arrow()
        check("rows in table", table.num_rows, 19)
        check("hosts", sorted(set(table["host"].to_pylist())), ["pi-a", "pi-b"])
        check("nfs_write total", sum(table["nfs_write"].to_pylist()), 6 * 19)
        check("ts is UTC", str(table.schema.field("ts").type), "timestamp[us, tz=UTC]")

        # A NAS copy of pi-c's 10:00 file stopped partway through, then the
        # rest arrived, along with a late hour.
        write_hour(raw, "pi-c", h10, 3)
        check("partial hour loads", to_iceberg.convert(raw, warehouse, later), 3)
        write_hour(raw, "pi-c", h10, 8)
        write_hour(raw, "pi-c", h10 - dt.timedelta(hours=1), 2)
        check(
            "completed hour replaced, late hour added",
            to_iceberg.convert(raw, warehouse, later),
            5 + 2,
        )
        check("then nothing", to_iceberg.convert(raw, warehouse, later), 0)

        # pi-d was renamed pi-e partway through its 10:00 hour.
        write_hour(raw, "pi-d", h10, 4)
        path = raw / "pi-d" / h10.strftime("%Y-%m-%d") / h10.strftime("%H.jsonl")
        with open(path, "a") as f:
            for line in path.read_text().splitlines():
                row = json.loads(line) | {"host": "pi-e"}
                f.write(json.dumps(row | {"ts": row["ts"] + 1800}) + "\n")
        check("renamed mid-hour loads", to_iceberg.convert(raw, warehouse, later), 8)
        check("and stays loaded", to_iceberg.convert(raw, warehouse, later), 0)

        table = to_iceberg.open_table(warehouse).scan().to_arrow()
        hosts = table["host"].to_pylist()
        check("pi-d and pi-e rows", (hosts.count("pi-d"), hosts.count("pi-e")), (4, 4))
        check("pi-c rows, no duplicates", hosts.count("pi-c"), 8 + 2)
        keys = [
            (h, t, p)
            for h, t, p in zip(
                hosts, table["ts"].to_pylist(), table["pid"].to_pylist(), strict=True
            )
        ]
        check("rows unique", len(set(keys)), len(keys))
        check("pi-a and pi-b untouched", hosts.count("pi-a") + hosts.count("pi-b"), 19)
        files = list(warehouse.rglob("*.parquet"))
        check(
            "parquet partitioned by day",
            all("day=2026-10-22" in str(p) for p in files),
            True,
        )
    print("PASS")


if __name__ == "__main__":
    main()
