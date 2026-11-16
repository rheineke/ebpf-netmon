/* Shared between the BPF program and user space. */
#pragma once

#define NETMON_COMM_LEN 16

enum netmon_counter {
	NETMON_TCP_TX,
	NETMON_TCP_RX,
	NETMON_UDP_TX,
	NETMON_UDP_RX,
	NETMON_NFS_READ, /* file bytes, not wire bytes; see netmon.bpf.c */
	NETMON_NFS_WRITE,
	NETMON_NR_COUNTERS,
};

struct netmon_traffic {
	unsigned long long bytes[NETMON_NR_COUNTERS];
	unsigned int uid;
	char comm[NETMON_COMM_LEN];
};

/*
 * The record of the sock:sock_send_length and sock:sock_recv_length
 * tracepoints (kernel 6.3+), laid out as their format files describe it.
 * No BTF describes it on a Raspberry Pi, so netmon.cpp checks these offsets
 * against tracefs before attaching.
 */
struct netmon_sock_msg_length {
	unsigned long long common; /* type, flags, preempt count, pid */
	unsigned long long sk;
	unsigned short family;
	unsigned short protocol;
	int ret; /* bytes sent or received, or -errno */
	int flags; /* MSG_* flags; 0 for sends */
};
