"""Append netmon's raw hourly JSON-lines files to an Iceberg table.

  python3 tools/to_iceberg.py --raw DIR --warehouse DIR [--catalog URI]
  python3 tools/to_iceberg.py --maintain --warehouse DIR [--catalog URI]

Reads each RAW/.../YYYY-MM-DD/HH.jsonl from the last --days days whose UTC
hour has ended, and loads it into netmon.traffic in WAREHOUSE, as Parquet
partitioned by day. RAW may hold several hosts' files, e.g. RAW/HOST/....
The raw files are left as they are.

A file is loaded again when it has more rows than the table holds for its
host and hour, so an hour copied before it was complete (a NAS that was
down) is replaced once the rest arrives. Each run commits in one
transaction, so a crash loads all of its hours or none. Run one converter
per warehouse.

The catalog, which points at the table's current metadata, is a SQLite file
in the warehouse unless --catalog names a SQLAlchemy URI, such as
postgresql+psycopg2://netmon@/netmon?host=/var/run/postgresql for a local
PostgreSQL (scripts/install_postgres.sh). A catalog without the table
adopts the newest metadata in the warehouse, so changing the URI moves the
table to the new catalog. The old catalog then refuses to run.

--maintain, run weekly, compacts each finished day into one Parquet file,
expires snapshots older than --keep-days (28), and deletes files no
remaining snapshot uses. Time travel works back --keep-days.
"""

import argparse
import datetime as dt
import json
import socket
import sys
import urllib.parse
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
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


def newest_metadata(warehouse):
    """The table's newest metadata file in the warehouse, or None."""
    metadata = Path(warehouse).resolve() / "netmon" / "traffic" / "metadata"
    files = metadata.glob("*.metadata.json")
    # NNNNN-UUID.metadata.json, numbered from 00000 by each commit.
    return max(files, key=lambda p: (int(p.name.split("-")[0]), p.name), default=None)


def open_catalog(warehouse, catalog_uri=None):
    catalog = SqlCatalog(
        "netmon",
        uri=catalog_uri or f"sqlite:///{warehouse / 'catalog.db'}",
        warehouse=warehouse.as_uri(),
    )
    catalog.create_namespace_if_not_exists("netmon")
    return catalog


def open_table(warehouse, catalog_uri=None):
    warehouse = Path(warehouse).resolve()
    warehouse.mkdir(parents=True, exist_ok=True)
    catalog = open_catalog(warehouse, catalog_uri)
    # Not create_table_if_not_exists: it writes a metadata file even when the
    # table exists.
    metadata = newest_metadata(warehouse)
    if catalog.table_exists(TABLE):
        table = catalog.load_table(TABLE)
        if metadata and table.metadata_location != metadata.as_uri():
            sys.exit(
                f"{table.metadata_location} is not the newest metadata,"
                f" {metadata}: another catalog has committed since. Remove"
                f" {TABLE} from this catalog to adopt it."
            )
        return table
    if metadata:
        print(f"adopting {metadata}")
        return catalog.register_table(TABLE, metadata.as_uri())
    return catalog.create_table(TABLE, schema=SCHEMA, partition_spec=SPEC)


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


def hour_counts(table, oldest):
    """Rows in the table per (host, hour) from oldest on."""
    scan = table.scan(
        row_filter=GreaterThanOrEqual("ts", oldest.isoformat()),
        selected_fields=("host", "ts"),
    ).to_arrow()
    hours = pa.table(
        {
            "host": scan["host"],
            "hour": pc.floor_temporal(scan["ts"], unit="hour"),
        }
    )
    counts = hours.group_by(["host", "hour"]).aggregate([([], "count_all")])
    return {(r["host"], r["hour"]): r["count_all"] for r in counts.to_pylist()}


def hour_filter(host, start):
    return And(
        EqualTo("host", host),
        GreaterThanOrEqual("ts", start.isoformat()),
        LessThan("ts", (start + dt.timedelta(hours=1)).isoformat()),
    )


