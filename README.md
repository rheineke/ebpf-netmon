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
with a SQLite catalog in the warehouse directory. An hour that is already
in the table is skipped, so reruns are safe. To run it at five past every
hour:

```sh
scripts/install_converter.sh WAREHOUSE
```

`uv run tests/iceberg_test.py` tests it. The Python dependencies are in
`pyproject.toml`, pinned in `uv.lock`.
On a Pi 4, 1 MB of raw JSON became 108 KB of Parquet.

## Run a Docker-built binary on a Pi

The binary links libbpf statically and needs only `libelf1t64` and `zlib1g`:

```sh
docker build --target build -t ebpf-netmon:build .
docker run --rm -v "$PWD":/out ebpf-netmon:build cp build/netmon /out/
scp netmon pi:
ssh -t pi sudo ./netmon
```
