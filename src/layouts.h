// The record layouts netmon.bpf.c reads, for each tracepoint it attaches.
// Without kernel BTF the BPF program reads fields at offsets fixed when it
// was built, so netmon.cpp checks them against tracefs (tracefs.h) first.
#pragma once

#include <cstddef>
#include <string>
#include <vector>

#include "netmon.h"
#include "tracefs.h"

// The fields of the sock tracepoints that netmon.bpf.c reads.
inline const std::vector<TracepointField> kSockMsgLength = {
	{"family", offsetof(netmon_sock_msg_length, family), 2},
	{"protocol", offsetof(netmon_sock_msg_length, protocol), 2},
	{"ret", offsetof(netmon_sock_msg_length, ret), 4},
	{"flags", offsetof(netmon_sock_msg_length, flags), 4},
};

// The fields of the NFS tracepoints that netmon.bpf.c reads.
inline const std::vector<TracepointField> kNfsInitiateRead = {
	{"dev", offsetof(netmon_nfs_initiate_read, dev), 4},
	{"fhandle", offsetof(netmon_nfs_initiate_read, fhandle), 4},
	{"fileid", offsetof(netmon_nfs_initiate_read, fileid), 8},
	{"offset", offsetof(netmon_nfs_initiate_read, offset), 8},
};
inline const std::vector<TracepointField> kNfsReadpageDone = {
	{"dev", offsetof(netmon_nfs_readpage_done, dev), 4},
	{"fhandle", offsetof(netmon_nfs_readpage_done, fhandle), 4},
	{"fileid", offsetof(netmon_nfs_readpage_done, fileid), 8},
	{"offset", offsetof(netmon_nfs_readpage_done, offset), 8},
	{"arg_count", offsetof(netmon_nfs_readpage_done, arg_count), 4},
	{"res_count", offsetof(netmon_nfs_readpage_done, res_count), 4},
	{"eof", offsetof(netmon_nfs_readpage_done, eof), 1},
	{"error", offsetof(netmon_nfs_readpage_done, error), 4},
};

// The layout netmon.bpf.c expects of the tracepoint a program's section
// names ("tracepoint/sock/sock_send_length"), and whether netmon needs it.
struct TracepointLayout {
	std::string category, event;
	const std::vector<TracepointField> *fields = nullptr;
	bool required = false;
	// Tracepoints that count only together, refused together.
	std::string group;
};

inline TracepointLayout layout_of(const std::string &section)
{
	TracepointLayout l;
	auto event = section.rfind('/');
	auto category = section.rfind('/', event - 1);
	l.category = section.substr(category + 1, event - category - 1);
	l.event = section.substr(event + 1);
	if (l.category == "sock") {
		l.fields = &kSockMsgLength;
		l.required = true;
	} else if (l.event == "nfs_initiate_read") {
		l.fields = &kNfsInitiateRead;
		l.group = "nfs_fetched";
	} else if (l.event == "nfs_readpage_done") {
		l.fields = &kNfsReadpageDone;
		l.group = "nfs_fetched";
	}
	return l;
}

enum class Readiness {
	kMissing,  // not there yet: a module's appear when it loads
	kReady,  // there, with the layout netmon.bpf.c reads
	kRefused,  // there, with another layout: never attach it
};

// Whether an optional tracepoint under tracefs root can be attached, and
// if refused, why, in problem. Decided once per try, so a tracepoint that
// appears meanwhile is never attached unchecked.
inline Readiness check_optional(const std::string &root,
				const TracepointLayout &l, std::string &problem)
{
	problem.clear();
	if (!tracepoint_exists(root, l.category, l.event))
		return Readiness::kMissing;
	if (!l.fields) {
		problem = " netmon doesn't know its layout";
		return Readiness::kRefused;
	}
	problem = check_tracepoint(root, l.category, l.event, *l.fields);
	if (problem.empty())
		return Readiness::kReady;
	if (!tracepoint_exists(root, l.category, l.event)) {
		problem.clear();  // gone meanwhile, the module unloading
		return Readiness::kMissing;
	}
	return Readiness::kRefused;
}
