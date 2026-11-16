// Checks json_escape() and HourlyFiles: hour boundaries, appends, and a
// line cut off by a crash.
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>

#include "capture.h"

namespace {

int failures = 0;

void check(bool ok, const char *what)
{
	std::printf("%s: %s\n", ok ? "ok  " : "FAIL", what);
	if (!ok)
		++failures;
}

std::string slurp(const std::filesystem::path &path)
{
	std::ifstream in(path);
	std::stringstream ss;
	ss << in.rdbuf();
	return ss.str();
}

void test_json_escape()
{
	check(json_escape("Plex Media Serv") == "Plex Media Serv",
	      "plain names pass through");
	check(json_escape("a\"b\\c") == "a\\\"b\\\\c",
	      "quotes and backslashes are escaped");
	check(json_escape(std::string("x\x01\n\x1f", 4)) ==
		      "x\\u0001\\u000a\\u001f",
	      "control bytes become \\u escapes");
	// Bytes over 0x7f are negative chars on x86 and must not be escaped
	// as control bytes there.
	check(json_escape("\xc3\xa9t\xc3\xa9") == "\xc3\xa9t\xc3\xa9",
	      "UTF-8 passes through unchanged");
}

void test_hourly_files(const std::filesystem::path &dir)
{
	// 2026-03-08 06:59:59 and 07:00:00 UTC: one second apart, two files.
	const std::time_t before = 1772953199, after = before + 1;
	HourlyFiles files(dir.string());
	check(files.path_for(before) == dir / "2026-03-08" / "06.jsonl",
	      "path is DIR/YYYY-MM-DD/HH.jsonl in UTC");
	check(files.path_for(after) == dir / "2026-03-08" / "07.jsonl",
	      "a new hour gets a new file");

	std::FILE *f = files.at(before);
	check(f != nullptr, "opens and creates the day folder");
	std::fputs("{\"a\":1}\n", f);
	check(files.at(before) == f, "the same hour reuses the open file");
	std::FILE *g = files.at(after);
	std::fputs("{\"b\":2}\n", g);
	std::fflush(g);
	check(slurp(files.path_for(before)) == "{\"a\":1}\n" &&
		      slurp(files.path_for(after)) == "{\"b\":2}\n",
	      "each row lands in its own hour's file");
}

void test_appends_after_cut_line(const std::filesystem::path &dir)
{
	const std::time_t ts = 1772953200;
	std::filesystem::path path;
	{
		HourlyFiles files(dir.string());
		path = files.path_for(ts);
		std::filesystem::create_directories(path.parent_path());
		std::ofstream(path) << "{\"whole\":1}\n{\"cut\":";
		std::FILE *f = files.at(ts);
		std::fputs("{\"next\":2}\n", f);
	}
	check(slurp(path) == "{\"whole\":1}\n{\"cut\":\n{\"next\":2}\n",
	      "a cut-off last line is ended before the next row");
	{
		HourlyFiles files(dir.string());
		std::fputs("{\"more\":3}\n", files.at(ts));
	}
	check(slurp(path) ==
		      "{\"whole\":1}\n{\"cut\":\n{\"next\":2}\n{\"more\":3}\n",
	      "a whole last line gets no blank line after it");
}

}  // namespace

int main()
{
	char tmpl[] = "/tmp/capture_test.XXXXXX";
	const char *made = mkdtemp(tmpl);
	if (!made) {
		std::perror("mkdtemp");
		return 1;
	}
	std::filesystem::path dir(made);
	test_json_escape();
	test_hourly_files(dir / "a");
	test_appends_after_cut_line(dir / "b");
	std::filesystem::remove_all(dir);
	if (failures)
		std::printf("%d failed\n", failures);
	return failures ? 1 : 0;
}
