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
`-n` rows (0 = all), `-j` JSON lines.

It uses kprobes on `tcp_sendmsg`, `tcp_cleanup_rbuf`, `udp(v6)_sendmsg` and
`udp(v6)_recvmsg`, and reads only their integer arguments and return values.
So it needs no CO-RE and no kernel BTF, which Raspberry Pi kernels don't ship.

## Build and test

Everything builds in Docker on Debian trixie:

```sh
docker build --target test -t ebpf-netmon:test .
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
