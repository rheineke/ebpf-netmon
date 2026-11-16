// SPDX-License-Identifier: (MIT OR GPL-2.0-only)
/*
 * Per-process TCP/UDP byte counts, and NFS file bytes read and written.
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
#define MSG_PEEK 0x2
#define MSG_ERRQUEUE 0x2000

/* Keyed by tgid. LRU so exited processes age out without user-space cleanup. */
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 16384);
	__type(key, __u32);
	__type(value, struct netmon_traffic);
} traffic SEC(".maps");

static __always_inline int account(enum netmon_counter counter, long bytes)
{
	if (bytes <= 0)
		return 0;

	__u32 tgid = bpf_get_current_pid_tgid() >> 32;
	__u32 uid = (__u32)bpf_get_current_uid_gid();
	struct netmon_traffic *t = bpf_map_lookup_elem(&traffic, &tgid);
	if (!t) {
		struct netmon_traffic init = {};
		init.uid = uid;
		bpf_get_current_comm(init.comm, sizeof(init.comm));
		bpf_map_update_elem(&traffic, &tgid, &init, BPF_NOEXIST);
		t = bpf_map_lookup_elem(&traffic, &tgid);
		if (!t)
			return 0;
	} else {
		/*
		 * Keep the owner current: the process may have dropped
		 * privileges, or be a new one that reused an exited one's pid.
		 */
		t->uid = uid;
		bpf_get_current_comm(t->comm, sizeof(t->comm));
	}
	__sync_fetch_and_add(&t->bytes[counter], bytes);
	return 0;
}

static __always_inline int account_sock(const struct netmon_sock_msg_length *m,
					enum netmon_counter tcp,
					enum netmon_counter udp)
{
	if (m->family != AF_INET && m->family != AF_INET6)
		return 0;
	if (m->protocol == IPPROTO_TCP)
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
 * NFS reads and writes go over the wire from kernel threads (nfsiod,
 * rpciod), so the socket counts above cannot see which process asked for
 * them. These run in the calling process and return the file bytes it read
 * or wrote, including reads served from the page cache.
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
