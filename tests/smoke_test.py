"""Run netmon while moving known amounts of loopback traffic, then check it.

Needs root and a kernel that allows kprobes: docker run --privileged.
"""

import json
import socket
import subprocess
import sys
import threading
import time

PAYLOAD = 4 * 1024 * 1024
DATAGRAMS = 200
DATAGRAM_SIZE = 1000


def tcp_traffic():
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def sink():
        conn, _ = server.accept()
        with conn:
            while conn.recv(65536):
                pass

    t = threading.Thread(target=sink)
    t.start()
    with socket.create_connection(("127.0.0.1", port)) as client:
        client.sendall(b"x" * PAYLOAD)
    t.join()
    server.close()


def udp_traffic():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for _ in range(DATAGRAMS):
        tx.sendto(b"y" * DATAGRAM_SIZE, rx.getsockname())
        rx.recv(DATAGRAM_SIZE)
    tx.close()
    rx.close()


def main():
    netmon = subprocess.Popen(
        ["netmon", "-j", "-n", "0", "-i", "1", "-c", "4"],
        stdout=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.5)  # let the probes attach and the baseline read happen
    tcp_traffic()
    udp_traffic()
    out, _ = netmon.communicate(timeout=30)
    if netmon.returncode != 0:
        sys.exit(f"netmon exited with {netmon.returncode}")

    # Sender and receiver are both this process, so expect at least the
    # payload in each direction.
    totals = {"tcp_tx": 0, "tcp_rx": 0, "udp_tx": 0, "udp_rx": 0}
    for line in out.splitlines():
        row = json.loads(line)
        if row["pid"] == netmon.pid or row["comm"] != "python3":
            continue
        for k in totals:
            totals[k] += row[k]
    print("python3 totals:", totals)

    want = {
        "tcp_tx": PAYLOAD,
        "tcp_rx": PAYLOAD,
        "udp_tx": DATAGRAMS * DATAGRAM_SIZE,
        "udp_rx": DATAGRAMS * DATAGRAM_SIZE,
    }
    failed = [k for k in want if totals[k] < want[k]]
    if failed:
        sys.exit(f"FAIL: too few bytes for {failed}; want at least {want}")
    print("PASS")


if __name__ == "__main__":
    main()
