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

Runs take turns through a lock file (--lock), and a run that stops partway
leaves a marker on this machine's disk (--marker), so the next one deletes
the metadata it wrote but never committed. Only main() does these; call
convert() and maintain() only while no other run can.
"""

import argparse
import datetime as dt
import fcntl
import json
import re
import socket
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NoSuchTableError
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


LOCK = "/run/netmon-iceberg.lock"  # in /run, which only root can write
MARKER = "/var/lib/netmon/iceberg-run.json"  # local, and kept across reboots
COMPACT_MAX_ROWS = 1_000_000  # bigger days stay as they are, to bound memory
# NNNNN-UUID.metadata.json, numbered from 00000 by each commit.
METADATA_NAME = re.compile(r"(\d+)-.+\.metadata\.json")


def local_path(uri):
    """file:///a%20b/c or /a b/c -> Path('/a b/c'), resolved."""
    return Path(urllib.parse.unquote(urllib.parse.urlparse(uri).path)).resolve()


def metadata_files(warehouse):
    """{path: version} of the table's metadata files in the warehouse."""
    metadata = Path(warehouse).resolve() / "netmon" / "traffic" / "metadata"
    files = {}
    for path in metadata.glob("*.metadata.json"):
        if match := METADATA_NAME.fullmatch(path.name):
            files[path] = int(match[1])
    return files


def complete(path):
    """Whether a metadata file parses and its current snapshot's manifest
    list exists. A run that stops partway through a commit can leave one
    that doesn't. Read errors are raised, not taken as incomplete."""
    try:
        metadata = json.loads(path.read_text())
    except ValueError:
        return False
    current = metadata.get("current-snapshot-id")
    if current in (None, -1):
        return True  # a new table, without snapshots
    for snapshot in metadata.get("snapshots", []):
        if snapshot.get("snapshot-id") == current:
            return local_path(snapshot["manifest-list"]).exists()
    return False


def newest_metadata(warehouse):
    """The table's newest complete metadata file in the warehouse, or None."""
    files = sorted(metadata_files(warehouse).items(), key=lambda f: (f[1], f[0]))
    return next((path for path, _ in reversed(files) if complete(path)), None)


def storage_time(folder):
    """Now, by the clock of the machine storing folder (a NAS)."""
    with tempfile.NamedTemporaryFile(dir=folder) as probe:
        return Path(probe.name).stat().st_mtime


def mark_run(marker, warehouse, table):
    """Notes, while a run of main() loads or maintains, the warehouse, the
    metadata file it started from and when, so the next run can tell that
    it stopped partway, even across a reboot."""
    warehouse = Path(warehouse).resolve()
    run = {"warehouse": str(warehouse), "start": table.metadata_location}
    run["started"] = storage_time(local_path(table.metadata_location).parent)
    Path(marker).write_text(json.dumps(run))


def recover(warehouse, catalog_uri, marker):
    """If the last run on this machine stopped partway, deletes the metadata
    files it wrote after its last commit: the catalog never took them, and
    their rows load again from the raw files. Only when this catalog has
    moved on only from where that run started, so an older copy of it is
    left to open_table() to refuse."""
    warehouse = Path(warehouse).resolve()
    try:
        run = json.loads(Path(marker).read_text())
    except FileNotFoundError:
        return
    except ValueError:
        run = {}
    if run.get("warehouse") != str(warehouse) or not {"start", "started"} <= run.keys():
        return
    table = load_table(open_catalog(warehouse, catalog_uri))
    if table is None:
        return
    current = local_path(table.metadata_location)
    history = {current} | {
        local_path(entry.metadata_file) for entry in table.metadata.metadata_log
    }
    files = metadata_files(warehouse)
    if local_path(run["start"]) not in history or current not in files:
        return
    for path, version in files.items():
        if version > files[current] and path.stat().st_mtime >= run["started"]:
            print(f"deleting uncommitted {path}")
            path.unlink()
    Path(marker).unlink()


def load_table(catalog):
    """The catalog's table, or None if it has none. Exits if its metadata
    file is gone, as when another catalog took the table over long ago."""
    try:
        return catalog.load_table(TABLE)
    except NoSuchTableError:
        return None
    except FileNotFoundError as e:
        sys.exit(
            f"{e.filename or e}: this catalog's metadata is gone; another catalog"
            f" has the table. Remove {TABLE} from this catalog to adopt it."
        )


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
    if warehouse.as_uri() != f"file://{warehouse}":
        # pyiceberg would write to a folder named with the escapes, e.g. a%20b.
        sys.exit(f"{warehouse}: use a warehouse path without spaces or %")
    warehouse.mkdir(parents=True, exist_ok=True)
    catalog = open_catalog(warehouse, catalog_uri)
    # Not create_table_if_not_exists: it writes a metadata file even when the
    # table exists.
    if table := load_table(catalog):
        files = metadata_files(warehouse)
        metadata = newest_metadata(warehouse)
        current = files.get(local_path(table.metadata_location), -1)
        if metadata and files[metadata] > current:
            sys.exit(
                f"{metadata} is newer than {table.metadata_location}, which"
                f" this catalog has: another catalog has committed since."
                f" Remove {TABLE} from this catalog to adopt it."
            )
        return table
    metadata = newest_metadata(warehouse)
    if metadata:
        print(f"adopting {metadata}")
        return catalog.register_table(TABLE, metadata.as_uri())
    return catalog.create_table(TABLE, schema=SCHEMA, partition_spec=SPEC)


