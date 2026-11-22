"""Check that netmon charges NFS file I/O to the process that did it.

On a machine with an NFS mount, from the repo root:

  sudo python3 tests/nfs_test.py FILE_ON_NFS [WRITABLE_DIR_ON_NFS]

Reads FILE_ON_NFS four times: with read() after dropping its cached pages,
with sendfile() from the page cache, then memory-mapped and with O_DIRECT,
each after dropping its pages again. If a writable directory is given, writes
scratch files there three ways: write() then fsync(), stores through a
mapping then msync(), and O_DIRECT, then five that another process writes
too: appended to with O_APPEND, stored to through a mapping, rewritten and
appended to with O_DIRECT while the other's pages wait, and written, or
stored to through a mapping, while the other's pages wait, both sent by this
process's fsync(). It deletes them all. Passes when netmon's nfs_read and
nfs_write for this process match the file bytes it read and wrote (read(),
sendfile() and write(), not the mapped ones), nfs_fetched the bytes the
server sent, the file once for each read but the cached one, and nfs_sent the
bytes the server took for this process's writes only. This process's and the
other's nfs_sent must also add up to what the kernel counts the server took
on the directory's mount (serverwritebytes in /proc/self/mountstats), which
nothing else may write to meanwhile.

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


OTHER_WRITER = """
import os, sys
with open(sys.argv[1], "r+b" if sys.argv[3] == "hold-tail" else "wb") as f:
    if sys.argv[3] == "hold-tail":  # all but the first MiB
        f.seek(1 << 20)
        f.write(b"o" * (int(sys.argv[2]) - (1 << 20)))
    else:
        f.write(b"o" * int(sys.argv[2]))
    if sys.argv[3] == "fsync":
        os.fsync(f.fileno())
    else:  # hold the pages dirty until told
        print("written", flush=True)
        sys.stdin.readline()
