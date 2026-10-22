"""Check netmon against exact byte counts sent over a real network.

On the Pi, from the repo root:   sudo python3 tests/lan_test.py serve
On another machine on the LAN:   python3 tests/lan_test.py send PI_ADDRESS

The sender pushes TCP_BYTES to the Pi, pulls TCP_BYTES back and sends
DATAGRAMS UDP datagrams. The Pi side counts what actually arrived and checks
that netmon attributed the same bytes to this process. Only needs python3.
"""

import json
import os
import socket
import subprocess
import sys
import time

PORT = 5201
TCP_BYTES = 100 * 1024 * 1024
DATAGRAMS = 5000
DATAGRAM_SIZE = 1200
NETMON = os.path.join(os.path.dirname(__file__), "..", "build", "netmon")


def serve():
    tcp = socket.create_server(("0.0.0.0", PORT))
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("0.0.0.0", PORT))
    netmon = subprocess.Popen(
        [NETMON, "-j", "-n", "0", "-i", "1"], stdout=subprocess.PIPE, text=True
    )
    time.sleep(1.5)  # let the probes attach and the baseline read happen
    print("waiting for: python3 tests/lan_test.py send <this host>", flush=True)

    got = {"tcp_rx": 0, "tcp_tx": 0, "udp_rx": 0}
    conn, peer = tcp.accept()
    with conn:
        while chunk := conn.recv(65536):
            got["tcp_rx"] += len(chunk)
    conn, _ = tcp.accept()
    with conn:
        conn.sendall(b"z" * TCP_BYTES)
        got["tcp_tx"] = TCP_BYTES
    udp.settimeout(3)
    try:
        while True:
            got["udp_rx"] += len(udp.recv(2048))
    except socket.timeout:
        pass
    print(f"from {peer[0]}: {got}", flush=True)

    time.sleep(2)  # one more report after the last byte
    netmon.terminate()
    out, _ = netmon.communicate(timeout=10)
    counted = {k: 0 for k in got}
    for line in out.splitlines():
        row = json.loads(line)
        if row["pid"] == os.getpid():
            for k in counted:
                counted[k] += row[k]
    print(f"netmon counted: {counted}")
    if counted != got:
        sys.exit("FAIL: netmon disagrees with the bytes moved")
    print("PASS")


def send(host):
    with socket.create_connection((host, PORT)) as s:
        s.sendall(b"a" * TCP_BYTES)
    got = 0
    with socket.create_connection((host, PORT)) as s:
        while chunk := s.recv(65536):
            got += len(chunk)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i in range(DATAGRAMS):
        udp.sendto(b"u" * DATAGRAM_SIZE, (host, PORT))
        if i % 100 == 0:
            time.sleep(0.01)  # pace so a LAN switch does not drop bursts
    print(f"sent {TCP_BYTES} TCP, received {got} TCP, "
          f"sent {DATAGRAMS * DATAGRAM_SIZE} UDP")


def main():
    if sys.argv[1:2] == ["serve"]:
        serve()
    elif sys.argv[1:2] == ["send"] and len(sys.argv) == 3:
        send(sys.argv[2])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
