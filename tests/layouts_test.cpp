// Checks the record layouts netmon.bpf.c reads against the format files of a
// Raspberry Pi kernel (6.18.60+rpt-rpi-v8), which tracepoints netmon needs,
// when it attaches an optional one, when an NFS READ is retried, and how a
// WRITE's reply splits into chunks.
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
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

// events/nfs/nfs_file_write/format
const char *kFileWrite =
	"\tfield:dev_t dev;\toffset:8;\tsize:4;\tsigned:0;\n"
	"\tfield:u32 fhandle;\toffset:12;\tsize:4;\tsigned:0;\n"
	"\tfield:u64 fileid;\toffset:16;\tsize:8;\tsigned:0;\n"
	"\tfield:u64 version;\toffset:24;\tsize:8;\tsigned:0;\n"
	"\tfield:loff_t offset;\toffset:32;\tsize:8;\tsigned:1;\n"
	"\tfield:size_t count;\toffset:40;\tsize:8;\tsigned:0;\n"
	"\tfield:int flags;\toffset:48;\tsize:4;\tsigned:1;\n";

// events/nfs/nfs_writepage_setup/format
const char *kWritepageSetup =
	"\tfield:dev_t dev;\toffset:8;\tsize:4;\tsigned:0;\n"
	"\tfield:u32 fhandle;\toffset:12;\tsize:4;\tsigned:0;\n"
	"\tfield:u64 fileid;\toffset:16;\tsize:8;\tsigned:0;\n"
	"\tfield:const struct nfs_page * req;\toffset:24;\tsize:8;\tsigned:0;\n"
	"\tfield:loff_t offset;\toffset:32;\tsize:8;\tsigned:1;\n"
	"\tfield:unsigned int count;\toffset:40;\tsize:4;\tsigned:0;\n"
	"\tfield:unsigned long flags;\toffset:48;\tsize:8;\tsigned:0;\n";

// events/nfs/nfs_direct_write_schedule_iovec/format
const char *kDirectWrite =
	"\tfield:dev_t dev;\toffset:8;\tsize:4;\tsigned:0;\n"
	"\tfield:u64 fileid;\toffset:16;\tsize:8;\tsigned:0;\n"
	"\tfield:u32 fhandle;\toffset:24;\tsize:4;\tsigned:0;\n"
	"\tfield:loff_t offset;\toffset:32;\tsize:8;\tsigned:1;\n"
	"\tfield:ssize_t count;\toffset:40;\tsize:8;\tsigned:1;\n"
	"\tfield:ssize_t error;\toffset:48;\tsize:8;\tsigned:1;\n"
	"\tfield:int flags;\toffset:56;\tsize:4;\tsigned:1;\n";

