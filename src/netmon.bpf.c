// SPDX-License-Identifier: (MIT OR GPL-2.0-only)
/*
 * Per-process TCP/UDP byte counts, NFS file bytes read and written, and the
 * NFS file bytes fetched from the server for each process's reads.
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
 * Adds bytes to process tgid's counter, naming it uid and comm: the owner as
 * of now, as the process may have dropped privileges, or be a new one that
 * reused an exited one's pid.
 */
static __always_inline int add_bytes(__u32 tgid, __u32 uid,
				     const char comm[NETMON_COMM_LEN],
				     enum netmon_counter counter, long bytes)
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
	} else {
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
			 (__u32)bpf_get_current_uid_gid(), comm, counter,
			 bytes);
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
struct nfs_read_key {
	__u64 fileid;
	__s64 offset;
	__u32 dev;
	__u32 pad; /* zeroed, as hash keys compare every byte */
};

struct nfs_reader {
	__u32 tgid;
	__u32 uid;
	char comm[NETMON_COMM_LEN];
};

/*
 * READs in flight. LRU, so one whose reply never comes (the mount was forced
 * off) ages out. Two processes reading the same bytes of a file at once
 * (O_DIRECT; buffered reads share the page cache) share one entry, and the
 * later one is charged.
 */
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 4096);
	__type(key, struct nfs_read_key);
	__type(value, struct nfs_reader);
} nfs_reads SEC(".maps");

SEC("tracepoint/nfs/nfs_initiate_read")
int nfs_initiate_read(const struct netmon_nfs_initiate_read *r)
{
	struct nfs_read_key key = {};
	key.fileid = r->fileid;
	key.offset = r->offset;
	key.dev = r->dev;
	struct nfs_reader reader = {};
	reader.tgid = bpf_get_current_pid_tgid() >> 32;
	reader.uid = (__u32)bpf_get_current_uid_gid();
	bpf_get_current_comm(reader.comm, sizeof(reader.comm));
	bpf_map_update_elem(&nfs_reads, &key, &reader, BPF_ANY);
	return 0;
}

SEC("tracepoint/nfs/nfs_readpage_done")
int nfs_readpage_done(const struct netmon_nfs_readpage_done *r)
{
	struct nfs_read_key key = {};
	key.fileid = r->fileid;
	key.offset = r->offset;
	key.dev = r->dev;
	struct nfs_reader *found = bpf_map_lookup_elem(&nfs_reads, &key);
	if (!found)
		return 0;
	struct nfs_reader reader = *found;
	bpf_map_delete_elem(&nfs_reads, &key);
	if (r->res_count)
		add_bytes(reader.tgid, reader.uid, reader.comm,
			  NETMON_NFS_FETCHED, r->res_count);
	/*
	 * A short read without an error or the end of the file is sent again
	 * for the rest, from where this one stopped, without a new
	 * nfs_initiate_read (nfs_readpage_retry).
	 */
	if (r->error >= 0 && !r->eof && r->res_count &&
	    r->res_count < r->arg_count) {
		key.offset += r->res_count;
		bpf_map_update_elem(&nfs_reads, &key, &reader, BPF_ANY);
	}
	return 0;
}
