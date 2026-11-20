"""Check that netmon counts the NFS bytes of many READs in flight at once.

On a machine with an NFS mount, from the repo root:

  sudo python3 tests/nfs_inflight_test.py FILE_ON_NFS

Reads FILE_ON_NFS with one O_DIRECT call, after dropping its cached pages,
which starts a READ per rsize bytes before the first reply: on a mount with
rsize=4096, 12288 for a 48 MiB file, near the 16384 netmon tracks. Passes
when this process's nfs_fetched is the file's size, short of the replies
netmon logged as unmatched because the kernel skipped their start.
"""

import json
import mmap
import os
import re
import subprocess
import sys
import time

NETMON = os.path.join(os.path.dirname(__file__), "..", "build", "netmon")
if not os.path.exists(NETMON):
    NETMON = "netmon"


def read_direct(path):
    """One O_DIRECT read of the whole file, into a page-aligned buffer."""
    size = os.path.getsize(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        with mmap.mmap(-1, size) as buf:
            return os.preadv(fd, [buf], 0)
    finally:
        os.close(fd)


def logged(pattern, text):
    return [tuple(map(int, m)) for m in re.findall(pattern, text)]


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    netmon = subprocess.Popen(
        [NETMON, "-j", "-n", "0", "-i", "1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(3.5)  # attached, and past the two reports it logs nothing in
    size = read_direct(sys.argv[1])
    time.sleep(2.5)  # one more report after the last reply
    netmon.terminate()
    out, err = netmon.communicate(timeout=10)
    fetched = sum(
        row["nfs_fetched"]
        for row in map(json.loads, out.splitlines())
        if row["pid"] == os.getpid()
    )
    unmatched = logged(r"(\d+) NFS READ replies \((\d+) bytes\)", err)
    replies = sum(n for n, _ in unmatched)
    lost = sum(b for _, b in unmatched)
    starts = sum(n for (n,) in logged(r"nfs_initiate_read skipped (\d+)", err))
    dones = sum(n for (n,) in logged(r"nfs_readpage_done skipped (\d+)", err))
    print(
        f"read {size}, nfs_fetched {fetched}, logged unmatched {replies}"
        f" replies ({lost} bytes); skipped starts {starts}, replies {dones}"
    )
    if replies > starts:
        sys.exit("FAIL: replies lost with fewer READs in flight than netmon tracks")
    # A skipped reply's bytes can't be known, so then only a bound holds.
    if fetched + lost != size and not (dones and fetched + lost < size):
        sys.exit("FAIL: netmon's count and log disagree with the bytes read")
    print("PASS")


if __name__ == "__main__":
    main()
