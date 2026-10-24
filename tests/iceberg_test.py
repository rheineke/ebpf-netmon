"""Unit test for tools/to_iceberg.py. Needs no root.

uv run tests/iceberg_test.py

Partway through, it moves the table from the default SQLite catalog to
$NETMON_TEST_CATALOG (a SQLAlchemy URI), or to a second SQLite file.
"""

import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pyiceberg.exceptions import NoSuchTableError

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import to_iceberg  # noqa: E402

DAY = 86400
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


def readable(table):
    """Whether every snapshot's files are all there."""
    try:
        for snapshot in table.snapshots():
            table.scan(snapshot_id=snapshot.snapshot_id).to_arrow()
    except FileNotFoundError:
        return False
    return True


def mark_run(marker, warehouse, start):
    """Leaves a run marker as main() does, as of a minute ago."""
    run = {"warehouse": str(Path(warehouse).resolve()), "start": str(start)}
    marker.write_text(json.dumps(run | {"started": time.time() - 60}))


# Runs main() with convert() failing after the run marker is written.
STOPS = (
    "import sys, to_iceberg; sys.argv[0] = 'to_iceberg';"
    " to_iceberg.convert = lambda *args, **kwargs: sys.exit(3); to_iceberg.main()"
)


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

        # A run that stops partway can leave newer metadata files the
        # catalog never took: cut off, without their manifest list, or
        # complete (the catalog update failed).
        current = to_iceberg.newest_metadata(warehouse)
        version = int(current.name.split("-")[0])
        tie = current.with_name(f"{version:05d}-tie.metadata.json")
        tie.write_text(current.read_text())
        cut = current.with_name(f"{version + 1:05d}-cut.metadata.json")
        cut.write_text(current.read_text()[:100])
        unlisted = current.with_name(f"{version + 1:05d}-unlisted.metadata.json")
        unlisted.write_text(current.read_text().replace("/snap-", "/gone-"))
        odd = current.with_name("odd.metadata.json")
        odd.write_text("{}")
        check(
            "same-version, cut-off, unlisted or odd files don't stop runs",
            refuses(to_iceberg.open_table, warehouse, uri),
            False,
        )
        failed = current.with_name(f"{version + 2:05d}-failed.metadata.json")
        failed.write_text(current.read_text())
        check(
            "a complete newer file does",
            refuses(to_iceberg.open_table, warehouse, uri),
            True,
        )
        marker = Path(tmp) / "run.json"
        for name, other, start in (
            ("another warehouse's run marker", Path(tmp) / "other", current),
            ("the marker of a run from metadata this catalog lacks", warehouse, "/x"),
        ):
            mark_run(marker, other, start)
            to_iceberg.recover(warehouse, uri, marker)
            check(
                f"{name} is left alone",
                (failed.exists(), marker.exists()),
                (True, True),
            )
        early = current.with_name(f"{version + 3:05d}-early.metadata.json")
        early.write_text(current.read_text())
        mark_run(marker, warehouse, current)
        os.utime(early, (time.time() - 120,) * 2)
        to_iceberg.recover(warehouse, uri, marker)
        check(
            "after a run stopped, the newer files it wrote go",
            [f.exists() for f in (tie, cut, unlisted, failed, early, odd, marker)],
            [True, False, False, False, True, True, False],
        )
        for path in (tie, early, odd):
            path.unlink()
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

        # Copies of pi-f's 10:00 file: whole in a backup folder and in
        # Synology's #recycle, and its first lines in an old folder.
        write_hour(raw, "pi-f", h10, 5)
        path = raw / "pi-f" / h10.strftime("%Y-%m-%d") / h10.strftime("%H.jsonl")
        for folder in ("pi-f.bak", "#recycle/pi-f", "pi-f-old"):
            copy = raw / folder / path.relative_to(raw / "pi-f")
            copy.parent.mkdir(parents=True)
            lines = path.read_text().splitlines(keepends=True)
            copy.write_text("".join(lines[:3] if folder == "pi-f-old" else lines))
        # #recycle's also has a row the real file doesn't.
        row = json.loads(lines[0]) | {"pid": 999}
        with open(raw / "#recycle" / path.relative_to(raw), "a") as f:
            f.write(json.dumps(row) + "\n")
        check("rows in copies of an hour load once", load(later), 5)
        check("and stay so", load(later), 0)

        # Odd raw files and lines are skipped.
        write_hour(raw, "pi-g", h10, 2, extra="null\n[1]\n5\n")
        path = raw / "pi-g" / h10.strftime("%Y-%m-%d") / h10.strftime("%H.jsonl")
        path.write_text("null\n" + path.read_text())
        (path.parent / "24.jsonl").write_text("x\n")
        (path.parent / "-1.jsonl").write_text("x\n")
        check("odd files and lines are skipped", load(later), 2)

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
        check(
            "today's day isn't compacted",
            to_iceberg.maintain(warehouse, NOW, catalog_uri=uri)["days compacted"],
            0,
        )
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
        table = to_iceberg.open_table(warehouse, uri)
        check("snapshots kept", len(table.snapshots()) > 1, True)
        metadata = warehouse / "netmon" / "traffic" / "metadata"
        check(
            "fresh unused files are kept",
            to_iceberg.delete_unreferenced(
                table, dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
            ),
            0,
        )
        # Our clock 2 days behind the storage's: its file from 1.5 days ago
        # is past the 1-day grace, and its new one isn't.
        stamp = time.time()
        old = metadata / "old.avro"
        old.touch()
        os.utime(old, (stamp - 1.5 * DAY, stamp - 1.5 * DAY))
        clock = time.time
        time.time = lambda: clock() - 2 * DAY
        try:
            local_now = dt.datetime.fromtimestamp(stamp - 2 * DAY, dt.UTC)
            deleted = to_iceberg.delete_unreferenced(
                table, local_now - dt.timedelta(days=1)
            )
        finally:
            time.time = clock
        check(
            "the grace period follows the storage's clock",
            (deleted, old.exists(), (metadata / "stray.avro").exists()),
            (1, False, True),
        )
        far = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
        to_iceberg.delete_unreferenced(table, far)
        check("kept snapshots still read whole", readable(table), True)

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

        # main() waits for a run holding the lock, then recovers from the
        # last run, which stopped partway, and loads.
        current = to_iceberg.newest_metadata(warehouse)
        version = int(current.name.split("-")[0])
        mark_run(marker, warehouse, current)
        failed = current.with_name(f"{version + 1:05d}-failed.metadata.json")
        failed.write_text(current.read_text())
        lock = Path(tmp) / "lock"
        run = ["--raw", str(raw), "--warehouse", str(warehouse)]
        run += ["--lock", str(lock), "--marker", str(marker)]
        main = [sys.executable, str(TOOLS / "to_iceberg.py"), *run]
        with open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            proc = subprocess.Popen(
                [*main, "--catalog", uri],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            check(
                "a run waits for the lock",
                proc.stderr.readline().strip(),
                "waiting for another run to finish",
            )
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            check(
                "and does nothing meanwhile",
                (proc.poll(), failed.exists(), marker.exists()),
                (None, True, True),
            )
        check("then runs", proc.wait(timeout=120), 0)
        check("having deleted the uncommitted file", failed.exists(), False)
        check("and leaves no run marker", marker.exists(), False)

        env = os.environ | {"PYTHONPATH": str(TOOLS)}
        stopped = subprocess.run(
            [sys.executable, "-c", STOPS, *run, "--catalog", uri],
            env=env,
            capture_output=True,
        )
        check(
            "a run that stops partway leaves its marker",
            (stopped.returncode, marker.exists()),
            (3, True),
        )
        current = to_iceberg.newest_metadata(warehouse)
        failed.write_text(current.read_text())  # as if its commit failed
        check(
            "the next run cleans up after it",
            (subprocess.run([*main, "--catalog", uri]).returncode, failed.exists()),
            (0, False),
        )
        stale = subprocess.run(main, capture_output=True)  # the SQLite catalog
        check(
            "a stale catalog's refused run leaves no marker",
            (stale.returncode != 0, marker.exists()),
            (True, False),
        )

        table = to_iceberg.open_table(warehouse, uri)
        data = next(iter(table.scan().plan_files())).file.file_path
        to_iceberg.local_path(data).unlink()
        (metadata / "stray.avro").touch()
        check(
            "cleanup deletes nothing when a referenced file is missing",
            (
                refuses(to_iceberg.delete_unreferenced, table, far),
                any(metadata.glob("stray.avro")),
            ),
            (True, True),
        )
    print("PASS")


if __name__ == "__main__":
    main()
