// JSON-lines output: string escaping and the hourly files of netmon -o.
#pragma once

#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <string>

// Escapes a string for a JSON string literal. Any process can set its name
// to any bytes, so quotes, backslashes and control bytes must not get
// through. Other bytes pass as they are; invalid UTF-8 is left to readers.
inline std::string json_escape(const std::string &s)
{
	std::string out;
	for (char ch : s) {
		const auto byte = static_cast<unsigned char>(ch);
		if (ch == '"' || ch == '\\') {
			out += '\\';
			out += ch;
		} else if (byte < 0x20) {
			char buf[8];
			std::snprintf(buf, sizeof(buf), "\\u%04x", byte);
			out += buf;
		} else {
			out += ch;
		}
	}
	return out;
}

// Appends to dir/YYYY-MM-DD/HH.jsonl (or another suffix) for the UTC hour
// of each report, so files are complete once the hour has passed and easy
// to convert in bulk.
class HourlyFiles {
public:
	// suffix ends each file's name, after the hour: .jsonl for the
	// processes' rows, .interfaces.jsonl for the interfaces'.
	explicit HourlyFiles(std::string dir, std::string suffix = ".jsonl")
	    : dir_(std::move(dir)), suffix_(std::move(suffix))
	{
	}
	HourlyFiles(const HourlyFiles &) = delete;
	HourlyFiles &operator=(const HourlyFiles &) = delete;
	~HourlyFiles() { close(); }

	// The file for the hour of ts, or nullptr if it can't be opened.
	std::FILE *at(std::time_t ts)
	{
		std::filesystem::path path = path_for(ts);
		if (file_ && path == path_)
			return file_;
		close();
		std::error_code ec;
		std::filesystem::create_directories(path.parent_path(), ec);
		// a+ rather than a, to read the last byte below.
		file_ = std::fopen(path.c_str(), "a+");
		if (!file_) {
			std::fprintf(stderr, "netmon: cannot open %s: %s\n",
				     path.c_str(), std::strerror(errno));
			return nullptr;
		}
		path_ = path;
		end_cut_line();
		return file_;
	}

	std::filesystem::path path_for(std::time_t ts) const
	{
		std::tm tm{};
		gmtime_r(&ts, &tm);
		char day[16], hour[16];
		std::strftime(day, sizeof(day), "%F", &tm);
		std::strftime(hour, sizeof(hour), "%H", &tm);
		return std::filesystem::path(dir_) / day / (hour + suffix_);
	}

private:
	// A crash or a full disk can leave the last line cut off. End it, so
	// the first new row starts a line of its own and stays readable.
	void end_cut_line()
	{
		struct stat st{};
		char last = '\n';
		int fd = fileno(file_);
		if (fstat(fd, &st) == 0 && st.st_size > 0 &&
		    pread(fd, &last, 1, st.st_size - 1) == 1 && last != '\n')
			std::fputc('\n', file_);
	}

	void close()
	{
		if (file_)
			std::fclose(file_);
		file_ = nullptr;
	}

	std::string dir_;
	std::string suffix_;
	std::filesystem::path path_;
	std::FILE *file_ = nullptr;
};
