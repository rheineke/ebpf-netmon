// Checks the tracepoint format parsing that guards netmon.bpf.c's fixed
// offsets into the sock tracepoints' records.
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "netmon.h"
#include "tracefs.h"

namespace {

int failures = 0;

void check(bool ok, const char *what)
{
	std::printf("%s: %s\n", ok ? "ok  " : "FAIL", what);
	if (!ok)
		++failures;
}

// /sys/kernel/tracing/events/sock/sock_recv_length/format on 6.18.
const char *kFormat =
	"name: sock_recv_length\n"
	"ID: 1838\n"
	"format:\n"
	"\tfield:unsigned short common_type;\toffset:0;\tsize:2;\tsigned:0;\n"
	"\tfield:unsigned char common_flags;\toffset:2;\tsize:1;\tsigned:0;\n"
	"\tfield:unsigned char common_preempt_count;\toffset:3;\tsize:1;\tsigned:0;\n"
	"\tfield:int common_pid;\toffset:4;\tsize:4;\tsigned:1;\n"
	"\n"
	"\tfield:void * sk;\toffset:8;\tsize:8;\tsigned:0;\n"
	"\tfield:__u16 family;\toffset:16;\tsize:2;\tsigned:0;\n"
	"\tfield:__u16 protocol;\toffset:18;\tsize:2;\tsigned:0;\n"
	"\tfield:int ret;\toffset:20;\tsize:4;\tsigned:1;\n"
	"\tfield:int flags;\toffset:24;\tsize:4;\tsigned:1;\n"
	"\n"
	"print fmt: \"sk address = %p, family = %s protocol = %s\", REC->sk\n";

// What netmon.cpp expects, from the struct the BPF program reads.
const std::vector<TracepointField> kExpected = {
	{"family", offsetof(netmon_sock_msg_length, family), 2},
	{"protocol", offsetof(netmon_sock_msg_length, protocol), 2},
	{"ret", offsetof(netmon_sock_msg_length, ret), 4},
	{"flags", offsetof(netmon_sock_msg_length, flags), 4},
};

void test_parse_line()
{
	TracepointField f;
	check(parse_format_line("\tfield:void * sk;\toffset:8;\tsize:8;", f) &&
		      f.name == "sk" && f.offset == 8 && f.size == 8,
	      "parses a pointer field");
	check(parse_format_line("\tfield:char comm[16];\toffset:8;\tsize:16;",
				f) &&
		      f.name == "comm" && f.size == 16,
	      "parses an array field");
	check(parse_format_line(
		      "\tfield:__data_loc char[] name;\toffset:12;\tsize:4;",
		      f) &&
		      f.name == "name",
	      "parses a __data_loc field");
	check(!parse_format_line("ID: 1838", f), "skips other lines");
	check(!parse_format_line("\tfield:int x;\toffset:;\tsize:4;", f),
	      "rejects a field without an offset");
}

void test_check_format()
{
	std::istringstream good(kFormat);
	check(check_format(good, kExpected).empty(),
	      "the kernel's layout matches the struct netmon.bpf.c reads");

	std::string moved(kFormat);
	moved.replace(moved.find("offset:20"), 9, "offset:28");
	std::istringstream bad(moved);
	std::string problem = check_format(bad, kExpected);
	check(problem.find("ret at offset 28") != std::string::npos,
	      "a moved field is reported");

	std::string gone(kFormat);
	gone.erase(gone.find("\tfield:int flags;"));
	std::istringstream missing(gone);
	check(check_format(missing, kExpected).find("no field flags") !=
		      std::string::npos,
	      "a missing field is reported");
}

void test_check_tracepoint()
{
	char tmpl[] = "/tmp/tracefs_test.XXXXXX";
	const char *made = mkdtemp(tmpl);
	if (!made) {
		check(false, "mkdtemp");
		return;
	}
	std::filesystem::path root(made);
	std::filesystem::create_directories(root /
					    "events/sock/sock_recv_length");
	std::ofstream(root / "events/sock/sock_recv_length/format") << kFormat;

	check(check_tracepoint(root, "sock", "sock_recv_length", kExpected)
		      .empty(),
	      "reads the format file under the tracefs root");
	check(check_tracepoint("", "sock", "sock_recv_length", kExpected)
			      .find("not mounted") != std::string::npos,
	      "says when tracefs isn't mounted");
	check(check_tracepoint(root, "sock", "sock_send_length", kExpected)
			      .find("needs Linux 6.3") != std::string::npos,
	      "says when the kernel lacks the tracepoint");
	std::filesystem::remove_all(root);
}

}  // namespace

int main()
{
	test_parse_line();
	test_check_format();
	test_check_tracepoint();
	if (failures)
		std::printf("%d failed\n", failures);
	return failures ? 1 : 0;
}
