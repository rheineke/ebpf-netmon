# ebpf-netmon

eBPF network traffic monitoring for Raspberry Pis (Debian trixie, arm64).

```
 each Pi                               NAS                   one Pi
┌──────────────────────────────┐      ┌──────────────┐      ┌──────────────────────┐
│ netmon (BPF) ─ hourly JSONL ─┼rsync─▶ raw/HOST/... ├──┬───▶ to_iceberg.py        │
│ /var/lib/netmon/raw          │      │              │  │   │   (hourly)           │
└──────────────────────────────┘      │ Iceberg      ◀──┼───┤                      │
                                      │ warehouse    ├──┴───▶ ClickHouse ─ Grafana │
                                      └──────────────┘      └──────────────────────┘
```

`netmon` counts each process's TCP, UDP and NFS bytes in the kernel, and
every 10 seconds writes a row for each process that moved any, and for each
network interface, to check the processes' rows against. A minute
later the row is on the NAS, and Grafana's live view shows it. Each hour the
converter loads the finished hour into an Iceberg table, which ClickHouse
serves to Grafana's dashboard and alerts.

## netmon

`netmon` prints the TCP and UDP bytes each process sent and received in the
last interval, the NFS file bytes it read and wrote, and those the NFS server
sent for its reads and took for its writes, like `top`:

```
     PID    UID COMM                TCP_TX    TCP_RX    UDP_TX    UDP_RX    NFS_RD    NFS_WR NFS_FETCH  NFS_SENT
     850    999 Plex Media Serv         0B        0B      687B      1.1K     12.0M        0B      4.0M        0B
```

Options: `-i` interval in seconds (default 2), `-c` number of reports,
`-n` rows (default 10, 0 = all), `-k` priority-sample rows (see below), `-j`
JSON lines, `-o DIR` JSON lines appended to hourly files, all rows unless
`-n` says otherwise (see Capture), `-w SECONDS` hold reports at start until
the clock is synchronized.

### How it counts

- **TCP and UDP**: the `sock:sock_send_length` and `sock:sock_recv_length`
  tracepoints (kernel 6.3+; `sendfile()` and `splice()` into a socket reach
  them from 6.5). They fire once per `sock_sendmsg()` and
  `sock_recvmsg()`, which every send and receive goes through, with the
  bytes moved. Peeks are skipped, since the read that follows counts.
  MPTCP counts as TCP. `splice()` from a TCP socket skips them, so a
  kretprobe on `tcp_splice_read` counts it.
- **NFS**: kretprobes on `nfs_file_read`, `nfs_file_splice_read` and
  `nfs_file_write`, and the `nfs:nfs_initiate_read` and
  `nfs:nfs_readpage_done` tracepoints (see NFS traffic below).

It reads only the tracepoints' records and the functions' return values,
never kernel structs. So it needs no CO-RE and no kernel BTF, which Raspberry
Pi kernels don't ship. At startup it checks the tracepoints' record layout
against tracefs. Without the tracepoints, or with another layout, it exits
saying why rather than run without TCP and UDP. In a container it mounts
tracefs itself, which needs `--privileged`.

Traffic over loopback (`127.0.0.1`, say Grafana querying ClickHouse on the
same Pi) counts as both sent and received, as each end is a process here.
So does traffic through a VPN run as a process (Tailscale, wireguard-go,
OpenVPN): the app's TCP inside the tunnel and the daemon's UDP outside it.
A raw IP socket (`SOCK_RAW` with `IPPROTO_TCP`, as some packet tools use)
counts as TCP or UDP too: the tracepoint doesn't tell socket types apart.
Packet capture through `AF_PACKET` (tcpdump, libpcap) isn't counted.

The tracepoints replace kprobes on `tcp_sendmsg`, `tcp_cleanup_rbuf` and
`udp(v6)_sendmsg`/`recvmsg`, which overcounted: a `MSG_WAITALL` read was
counted again each time it slept, peeks counted twice, and so did an IPv6
socket sending to an IPv4 address, which goes through both `udpv6_sendmsg`
and `udp_sendmsg`. The smoke test checks each of these exactly.