def convert(raw, warehouse, now=None, days=7, catalog_uri=None):
    """Loads every finished hour the table lacks rows of. Returns rows added."""
    now = now or dt.datetime.now(dt.UTC)
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    oldest = this_hour - dt.timedelta(days=days)
    table = open_table(warehouse, catalog_uri)
    counts = hour_counts(table, oldest)
    new_rows, replace, files = [], [], []
    for path in sorted(Path(raw).rglob("*.jsonl")):
        start = hour_of(path)
        if start is None or not oldest <= start < this_hour:
            continue  # not a raw file, too old, or still being written
        # Counting lines is cheap; parse only files that may have new rows.
        host = first_host(path)
        if host is None or path.read_bytes().count(b"\n") <= counts.get(
            (host, start), 0
        ):
            continue
        # A file has one host unless the machine was renamed mid-hour.
        rows = read_rows(path)
        keys = {(row["host"], start) for row in rows}
        if len(rows) <= sum(counts.get(key, 0) for key in keys):
            continue  # the extra lines were malformed
        new_rows.extend(rows)
        files.append(path)
        replace.extend(key for key in keys if key in counts)
    if new_rows:
        with table.transaction() as tx:
            for host, start in replace:
                tx.delete(hour_filter(host, start))
            tx.append(pa.Table.from_pylist(new_rows, schema=ARROW_SCHEMA))
    for path in files:
        print(f"loaded {path}")
    return len(new_rows) - sum(counts[key] for key in replace)


def day_filter(start):
    return And(
        GreaterThanOrEqual("ts", start.isoformat()),
        LessThan("ts", (start + dt.timedelta(days=1)).isoformat()),
    )


def compact(table, before):
    """Rewrites each day before `before` held in several files as one file."""
    files = {}
    for task in table.scan(row_filter=LessThan("ts", before.isoformat())).plan_files():
        day = task.file.partition[0]  # days since 1970-01-01
        files[day] = files.get(day, 0) + 1
    days = sorted(day for day, n in files.items() if n > 1)
    for day in days:  # one day at a time, to bound memory
        start = dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=day)
        rows = table.scan(row_filter=day_filter(start)).to_arrow()
        with table.transaction() as tx:
            tx.delete(day_filter(start))
            tx.append(rows.cast(ARROW_SCHEMA))
    return len(days)


def referenced_files(table):
    """Every file the table's metadata and remaining snapshots use."""
    uris = {table.metadata_location}
    uris |= {entry.metadata_file for entry in table.metadata.metadata_log}
    for snapshot in table.snapshots():
        uris.add(snapshot.manifest_list)
        for manifest in snapshot.manifests(table.io):
            uris.add(manifest.manifest_path)
            entries = manifest.fetch_manifest_entry(table.io, discard_deleted=False)
            uris |= {entry.data_file.file_path for entry in entries}
    return {Path(urllib.parse.urlparse(uri).path) for uri in uris}


def delete_unreferenced(table, before):
    """Deletes files in the table's folder that nothing uses, if older than
    `before`, so a converter run in progress keeps its new files."""
    root = Path(urllib.parse.urlparse(table.location()).path)
    keep = referenced_files(table)
    if not all(path.exists() for path in keep if path.suffix == ".parquet"):
        sys.exit(f"{root}: the metadata names files that aren't there; not deleting")
    deleted = 0
    for path in root.rglob("*"):
        if (
            path.is_file()
            and path not in keep
            and path.stat().st_mtime < before.timestamp()
        ):
            path.unlink()
            deleted += 1
    return deleted


def maintain(warehouse, now=None, keep_days=28, catalog_uri=None):
    """Compacts, expires and cleans the table; returns what it did."""
    now = now or dt.datetime.now(dt.UTC)
    table = open_table(warehouse, catalog_uri)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = compact(table, today)
    snapshots = len(table.snapshots())
    table.maintenance.expire_snapshots().older_than(
        now - dt.timedelta(days=keep_days)
    ).commit()
    table = open_table(warehouse, catalog_uri)
    expired = snapshots - len(table.snapshots())
    deleted = delete_unreferenced(table, now - dt.timedelta(days=1))
    return {
        "days compacted": days,
        "snapshots expired": expired,
        "files deleted": deleted,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", default="/var/lib/netmon/raw")
    parser.add_argument("--warehouse", required=True)
    parser.add_argument(
        "--catalog", help="SQLAlchemy URI of the catalog (default: SQLite in WAREHOUSE)"
    )
    parser.add_argument(
        "--days", type=int, default=7, help="look back this many days (default 7)"
    )
    parser.add_argument(
        "--maintain",
        action="store_true",
        help="compact, expire old snapshots and delete unused files; load nothing",
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=28,
        help="with --maintain, keep snapshots this many days (default 28)",
    )
    args = parser.parse_args()
    if args.maintain:
        done = maintain(
            args.warehouse, keep_days=args.keep_days, catalog_uri=args.catalog
        )
        print(", ".join(f"{n} {what}" for what, n in done.items()))
        return
    n = convert(args.raw, args.warehouse, days=args.days, catalog_uri=args.catalog)
    print(f"added {n} rows to {TABLE}")


if __name__ == "__main__":
    main()
