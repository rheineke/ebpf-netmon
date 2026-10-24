"""Unit test for tools/to_iceberg.py. Needs no root.

uv run tests/iceberg_test.py

Partway through, it moves the table from the default SQLite catalog to
$NETMON_TEST_CATALOG (a SQLAlchemy URI), or to a second SQLite file.
"""

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

from pyiceberg.exceptions import NoSuchTableError

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


def refuses(f, *args):
    try:
        f(*args)
    except SystemExit:
        return True
    return False


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
        metadata = len(list(warehouse.rglob("*.metadata.json")))
        to_iceberg.convert(raw, warehouse, later)
        check(
            "a run with nothing to add writes no metadata",
            len(list(warehouse.rglob("*.metadata.json"))),
            metadata,
        )

        # Moving to another catalog adopts the newest metadata.
        uri = os.environ.get("NETMON_TEST_CATALOG") or f"sqlite:///{tmp}/moved.db"
        catalog = to_iceberg.open_catalog(warehouse, uri)
        try:  # a previous run's table; its files are gone
            catalog.drop_table(to_iceberg.TABLE)
        except NoSuchTableError:
            pass
        moved = to_iceberg.open_table(warehouse, uri).scan().to_arrow()
        check("moved catalog has the rows", moved.num_rows, 19)

        def load(now):
            return to_iceberg.convert(raw, warehouse, now, catalog_uri=uri)

        # A NAS copy of pi-c's 10:00 file stopped partway through, then the
        # rest arrived, along with a late hour.
        write_hour(raw, "pi-c", h10, 3)
        check("partial hour loads", load(later), 3)
        # The SQLite catalog, now stale.
        check(
            "the old catalog refuses to run",
            refuses(to_iceberg.open_table, warehouse),
            True,
        )

        # A lost commit leaves a second file of the current version, and a
        # run that stops partway, a newer cut-off one.
        current = to_iceberg.newest_metadata(warehouse)
        version = int(current.name.split("-")[0])
        lost = current.with_name(f"{version:05d}-lost.metadata.json")
        lost.write_text(current.read_text())
        cut = current.with_name(f"{version + 1:05d}-cut.metadata.json")
        cut.write_text(current.read_text()[:100])
        odd = current.with_name("odd.metadata.json")
        odd.write_text("{}")
        check(
            "newest skips cut-off metadata",
            to_iceberg.newest_metadata(warehouse) != cut,
            True,
        )
        to_iceberg.open_table(warehouse, uri)
        check(
            "uncommitted metadata deleted, odd names left",
            (lost.exists(), cut.exists(), odd.exists()),
            (False, False, True),
        )
        odd.unlink()
        check(
            "a warehouse path with a space is refused",
            refuses(to_iceberg.open_table, Path(tmp) / "a b"),
            True,
        )
        write_hour(raw, "pi-c", h10, 8)
        write_hour(raw, "pi-c", h10 - dt.timedelta(hours=1), 2)
        check(
            "completed hour replaced, late hour added",
            load(later),
            5 + 2,
        )
        check("then nothing", load(later), 0)

        # pi-d was renamed pi-e partway through its 10:00 hour.
        write_hour(raw, "pi-d", h10, 4)
        path = raw / "pi-d" / h10.strftime("%Y-%m-%d") / h10.strftime("%H.jsonl")
        with open(path, "a") as f:
            for line in path.read_text().splitlines():
                row = json.loads(line) | {"host": "pi-e"}
                f.write(json.dumps(row | {"ts": row["ts"] + 1800}) + "\n")
        check("renamed mid-hour loads", load(later), 8)
        check("and stays loaded", load(later), 0)

        table = to_iceberg.open_table(warehouse, uri).scan().to_arrow()
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
        check(
            "the moved catalog commits the newest metadata",
            to_iceberg.open_table(warehouse, uri).metadata_location,
            to_iceberg.newest_metadata(warehouse).as_uri(),
        )
        files = list(warehouse.rglob("*.parquet"))
        check(
            "parquet partitioned by day",
            all("day=2026-10-22" in str(p) for p in files),
            True,
        )

        # A day in another file, a stray file and a snapshot per load so far.
        write_hour(raw, "pi-a", h10 - dt.timedelta(days=1), 2)
        check("previous day loads", load(later), 2)
        (warehouse / "netmon" / "traffic" / "metadata" / "stray.avro").touch()
        before = to_iceberg.open_table(warehouse, uri).scan().to_arrow()
        to_iceberg.COMPACT_MAX_ROWS = 5
        check(
            "days over COMPACT_MAX_ROWS aren't compacted",
            to_iceberg.maintain(warehouse, catalog_uri=uri)["days compacted"],
            0,
        )
        to_iceberg.COMPACT_MAX_ROWS = 1_000_000
        check(
            "maintain now compacts 2026-10-22 only and keeps recent snapshots",
            to_iceberg.maintain(warehouse, catalog_uri=uri),
            {"days compacted": 1, "snapshots expired": 0, "files deleted": 0},
        )
        # A month on, everything is past --keep-days and the grace period.
        month = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
        done = to_iceberg.maintain(warehouse, month, catalog_uri=uri)
        check("a month on, nothing to compact", done["days compacted"], 0)
        table = to_iceberg.open_table(warehouse, uri)
        after = table.scan().to_arrow()
        check("same rows after compacting", after.num_rows, before.num_rows)
        check(
            "same totals after compacting",
            [sum(after[c].to_pylist()) for c in to_iceberg.COUNTERS],
            [sum(before[c].to_pylist()) for c in to_iceberg.COUNTERS],
        )
        check("one data file per day", len(list(table.scan().plan_files())), 2)
        check("only the current snapshot is left", len(table.snapshots()), 1)
        check("stray file deleted", any(warehouse.rglob("stray.avro")), False)
        check(
            "only the current files are left",
            sorted(p.name for p in warehouse.rglob("*.parquet")),
            sorted(Path(t.file.file_path).name for t in table.scan().plan_files()),
        )
        check(
            "and nothing more next time",
            to_iceberg.maintain(warehouse, month, catalog_uri=uri),
            {"days compacted": 0, "snapshots expired": 0, "files deleted": 0},
        )
        check("still loads", load(later + dt.timedelta(hours=1)), 0)
    print("PASS")


if __name__ == "__main__":
    main()