A kretprobe can track only so many calls in progress at once: 10 on a 4-core
Pi, across all processes. Each thread blocked inside the function holds one,
so with ten NFS reads waiting on a slow NAS, the next one's bytes go
uncounted. netmon reads the kernel's count of these and logs `netmon:
kretprobe/nfs_file_read missed N calls`. The kernel also skips every kprobe
and tracepoint program that fires on a CPU while a task there is in a BPF
map system call, netmon's own reads of its maps included; netmon logs those
too (`skipped N calls`). On a Pi 4 that's about a hundred calls a day, out
of millions, but a program hammering BPF maps can make it thousands a
second. Probes that can't attach, such as NFS before the `nfs` module loads
at the first mount, are retried every minute. An NFS tracepoint whose layout
differs from the one netmon was built for isn't attached, nor its partner,
and netmon says so; the rest still count. While netmon runs, its NFS
tracepoints hold the `nfs` module, so `modprobe -r nfs` fails until it stops
(where NFS is built into the kernel, as on a Pi, this doesn't arise).

## Priority sampling

With many hosts, even per-interval totals add up to a lot of telemetry. `-k N`
keeps N rows per report using priority sampling ([Duffield, Lund and Thorup,
2007](https://doi.org/10.1145/1314690.1314696)), the approach HRT describes in
[Long on Research, Short on Bandwidth](https://www.hudsonrivertrading.com/hrtbeat/long-on-research-short-on-bandwidth/).

Each row's weight `w` is its total bytes, and its priority is `w / u` with
`u` uniform on (0, 1]. The N rows with the highest priorities are kept. If
`tau` is the (N+1)th highest priority, a kept row's estimated weight is
`max(w, tau)`. Each row carries `scale = max(w, tau) / w` (1 when nothing
was dropped). Multiply its counters by `scale` and sum them: the sum over
any subset, such as one user, one program or all UDP, is an unbiased
estimate of that subset's true total. Big talkers are almost always kept
with scale 1, and the many small ones are represented by a few rows with
larger scales. Unlike the `-n` top-N cut, `-k` doesn't drop the small
talkers' traffic from the totals.

## Build and test

Everything builds in Docker on Debian trixie:

```sh
docker build --target test -t ebpf-netmon:test .  # also runs unit tests
docker run --rm --privileged --pid=host ebpf-netmon:test   # smoke test
docker build --target runtime -t ebpf-netmon .
docker run --rm --privileged --pid=host ebpf-netmon        # watch the Docker VM
```

`--pid=host` because netmon reports the host's pids: in a container with
pids of its own, it shows the names the BPF side saw instead of reading
`/proc`. The smoke test moves known traffic in a process per case and
checks netmon's counts for each pid exactly: plain TCP and UDP, a
`MSG_WAITALL` read that sleeps, TCP and UDP peeks, `splice()`, IPv6 to IPv4,
MPTCP, traffic while another process holds 12 threads blocked in sends and
receives, a thread renamed after it started (the name read from `/proc`),
and a process name full of characters JSON must escape. It also checks
that netmon logs the `splice()` calls a crowd of them made it miss.

## Linters and formatters

[pre-commit](https://pre-commit.com) runs them on each commit, and CI runs
them on every file:

```sh
pip install pre-commit && pre-commit install
pre-commit run --all-files
docker build --target lint .   # clang-tidy, which needs Linux headers
```

| Language | Formatter | Linter |
|---|---|---|
| C (BPF), C++ | clang-format (kernel style, `.clang-format`) | clang-tidy (`.clang-tidy`), compiler `-Wall -Werror` |
| Python | ruff format | ruff check (`ruff.toml`) |
| Shell | shfmt (Google style: `-i 2 -ci -bn`) | ShellCheck |
| Dockerfile | | hadolint (`.hadolint.yaml`) |
| GitHub Actions | | actionlint, zizmor (security) |
| Everything | | gitleaks (secrets), typos (spelling) |

CI also scans the whole git history with gitleaks, runs the unit and smoke
tests under ASan and UBSan (`make sanitize`, or
`docker build --target sanitize`).

## Build on a Pi

On a Pi running Raspberry Pi OS (Debian trixie, arm64), from a clone of
this repo:

```sh
scripts/build_on_pi.sh
sudo build/netmon
```

The script installs the build packages with apt, checks that the kernel has
BPF and kprobes, builds, and runs the unit and smoke tests. Use `--no-deps`
to skip apt and `--no-smoke` to skip the smoke test, which needs sudo.
Tested on a Pi 4 with kernel 6.18.60+rpt-rpi-v8.

To check the counts over a real network, run the Pi side first, then the
sender on another machine (needs only python3):

```sh
sudo python3 tests/lan_test.py serve        # on the Pi
python3 tests/lan_test.py send PI_ADDRESS   # on another machine
```

It moves 100 MiB of TCP each way and 6 MB of UDP, and passes when netmon's
counts for the server process match the bytes that actually arrived.

### NFS traffic

The NFS client's wire bytes don't say which process asked for them. Replies
arrive in kernel worker threads, so they show up as `kworker/...` rows'
`tcp_rx`, named for whichever work queue the thread runs when netmon reads
its name (`-rpciod`, `-xprtiod`, `-events_unbound`…). Requests mostly leave
from kernel threads too, but a synchronous call (a `stat()`, an `open()`) is
sent by the calling process, along with any requests queued behind it, so a
process's `tcp_tx` can include some NFS request bytes. `NFS_RD` and `NFS_WR`
charge the file bytes to the process that read or wrote them, through
`read()`, `sendfile()`/`splice()` or `write()`. They count file bytes, not
wire bytes: reads served from the page cache count too, and memory-mapped
access does not.

`NFS_FETCH` (`nfs_fetched` in files) is the file bytes the server sent for
a process's reads: page-cache misses, readahead, memory-mapped and
`O_DIRECT` reads, but not reads the page cache served. It is what the
kernel counts as the mounts' server read bytes (the fifth number on the
`bytes:` lines of `/proc/self/mountstats`), split by process: on a Pi 4 the
sum matched it to the byte, short of what netmon logged as lost (below).
Their RPC and TCP headers aren't in it; the replies, headers and all, are
the kernel worker threads' `tcp_rx`.

The client starts each `READ` call in the process that needs the pages
(`nfs:nfs_initiate_read`), and the reply completes in a kernel thread
(`nfs:nfs_readpage_done`), so netmon records which process asked under the
file handle and offset both name, and charges it with the reply's bytes.
Retries keep the reader: the client resends a short read, or a call the
server asked to repeat, without starting a new one. Only a pNFS read resent
through the metadata server after a layout error is charged to the kernel
thread that resends it. Concurrent `READ`s of the same bytes (threads or
io_uring with `O_DIRECT`) are counted, and charged to the last process to
start one; rarely, when one starts on another CPU just as the last reply
before it lands, its own reply is lost, and logged as below.

netmon tracks 16384 `READ`s in flight at once. A single `O_DIRECT` or
io_uring call can start one per `rsize` bytes (128 KiB on most mounts)
before the first reply, so past 2 GiB in flight the oldest are forgotten,
and their replies' bytes go uncounted. So do those of a `READ` whose start
the kernel skipped. netmon logs them:
`netmon: N NFS READ replies (B bytes) found no recorded reader`. With those
added, the counts matched the server's to the byte on a Pi 4, with 20,000
in flight. A skipped `nfs_readpage_done` loses bytes netmon can't know; it
shows only as `skipped N calls`. Replies to `READ`s started before netmon
attached aren't logged: it waits two reports before it does.

`NFS_SENT` (`nfs_sent` in files) is the file bytes the server took for a
process's writes: what the kernel counts as the mounts' server write bytes
(the sixth number on the `bytes:` lines), split by process. A `write()` only
fills the page cache; the client sends the dirty pages later, from a kernel
thread (writeback, 30 s after a page was first dirtied, by default), or on
`fsync()` or `close()`, and pages of several writers can go out in one
`WRITE`. So netmon records each `write()`'s process (`nfs:nfs_file_write`)
as the last writer of every MiB of the file it covers, and of the file, and
charges each `WRITE`'s reply (`nfs:nfs_writeback_done`) to the last writer
of the MiB it covers, or of the file. A store through a shared mapping
(`nfs:nfs_update_folio`) records the file's writer only, since that
tracepoint doesn't say where in the file. Processes writing the same MiB
between two `WRITE`s are charged as the last one; one writing past 64 MiB
in a single `write()` is the file's writer for the rest. `O_DIRECT` writes
are sent at once, and count the same way. On a Pi 4 the counts matched the
kernel's to the byte, for `write()` with `fsync()`, writeback, a mapping and
`O_DIRECT`, and for a minute of the NAS share.

netmon remembers the last 16384 MiB written. Replies with no recorded
writer, mostly pages written before netmon attached and sent later, are
logged: `netmon: N NFS WRITE replies (B bytes) found no recorded writer`.
`nfs:nfs_file_write` is newer than kernel 6.15; without it, netmon doesn't
count `NFS_SENT` (the Raspberry Pi kernel, 6.18, has it).

To check it on a machine with an NFS mount (the directory is optional and
must be writable):

```sh
sudo python3 tests/nfs_test.py FILE_ON_NFS [DIR_ON_NFS]
```

CI runs it against a loopback NFS export; on a kernel without
`nfs:nfs_file_write` it skips `NFS_SENT`. On a Pi 4 it matched exactly for
the Synology NAS (read-only) and a loopback export (read and write).

## Capture

`netmon -o DIR` appends each report as JSON lines to
`DIR/YYYY-MM-DD/HH.jsonl`, one file per UTC hour, so a file is complete
once its hour has passed. Each row is one process in one interval of
`interval_s` seconds, ending at `ts`. Reports fall on wall-clock multiples
of `-i` (:00, :10, :20…) on every host, so a dashboard bucket a multiple of
that wide holds whole reports. The dashboard's rate panels assume the
service's `-i 10`:

```json
{"ts":1792666260,"host":"raspberrypi","interval_s":10,"pid":4309,"uid":0,"comm":"nfsd","tcp_tx":84,"tcp_rx":120,"udp_tx":0,"udp_rx":0,"nfs_read":0,"nfs_write":0,"nfs_fetched":0,"nfs_sent":0,"scale":1}
```

Each report also appends what every network interface moved in its
interval, headers and all, to `DIR/YYYY-MM-DD/HH.interfaces.jsonl`: the
kernel's counters from `/proc/1/net/dev`, the host's network namespace
(the service has a private one, with only a loopback). Interfaces that
moved nothing are left out, and the first report counts from when netmon
started, as the processes' rows do:

```json
{"ts":1792666260,"host":"raspberrypi","interval_s":10,"interface":"eth0","rx_bytes":21714,"rx_packets":61,"tx_bytes":9822,"tx_packets":48}
```

They are an independent total to check netmon's per-process counts
against, as HRT checks theirs against their switches' counters (see How
much netmon accounts for).

To capture continuously on a Pi, after `scripts/build_on_pi.sh` and once
the Pi has its host name (see Several Pis):

```sh
scripts/install_service.sh
```

It installs `netmon` and a systemd service that reports every 10 seconds
to `/var/lib/netmon/raw`. The raw files are kept as written; converting
them to Parquet is a separate step. Nothing deletes old files yet. An idle
Pi writes about 650 rows (120 KB) an hour, or 3 MB a day; busier Pis write
more. The interface files add 360 rows (50 KB) an hour for each interface
in use, such as `eth0` and `lo`.

The service runs as root, but with only `CAP_BPF` and `CAP_PERFMON`, no
network, and a read-only view of the system apart from its own folder
(`systemd-analyze security` rates it 2.8, against 8.7 before). A Pi has no
clock battery and boots with the time of its last shutdown, so the service
holds its reports until NTP sets the clock (`-w 300`); the counts keep
adding up meanwhile, so the first report covers everything. If the kernel
can't give it the sock tracepoints (before Linux 6.3), netmon exits with
status 3, saying why, and systemd doesn't restart it. A crash that
cuts off the last line of an hour's file costs only that row: the next run
starts a new line.

## Iceberg

`tools/to_iceberg.py` appends each finished hour of raw files to the
Iceberg table `netmon.traffic`, and of interface files to
`netmon.interfaces`, stored as Parquet and partitioned by day, with a
SQLite catalog in the warehouse directory. A file is loaded again
only when it has more rows than the table holds for its hour, so reruns are
safe and an hour copied before it was complete is filled in later. It
commits a day at a time, so a long backlog loads in bounded memory. It
refuses to commit with the clock more than a minute behind the table's last
commit, since Iceberg's Java readers reject a history that goes back in
time. If a clock that ran ahead made that commit, the converter waits
until real time passes it. Don't roll the table back instead: a rollback
keeps the future commit in the table's logs, which Java readers reject
just the same. If the wait outlasts the converter's 7-day look-back, load
the hours it missed with `--days`, as under Several Pis below.

To run it at five past every hour:

```sh
scripts/install_converter.sh WAREHOUSE [RAW]
```

RAW defaults to this machine's `/var/lib/netmon/raw`.

### Several Pis

Run one converter, and have every Pi copy its raw files to shared storage,
such as an NFS share on a NAS mounted at the same path on each. Each Pi
needs a host name of its own: Raspberry Pi OS names every Pi `raspberrypi`,
and two Pis with one name would overwrite each other's files. So the sync's
installer writes the Pi's id, a hash of its board's serial number, to
`RAW/HOST/.netmon-sync-id`, and every copy checks it: another Pi under the
same name stops, and so does a copy of this Pi's SD card in another Pi,
while this Pi keeps its folder with a new card. If the Pi itself is new,
remove that file as `netmon` and re-run the installer.

```sh
scripts/install_sync.sh /mnt/nas/netmon/raw     # on every Pi
scripts/install_converter.sh /mnt/nas/netmon/warehouse /mnt/nas/netmon/raw
```

The sync's installer needs `/mnt/nas/netmon` to exist, so that it stops
while the share is unmounted, and makes the rest.

The sync rsyncs `/var/lib/netmon/raw` to `RAW/$(hostname)/` every minute,
and never deletes there. Local capture does not depend on the NAS: while it
is unmounted the sync and the converter skip their runs, and the next sync
catches up. That needs the share in `/etc/fstab` (with `nofail`), so
systemd knows it is a mount; under a share mounted by hand, the jobs would
write to the empty folder beneath it. Each row carries the host name it was
captured under, so renaming a Pi starts a new host in the table. netmon
reads the name when it starts, so restart it after a rename. Files
captured under the old name would load as that name's host, so the sync's
installer stops while `/var/lib/netmon/raw` holds any from the last 8 days;
move them elsewhere.

The converter looks back 7 days. It reports files that arrive later than
that, but not hours that arrived on time while it was stopped for longer.
Either way, load them with a longer look-back, as `netmon`:

```sh
sudo -u netmon /opt/netmon/venv/bin/python /opt/netmon/to_iceberg.py \
  --warehouse WAREHOUSE --raw RAW --catalog CATALOG --days 30
