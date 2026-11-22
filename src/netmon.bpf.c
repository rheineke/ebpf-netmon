// SPDX-License-Identifier: (MIT OR GPL-2.0-only)
/*
 * Per-process TCP/UDP byte counts, NFS file bytes read and written, and the
 * NFS file bytes the server sent for each process's reads and took for its
 * writes.
 *
 * Nothing here reads kernel structs, only tracepoint records, integer return
 * values and process ids, so this needs no CO-RE and no kernel BTF.
 * Raspberry Pi kernels ship without /sys/kernel/btf/vmlinux.
 */
#include <linux/bpf.h>
#include <linux/ptrace.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "netmon.h"

char LICENSE[] SEC("license") = "Dual MIT/GPL";

/* From the kernel's socket.h and in.h, which BPF programs can't include. */
#define AF_INET 2
#define AF_INET6 10
#define IPPROTO_TCP 6
#define IPPROTO_UDP 17
#define IPPROTO_UDPLITE 136
#define IPPROTO_MPTCP 262
#define MSG_PEEK 0x2
#define MSG_ERRQUEUE 0x2000

/* Keyed by tgid. LRU so exited processes age out without user-space cleanup. */
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 16384);
	__type(key, __u32);
	__type(value, struct netmon_traffic);
} traffic SEC(".maps");

/*
 * Adds bytes to process tgid's counter. A new entry is named uid and comm.
 * With refresh, so is an existing one: the owner as of now, as the process
 * may have dropped privileges, or be a new one that reused an exited one's
 * pid. Without it, from a kernel thread, the names saved earlier don't
 * replace newer ones.
 */
static __always_inline int add_bytes(__u32 tgid, __u32 uid,
				     const char comm[NETMON_COMM_LEN],
				     enum netmon_counter counter, long bytes,
				     int refresh)
{
	struct netmon_traffic *t = bpf_map_lookup_elem(&traffic, &tgid);
	if (!t) {
		struct netmon_traffic init = {};
		init.uid = uid;
		__builtin_memcpy(init.comm, comm, sizeof(init.comm));
		bpf_map_update_elem(&traffic, &tgid, &init, BPF_NOEXIST);
		t = bpf_map_lookup_elem(&traffic, &tgid);
		if (!t)
			return 0;
	} else if (refresh) {
		t->uid = uid;
		__builtin_memcpy(t->comm, comm, sizeof(t->comm));
	}
	__sync_fetch_and_add(&t->bytes[counter], bytes);
	return 0;
}

/* Charges bytes to the process running now. */
static __always_inline int account(enum netmon_counter counter, long bytes)
{
	if (bytes <= 0)
		return 0;

	char comm[NETMON_COMM_LEN];
	bpf_get_current_comm(comm, sizeof(comm));
	return add_bytes(bpf_get_current_pid_tgid() >> 32,
			 (__u32)bpf_get_current_uid_gid(), comm, counter, bytes,
			 1);
}

static __always_inline int account_sock(const struct netmon_sock_msg_length *m,
					enum netmon_counter tcp,
					enum netmon_counter udp)
{
	if (m->family != AF_INET && m->family != AF_INET6)
		return 0;
	/*
	 * An MPTCP socket's subflows are TCP sockets the kernel drives
	 * itself, without sock_sendmsg(), so its bytes count once, here.
	 */
	if (m->protocol == IPPROTO_TCP || m->protocol == IPPROTO_MPTCP)
		return account(tcp, m->ret);
	if (m->protocol == IPPROTO_UDP || m->protocol == IPPROTO_UDPLITE)
		return account(udp, m->ret);
	return 0;
}

/*
 * TCP and UDP: once per sock_sendmsg() and sock_recvmsg(), which every send
 * and receive system call (and io_uring) goes through, with the bytes it
 * moved. Probing tcp_sendmsg, udp_sendmsg and their kin instead counts some
 * bytes twice: an IPv6 socket sending to an IPv4 address goes through both
 * udpv6_sendmsg and udp_sendmsg, and tcp_cleanup_rbuf runs again each time
 * a MSG_WAITALL read sleeps. Their kretprobes also lose every call while
 * more than a few threads wait inside the function.
 */
SEC("tracepoint/sock/sock_send_length")
int sock_send_length(const struct netmon_sock_msg_length *m)
{
	return account_sock(m, NETMON_TCP_TX, NETMON_UDP_TX);
}

