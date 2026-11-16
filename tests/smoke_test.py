"""Run netmon while moving known amounts of traffic, then check its counts.

Needs root, a kernel that allows kprobes and tracepoints, and the host's
pids, as netmon reports them: docker run --privileged --pid=host.

Each scenario runs in a process of its own and returns the bytes it moved,
and netmon's TCP and UDP counts for that pid must match them exactly. The
scenarios are the ways a probe can miscount: reads that sleep part way
(MSG_WAITALL), peeks, an IPv6 socket sending to an IPv4 address, splice(),
MPTCP, and many threads blocked in the kernel while others move data.
netmon mounts tracefs itself in a container, so this test doesn't.
"""

import ctypes
import glob
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time

# Not the NFS counters: tests/nfs_test.py checks those, and a clone on NFS
# reads this script over NFS.
COUNTERS = ("tcp_tx", "tcp_rx", "udp_tx", "udp_rx")
PARKED = 12  # threads blocked in the kernel at once
ODD_NAME = 'q"b\\\x01z'  # a process name with every byte JSON must escape
SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def tcp_pair():
    server = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(server.getsockname())
    conn, _ = server.accept()
    server.close()
    return client, conn


def send_and_drain(client, conn, total):
    """Sends total bytes from client and reads them all at conn."""

    def send():
        client.sendall(b"x" * total)
        client.close()

    sender = threading.Thread(target=send)
    sender.start()
    got = 0
    while chunk := conn.recv(65536):
        got += len(chunk)
    sender.join()
    conn.close()
    return got


def udp_pair(family=socket.AF_INET):
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    tx = socket.socket(family, socket.SOCK_DGRAM)
    if family == socket.AF_INET6:
        tx.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        return rx, tx, ("::ffff:127.0.0.1", rx.getsockname()[1])
    return rx, tx, rx.getsockname()


@scenario
def tcp():
    client, conn = tcp_pair()
    got = send_and_drain(client, conn, 4 << 20)
    return {"tcp_tx": 4 << 20, "tcp_rx": got}


@scenario
def udp():
    rx, tx, to = udp_pair()
    for _ in range(200):
        tx.sendto(b"y" * 1000, to)
        rx.recv(2000)
    return {"udp_tx": 200_000, "udp_rx": 200_000}


