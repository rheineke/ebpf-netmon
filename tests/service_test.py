"""Checks the netmon service as scripts/install_service.sh installs it: it
runs sandboxed with only CAP_BPF and CAP_PERFMON, and its hourly files count
this process's traffic exactly. Its private network has only a loopback, yet
its interface files count the host's: lo carried this process's traffic.
Needs systemd and sudo; CI runs it:

  scripts/install_service.sh && python3 tests/service_test.py
"""

import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time

RAW = "/var/lib/netmon/raw"
TCP_BYTES = 1 << 20
DATAGRAMS = 100


def show(prop):
    out = subprocess.run(
        ["systemctl", "show", "-p", prop, "--value", "netmon.service"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def move_traffic():
    server = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(server.getsockname())
    conn, _ = server.accept()

    def send():
        client.sendall(b"x" * TCP_BYTES)
        client.close()

    sender = threading.Thread(target=send)
    sender.start()
    got = 0
    while chunk := conn.recv(65536):
        got += len(chunk)
    sender.join()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for _ in range(DATAGRAMS):
        tx.sendto(b"y" * 1000, rx.getsockname())
        rx.recv(2000)
    return {
        "tcp_tx": TCP_BYTES,
        "tcp_rx": got,
        "udp_tx": DATAGRAMS * 1000,
        "udp_rx": DATAGRAMS * 1000,
    }


def counted():
    """This process's totals in the service's files, and their hosts."""
    got = {"tcp_tx": 0, "tcp_rx": 0, "udp_tx": 0, "udp_rx": 0}
    hosts = set()
    for path in glob.glob(os.path.join(RAW, "*", "[0-9][0-9].jsonl")):
        for line in open(path):
            row = json.loads(line)
            if row.get("pid") == os.getpid():
                hosts.add(row["host"])
                for k in got:
                    got[k] += row[k]
    return got, hosts


def interfaces_since(start):
    """The bytes each interface sent in the service's reports from start on,
    and the interface files' hosts."""
    sent, hosts = {}, set()
    for path in glob.glob(os.path.join(RAW, "*", "*.interfaces.jsonl")):
        for line in open(path):
            row = json.loads(line)
            if row["ts"] > start:
                hosts.add(row["host"])
                sent[row["interface"]] = sent.get(row["interface"], 0) + row["tx_bytes"]
    return sent, hosts


def fail(why):
    subprocess.run(["sudo", "journalctl", "-u", "netmon.service", "-n", "30"])
    sys.exit(f"FAIL: {why}")


def main():
    if show("ActiveState") != "active":
        fail("netmon.service is not running")
    caps = show("CapabilityBoundingSet").split()
    if sorted(caps) != ["cap_bpf", "cap_perfmon"]:
        fail(f"capabilities {caps}, want only cap_bpf and cap_perfmon")
    print("ok  : running, with only cap_bpf and cap_perfmon")

    start = time.time()
    want = move_traffic()
    # Reports come every 10 s, but the unit's -w 300 holds them while the
    # clock is unsynchronized.
    deadline = time.monotonic() + 330
    while (got := counted())[0] != want and time.monotonic() < deadline:
        time.sleep(2)
    got, hosts = counted()
    if got != want:
        fail(f"the files count {got} for this process, want {want}")
    if hosts != {socket.gethostname()}:
        fail(f"rows say host {hosts}, want {socket.gethostname()}")
    print(f"ok  : {RAW} counts {got}")

    # The report after the traffic is in; lo carried it, headers and all.
    moved = want["tcp_tx"] + want["udp_tx"]
    deadline = time.monotonic() + 30  # written just after the processes' rows
    while (sent := interfaces_since(int(start))[0]).get("lo", 0) < moved:
        if time.monotonic() > deadline:
            break
        time.sleep(2)
    sent, hosts = interfaces_since(int(start))
    if sent.get("lo", 0) < moved:
        fail(
            f"lo sent {sent.get('lo', 0)} bytes in the interface files, want >= {moved}"
        )
    if set(sent) == {"lo"}:
        fail(f"only lo in the interface files, the service's own namespace: {sent}")
    if hosts != {socket.gethostname()}:
        fail(f"interface rows say host {hosts}, want {socket.gethostname()}")
    print(f"ok  : the host's interfaces, not the sandbox's: {sorted(sent)}")
    print("PASS")


if __name__ == "__main__":
    main()