SEC("tracepoint/sock/sock_recv_length")
int sock_recv_length(const struct netmon_sock_msg_length *m)
{
	/* A peek leaves the bytes for the read that follows. */
	if (m->flags & (MSG_PEEK | MSG_ERRQUEUE))
		return 0;
	return account_sock(m, NETMON_TCP_RX, NETMON_UDP_RX);
}

/*
 * splice() from a TCP socket, as some proxies use, skips sock_recvmsg().
 * Returns the bytes moved into the pipe.
 */
SEC("kretprobe/tcp_splice_read")
int BPF_KRETPROBE(tcp_splice_read_ret, long ret)
{
	return account(NETMON_TCP_RX, ret);
}

/*
 * The NFS client's wire bytes don't tell which process asked for them:
 * replies arrive in kernel threads, and a synchronous call (stat, open)
 * sends from the calling process along with any other requests queued
 * behind it. These run in the calling process and return the file bytes it
 * read or wrote, including reads served from the page cache.
 */
SEC("kretprobe/nfs_file_read")
int BPF_KRETPROBE(nfs_file_read_ret, long ret)
{
	return account(NETMON_NFS_READ, ret);
}

/* sendfile() and splice() from an NFS file, e.g. a media server streaming. */
SEC("kretprobe/nfs_file_splice_read")
int BPF_KRETPROBE(nfs_file_splice_read_ret, long ret)
{
	return account(NETMON_NFS_READ, ret);
}

SEC("kretprobe/nfs_file_write")
int BPF_KRETPROBE(nfs_file_write_ret, long ret)
{
	return account(NETMON_NFS_WRITE, ret);
}

/*
 * File bytes the NFS server sent for a process's reads: page-cache misses,
 * readahead, memory-mapped and O_DIRECT reads, but not reads the page cache
 * served. The NFS client starts each READ in the process that needs the
 * pages (nfs_initiate_read), and the reply completes in a kernel thread
 * (nfs_readpage_done), so the first records who asked, keyed by what both
 * records carry, and the second charges that process with the bytes sent.
 */
struct nfs_reader {
	__u32 tgid;
	__u32 uid;
	char comm[NETMON_COMM_LEN];
	__u32 pending; /* READs of these bytes in flight */
	__u32 pad;
};

/*
 * READs in flight: one O_DIRECT or io_uring call can start many, one per
 * rsize bytes, before the first reply. 16384 covers 2 GiB in flight at a
 * time with the usual 128 KiB rsize; past that, the LRU evicts the oldest,
 * whose replies are then counted in nfs_unmatched, as are those of a READ
 * started before netmon attached, or whose start the kernel skipped. The
 * last reply deletes its entry: answered entries left in place would fill
 * the map, and the LRU, which keeps what was looked up last, would then
 * evict READs still in flight first. LRU also ages out a READ whose reply
 * never came (a mount forced off).
 */
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 16384);
	__type(key, struct netmon_nfs_read_key);
	__type(value, struct nfs_reader);
} nfs_reads SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct netmon_nfs_unmatched);
} nfs_unmatched SEC(".maps");

/*
 * Records reader as the process to charge for a READ's reply. Concurrent
 * READs of the same bytes (threads or io_uring with O_DIRECT; buffered
 * reads share the page cache) share an entry, counting them, and are all
 * charged to the last to start.
 */
static __always_inline void expect_reply(const struct netmon_nfs_read_key *key,
					 struct nfs_reader *reader)
{
	struct nfs_reader *found = bpf_map_lookup_elem(&nfs_reads, key);
	if (!found) {
		reader->pending = 1;
		if (!bpf_map_update_elem(&nfs_reads, key, reader, BPF_NOEXIST))
			return;
		found = bpf_map_lookup_elem(&nfs_reads, key); /* raced */
		if (!found)
			return;
	}
	found->tgid = reader->tgid;
	found->uid = reader->uid;
	__builtin_memcpy(found->comm, reader->comm, sizeof(found->comm));
	__sync_fetch_and_add(&found->pending, 1);
}

