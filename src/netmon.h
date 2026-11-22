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
 * file, nfs:nfs_writepage_setup, as a write() or a store through a mapping
 * dirties a page (both since kernel 6.18), and nfs:nfs_writeback_done, as
 * the server answers a WRITE. Checked against tracefs like the others.
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

/* nfs:nfs_direct_write_schedule_iovec, as an O_DIRECT write() starts its
 * WRITEs: fhandle follows fileid here. Its count is the bytes the server
 * has acknowledged so far, 0 at this point, so netmon reads only up to
 * offset. */
struct netmon_nfs_direct_write {
	unsigned long long common;
	unsigned int dev;
	unsigned long long fileid;
	unsigned int fhandle;
	long long offset; /* the file position, after O_APPEND */
};

struct netmon_nfs_writepage_setup {
	unsigned long long common;
	unsigned int dev;
	unsigned int fhandle;
	unsigned long long fileid;
	unsigned long long req; /* the kernel's struct nfs_page * */
	long long offset; /* the file position */
	unsigned int count; /* bytes of the page dirtied */
	unsigned long long flags;
};

struct netmon_nfs_writeback_done {
	unsigned long long common;
	unsigned int dev;
	unsigned int fhandle;
	unsigned long long fileid;
	long long offset;
	unsigned int arg_count; /* bytes sent */
	unsigned int res_count; /* bytes the server took */
	int error; /* the RPC's status: 0, or -errno */
	unsigned long long stable; /* how the server stored them; 0: UNSTABLE */
	char verifier[8];
};

/*
 * nfs:nfs_initiate_write, as the client starts a WRITE, laid out as
 * nfs:nfs_initiate_read: in the thread that asked, for an O_DIRECT write().
 */
struct netmon_nfs_initiate_write {
	unsigned long long common;
	unsigned int dev;
	unsigned int fhandle;
	unsigned long long fileid;
	long long offset;
	unsigned int count; /* bytes to send */
};

/*
 * An O_DIRECT write() of a thread, from nfs:nfs_file_write until its last
 * WRITE starts or the write() returns. Its WRITEs start in order from the
 * position nfs:nfs_direct_write_schedule_iovec gives (after O_APPEND), each
 * where the last ended, before anything else the write() sends: dirty pages
 * of the file the kernel launders afterwards, when the file is already in
 * O_DIRECT mode, lie behind its last WRITE. A write() a file size limit
 * cuts short starts fewer WRITEs than it asked for, and the entry ends when
 * the write() returns.
 */
struct netmon_nfs_direct_pending {
	unsigned long long fileid;
	long long next; /* where its WRITEs so far end; -1 until known */
	long long end; /* the length asked for, then where it ends */
	unsigned int dev;
	unsigned int pad;
};

/* Records where the write() starts, from nfs_direct_write_schedule_iovec. */
static inline void netmon_nfs_direct_start(struct netmon_nfs_direct_pending *p,
					   long long offset)
{
	if (p->next < 0) {
		p->next = offset;
		p->end += offset;
	}
}

/*
 * Whether a WRITE the thread starts is one of its O_DIRECT write()'s: it
 * starts at or past where the last one ended, so a WRITE netmon missed costs
 * only itself, and ends within the length asked for. The file is matched by
 * dev and fileid: pNFS WRITEs name the data server's file handle.
 */
static inline int
netmon_nfs_direct_takes(const struct netmon_nfs_direct_pending *p,
			const struct netmon_nfs_initiate_write *r)
{
	return p->next >= 0 && p->fileid == r->fileid && p->dev == r->dev &&
	       r->offset >= p->next &&
	       r->offset + (long long)r->count <= p->end;
}

/*
 * What nfs_initiate_write and nfs_writeback_done both name a WRITE by: the
 * key of netmon.bpf.c's map of O_DIRECT WRITEs in flight. The count tells a
 * page of the file another process dirtied, sent at the same offset from the
 * same thread, from the O_DIRECT write's own WRITEs.
 */
struct netmon_nfs_direct_key {
	unsigned long long fileid;
	long long offset;
	unsigned int count;
	unsigned int dev;
	unsigned int fhandle;
	unsigned int pad;
};

/*
 * Whether a WRITE's reply leaves the rest to a resend, which
 * nfs_writeback_result() sends without a new nfs_initiate_write: a short
 * write without an error. Unless the server stored it UNSTABLE, the resend
 * goes from where it stopped; if UNSTABLE, all of it again, stably. Returns
 * 1 and sets *next to the resend's key, else 0.
 */
static inline int
netmon_nfs_write_resend(const struct netmon_nfs_writeback_done *r,
			struct netmon_nfs_direct_key *next)
{
	if (r->error < 0 || !r->res_count || r->res_count >= r->arg_count)
		return 0;
	next->fileid = r->fileid;
	next->dev = r->dev;
	next->fhandle = r->fhandle;
	next->pad = 0;
	if (r->stable) {
		next->offset = r->offset + r->res_count;
		next->count = r->arg_count - r->res_count;
	} else {
		next->offset = r->offset;
		next->count = r->arg_count;
	}
	return 1;
}

/*
 * The key of netmon.bpf.c's map of NFS writers: a file's chunk of
 * 1 << NETMON_NFS_CHUNK_SHIFT bytes, chunk = offset >> NETMON_NFS_CHUNK_SHIFT,
 * or with chunk NETMON_NFS_WHOLE_FILE, the file's last writer.
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
