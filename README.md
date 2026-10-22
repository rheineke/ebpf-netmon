# ebpf-netmon

eBPF network traffic monitoring for Raspberry Pis (Debian trixie, arm64).

## netmon

`netmon` prints the TCP and UDP bytes each process sent and received in the
last interval, like `top`:

```
     PID    UID COMM                TCP_TX    TCP_RX    UDP_TX    UDP_RX
    1207    501 sshd                  196B        0B        0B        0B
```

Options: `-i` interval in seconds (default 2), `-c` number of reports,
`-n` rows (0 = all), `-k` priority-sample rows (see below), `-j` JSON lines.

It uses kprobes on `tcp_sendmsg`, `tcp_cleanup_rbuf`, `udp(v6)_sendmsg` and
`udp(v6)_recvmsg`, and reads only their integer arguments and return values.
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

## Run on a Pi

The binary links libbpf statically and needs only `libelf1t64` and `zlib1g`:

```sh
docker build --target build -t ebpf-netmon:build .
docker run --rm -v "$PWD":/out ebpf-netmon:build cp build/netmon /out/
scp netmon pi:
ssh -t pi sudo ./netmon
```