@scenario
def tcp_waitall():
    """One recv(MSG_WAITALL) that sleeps 16 times while the data trickles in."""
    client, conn = tcp_pair()
    total, piece = 1 << 20, 64 << 10

    def trickle():
        for _ in range(total // piece):
            client.sendall(b"x" * piece)
            time.sleep(0.01)

    sender = threading.Thread(target=trickle)
    sender.start()
    got = len(conn.recv(total, socket.MSG_WAITALL))
    sender.join()
    return {"tcp_tx": total, "tcp_rx": got}


@scenario
def tcp_peek():
    client, conn = tcp_pair()
    got = 0
    for _ in range(100):
        client.sendall(b"x" * 1000)
        while len(conn.recv(1000, socket.MSG_PEEK)) < 1000:
            time.sleep(0.001)
        got += len(conn.recv(1000))
    return {"tcp_tx": 100_000, "tcp_rx": got}


@scenario
def udp_peek():
    rx, tx, to = udp_pair()
    for _ in range(100):
        tx.sendto(b"y" * 1000, to)
        rx.recv(2000, socket.MSG_PEEK)
        rx.recv(2000)
    return {"udp_tx": 100_000, "udp_rx": 100_000}


@scenario
def tcp_splice():
    """splice() from a TCP socket into a pipe, which skips recvmsg."""
    client, conn = tcp_pair()
    total = 1 << 20
    sender = threading.Thread(target=client.sendall, args=(b"x" * total,))
    sender.start()
    r, w = os.pipe()
    got = 0
    while got < total:
        n = os.splice(conn.fileno(), w, 65536)
        os.read(r, n)
        got += n
    sender.join()
    return {"tcp_tx": total, "tcp_rx": got}


@scenario
def udp_v4_mapped():
    """A dual-stack IPv6 socket sending to IPv4, as Go's ListenUDP("udp")."""
    rx, tx, to = udp_pair(socket.AF_INET6)
    for _ in range(100):
        tx.sendto(b"y" * 1000, to)
        rx.recv(2000)
    return {"udp_tx": 100_000, "udp_rx": 100_000}


@scenario
def busy_neighbour():
    """Another process holds PARKED threads blocked in TCP sends and as many
    in UDP receives; this one's traffic must still count. Kretprobes on
    tcp_sendmsg and udp_recvmsg, as netmon once used, lose it all."""
    parker = subprocess.Popen(
        [sys.executable, __file__, "park"], stdout=subprocess.PIPE, text=True
    )
    if parker.stdout.readline().strip() != "parked":
        sys.exit("FAIL: the parked threads did not start")
    time.sleep(0.5)  # let the last ones block
    try:
        client, conn = tcp_pair()
        got = send_and_drain(client, conn, 1 << 20)
        rx, tx, to = udp_pair()
        for _ in range(100):
            tx.sendto(b"y" * 1000, to)
            rx.recv(2000)
    finally:
        parker.kill()
        parker.wait()
    return {"tcp_tx": 1 << 20, "tcp_rx": got, "udp_tx": 100_000, "udp_rx": 100_000}


def kretprobe_slots():
    """How many calls a kretprobe tracks at once: max(10, 2 * CPUs)."""
    with open("/sys/devices/system/cpu/possible") as f:
        last = f.read().strip().split(",")[-1].split("-")[-1]
    return max(10, 2 * (int(last) + 1))


@scenario
def crowded_splice():
    """More threads blocked in splice() from TCP than its kretprobe tracks:
    netmon must log the calls it missed (checked in main())."""
    keep = []
    for _ in range(kretprobe_slots() + 2):
        client, conn = tcp_pair()  # nothing is sent, so splice() waits
        r, w = os.pipe()
        keep.append((client, conn, r, w))
        threading.Thread(
            target=os.splice, args=(conn.fileno(), w, 1000), daemon=True
        ).start()
    time.sleep(1.5)  # a report while they wait
    return {}


@scenario
def thread_name():
    """A thread that renamed itself moves the bytes; the row has the
    process's name, from /proc while the process lives."""

    def named_sender():
        ctypes.CDLL(None).prctl(15, b"worker", 0, 0, 0)  # PR_SET_NAME
        rx, tx, to = udp_pair()
        tx.sendto(b"y" * 1000, to)
        rx.recv(2000)

    worker = threading.Thread(target=named_sender)
    worker.start()
    worker.join()
    time.sleep(1.5)  # a report while this process still lives
    return {"udp_tx": 1000, "udp_rx": 1000, "comm": "python3"}


@scenario
def mptcp():
    """MPTCP at both ends: the kernel drives the TCP subflows itself."""
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_MPTCP)
    except OSError:
        return {"skip": "MPTCP is off in this kernel"}
    server.bind(("127.0.0.1", 0))
    server.listen()
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_MPTCP)
    client.connect(server.getsockname())
    conn, _ = server.accept()
    got = send_and_drain(client, conn, 1 << 20)
    return {"tcp_tx": 1 << 20, "tcp_rx": got}


def park():
    keep = []
    for _ in range(PARKED):
        client, conn = tcp_pair()  # conn never reads, so sendall blocks
        keep.append((client, conn))
        threading.Thread(
            target=client.sendall, args=(b"p" * (64 << 20),), daemon=True
        ).start()
        idle = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        idle.bind(("127.0.0.1", 0))
        keep.append(idle)
        threading.Thread(target=idle.recv, args=(10,), daemon=True).start()
    print("parked", flush=True)
    time.sleep(60)


@scenario
def odd_name():
    """A process may name itself anything; the JSON must still parse."""
    libc = ctypes.CDLL(None)
    pr_set_name = 15
    libc.prctl(pr_set_name, ODD_NAME.encode(), 0, 0, 0)
    rx, tx, to = udp_pair()
    tx.sendto(b"y" * 1000, to)
    rx.recv(2000)
    return {"udp_tx": 1000, "udp_rx": 1000, "comm": ODD_NAME}


