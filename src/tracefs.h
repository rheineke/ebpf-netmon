// Checks a tracepoint's record layout against tracefs. Without kernel BTF,
// a BPF program reads tracepoint fields at offsets fixed when it was built,
// so netmon refuses to attach one whose fields have moved.
#pragma once

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <fstream>
#include <istream>
#include <stdexcept>
#include <string>
#include <vector>

struct TracepointField {
	std::string name;
	unsigned offset;
	unsigned size;
};

// Parses one line of a tracepoint's format file, such as
// "\tfield:int ret;\toffset:20;\tsize:4;\tsigned:1;".
inline bool parse_format_line(const std::string &line, TracepointField &out)
{
	auto field = line.find("field:");
	auto semi = line.find(';', field);
	if (field == std::string::npos || semi == std::string::npos)
		return false;
	// The name is the declaration's last word, once any [size] is dropped:
	// "char comm[16]", "__data_loc char[] name".
	std::string decl = line.substr(field + 6, semi - field - 6);
	auto open = decl.find('['), close = decl.find(']', open);
	if (open != std::string::npos && close != std::string::npos)
		decl.erase(open, close - open + 1);
	auto space = decl.find_last_of(" *");
	out.name = space == std::string::npos ? decl : decl.substr(space + 1);
	auto number_after = [&line](const char *key, unsigned &value) {
		auto at = line.find(key);
		if (at == std::string::npos)
			return false;
		try {
			value = static_cast<unsigned>(std::stoul(
				line.substr(at + std::string(key).size())));
		} catch (const std::exception &) {
			return false;
		}
		return true;
	};
	return number_after("offset:", out.offset) &&
	       number_after("size:", out.size) && !out.name.empty();
}

// Returns "" if every expected field is in the format at its offset and
// size, else what differs.
inline std::string check_format(std::istream &format,
				const std::vector<TracepointField> &expected)
{
	std::vector<TracepointField> found;
	std::string line;
	while (std::getline(format, line)) {
		TracepointField f;
		if (parse_format_line(line, f))
			found.push_back(f);
	}
	std::string problems;
	for (const TracepointField &want : expected) {
		const TracepointField *got = nullptr;
		for (const TracepointField &f : found)
			if (f.name == want.name)
				got = &f;
		if (!got)
			problems += " no field " + want.name + ";";
		else if (got->offset != want.offset || got->size != want.size)
			problems += " " + want.name + " at offset " +
				    std::to_string(got->offset) + " size " +
				    std::to_string(got->size) + ", built for " +
				    std::to_string(want.offset) + " size " +
				    std::to_string(want.size) + ";";
	}
	return problems;
}

// Where tracefs is mounted, as libbpf looks for it, or "" if nowhere. A
// root that is there but can't be read counts, so the error says why.
inline std::string tracefs_root()
{
	for (const char *root :
	     {"/sys/kernel/tracing", "/sys/kernel/debug/tracing"}) {
		struct stat st{};
		if (stat((std::string(root) + "/events").c_str(), &st) == 0 ||
		    errno != ENOENT)
			return root;
	}
	return "";
}

// Returns "" if the tracepoint category:event under tracefs root has the
// expected layout, else why not.
inline std::string
check_tracepoint(const std::string &root, const std::string &category,
		 const std::string &event,
		 const std::vector<TracepointField> &expected)
{
	if (root.empty())
		return " tracefs is not mounted at /sys/kernel/tracing";
	std::string path = root;
	path.append("/events/").append(category).append("/");
	path.append(event).append("/format");
	// open() first: unlike std::ifstream, it says why it failed.
	int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC);
	if (fd < 0 && errno == ENOENT)
		return " this kernel has no " + category + ":" + event +
		       " tracepoint (it needs Linux 6.3 or later)";
	if (fd < 0)
		return " can't read " + path + ": " + std::strerror(errno);
	close(fd);
	std::ifstream format(path);
	return check_format(format, expected);
}
