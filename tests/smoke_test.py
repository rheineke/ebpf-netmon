"""Run netmon while moving known amounts of loopback traffic, then check it.

Needs root and a kernel that allows kprobes: docker run --privileged.
"""

import glob
import json
import os
import socket
import subprocess
import sys
import tempfile
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


def check_sampled(out):
    """-k 1 prints at most one row per report, each with a scale >= 1."""
    rows = [json.loads(line) for line in out.splitlines()]
    per_report = {}
    for row in rows:
        per_report[row["ts"]] = per_report.get(row["ts"], 0) + 1
        if row["scale"] < 1:
            sys.exit(f"FAIL: scale below 1: {row}")
    if not rows or max(per_report.values()) > 1:
        sys.exit(f"FAIL: want 1 row per report with -k 1, got {per_report}")
    print("sampled rows per report:", per_report)


def check_files(out_dir, stdout_lines):
    """-o writes the same JSON as -j into out_dir/YYYY-MM-DD/HH.jsonl."""
    paths = glob.glob(os.path.join(out_dir, "*", "*.jsonl"))
    rows = [json.loads(line) for p in paths for line in open(p)]
    if not rows:
        sys.exit(f"FAIL: no rows in {out_dir}: {paths}")
    for p in paths:
        day, hour = p.split(os.sep)[-2:]
        if len(day) != 10 or len(hour) != len("HH.jsonl"):
            sys.exit(f"FAIL: unexpected file name {p}")
    stdout_keys = set(json.loads(stdout_lines.splitlines()[0]))
    if set(rows[0]) != stdout_keys or rows[0]["host"] != socket.gethostname():
        sys.exit(f"FAIL: file rows differ from stdout rows: {rows[0]}")
    print(f"-o wrote {len(rows)} rows to {len(paths)} file(s)")


def main():
    netmon = subprocess.Popen(
        ["netmon", "-j", "-n", "0", "-i", "1", "-c", "4"],
        stdout=subprocess.PIPE,
        text=True,
    )
    # A second instance keeps one row per report via priority sampling.
    sampled = subprocess.Popen(
        ["netmon", "-j", "-k", "1", "-i", "1", "-c", "4"],
        stdout=subprocess.PIPE,
        text=True,
    )
    # A third writes hourly files, the way the systemd service runs it.
    out_dir = tempfile.mkdtemp()
    to_files = subprocess.Popen(
        ["netmon", "-n", "0", "-i", "1", "-c", "4", "-o", out_dir]
    )
    time.sleep(1.5)  # let the probes attach and the baseline read happen
    tcp_traffic()
    udp_traffic()
    out, _ = netmon.communicate(timeout=30)
    sampled_out, _ = sampled.communicate(timeout=30)
    if netmon.returncode != 0 or sampled.returncode != 0:
        sys.exit(f"netmon exited with {netmon.returncode}, {sampled.returncode}")
    check_sampled(sampled_out)
    if to_files.wait(timeout=30) != 0:
        sys.exit(f"netmon -o exited with {to_files.returncode}")
    check_files(out_dir, out)

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