// events/nfs/nfs_writeback_done/format
const char *kWritebackDone =
	"\tfield:dev_t dev;\toffset:8;\tsize:4;\tsigned:0;\n"
	"\tfield:u32 fhandle;\toffset:12;\tsize:4;\tsigned:0;\n"
	"\tfield:u64 fileid;\toffset:16;\tsize:8;\tsigned:0;\n"
	"\tfield:loff_t offset;\toffset:24;\tsize:8;\tsigned:1;\n"
	"\tfield:u32 arg_count;\toffset:32;\tsize:4;\tsigned:0;\n"
	"\tfield:u32 res_count;\toffset:36;\tsize:4;\tsigned:0;\n"
	"\tfield:int error;\toffset:40;\tsize:4;\tsigned:1;\n"
	"\tfield:unsigned long stable;\toffset:48;\tsize:8;\tsigned:0;\n"
	"\tfield:char verifier[8];\toffset:56;\tsize:8;\tsigned:0;\n";

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

	// fhandle is part of the key netmon.bpf.c builds from both records.
	for (const char *fields : {kInitiateRead, kReadpageDone}) {
		std::string moved(fields);
		moved.replace(moved.find("offset:12"), 9, "offset:44");
		const auto &expected = fields == kInitiateRead
					       ? kNfsInitiateRead
					       : kNfsReadpageDone;
		check(problems(moved.c_str(), expected)
				      .find("fhandle at offset 44") !=
			      std::string::npos,
		      fields == kInitiateRead
			      ? "a moved fhandle is reported (initiate_read)"
			      : "a moved fhandle is reported (readpage_done)");
	}

	check(problems(kFileWrite, kNfsFileWrite).empty(),
	      "nfs_file_write matches the struct netmon.bpf.c reads");
	check(problems(kDirectWrite, kNfsDirectWrite).empty(),
	      "nfs_direct_write_schedule_iovec matches the struct netmon.bpf.c "
	      "reads");
	check(problems(kWritepageSetup, kNfsWritepageSetup).empty(),
	      "nfs_writepage_setup matches the struct netmon.bpf.c reads");
	check(problems(kWritebackDone, kNfsWritebackDone).empty(),
	      "nfs_writeback_done matches the struct netmon.bpf.c reads");
	std::string narrow(kFileWrite);  // a 32-bit size_t
	narrow.replace(narrow.find("size_t count;\toffset:40;\tsize:8"), 31,
		       "size_t count;\toffset:40;\tsize:4");
	check(problems(narrow.c_str(), kNfsFileWrite).find("count") !=
		      std::string::npos,
	      "a 4-byte count in nfs_file_write is reported");

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
	check(!layout_of("tracepoint/nfs/nfs_commit_done").fields,
	      "a tracepoint netmon has no layout for has none");
	check(init.group == "nfs_fetched" && done.group == init.group &&
		      send.group.empty(),
	      "the NFS tracepoints are refused together");
	TracepointLayout write = layout_of("tracepoint/nfs/nfs_file_write");
	TracepointLayout wb = layout_of("tracepoint/nfs/nfs_writeback_done");
	TracepointLayout page = layout_of("tracepoint/nfs/nfs_writepage_setup");
	TracepointLayout direct =
		layout_of("tracepoint/nfs/nfs_direct_write_schedule_iovec");
	check(write.fields == &kNfsFileWrite &&
		      wb.fields == &kNfsWritebackDone &&
		      page.fields == &kNfsWritepageSetup &&
		      direct.fields == &kNfsDirectWrite && !write.required &&
		      !wb.required && !page.required && !direct.required,
	      "the NFS write tracepoints are optional");
	check(write.group == "nfs_sent" && wb.group == write.group &&
		      page.group == write.group && direct.group == write.group,
	      "the NFS write tracepoints are refused together");
	check(wb.needs == "nfs_file_write" && page.needs == "nfs_file_write" &&
		      direct.needs == "nfs_file_write" && write.needs.empty(),
	      "the others wait for nfs_file_write");
}