SEC("tracepoint/nfs/nfs_initiate_read")
int nfs_initiate_read(const struct netmon_nfs_initiate_read *r)
{
	struct netmon_nfs_read_key key = {};
	key.fileid = r->fileid;
	key.offset = r->offset;
	key.dev = r->dev;
	key.fhandle = r->fhandle;
	struct nfs_reader reader = {};
	reader.tgid = bpf_get_current_pid_tgid() >> 32;
	reader.uid = (__u32)bpf_get_current_uid_gid();
	bpf_get_current_comm(reader.comm, sizeof(reader.comm));
	expect_reply(&key, &reader);
	return 0;
}

/* Counts a reply no recorded READ is waiting for. */
static __always_inline int unmatched(__u32 bytes)
{
	__u32 zero = 0;
	struct netmon_nfs_unmatched *u =
		bpf_map_lookup_elem(&nfs_unmatched, &zero);
	/* Plain adds: the kernel runs one tracing program at a time per CPU,
	 * with preemption off. */
	if (u && bytes) {
		u->replies++;
		u->bytes += bytes;
	}
	return 0;
}

SEC("tracepoint/nfs/nfs_readpage_done")
int nfs_readpage_done(const struct netmon_nfs_readpage_done *r)
{
	struct netmon_nfs_read_key key = {};
	key.fileid = r->fileid;
	key.offset = r->offset;
	key.dev = r->dev;
	key.fhandle = r->fhandle;
	struct nfs_reader *found = bpf_map_lookup_elem(&nfs_reads, &key);
	if (!found)
		return unmatched(r->res_count);
	struct nfs_reader reader = *found;
	__u32 old = __sync_fetch_and_add(&found->pending, (__u32)-1);
	/* None in flight: undo, and say so. Another reply here meanwhile sees
	 * the count wrap, and is charged; totals still match. */
	if (old == 0) {
		__sync_fetch_and_add(&found->pending, 1);
		return unmatched(r->res_count);
	}
	/*
	 * The delete is by key, so it can't wait for the count: a READ of the
	 * same bytes started on another CPU just now loses its entry, and its
	 * reply is counted in nfs_unmatched. Overlapping identical READs are
	 * rare (O_DIRECT from several threads at once), and buffered reads
	 * share the page cache instead.
	 */
	if (old == 1)
		bpf_map_delete_elem(&nfs_reads, &key);
	if (r->res_count)
		add_bytes(reader.tgid, reader.uid, reader.comm,
			  NETMON_NFS_FETCHED, r->res_count, 0);
	struct netmon_nfs_read_key next = {};
	if (netmon_nfs_read_retry(r, &next))
		expect_reply(&next, &reader);
	return 0;
}

/*
 * File bytes the NFS server took for a process's writes. A write() fills
 * the page cache, and the client sends the dirty pages later: mostly from a
 * kernel thread (writeback), or on fsync() or close(), when the pages of
 * several writers may go out together. So netmon records who wrote where,
 * and nfs_writeback_done charges each WRITE's reply to the last writer of
 * the file's MiB it covers:
 * - nfs_writepage_setup, as a write() or a store through a mapping dirties
 *   a page, records the process as the writer of the page's MiB, at its
 *   file position (an O_APPEND write's true one);
 * - nfs_file_write records an O_DIRECT write()'s MiBs, which it sends at
 *   once, and every write()'s process as the file's writer, the fallback.
 * Writers of the same MiB between two WRITEs are charged as the last; a
 * reply with no writer recorded (pages written before netmon attached) is
 * counted in nfs_unmatched.
 */
struct nfs_writer {
	__u32 tgid;
	__u32 uid;
	char comm[NETMON_COMM_LEN];
};

/*
 * 16384 entries: a MiB of a file each, and one per file written. LRU, so
 * those written longest ago age out; each reply's lookups keep its alive.
 */
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 16384);
	__type(key, struct netmon_nfs_write_key);
	__type(value, struct nfs_writer);
} nfs_writers SEC(".maps");

/* A write() or page records at most this many MiBs; for an O_DIRECT
 * write(), the file's entry covers the rest. */
#define NFS_WRITE_CHUNKS 64
/* From the kernel's fs.h, as 6.18's format file prints them. An O_DIRECT
 * RWF_APPEND write's position isn't known yet: it is the file's writer
 * only. */
#define IOCB_APPEND 0x10
#define IOCB_DIRECT (1 << 17)

static __always_inline void current_writer(struct nfs_writer *w)
{
	w->tgid = bpf_get_current_pid_tgid() >> 32;
	w->uid = (__u32)bpf_get_current_uid_gid();
	bpf_get_current_comm(w->comm, sizeof(w->comm));
}

