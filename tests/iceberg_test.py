"""Unit test for tools/to_iceberg.py; needs tools/requirements.txt, not root.

python3 tests/iceberg_test.py
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
        files = list(warehouse.rglob("*.parquet"))
        check(
            "parquet partitioned by day",
            all("day=2026-10-22" in str(p) for p in files),
            True,
        )
    print("PASS")


if __name__ == "__main__":
    main()