void test_check_optional()
{
	char tmpl[] = "/tmp/layouts_test.XXXXXX";
	const char *made = mkdtemp(tmpl);
	if (!made) {
		check(false, "mkdtemp");
		return;
	}
	std::filesystem::path root(made);
	TracepointLayout done = layout_of("tracepoint/nfs/nfs_readpage_done");
	std::string problem;
	check(check_optional(root, done, problem) == Readiness::kMissing &&
		      problem.empty(),
	      "a tracepoint whose module hasn't loaded is missing");

	std::filesystem::path dir = root / "events/nfs/nfs_readpage_done";
	std::filesystem::create_directories(dir);
	std::ofstream(dir / "format") << kCommon << kReadpageDone;
	check(check_optional(root, done, problem) == Readiness::kReady,
	      "one with the layout netmon reads is ready");

	std::string moved(kReadpageDone);
	moved.replace(moved.find("offset:36"), 9, "offset:40");
	std::ofstream(dir / "format") << kCommon << moved;
	check(check_optional(root, done, problem) == Readiness::kRefused &&
		      problem.find("res_count at offset 40") !=
			      std::string::npos,
	      "one with another layout is refused, saying why");

	TracepointLayout unknown =
		layout_of("tracepoint/nfs/nfs_readpage_done");
	unknown.fields = nullptr;
	check(check_optional(root, unknown, problem) == Readiness::kRefused,
	      "one whose layout netmon doesn't know is refused");

	// An older kernel: nfs_writeback_done, but no nfs_file_write.
	TracepointLayout wb = layout_of("tracepoint/nfs/nfs_writeback_done");
	std::filesystem::path wb_dir = root / "events/nfs/nfs_writeback_done";
	std::filesystem::create_directories(wb_dir);
	std::ofstream(wb_dir / "format") << kCommon << kWritebackDone;
	check(check_optional(root, wb, problem) == Readiness::kMissing &&
		      problem.find("nfs:nfs_file_write") != std::string::npos,
	      "one whose needed tracepoint is missing waits, saying which");
	std::filesystem::path fw_dir = root / "events/nfs/nfs_file_write";
	std::filesystem::create_directories(fw_dir);
	std::ofstream(fw_dir / "format") << kCommon << kFileWrite;
	check(check_optional(root, wb, problem) == Readiness::kReady,
	      "and is ready once it appears");
	std::filesystem::remove_all(root);
}

netmon_nfs_readpage_done reply(unsigned asked, unsigned sent, bool eof,
			       int error)
{
	netmon_nfs_readpage_done r{};
	r.dev = 7;
	r.fhandle = 0xabcd;
	r.fileid = 42;
	r.offset = 1 << 20;
	r.arg_count = asked;
	r.res_count = sent;
	r.eof = eof;
	r.error = error;
	return r;
}

void test_read_retry()
{
	netmon_nfs_read_key next{};
	netmon_nfs_readpage_done shrt = reply(131072, 4096, false, 0);
	check(netmon_nfs_read_retry(&shrt, &next) &&
		      next.offset == (1 << 20) + 4096 && next.fileid == 42 &&
		      next.dev == 7 && next.fhandle == 0xabcd,
	      "a short read is retried from where it stopped");
	netmon_nfs_readpage_done whole = reply(131072, 131072, false, 0);
	netmon_nfs_readpage_done end = reply(131072, 4096, true, 0);
	netmon_nfs_readpage_done none = reply(131072, 0, false, 0);
	netmon_nfs_readpage_done failed = reply(131072, 4096, false, -5);
	check(!netmon_nfs_read_retry(&whole, &next) &&
		      !netmon_nfs_read_retry(&end, &next) &&
		      !netmon_nfs_read_retry(&none, &next) &&
		      !netmon_nfs_read_retry(&failed, &next),
	      "a whole read, the end of the file, no progress or an error "
	      "isn't");
}

void test_chunks()
{
	const long long mib = 1LL << 20;
	check(netmon_nfs_chunk(0) == 0 && netmon_nfs_chunk(mib - 1) == 0 &&
		      netmon_nfs_chunk(mib) == 1 &&
		      netmon_nfs_chunk(5 * mib + 7) == 5 &&
		      netmon_nfs_chunk(-1) == 0,
	      "a position's chunk is its MiB");
	check(netmon_nfs_chunk_bytes(0, 131072) == 131072 &&
		      netmon_nfs_chunk_bytes(mib - 131072, 131072) == 131072,
	      "a WRITE inside one chunk stays whole");
	check(netmon_nfs_chunk_bytes(mib - 4096, 131072) == 4096 &&
		      netmon_nfs_chunk_bytes(mib, 131072) == 131072,
	      "a WRITE across a chunk's end splits there");
	check(netmon_nfs_chunk_bytes(3 * mib, 4u << 20) == mib,
	      "a WRITE longer than a chunk takes a chunk at a time");
}

}  // namespace

int main()
{
	test_nfs_layouts();
	test_layout_of();
	test_check_optional();
	test_read_retry();
	test_chunks();
	if (failures)
		std::printf("%d failed\n", failures);
	return failures ? 1 : 0;
}