def run_scenario(name):
    """Runs a scenario in a new process. Returns its pid and expected row."""
    child = subprocess.run(
        [sys.executable, __file__, "scenario", name],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    pid, want = json.loads(child.stdout)
    return pid, want


def check_counts(rows, results):
    failed = []
    for name, (pid, want) in results.items():
        got = dict.fromkeys(COUNTERS, 0)
        comms = set()
        for row in rows:
            if row["pid"] == pid:
                comms.add(row["comm"])
                for k in COUNTERS:
                    got[k] += row[k]
        if "skip" in want:
            print(f"skip: {name}: {want['skip']}")
            continue
        expect = {k: want.get(k, 0) for k in COUNTERS}
        ok = got == expect
        if "comm" in want:
            ok = ok and comms == {want["comm"]}
        print(f"{'ok  ' if ok else 'FAIL'}: {name}: netmon {got}, comm {comms}")
        if not ok:
            print(f"      moved {expect}")
            failed.append(name)
    return failed


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
    print("ok  : -k 1 sampled rows per report:", per_report)


def check_files(out_dir, stdout_rows, pids):
    """-o writes the same rows as -j -n 0 to out_dir/YYYY-MM-DD/HH.jsonl:
    every row, though -n defaults to 10."""
    paths = glob.glob(os.path.join(out_dir, "*", "*.jsonl"))
    rows = [json.loads(line) for p in paths for line in open(p)]
    for p in paths:
        day, hour = p.split(os.sep)[-2:]
        if len(day) != 10 or len(hour) != len("HH.jsonl"):
            sys.exit(f"FAIL: unexpected file name {p}")
    if not rows or set(rows[0]) != set(stdout_rows[0]):
        sys.exit(f"FAIL: file rows differ from stdout rows: {rows[:1]}")
    if rows[0]["host"] != socket.gethostname():
        sys.exit(f"FAIL: host {rows[0]['host']}, want {socket.gethostname()}")
    if not set(pids) <= {row["pid"] for row in rows}:
        sys.exit("FAIL: -o left out some processes' rows")
    print(f"ok  : -o wrote {len(rows)} rows to {len(paths)} file(s)")


def check_missed_logged(err):
    """The kernel reports kretprobe misses from 6.7 on."""
    version = tuple(int(x) for x in platform.release().split(".")[:2])
    if version < (6, 7):
        print("skip: missed calls aren't reported before Linux 6.7")
    elif "kretprobe/tcp_splice_read missed" not in err:
        sys.exit(f"FAIL: netmon didn't log the splice() calls it missed:\n{err}")
    else:
        print("ok  : logged the splice() calls it missed")


def check_pid_namespace():
    """netmon reports pids as the host sees them, so must this test."""
    proc_pid_init_ino = 0xEFFFFFFC
    if os.stat("/proc/self/ns/pid").st_ino != proc_pid_init_ino:
        sys.exit("FAIL: in a pid namespace of its own; use docker run --pid=host")


def main():
    check_pid_namespace()
    netmon = subprocess.Popen(
        ["netmon", "-j", "-n", "0", "-i", "1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # A second instance keeps one row per report via priority sampling. It
    # stops by itself: a stop's last report can share a second with the one
    # before. A third writes hourly files, as the service does.
    sampled = subprocess.Popen(
        ["netmon", "-j", "-k", "1", "-i", "1", "-c", "4"],
        stdout=subprocess.PIPE,
        text=True,
    )
    out_dir = tempfile.mkdtemp()
    to_files = subprocess.Popen(["netmon", "-i", "1", "-w", "5", "-o", out_dir])
    time.sleep(1.5)  # let the probes attach and the baseline read happen

    results = {name: run_scenario(name) for name in SCENARIOS}
    time.sleep(2.5)  # a report after the last byte
    for p in (netmon, to_files):
        p.terminate()
    out, err = netmon.communicate(timeout=30)
    sampled_out, _ = sampled.communicate(timeout=30)
    for p in (netmon, sampled, to_files):
        if p.wait(timeout=30) != 0:
            sys.exit(f"FAIL: {p.args} exited with {p.returncode}")

    rows = [json.loads(line) for line in out.splitlines()]
    failed = check_counts(rows, results)
    check_missed_logged(err)
    check_sampled(sampled_out)
    moved = [
        pid for pid, want in results.values() if any(want.get(k) for k in COUNTERS)
    ]
    check_files(out_dir, rows, moved)
    if failed:
        sys.exit(f"FAIL: wrong counts for {failed}")
    print("PASS")


if __name__ == "__main__":
    if sys.argv[1:2] == ["scenario"]:
        want = SCENARIOS[sys.argv[2]]()
        print(json.dumps([os.getpid(), want]), flush=True)
        os._exit(0)  # leave any parked threads behind
    elif sys.argv[1:2] == ["park"]:
        park()
    else:
        main()
