// SPDX-License-Identifier: (MIT OR GPL-2.0-only)
/*
 * Per-process TCP/UDP byte counts.
 *
 * Every probe reads only integer arguments and return values, never kernel
 * structs, so this needs no CO-RE and no kernel BTF. Raspberry Pi kernels
 * ship without /sys/kernel/btf/vmlinux.
 */
#include <linux/bpf.h>
#include <linux/ptrace.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "netmon.h"

char LICENSE[] SEC("license") = "Dual MIT/GPL";

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
	struct netmon_traffic *t = bpf_map_lookup_elem(&traffic, &tgid);
	if (!t) {
		struct netmon_traffic init = {};
		init.uid = (__u32)bpf_get_current_uid_gid();
		bpf_get_current_comm(init.comm, sizeof(init.comm));
		bpf_map_update_elem(&traffic, &tgid, &init, BPF_NOEXIST);
		t = bpf_map_lookup_elem(&traffic, &tgid);
		if (!t)
			return 0;
	}
	__sync_fetch_and_add(&t->bytes[counter], bytes);
	return 0;
}

/* Return value is the number of bytes copied from user space. */
SEC("kretprobe/tcp_sendmsg")
int BPF_KRETPROBE(tcp_sendmsg_ret, int ret)
{
	return account(NETMON_TCP_TX, ret);
}

/* Called with the number of bytes the reader consumed, including splice(). */
SEC("kprobe/tcp_cleanup_rbuf")
int BPF_KPROBE(tcp_cleanup_rbuf, void *sk, int copied)
{
	return account(NETMON_TCP_RX, copied);
}

SEC("kretprobe/udp_sendmsg")
int BPF_KRETPROBE(udp_sendmsg_ret, int ret)
{
	return account(NETMON_UDP_TX, ret);
}

SEC("kretprobe/udpv6_sendmsg")
int BPF_KRETPROBE(udpv6_sendmsg_ret, int ret)
{
	return account(NETMON_UDP_TX, ret);
}

SEC("kretprobe/udp_recvmsg")
int BPF_KRETPROBE(udp_recvmsg_ret, int ret)
{
	return account(NETMON_UDP_RX, ret);
}

SEC("kretprobe/udpv6_recvmsg")
int BPF_KRETPROBE(udpv6_recvmsg_ret, int ret)
{
	return account(NETMON_UDP_RX, ret);
}