def hour_of(path):
    """RAW/.../2026-10-22/11.jsonl -> 2026-10-22 11:00 UTC, or None."""
    try:
        day = dt.date.fromisoformat(path.parent.name)
        return dt.datetime(day.year, day.month, day.day, int(path.stem), tzinfo=dt.UTC)
    except ValueError:
        return None


def first_host(path):
    """The host of a raw file, from its first parseable line."""
    with open(path) as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                return row.get("host", socket.gethostname())
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
            except (ValueError, KeyError, TypeError, OverflowError, OSError):
                bad += 1
    if bad:
        print(f"{path}: skipped {bad} malformed line(s)", file=sys.stderr)
    return rows


def raw_files(raw):
    """RAW's raw files, skipping folders starting with . # or @ (Synology's
    #recycle and @eaDir)."""
    raw = Path(raw)
    for path in sorted(raw.rglob("*.jsonl")):
        if not any(part[0] in ".#@" for part in path.relative_to(raw).parts[:-1]):
            yield path


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
    hours, files = {}, []  # (host, hour) -> {row's values: row}
    for path in raw_files(raw):
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
        for row in rows:
            values = tuple(row.get(name) for name in ARROW_SCHEMA.names)
            hours.setdefault((row["host"], start), {})[values] = row
        files.append(path)
    # Two copies of an hour (a renamed Pi's old folder, a backup) share their
    # rows, which count once.
    new = {key: rows for key, rows in hours.items() if len(rows) > counts.get(key, 0)}
    replace = [key for key in new if key in counts]
    if new:
        with table.transaction() as tx:
            for host, start in replace:
                tx.delete(hour_filter(host, start))
            rows = [row for hour in new.values() for row in hour.values()]
            tx.append(pa.Table.from_pylist(rows, schema=ARROW_SCHEMA))
    for path in files:
        print(f"loaded {path}")
    return sum(len(rows) for rows in new.values()) - sum(counts[k] for k in replace)


def day_filter(start):
    return And(
        GreaterThanOrEqual("ts", start.isoformat()),
        LessThan("ts", (start + dt.timedelta(days=1)).isoformat()),
    )


def compact(table, before):
    """Rewrites each day before `before` held in several files as one file."""
    files, rows = {}, {}
    for task in table.scan(row_filter=LessThan("ts", before.isoformat())).plan_files():
        day = task.file.partition[0]  # days since 1970-01-01
        files[day] = files.get(day, 0) + 1
        rows[day] = rows.get(day, 0) + task.file.record_count
    days = sorted(
        day for day, n in files.items() if n > 1 and rows[day] <= COMPACT_MAX_ROWS
    )
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
    manifests = {}  # shared by snapshots, so read each once
    for snapshot in table.snapshots():
        uris.add(snapshot.manifest_list)
        for manifest in snapshot.manifests(table.io):
            manifests[manifest.manifest_path] = manifest
    for path, manifest in manifests.items():
        uris.add(path)
        entries = manifest.fetch_manifest_entry(table.io, discard_deleted=False)
        uris |= {entry.data_file.file_path for entry in entries}
    return {local_path(uri) for uri in uris}


def delete_unreferenced(table, before):
    """Deletes files in the table's folder that nothing uses, if older than
    `before`, so a converter run in progress keeps its new files."""
    root = local_path(table.location())
    try:
        keep = referenced_files(table)
    except FileNotFoundError as e:
        sys.exit(f"{root}: not deleting, the metadata names missing {e.filename or e}")
    if missing := [path for path in keep if not path.exists()]:
        sys.exit(f"{root}: not deleting, the metadata names missing {missing[0]}")
    # Compare file times with the clock of the machine storing them (a NAS).
    skew = storage_time(root) - time.time()
    deleted = 0
    for path in root.rglob("*"):
        if (
            path.is_file()
            and path not in keep
            and path.stat().st_mtime < before.timestamp() + skew
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
    parser.add_argument(
        "--lock",
        default=LOCK,
        help=f"lock file, so that loads and maintenance take turns (default {LOCK})",
    )
    parser.add_argument(
        "--marker",
        default=MARKER,
        help=f"where a run notes it is going, on local disk (default {MARKER})",
    )
    args = parser.parse_args()
    lock = open(args.lock, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("waiting for another run to finish", file=sys.stderr, flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
    recover(args.warehouse, args.catalog, args.marker)
    table = open_table(args.warehouse, args.catalog)  # exits if it's stale
    Path(args.marker).parent.mkdir(parents=True, exist_ok=True)
    mark_run(args.marker, args.warehouse, table)
    if args.maintain:
        done = maintain(
            args.warehouse, keep_days=args.keep_days, catalog_uri=args.catalog
        )
        print(", ".join(f"{n} {what}" for what, n in done.items()))
    else:
        n = convert(args.raw, args.warehouse, days=args.days, catalog_uri=args.catalog)
        print(f"added {n} rows to {TABLE}")
    Path(args.marker).unlink()


if __name__ == "__main__":
    main()
