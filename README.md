# ebpf-netmon

eBPF network traffic monitoring for Raspberry Pis (Debian trixie, arm64).

## netmon

`netmon` prints the TCP and UDP bytes each process sent and received in the
last interval, and the NFS file bytes it read and wrote, like `top`:

```
     PID    UID COMM                TCP_TX    TCP_RX    UDP_TX    UDP_RX    NFS_RD    NFS_WR
     850    999 Plex Media Serv         0B        0B      687B      1.1K     12.0M        0B
```

Options: `-i` interval in seconds (default 2), `-c` number of reports,
`-n` rows (0 = all), `-k` priority-sample rows (see below), `-j` JSON lines,
`-o DIR` JSON lines appended to hourly files (see Capture).

It uses kprobes on `tcp_sendmsg`, `tcp_cleanup_rbuf`, `udp(v6)_sendmsg` and
`udp(v6)_recvmsg`, and kretprobes on `nfs_file_read`, `nfs_file_splice_read`
and `nfs_file_write`. It reads only their integer arguments and return values.
So it needs no CO-RE and no kernel BTF, which Raspberry Pi kernels don't ship.

## Priority sampling

With many hosts, even per-interval totals add up to a lot of telemetry. `-k N`
keeps N rows per report using priority sampling ([Duffield, Lund and Thorup,
2007](https://doi.org/10.1145/1314690.1314696)), the approach HRT describes in
[Long on Research, Short on Bandwidth](https://www.hudsonrivertrading.com/hrtbeat/long-on-research-short-on-bandwidth/).

Each row's weight `w` is its total bytes, and its priority is `w / u` with `u`
uniform on (0, 1]. The N rows with the highest priorities are kept. If `tau` is
the (N+1)th highest priority, a kept row's estimated weight is `max(w, tau)`.
Each row carries `scale = max(w, tau) / w` (1 when nothing was dropped).
Multiply its counters by `scale` and sum them: the sum over any subset, such as
one user, one program or all UDP, is an unbiased estimate of that subset's
true total. Big talkers are always kept with scale 1, and the many small ones
are represented by a few rows with larger scales. Unlike the `-n` top-N cut,
`-k` doesn't drop the small talkers' traffic from the totals.

## Build and test

Everything builds in Docker on Debian trixie:

```sh
docker build --target test -t ebpf-netmon:test .  # also runs unit tests
docker run --rm --privileged ebpf-netmon:test   # smoke test
docker build --target runtime -t ebpf-netmon .
docker run --rm --privileged ebpf-netmon        # watch the Docker VM
```

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
Tested on a Pi 4 with kernel 6.18.50+rpt-rpi-v8.

To check the counts over a real network, run the Pi side first, then the
sender on another machine (needs only python3):

```sh
sudo python3 tests/lan_test.py serve        # on the Pi
python3 tests/lan_test.py send PI_ADDRESS   # on another machine
```

It moves 100 MiB of TCP each way and 6 MB of UDP, and passes when netmon's
counts for the server process match the bytes that actually arrived.

### NFS traffic

The NFS client sends and receives in kernel threads (`kworker/...-nfsiod`,
`-xprtiod`, `-rpciod`), so the wire bytes show up as their `tcp_rx`/`tcp_tx`.
`NFS_RD` and `NFS_WR` charge the file bytes to the process that read or wrote
them, through `read()`, `sendfile()`/`splice()` or `write()`. They count
file bytes, not wire bytes: reads served from the page cache count too, and
memory-mapped access does not.

To check it on a machine with an NFS mount (the directory is optional and
must be writable):

```sh
sudo python3 tests/nfs_test.py FILE_ON_NFS [DIR_ON_NFS]
```

CI runs it against a loopback NFS export. On a Pi 4 it matched exactly for
the Synology NAS (read-only) and a loopback export (read and write).

## Capture

`netmon -o DIR` appends each report as JSON lines to
`DIR/YYYY-MM-DD/HH.jsonl`, one file per UTC hour, so a file is complete
once its hour has passed. Each row is one process in one interval:

```json
{"ts":1792666261,"host":"raspberrypi","interval_s":10,"pid":4309,"uid":0,"comm":"nfsd","tcp_tx":84,"tcp_rx":120,"udp_tx":0,"udp_rx":0,"nfs_read":0,"nfs_write":0,"scale":1}
```

To capture continuously on a Pi, after `scripts/build_on_pi.sh`:

```sh
scripts/install_service.sh
```

It installs `netmon` and a systemd service that reports every 10 seconds
to `/var/lib/netmon/raw`. The raw files are kept as written; converting
them to Parquet is a separate step. Nothing deletes old files yet. An idle
Pi writes about 650 rows (120 KB) an hour, or 3 MB a day; busier Pis write
more.

## Iceberg

`tools/to_iceberg.py` appends each finished hour of raw files to the
Iceberg table `netmon.traffic`, stored as Parquet and partitioned by day,
with a SQLite catalog in the warehouse directory. A file is loaded again
only when it has more rows than the table holds for its hour, so reruns are
safe and an hour copied before it was complete is filled in later. To run
it at five past every hour:

```sh
scripts/install_converter.sh WAREHOUSE [RAW]
```

RAW defaults to this machine's `/var/lib/netmon/raw`.

### Several Pis

Run one converter, and have every Pi copy its raw files to shared storage,
such as an NFS share on a NAS mounted at the same path on each:

```sh
scripts/install_sync.sh /mnt/nas/netmon/raw     # on every Pi
scripts/install_converter.sh /mnt/nas/netmon/warehouse /mnt/nas/netmon/raw
```

The sync rsyncs `/var/lib/netmon/raw` to `RAW/$(hostname)/` every minute,
and never deletes there. Local capture does not depend on the NAS: while it
is unmounted the sync and the converter skip their runs, and the next sync
catches up. Each row carries the host name it was captured under, so
renaming a Pi starts a new host in the table.

`uv run tests/iceberg_test.py` tests it. The Python dependencies are in
`pyproject.toml`, pinned in `uv.lock`.
On a Pi 4, 1 MB of raw JSON became 108 KB of Parquet.

## ClickHouse and Grafana

On the machine that holds the warehouse:

```sh
scripts/install_clickhouse.sh WAREHOUSE [RAW]
scripts/install_grafana.sh
```

ClickHouse listens on 127.0.0.1 only. It has two views:

- `netmon.traffic` reads the Iceberg table with `icebergLocal`, up to the
  last hour the converter loaded.
- `netmon.traffic_live` adds rows from the last two days of raw files in
  RAW for hours not yet in Iceberg, so it is as recent as the raw files:
  about a minute behind with the per-minute sync. Queries take about half
  a second longer on a Pi 4.

Grafana serves the `netmon traffic` dashboard on port 3000, from
`netmon.traffic_live`, refreshing every 30 seconds. The first login is
admin/admin.

### Alerts

`install_grafana.sh` also installs three alert rules, evaluated every
minute, in `grafana/provisioning/alerting/netmon-rules.yaml`:

| Rule | Fires when |
|---|---|
| Host stopped reporting | a host seen in the last day has had no rows for 5 minutes |
| Process moving over 10 MB/s | one process averages over 10 MB/s sent or received for 5 minutes |
| Iceberg converter behind | the newest row in Iceberg is over 2 hours old |

To get them by email, run this on the Grafana machine, in a terminal:

```sh
scripts/configure_alert_email.sh
```

It asks for the address and an SMTP login (for Gmail, an app password)
and stores them in `/etc/grafana/netmon-alerts.env`, readable only by root
and Grafana. Renaming a host makes its old name fire "Host stopped
reporting" until a day after its last row.

ClickHouse releases need ARMv8.2 (Pi 5) and die with "Illegal instruction"
on a Pi 4 (ARMv8.0). On a Pi 4 the script installs ClickHouse's nightly
ARMv8.0 build, which has no releases.

`tests/dashboard_test.py` checks every dashboard query's totals through
ClickHouse, with the latest hour only in raw files and again once it is
converted; CI runs it.

## Run a Docker-built binary on a Pi

The binary links libbpf statically and needs only `libelf1t64` and `zlib1g`:

```sh
docker build --target build -t ebpf-netmon:build .
docker run --rm -v "$PWD":/out ebpf-netmon:build cp build/netmon /out/
scp netmon pi:
ssh -t pi sudo ./netmon
```
