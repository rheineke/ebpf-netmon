"""Check that netmon charges NFS file I/O to the process that did it.

On a machine with an NFS mount, from the repo root:

  sudo python3 tests/nfs_test.py FILE_ON_NFS [WRITABLE_DIR_ON_NFS]

Reads FILE_ON_NFS four times: with read() after dropping its cached pages,
with sendfile() from the page cache, then memory-mapped and with O_DIRECT,
each after dropping its pages again. If a writable directory is given, writes
scratch files there three ways: write() then fsync(), stores through a
mapping then msync(), and O_DIRECT, and deletes them. Passes when netmon's
nfs_read and nfs_write for this process match the file bytes it read and
wrote (read(), sendfile() and write(), not the mapped ones), nfs_fetched the
bytes the server sent, the file once for each read but the cached one, and
nfs_sent the bytes the server took, each scratch file once. Every process's
nfs_sent must also add up to what the kernel counts the server took on the
directory's mount (serverwritebytes in /proc/self/mountstats).

On a kernel without the nfs:nfs_file_write tracepoint, netmon can't count
nfs_sent, and the test says so and checks the rest.
"""

import json
import mmap
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


def drop_cache(path):
    """Drops the file's clean pages, so the next read goes to the server."""
    with open(path, "rb") as f:
        os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)


def read_file(path):
    total = 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            total += len(chunk)
    return total


def mmap_file(path):
    """Touches every page: page faults read it, not read() (no nfs_read)."""
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        sum(m[i] for i in range(0, len(m), mmap.PAGESIZE))
        return len(m)


def direct_read_file(path):
    """O_DIRECT in 1 MiB requests, which ask for more than the file has left
    at the end: the server sends only what there is."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
    try:
        with mmap.mmap(-1, 1 << 20) as buf:  # page-aligned
            total = 0
            while n := os.preadv(fd, [buf], total):
                total += n
            return total
    finally:
        os.close(fd)


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


def scratch(directory, name):
    # NFS usually maps root to nobody, so the directory must allow that.
    return os.path.join(directory, f".netmon_nfs_test.{os.getpid()}.{name}")


def write_file(directory):
    """write(), then fsync(), which sends the pages from this process."""
    path = scratch(directory, "write")
    try:
        with open(path, "wb") as f:
            f.write(b"w" * WRITE_BYTES)
            os.fsync(f.fileno())
    finally:
        os.unlink(path)
    return WRITE_BYTES


def mmap_write_file(directory):
    """Stores through a shared mapping: page faults read the pages in and
    dirty them, with no write() (no nfs_write), and msync() sends them."""
    path = scratch(directory, "mmap")
    try:
        with open(path, "w+b") as f:
            f.truncate(WRITE_BYTES)
            with mmap.mmap(f.fileno(), WRITE_BYTES) as m:
                for i in range(0, WRITE_BYTES, mmap.PAGESIZE):
                    m[i] = 1
                m.flush()
    finally:
        os.unlink(path)
    return WRITE_BYTES


def direct_write_file(directory):
    """O_DIRECT in 1 MiB requests: sent at once, from this process."""
    path = scratch(directory, "direct")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_DIRECT, 0o644)
    try:
        with mmap.mmap(-1, 1 << 20) as buf:  # page-aligned
            buf.write(b"d" * (1 << 20))
            total = 0
            while total < WRITE_BYTES:
                total += os.pwritev(fd, [buf], total)
            return total
    finally:
        os.close(fd)
        os.unlink(path)


def server_written(directory):
    """The bytes the kernel counts the server took on directory's mount."""
    directory = os.path.realpath(directory)
    best, written = "", None
    with open("/proc/self/mountstats") as f:
        mount = None
        for line in f:
            words = line.split()
            if line.startswith("device "):
                mount = words[4] if "nfs" in line.split("fstype")[-1] else None
            elif mount and words[:1] == ["bytes:"]:
                inside = directory == mount or directory.startswith(
                    mount.rstrip("/") + "/"
                )
                if inside and len(mount) > len(best):
                    best, written = mount, int(words[6])
    return written


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    netmon = subprocess.Popen(
        [NETMON, "-j", "-n", "0", "-i", "1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.5)  # let the probes attach and the baseline read happen
    directory = sys.argv[2] if len(sys.argv) == 3 else None
    written_before = server_written(directory) if directory else None

    path = sys.argv[1]
    drop_cache(path)
    cold = read_file(path)
    warm = sendfile_file(path)  # from the page cache: nothing fetched
    drop_cache(path)
    mapped = mmap_file(path)
    drop_cache(path)
    direct = direct_read_file(path)
    # O_DIRECT reads are read() calls too; memory-mapped ones are not.
    want = {"nfs_read": cold + warm + direct, "nfs_fetched": cold + mapped + direct}
    if directory:
        written = write_file(directory)
        mapped_written = mmap_write_file(directory)
        direct_written = direct_write_file(directory)
        # write() and O_DIRECT are write() calls; stores to a mapping are not,
        # and fault each page in first, which the server sends.
        want["nfs_write"] = written + direct_written
        want["nfs_fetched"] += mapped_written
        want["nfs_sent"] = written + mapped_written + direct_written
    print(f"moved: {want}", flush=True)

    time.sleep(2)  # one more report after the last byte
    written_after = server_written(directory) if directory else None
    netmon.terminate()
    out, err = netmon.communicate(timeout=10)
    sys.stderr.write(err)
    if "could not attach tracepoint/nfs/nfs_file_write" in err:
        print("skip: this kernel has no nfs:nfs_file_write, so no nfs_sent")
        want.pop("nfs_sent", None)
    counted = dict.fromkeys(want, 0)
    sent_by_all = 0
    for line in out.splitlines():
        row = json.loads(line)
        sent_by_all += row["nfs_sent"]
        if row["pid"] == os.getpid():
            for k in counted:
                counted[k] += row[k]
    print(f"netmon counted: {counted}")
    if counted != want:
        sys.exit("FAIL: netmon disagrees with the NFS bytes moved")
    if "nfs_sent" in want:
        server = written_after - written_before
        print(f"nfs_sent of every process {sent_by_all}, serverwritebytes {server}")
        if sent_by_all != server:
            sys.exit("FAIL: nfs_sent disagrees with the kernel's count")
    print("PASS")


if __name__ == "__main__":
    main()
