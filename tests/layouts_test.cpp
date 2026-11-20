// Checks the record layouts netmon.bpf.c reads against the format files of a
// Raspberry Pi kernel (6.18.60+rpt-rpi-v8), and which tracepoints netmon
// needs.
#include <cstdio>
#include <sstream>
#include <string>

#include "layouts.h"

namespace {

int failures = 0;

void check(bool ok, const char *what)
{
	std::printf("%s: %s\n", ok ? "ok  " : "FAIL", what);
	if (!ok)
		++failures;
}

const char *kCommon =
	"format:\n"
	"\tfield:unsigned short common_type;\toffset:0;\tsize:2;\tsigned:0;\n"
	"\tfield:unsigned char common_flags;\toffset:2;\tsize:1;\tsigned:0;\n"
	"\tfield:unsigned char common_preempt_count;\toffset:3;\tsize:1;\tsigned:0;\n"
	"\tfield:int common_pid;\toffset:4;\tsize:4;\tsigned:1;\n"
	"\n";

// events/nfs/nfs_initiate_read/format
const char *kInitiateRead =
	"\tfield:dev_t dev;\toffset:8;\tsize:4;\tsigned:0;\n"
	"\tfield:u32 fhandle;\toffset:12;\tsize:4;\tsigned:0;\n"
	"\tfield:u64 fileid;\toffset:16;\tsize:8;\tsigned:0;\n"
	"\tfield:loff_t offset;\toffset:24;\tsize:8;\tsigned:1;\n"
	"\tfield:u32 count;\toffset:32;\tsize:4;\tsigned:0;\n";

// events/nfs/nfs_readpage_done/format
const char *kReadpageDone =
	"\tfield:dev_t dev;\toffset:8;\tsize:4;\tsigned:0;\n"
	"\tfield:u32 fhandle;\toffset:12;\tsize:4;\tsigned:0;\n"
	"\tfield:u64 fileid;\toffset:16;\tsize:8;\tsigned:0;\n"
	"\tfield:loff_t offset;\toffset:24;\tsize:8;\tsigned:1;\n"
	"\tfield:u32 arg_count;\toffset:32;\tsize:4;\tsigned:0;\n"
	"\tfield:u32 res_count;\toffset:36;\tsize:4;\tsigned:0;\n"
	"\tfield:bool eof;\toffset:40;\tsize:1;\tsigned:0;\n"
	"\tfield:int error;\toffset:44;\tsize:4;\tsigned:1;\n";

std::string problems(const char *fields,
		     const std::vector<TracepointField> &expected)
{
	std::istringstream format(std::string(kCommon) + fields);
	return check_format(format, expected);
}

void test_nfs_layouts()
{
	check(problems(kInitiateRead, kNfsInitiateRead).empty(),
	      "nfs_initiate_read matches the struct netmon.bpf.c reads");
	check(problems(kReadpageDone, kNfsReadpageDone).empty(),
	      "nfs_readpage_done matches the struct netmon.bpf.c reads");

	// An older kernel's nfs_readpage_done, with one count for both.
	std::string old(kReadpageDone);
	old.replace(old.find("arg_count"), 9, "count");
	check(problems(old.c_str(), kNfsReadpageDone)
			      .find("no field arg_count") != std::string::npos,
	      "another layout of nfs_readpage_done is reported");
}

void test_layout_of()
{
	TracepointLayout send = layout_of("tracepoint/sock/sock_send_length");
	check(send.category == "sock" && send.event == "sock_send_length" &&
		      send.fields == &kSockMsgLength && send.required,
	      "the sock tracepoints are required");
	TracepointLayout init = layout_of("tracepoint/nfs/nfs_initiate_read");
	check(init.category == "nfs" && init.event == "nfs_initiate_read" &&
		      init.fields == &kNfsInitiateRead && !init.required,
	      "nfs_initiate_read is optional");
	TracepointLayout done = layout_of("tracepoint/nfs/nfs_readpage_done");
	check(done.fields == &kNfsReadpageDone && !done.required,
	      "nfs_readpage_done is optional");
	check(!layout_of("tracepoint/nfs/nfs_writeback_done").fields,
	      "a tracepoint netmon has no layout for has none");
}

}  // namespace

int main()
{
	test_nfs_layouts();
	test_layout_of();
	if (failures)
		std::printf("%d failed\n", failures);
	return failures ? 1 : 0;
}
