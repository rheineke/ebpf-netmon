"""Check that netmon charges NFS file I/O to the process that did it.

On a machine with an NFS mount, from the repo root:

  sudo python3 tests/nfs_test.py FILE_ON_NFS [WRITABLE_DIR_ON_NFS]

Reads FILE_ON_NFS with read() and again with sendfile(), and if a writable
directory is given, writes and deletes a scratch file there. Passes when
netmon's nfs_read and nfs_write for this process match the bytes moved.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

# A local build if there is one, else netmon on PATH (the Docker test image).
NETMON = os.path.join(os.path.dirname(__file__), "..", "build", "netmon")
if not os.path.exists(NETMON):
    NETMON = "netmon"
WRITE_BYTES = 8 * 1024 * 1024


def read_file(path):
    total = 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            total += len(chunk)
    return total


def sendfile_file(path):
    """sendfile() goes through splice_read, the path media servers use."""
    rx, tx = socket.socketpair()

    def drain():
        while rx.recv(1 << 16):
            pass

    drainer = threading.Thread(target=drain)
    drainer.start()
    sent = 0
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        while sent < size:
            sent += os.sendfile(tx.fileno(), f.fileno(), sent, size - sent)
    tx.close()
    drainer.join()
    rx.close()
    return sent


def write_file(directory):
    # NFS usually maps root to nobody, so the directory must allow that.
    path = os.path.join(directory, f".netmon_nfs_test.{os.getpid()}")
    with open(path, "wb") as f:
        try:
            f.write(b"w" * WRITE_BYTES)
        finally:
            os.unlink(path)
    return WRITE_BYTES


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    netmon = subprocess.Popen(
        [NETMON, "-j", "-n", "0", "-i", "1"], stdout=subprocess.PIPE, text=True
    )
    time.sleep(1.5)  # let the probes attach and the baseline read happen

    want = {"nfs_read": read_file(sys.argv[1]) + sendfile_file(sys.argv[1])}
    if len(sys.argv) == 3:
        want["nfs_write"] = write_file(sys.argv[2])
    print(f"moved: {want}", flush=True)

    time.sleep(2)  # one more report after the last byte
    netmon.terminate()
    out, _ = netmon.communicate(timeout=10)
    counted = dict.fromkeys(want, 0)
    for line in out.splitlines():
        row = json.loads(line)
        if row["pid"] == os.getpid():
            for k in counted:
                counted[k] += row[k]
    print(f"netmon counted: {counted}")
    if counted != want:
        sys.exit("FAIL: netmon disagrees with the NFS bytes moved")
    print("PASS")


if __name__ == "__main__":
    main()