/* Records w as key's writer, unless it already is: a lookup is cheaper
 * than an update, and a write() dirties many pages. */
static __always_inline void set_writer(const struct netmon_nfs_write_key *key,
				       const struct nfs_writer *w)
{
	struct nfs_writer *found = bpf_map_lookup_elem(&nfs_writers, key);
	if (found && found->tgid == w->tgid && found->uid == w->uid)
		return;
	bpf_map_update_elem(&nfs_writers, key, w, BPF_ANY);
}

/* Records w as the writer of the MiBs of [offset, offset + count). */
static __always_inline void set_chunks(struct netmon_nfs_write_key *key,
				       const struct nfs_writer *w,
				       long long offset,
				       unsigned long long count)
{
	if (!count)
		return;
	__u64 first = netmon_nfs_chunk(offset);
	__u64 last = netmon_nfs_chunk(offset + (long long)(count - 1));
	for (int i = 0; i < NFS_WRITE_CHUNKS; i++) {
		if (first + i > last)
			break;
		key->chunk = first + i;
		set_writer(key, w);
	}
}

SEC("tracepoint/nfs/nfs_file_write")
int nfs_file_write_start(const struct netmon_nfs_file_write *r)
{
	if (!r->count)
		return 0;
	struct nfs_writer w = {};
	current_writer(&w);
	struct netmon_nfs_write_key key = {};
	key.fileid = r->fileid;
	key.dev = r->dev;
	key.fhandle = r->fhandle;
	key.chunk = NETMON_NFS_WHOLE_FILE;
	set_writer(&key, &w);
	/* Buffered: nfs_writepage_setup places each page. */
	if ((r->flags & IOCB_DIRECT) && !(r->flags & IOCB_APPEND))
		set_chunks(&key, &w, r->offset, r->count);
	return 0;
}

SEC("tracepoint/nfs/nfs_writepage_setup")
int nfs_writepage_setup(const struct netmon_nfs_writepage_setup *r)
{
	struct nfs_writer w = {};
	current_writer(&w);
	struct netmon_nfs_write_key key = {};
	key.fileid = r->fileid;
	key.dev = r->dev;
	key.fhandle = r->fhandle;
	set_chunks(&key, &w, r->offset, r->count);
	return 0;
}

/* Counts the bytes of a WRITE reply no recorded writer is waiting for. */
static __always_inline void unmatched_write(__u32 bytes, int first)
{
	__u32 zero = 0;
	struct netmon_nfs_unmatched *u =
		bpf_map_lookup_elem(&nfs_unmatched, &zero);
	if (u) { /* plain adds, as in unmatched() */
		u->write_replies += first;
		u->write_bytes += bytes;
	}
}

/*
 * A WRITE covers at most wsize bytes (1 MiB at most on Linux), so two
 * MiBs; up to four parts allows for larger, the last taking the rest.
 */
#define NFS_REPLY_PARTS 4

SEC("tracepoint/nfs/nfs_writeback_done")
int nfs_writeback_done(const struct netmon_nfs_writeback_done *r)
{
	struct netmon_nfs_write_key key = {};
	key.fileid = r->fileid;
	key.dev = r->dev;
	key.fhandle = r->fhandle;
	long long offset = r->offset;
	__u32 left = r->res_count; /* what mountstats counts as written */
	int first_unmatched = 1;
	for (int i = 0; i < NFS_REPLY_PARTS && left; i++) {
		__u32 part = i == NFS_REPLY_PARTS - 1
				     ? left
				     : netmon_nfs_chunk_bytes(offset, left);
		key.chunk = netmon_nfs_chunk(offset);
		struct nfs_writer *found =
			bpf_map_lookup_elem(&nfs_writers, &key);
		if (!found) {
			key.chunk = NETMON_NFS_WHOLE_FILE;
			found = bpf_map_lookup_elem(&nfs_writers, &key);
		}
		if (found) {
			struct nfs_writer w = *found;
			add_bytes(w.tgid, w.uid, w.comm, NETMON_NFS_SENT, part,
				  0);
		} else {
			unmatched_write(part, first_unmatched);
			first_unmatched = 0;
		}
		offset += part;
		left -= part;
	}
	return 0;
}