"""
OTHERS = []  # their pids


def written_by_another(path, size, then="fsync"):
    """Another process writes path, so netmon has a writer for its MiBs.
    Unless then is "fsync", returns it still running, holding them dirty."""
    other = subprocess.Popen(
        [sys.executable, "-c", OTHER_WRITER, path, str(size), then],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    OTHERS.append(other.pid)
    if then == "fsync":
        other.wait()
        return None
    other.stdout.readline()
    return other


def append_file(directory):
    """O_APPEND after another process wrote the file, ending mid-MiB: write()
    is called at position 0, and the kernel moves it to the end, in a MiB the
    other process wrote."""
    path = scratch(directory, "append")
    try:
        written_by_another(path, WRITE_BYTES + (512 << 10))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, b"a" * (64 << 10))
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.unlink(path)
    return 64 << 10


def mmap_after_another(directory):
    """Stores through a mapping to a file another process wrote. Returns the
    bytes stored, which the faults first read in."""
    path = scratch(directory, "mmap_other")
    try:
        written_by_another(path, WRITE_BYTES)
        drop_cache(path)  # so the faults read every page, as counted
        with open(path, "r+b") as f, mmap.mmap(f.fileno(), WRITE_BYTES) as m:
            for i in range(0, WRITE_BYTES, mmap.PAGESIZE):
                m[i] = 2
            m.flush()
    finally:
        os.unlink(path)
    return WRITE_BYTES


def write_beside_another(directory):
    """Writes the second half of a file whose first half another process
    wrote and holds dirty; this process's fsync() sends both."""
    path = scratch(directory, "beside")
    try:
        other = written_by_another(path, WRITE_BYTES // 2, then="hold")
        with open(path, "r+b") as f:
            f.seek(WRITE_BYTES // 2)
            f.write(b"b" * (WRITE_BYTES // 2))
            os.fsync(f.fileno())
        other.stdin.write("done\n")
        other.stdin.close()
        other.wait()
    finally:
        os.unlink(path)
    return WRITE_BYTES // 2


def direct_after_another(directory):
    """Rewrites with O_DIRECT the MiBs another process wrote and holds
    dirty: the kernel sends the other's pages first, charged to the other,
    then this process's. Then appends with O_DIRECT and RWF_APPEND, which
    lands mid-MiB in the other's last MiB."""
    path = scratch(directory, "direct_other")
    other = None
    try:
        other = written_by_another(path, WRITE_BYTES + (512 << 10), then="hold")
        fd = os.open(path, os.O_WRONLY | os.O_DIRECT)
        try:
            with mmap.mmap(-1, WRITE_BYTES) as buf:  # page-aligned
                buf.write(b"r" * WRITE_BYTES)
                n = os.pwritev(fd, [buf], 0)
            with mmap.mmap(-1, 64 << 10) as buf:
                buf.write(b"p" * (64 << 10))
                n += os.pwritev(fd, [buf], 0, os.RWF_APPEND)
            return n
        finally:
            os.close(fd)
    finally:
        if other:
            other.stdin.write("done\n")
            other.stdin.close()
            other.wait()
        os.unlink(path)


def map_beside_another(directory):
    """Stores through a mapping to the first MiB of a file, whose next MiBs
    another process writes and holds dirty meanwhile; this process's fsync()
    sends both. It maps the file before the other writes, since an open()
    or fstat() would send the other's pages first. Returns the bytes this
    process sent (the first page twice) and those its faults read in."""
    path = scratch(directory, "map_beside")
    mib = 1 << 20
    other = None
    try:
        with open(path, "w+b") as f:
            f.truncate(mib)  # the faults read nothing past the first MiB
            with mmap.mmap(f.fileno(), mib) as m:
                m[0] = 3
                m.flush()  # sent, so the other's open() sends nothing
                other = written_by_another(path, 4 * mib, then="hold-tail")
                for i in range(0, mib, mmap.PAGESIZE):
                    m[i] = 3
                m.flush()
                os.fsync(f.fileno())
    finally:
        if other:
            other.stdin.write("done\n")
            other.stdin.close()
            other.wait()
        os.unlink(path)
    return mib + mmap.PAGESIZE, mib


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
        appended = append_file(directory)
        want["nfs_write"] += appended
        want["nfs_sent"] += appended
        stored = mmap_after_another(directory)
        want["nfs_fetched"] += stored
        want["nfs_sent"] += stored
        beside = write_beside_another(directory)
        want["nfs_write"] += beside
        want["nfs_sent"] += beside
        rewritten = direct_after_another(directory)
        want["nfs_write"] += rewritten
        want["nfs_sent"] += rewritten
        sent_beside, fetched_beside = map_beside_another(directory)
        want["nfs_fetched"] += fetched_beside
        want["nfs_sent"] += sent_beside
    print(f"moved: {want}", flush=True)

    time.sleep(2)  # one more report after the last byte
    written_after = server_written(directory) if directory else None
    netmon.terminate()
    out, err = netmon.communicate(timeout=10)
    sys.stderr.write(err)
    if "could not attach tracepoint/nfs/nfs_file_write: no such tracepoint" in err:
        print("skip: this kernel has no nfs:nfs_file_write, so no nfs_sent")
        want.pop("nfs_sent", None)
    counted = dict.fromkeys(want, 0)
    sent_by_test = 0
    for line in out.splitlines():
        row = json.loads(line)
        if row["pid"] in OTHERS + [os.getpid()]:
            sent_by_test += row.get("nfs_sent", 0)  # left out while not counted
        if row["pid"] == os.getpid():
            for k in counted:
                counted[k] += row.get(k, 0)
    print(f"netmon counted: {counted}")
    if counted != want:
        sys.exit("FAIL: netmon disagrees with the NFS bytes moved")
    if "nfs_sent" in want:
        server = written_after - written_before
        print(f"nfs_sent of the test's processes {sent_by_test}", end=", ")
        print(f"serverwritebytes {server}")
        if sent_by_test != server:
            sys.exit("FAIL: nfs_sent disagrees with the kernel's count")
    print("PASS")


if __name__ == "__main__":
    main()