```

(The lock's folder, `/run/netmon-iceberg`, exists once either service has
run since boot.) A raw file of the window that the converter can't read,
such as a dangling link or a copy only its owner may read, is skipped. The
run loads the rest, then exits with status 66 (`EX_NOINPUT`), so the unit
shows as failed (`systemctl --failed`). Once the file's hour leaves the
window, only a run with a longer `--days` loads it.

### Running without root

The sync and the converter run as the user `netmon`, which the installers
create (`scripts/add_netmon_user.sh`). It only needs to read
`/var/lib/netmon/raw` and write the shared folders, so neither job holds
root on the NAS. Their units are sandboxed too: no capabilities, `/usr`,
`/boot` and `/etc` read-only, private `/tmp` and devices, and no `/home`.
(Not the whole system read-only: systemd would set up the writable exception
as root, which a NAS that squashes root refuses.) Have the NAS map root to a
guest (root squash, e.g. Synology's "Map root to guest"), and give `netmon`
the uid of a NAS user that owns the share and has no admin rights. Every
installer that creates `netmon` takes it (the sync, the converter and
PostgreSQL), and stops if `netmon` already has another uid, so set it for
each:

```sh
export NETMON_UID=1234
scripts/install_sync.sh /mnt/nas/netmon/raw
```

The installers pause their timers, let a run in progress finish, and hand
the folders to `netmon` while root still can: on local disk, or before the
NAS squashes root. They stop if `netmon` doesn't then own everything in
them, and they refuse a path through a symlink, which older versions
followed: give them the path `realpath` prints. The timers stay off, even across a reboot, until an installer gets
to its end, where it starts them again. To move an existing setup with a
PostgreSQL catalog, first re-run `scripts/install_postgres.sh`, so that
only `netmon` can log in to it. Then re-run the other installers, check
the jobs run, and turn on root squash.

### Maintenance

Each load commits a snapshot, which keeps the table's past versions for
time travel and rollback. Weekly, on Sunday at 03:30 UTC, the converter's
installer also runs `to_iceberg.py --maintain`, which:

- compacts each finished day of up to a million rows into one Parquet
  file,
- expires snapshots older than 28 days (`--keep-days`), so the metadata
  that every query reads stays small, and
- deletes files that no remaining snapshot uses and are over a day old.

The raw files are kept, so the table can always be rebuilt from them.
Loads and maintenance take turns through a lock file, and a run that stops
partway (a crash, a power cut) is cleaned up by the next one on the same
machine.

### PostgreSQL catalog

The catalog only records where the table's current metadata file is; the
data and metadata stay in the warehouse. To keep it in PostgreSQL on the
converter's machine instead of SQLite:

```sh
scripts/install_postgres.sh
scripts/install_converter.sh WAREHOUSE RAW \
  'postgresql+psycopg2://netmon@/netmon?host=/var/run/postgresql'
