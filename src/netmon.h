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
	NETMON_NFS_FETCHED, /* file bytes the NFS server sent; see netmon.bpf.c */
	NETMON_NFS_SENT, /* file bytes the NFS server took; see netmon.bpf.c */
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

/*
 * The records of the nfs:nfs_initiate_read and nfs:nfs_readpage_done
 * tracepoints, as their format files describe them. Like the sock ones,
 * netmon.cpp checks these offsets against tracefs before attaching.
 */
struct netmon_nfs_initiate_read {
	unsigned long long common;
	unsigned int dev;
	unsigned int fhandle;
	unsigned long long fileid;
	long long offset;
	unsigned int count; /* bytes asked for */
};

struct netmon_nfs_readpage_done {
	unsigned long long common;
	unsigned int dev;
	unsigned int fhandle;
	unsigned long long fileid;
	long long offset;
	unsigned int arg_count; /* bytes asked for */
	unsigned int res_count; /* bytes the server sent */
	unsigned char eof;
	int error; /* the RPC's status: 0, or -errno */
};

/*
 * What both NFS records name a READ by: the key of netmon.bpf.c's map of
 * READs in flight. fhandle is a hash of the file handle, which tells apart
 * files that share a fileid.
 */
struct netmon_nfs_read_key {
	unsigned long long fileid;
	long long offset;
	unsigned int dev;
	unsigned int fhandle;
};

/*
 * Whether a reply leaves the rest of its READ to a retry, which
 * nfs_readpage_retry() sends without a new nfs_initiate_read: a short read,
 * without an error or the end of the file, sent again from where it
 * stopped. If so, sets *next to the retry's key.
 */
static inline int
netmon_nfs_read_retry(const struct netmon_nfs_readpage_done *r,
		      struct netmon_nfs_read_key *next)
{
	if (r->error < 0 || r->eof || !r->res_count ||
	    r->res_count >= r->arg_count)
		return 0;
	next->fileid = r->fileid;
	next->offset = r->offset + r->res_count;
	next->dev = r->dev;
	next->fhandle = r->fhandle;
	return 1;
}

/*
 * The records of nfs:nfs_file_write, at the start of each write() to an NFS
 * file (kernels after 6.15), nfs:nfs_update_folio, as a write() or a store through
 * a mapping dirties a page, and nfs:nfs_writeback_done, as the server
 * answers a WRITE. Checked against tracefs like the others.
 */
struct netmon_nfs_file_write {
	unsigned long long common;
	unsigned int dev;
	unsigned int fhandle;
	unsigned long long fileid;
	unsigned long long version;
	long long offset; /* the file position */
	unsigned long long count; /* bytes to write */
	int flags; /* IOCB_* */
};

struct netmon_nfs_update_folio {
	unsigned long long common;
	unsigned int dev;
	unsigned int fhandle;
	unsigned long long fileid;
	unsigned long long version;
	long long offset; /* in the page, not the file */
	unsigned long long count;
};

struct netmon_nfs_writeback_done {
	unsigned long long common;
	unsigned int dev;
	unsigned int fhandle;
	unsigned long long fileid;
	long long offset;
	unsigned int arg_count; /* bytes sent */
	unsigned int res_count; /* bytes the server took */
	int error;
	unsigned long long stable;
	char verifier[8];
};

/*
 * The key of netmon.bpf.c's map of NFS writers: a file's chunk of
 * 1 << NETMON_NFS_CHUNK_SHIFT bytes, chunk = offset >> NETMON_NFS_CHUNK_SHIFT,
 * or with chunk NETMON_NFS_WHOLE_FILE, the file.
 */
#define NETMON_NFS_CHUNK_SHIFT 20
#define NETMON_NFS_WHOLE_FILE (~0ULL)

struct netmon_nfs_write_key {
	unsigned long long fileid;
	unsigned long long chunk;
	unsigned int dev;
	unsigned int fhandle;
};

/* The chunk holding file position offset. */
static inline unsigned long long netmon_nfs_chunk(long long offset)
{
	return offset < 0
		       ? 0
		       : (unsigned long long)offset >> NETMON_NFS_CHUNK_SHIFT;
}

/*
 * How many of bytes, starting at file position offset, fall in offset's
 * chunk: a WRITE's bytes past that belong to the next chunk.
 */
static inline unsigned int netmon_nfs_chunk_bytes(long long offset,
						  unsigned int bytes)
{
	unsigned long long next = (netmon_nfs_chunk(offset) + 1)
				  << NETMON_NFS_CHUNK_SHIFT;
	unsigned long long left =
		next - (unsigned long long)(offset < 0 ? 0 : offset);
	return bytes < left ? bytes : (unsigned int)left;
}

/*
 * NFS replies netmon.bpf.c found no process for, and their bytes, per CPU:
 * READ replies with no recorded reader, WRITE replies with no recorded
 * writer.
 */
struct netmon_nfs_unmatched {
	unsigned long long replies;
	unsigned long long bytes;
	unsigned long long write_replies;
	unsigned long long write_bytes;
};
