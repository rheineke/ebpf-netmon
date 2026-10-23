"""Append netmon's raw hourly JSON-lines files to an Iceberg table.

  python3 tools/to_iceberg.py --raw DIR --warehouse DIR

Reads each RAW/.../YYYY-MM-DD/HH.jsonl from the last --days days whose UTC
hour has ended, and appends the hours not already in the table to
netmon.traffic in WAREHOUSE, as Parquet partitioned by day. The raw files are
left as they are.

An hour counts as loaded when the table has any row for that host and hour.
Each run commits all new hours in one append, so a crash loads all of them
or none. The catalog is a SQLite file in the warehouse; run one converter
per warehouse.
"""

import argparse
import datetime as dt
import json
import socket
import sys
from pathlib import Path

import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.expressions import And, EqualTo, GreaterThanOrEqual, LessThan
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import DayTransform
from pyiceberg.types import (
    DoubleType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestamptzType,
)

TABLE = "netmon.traffic"
COUNTERS = ["tcp_tx", "tcp_rx", "udp_tx", "udp_rx", "nfs_read", "nfs_write"]

SCHEMA = Schema(
    NestedField(1, "ts", TimestamptzType(), required=True),
    NestedField(2, "host", StringType(), required=True),
    NestedField(3, "interval_s", IntegerType(), required=True),
    NestedField(4, "pid", LongType(), required=True),
    NestedField(5, "uid", LongType(), required=True),
    NestedField(6, "comm", StringType(), required=True),
    *(
        NestedField(7 + i, name, LongType(), required=True)
        for i, name in enumerate(COUNTERS)
    ),
    NestedField(13, "scale", DoubleType(), required=True),
)
SPEC = PartitionSpec(
    PartitionField(source_id=1, field_id=1000, transform=DayTransform(), name="day")
)
ARROW_SCHEMA = pa.schema(
    [
        pa.field("ts", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("host", pa.string(), nullable=False),
        pa.field("interval_s", pa.int32(), nullable=False),
        pa.field("pid", pa.int64(), nullable=False),
        pa.field("uid", pa.int64(), nullable=False),
        pa.field("comm", pa.string(), nullable=False),
        *(pa.field(name, pa.int64(), nullable=False) for name in COUNTERS),
        pa.field("scale", pa.float64(), nullable=False),
    ]
)


def open_table(warehouse):
    warehouse = Path(warehouse).resolve()
    warehouse.mkdir(parents=True, exist_ok=True)
    catalog = SqlCatalog(
        "netmon",
        uri=f"sqlite:///{warehouse / 'catalog.db'}",
        warehouse=warehouse.as_uri(),
    )
    catalog.create_namespace_if_not_exists("netmon")
    return catalog.create_table_if_not_exists(TABLE, schema=SCHEMA, partition_spec=SPEC)


def hour_of(path):
    """RAW/.../2026-10-22/11.jsonl -> 2026-10-22 11:00 UTC, or None."""
    try:
        day = dt.date.fromisoformat(path.parent.name)
        hour = int(path.stem)
    except ValueError:
        return None
    return dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.UTC)


def first_host(path):
    """The host of a raw file, from its first parseable line."""
    with open(path) as f:
        for line in f:
            try:
                return json.loads(line).get("host", socket.gethostname())
            except ValueError:
                continue
    return None


def read_rows(path):
    """Parses a raw file, skipping lines cut off by a crash mid-write."""
    rows, bad = [], 0
    with open(path) as f:
        for line in f:
            try:
                row = json.loads(line)
                row["ts"] = dt.datetime.fromtimestamp(row["ts"], dt.UTC)
                row.setdefault("host", socket.gethostname())  # before -o had it
                for name in COUNTERS:
                    row.setdefault(name, 0)  # before NFS counters existed
                rows.append(row)
            except (ValueError, KeyError, TypeError):
                bad += 1
    if bad:
        print(f"{path}: skipped {bad} malformed line(s)", file=sys.stderr)
    return rows


def loaded(table, host, start):
    """True if the table already has rows for this host and hour."""
    end = start + dt.timedelta(hours=1)
    scan = table.scan(
        row_filter=And(
            EqualTo("host", host),
            GreaterThanOrEqual("ts", start.isoformat()),
            LessThan("ts", end.isoformat()),
        ),
        selected_fields=("host",),
        limit=1,
    )
    return scan.to_arrow().num_rows > 0


def convert(raw, warehouse, now=None, days=7):
    """Appends every finished, unloaded hour. Returns the rows appended."""
    now = now or dt.datetime.now(dt.UTC)
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    oldest = this_hour - dt.timedelta(days=days)
    table = open_table(warehouse)
    new_rows, files = [], []
    for path in sorted(Path(raw).rglob("*.jsonl")):
        start = hour_of(path)
        if start is None or not oldest <= start < this_hour:
            continue  # not a raw file, too old, or still being written
        # One host per file; an hour already loaded is skipped as a whole.
        host = first_host(path)
        if host is None or loaded(table, host, start):
            continue
        new_rows.extend(read_rows(path))
        files.append(path)
    if new_rows:
        table.append(pa.Table.from_pylist(new_rows, schema=ARROW_SCHEMA))
    for path in files:
        print(f"loaded {path}")
    return len(new_rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", default="/var/lib/netmon/raw")
    parser.add_argument("--warehouse", required=True)
    parser.add_argument(
        "--days", type=int, default=7, help="look back this many days (default 7)"
    )
    args = parser.parse_args()
    n = convert(args.raw, args.warehouse, days=args.days)
    print(f"appended {n} rows to {TABLE}")


if __name__ == "__main__":
    main()