```

The converter reaches PostgreSQL over its Unix socket, with no password. On its
first run the converter adopts the table already in the warehouse, so
nothing is copied; ClickHouse reads the warehouse directly and doesn't
change. The old catalog then refuses to run, since its metadata is out of
date; to move back, remove `netmon.traffic` from it (for SQLite, move
`catalog.db` away) and re-run `install_converter.sh` with that URI, or
`''` for SQLite. Leaving the URI out keeps the installed one.

`uv run tests/iceberg_test.py` tests it. The Python dependencies are in
`pyproject.toml`, pinned in `uv.lock`.
On a Pi 4, 1 MB of raw JSON became 108 KB of Parquet.

### REST catalog

ClickHouse can read the table through the catalog too, instead of taking
the newest metadata file in the warehouse, which could be a stray one, say
from a crashed or second writer. On the machine with ClickHouse and the
PostgreSQL catalog:

```sh
scripts/install_rest_catalog.sh WAREHOUSE
scripts/install_clickhouse.sh WAREHOUSE RAW
```

The first serves the catalog over the Iceberg REST protocol, on port 8181 to
this machine only, with Apache Iceberg's own server (from its
`apache/iceberg-rest-fixture` image, checked by hash) and Debian's Java. It
logs in to PostgreSQL as `netmon_rest`, which may only read, and can't write
files. It uses about 200 MB of memory, and live queries take about 0.2 s
longer. A reader of the folder can, rarely, catch a metadata file the
converter is still writing and fail that one query; through the catalog it
can't, so use this for alerts you rely on. To read the folder again,
`systemctl disable --now netmon-iceberg-rest`, remove
`/etc/default/netmon-iceberg-rest` and re-run `install_clickhouse.sh`.

## ClickHouse and Grafana

On the machine that holds the warehouse:

```sh
scripts/install_clickhouse.sh WAREHOUSE [RAW]
scripts/install_grafana.sh
```

Install the converter first: ClickHouse's views need its tables, and an
upgrade that adds a table or a column (`nfs_fetched`, `nfs_sent`) needs its
next run (`install_converter.sh` runs it at once), then
`install_clickhouse.sh` again. Rows from a netmon without a counter are
null there. Upgrade the converter before netmon on any Pi that
feeds it, too: an older converter loads a newer netmon's rows without the
counters it doesn't know, and those hours aren't loaded again.

ClickHouse listens on 127.0.0.1 only. It has these views of each table, the
processes' rows (`traffic`) and the interfaces' (`interfaces`):

- `netmon.traffic` reads the Iceberg table, up to the last hour the
  converter loaded: through the REST catalog if there is one, or else the
  newest metadata file. A query's time range reaches the table, so it opens
  only those days' files.
- `netmon.traffic_live` adds rows from the last two days of raw files in
  RAW for hours not yet in Iceberg, so it is as recent as the raw files:
  about a minute behind with the per-minute sync. It opens only the raw
  files of hours not in Iceberg yet, found by path against the table's last
  two days, so queries take about 0.7 s longer on a Pi 4, whatever their
  range. It still lists every raw file to find them, and nothing deletes
  old raw files yet, so that cost grows with the history: about 0.7 s more
  for a year of three hosts' files. An hour the converter loaded before all
  of it arrived shows its late rows after the next run, at five past.
  ClickHouse must be able to read every file in RAW: one it can't open
  fails the live view's queries until the file is two days old.
- `netmon.interfaces` and `netmon.interfaces_live` do the same for the
  interface files.

Grafana's ClickHouse user, `grafana`, may only read these views and the
two tables in `netmon_lake` that they read. Only `netmon.traffic_raw` and
`netmon.interfaces_raw`, the live views' raw rows, read files: they run as
`netmon_views`, which can't log in. The others run as their reader, because
through a view that runs as another user, ClickHouse 26.11 and later don't
pass the time range on to the table and read all of it: on a Pi 4, each
read took 2 s longer for every month of history. ClickHouse's own `default`
user keeps its passwordless login from this machine, for the installer, so
don't give untrusted users a shell here.

Grafana serves the `netmon traffic` dashboard on port 3000, from
`netmon.traffic_live` and `netmon.interfaces_live`, refreshing every 30
seconds. The first login is admin/admin.

### How much netmon accounts for

The dashboard's heatmap, "Traffic netmon accounts for", is HRT's check of
their eBPF totals against their switches' counters, per Pi: a row for each
host's received and sent bytes, a column per time bucket, and in each cell
the bytes netmon charged to processes (TCP and UDP, scaled) as a share of
what the host's interfaces moved. Green is all of it, red half or less.

The interfaces count each packet's headers, which a process's sends and
receives don't, so the heatmap takes 66 bytes a packet off theirs:
Ethernet, IPv4 and TCP with timestamps. A bare ACK is then nothing, as it
should be. It counts `lo` as well, since netmon counts traffic between
local processes; it leaves out bridges and veth pairs (`veth*`, `docker*`,
`br-*`, `virbr*`), whose packets cross another interface too.

What stays uncounted is traffic no process sends or receives through a
socket: ICMP (ping), ARP, DHCP and IPv6 neighbour discovery, packet
capture, TCP retransmissions, and IPv6's 20 bytes of headers more than the
66 allowed for. On a Pi 4 serving this dashboard, netmon accounted for
about 94% each way. Over 100% means bytes counted twice, as a VPN run as a
process does: inside the tunnel and out.

### Alerts

`install_grafana.sh` also installs three alert rules, evaluated every
minute ("Host stopped reporting" every 2), in
`grafana/provisioning/alerting/netmon-rules.yaml`:

| Rule | Fires when |
|---|---|
| Host stopped reporting | a host seen in the last day has had no rows for 2 minutes at 5 checks in a row (about 10 minutes) |
| Program moving over 10 MB/s | one program (all its processes on a host) averages over 10 MB/s sent or received for 5 minutes |
| Iceberg converter behind | the newest row in Iceberg is over 2 hours old |

To get them by email, run this on the Grafana machine, in a terminal:

```sh
scripts/configure_alert_email.sh
```

It asks for the address, an SMTP login (for Gmail, an app password) and the
sender, and stores them in `/etc/grafana/netmon-alerts.env`, and the
password in `/etc/grafana/netmon-smtp-password`, which Grafana reads through
`$__file{}`; both are readable only by root and Grafana. CI checks that
Grafana logs in with that password, by sending a test mail to a stand-in
SMTP server. Renaming a host makes its old name fire "Host stopped
reporting" until a day after its last row, when the alert resolves; so
does a host that stays down that long.

ClickHouse releases need ARMv8.2 (Pi 5) and die with "Illegal instruction"
on a Pi 4 (ARMv8.0). On a Pi 4 the script installs ClickHouse's nightly
ARMv8.0 build, which has no releases.

`tests/dashboard_test.py` checks every dashboard query's totals through
ClickHouse, with the latest hour only in raw files and again once it is
converted, and each alert query's result, including the high-traffic
rate over rows in its window. CI runs it, then installs Grafana and runs
every panel and alert query through Grafana's ClickHouse plugin
(`tests/grafana_test.py`).

## Run a Docker-built binary on a Pi

The binary links libbpf statically and needs only `libelf1t64` and `zlib1g`.
Build it for arm64, even on another kind of machine:

```sh
docker build --platform linux/arm64 --target build -t ebpf-netmon:build .
docker run --rm -v "$PWD":/out ebpf-netmon:build cp build/netmon /out/
scp netmon pi:
ssh -t pi sudo ./netmon
```
