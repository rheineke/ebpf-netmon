// netmon: print per-process TCP/UDP bytes every interval, like top.
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <getopt.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <random>
#include <fstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "netmon.h"
#include "netmon.skel.h"
#include "priority_sample.h"

namespace {

volatile std::sig_atomic_t g_stop = 0;

void on_signal(int) { g_stop = 1; }

struct Options {
	int interval_s = 2;
	int count = 0;  // 0 = run until interrupted
	int top = 10;
	int sample = 0;  // priority-sample this many rows; 0 = off
	bool json = false;
};

struct Row {
	unsigned int pid;
	unsigned int uid;
	std::string comm;
	std::array<unsigned long long, NETMON_NR_COUNTERS> delta;
	double scale = 1.0;  // estimate = delta * scale, after sampling
	unsigned long long total() const
	{
		unsigned long long sum = 0;
		for (auto b : delta)
			sum += b;
		return sum;
	}
};

void usage(const char *prog)
{
	std::fprintf(
		stderr,
		"usage: %s [-i seconds] [-c count] [-n top | -k sample] [-j]\n"
		"  -i  interval between reports (default 2)\n"
		"  -c  number of reports, then exit (default: until ^C)\n"
		"  -n  rows per report (default 10, 0 = all)\n"
		"  -k  priority-sample this many rows per report, with\n"
		"      unbiased estimates (see README); overrides -n\n"
		"  -j  JSON lines instead of a table\n",
		prog);
}

bool parse_options(int argc, char **argv, Options &opts)
{
	int c;
	while ((c = getopt(argc, argv, "i:c:n:k:jh")) != -1) {
		switch (c) {
		case 'i':
			opts.interval_s = std::max(1, std::atoi(optarg));
			break;
		case 'c': opts.count = std::max(0, std::atoi(optarg)); break;
		case 'n': opts.top = std::max(0, std::atoi(optarg)); break;
		case 'k': opts.sample = std::max(0, std::atoi(optarg)); break;
		case 'j': opts.json = true; break;
		default: return false;
		}
	}
	return optind == argc;
}

// The BPF comm is the name of whichever thread first sent or received, so
// prefer the process name when the pid is visible in our pid namespace.
std::string process_name(unsigned int pid, const char *bpf_comm)
{
	std::ifstream in("/proc/" + std::to_string(pid) + "/comm");
	std::string name;
	if (in && std::getline(in, name) && !name.empty())
		return name;
	return std::string(bpf_comm, strnlen(bpf_comm, NETMON_COMM_LEN));
}

std::string human(unsigned long long bytes)
{
	static const char *units[] = {"B", "K", "M", "G", "T"};
	double v = static_cast<double>(bytes);
	int u = 0;
	while (v >= 1024 && u < 4) {
		v /= 1024;
		++u;
	}
	char buf[32];
	std::snprintf(buf, sizeof(buf), u ? "%.1f%s" : "%.0f%s", v, units[u]);
	return buf;
}

std::string json_escape(const std::string &s)
{
	std::string out;
	for (char ch : s) {
		if (ch == '"' || ch == '\\') {
			out += '\\';
			out += ch;
		} else if (static_cast<unsigned char>(ch) < 0x20) {
			char buf[8];
			std::snprintf(buf, sizeof(buf), "\\u%04x", ch);
			out += buf;
		} else {
			out += ch;
		}
	}
	return out;
}

// Reads the map and returns the bytes moved since the previous call.
std::vector<Row> collect(int map_fd,
			 std::unordered_map<unsigned int, netmon_traffic> &prev)
{
	std::unordered_map<unsigned int, netmon_traffic> cur;
	std::vector<Row> rows;
	unsigned int key, next;
	unsigned int *keyp = nullptr;
	netmon_traffic value;

	while (bpf_map_get_next_key(map_fd, keyp, &next) == 0) {
		key = next;
		keyp = &key;
		if (bpf_map_lookup_elem(map_fd, &key, &value) != 0)
			continue;  // evicted between the two calls
		cur[key] = value;

		Row row{key, value.uid, {}, {}};
		auto it = prev.find(key);
		for (int i = 0; i < NETMON_NR_COUNTERS; ++i) {
			unsigned long long before =
				it == prev.end() ? 0 : it->second.bytes[i];
			// Smaller than before: the LRU evicted and re-created it.
			row.delta[i] = value.bytes[i] >= before
					       ? value.bytes[i] - before
					       : value.bytes[i];
		}
		if (row.total() == 0)
			continue;
		row.comm = process_name(key, value.comm);
		rows.push_back(std::move(row));
	}
	prev = std::move(cur);

	std::sort(rows.begin(), rows.end(), [](const Row &a, const Row &b) {
		return a.total() > b.total();
	});
	return rows;
}

// Keeps opts.sample rows, weighted by total bytes, and sets each row's scale.
std::vector<Row> sample_rows(std::vector<Row> rows, size_t k,
			     std::mt19937_64 &rng)
{
	std::vector<double> weights;
	weights.reserve(rows.size());
	for (const Row &r : rows)
		weights.push_back(static_cast<double>(r.total()));
	std::vector<Row> kept;
	for (const PrioritySample &s : priority_sample(weights, k, rng)) {
		kept.push_back(std::move(rows[s.index]));
		kept.back().scale = s.scale;
	}
	std::sort(kept.begin(), kept.end(), [](const Row &a, const Row &b) {
		return a.total() * a.scale > b.total() * b.scale;
	});
	return kept;
}

size_t rows_to_print(const std::vector<Row> &rows, const Options &opts)
{
	if (opts.sample || !opts.top)
		return rows.size();
	return std::min<size_t>(opts.top, rows.size());
}

void print_table(const std::vector<Row> &rows, const Options &opts)
{
	std::time_t now = std::time(nullptr);
	char ts[32];
	std::strftime(ts, sizeof(ts), "%F %T", std::localtime(&now));
	std::printf("\n%s  bytes in the last %ds\n", ts, opts.interval_s);
	std::printf("%8s %6s %-16s %9s %9s %9s %9s%s\n", "PID", "UID", "COMM",
		    "TCP_TX", "TCP_RX", "UDP_TX", "UDP_RX",
		    opts.sample ? "    SCALE" : "");
	size_t n = rows_to_print(rows, opts);
	for (size_t i = 0; i < n; ++i) {
		const Row &r = rows[i];
		std::printf("%8u %6u %-16s %9s %9s %9s %9s", r.pid, r.uid,
			    r.comm.c_str(),
			    human(r.delta[NETMON_TCP_TX]).c_str(),
			    human(r.delta[NETMON_TCP_RX]).c_str(),
			    human(r.delta[NETMON_UDP_TX]).c_str(),
			    human(r.delta[NETMON_UDP_RX]).c_str());
		if (opts.sample)
			std::printf(" %8.2f", r.scale);
		std::printf("\n");
	}
	std::fflush(stdout);
}

void print_json(const std::vector<Row> &rows, const Options &opts)
{
	long long ts =
		std::chrono::duration_cast<std::chrono::seconds>(
			std::chrono::system_clock::now().time_since_epoch())
			.count();
	size_t n = rows_to_print(rows, opts);
	for (size_t i = 0; i < n; ++i) {
		const Row &r = rows[i];
		std::printf(
			"{\"ts\":%lld,\"interval_s\":%d,\"pid\":%u,\"uid\":%u,"
			"\"comm\":\"%s\",\"tcp_tx\":%llu,\"tcp_rx\":%llu,"
			"\"udp_tx\":%llu,\"udp_rx\":%llu,\"scale\":%.17g}\n",
			ts, opts.interval_s, r.pid, r.uid,
			json_escape(r.comm).c_str(), r.delta[NETMON_TCP_TX],
			r.delta[NETMON_TCP_RX], r.delta[NETMON_UDP_TX],
			r.delta[NETMON_UDP_RX], r.scale);
	}
	std::fflush(stdout);
}

// Attaches each probe on its own so a kernel missing one symbol still runs.
int attach(netmon_bpf *skel)
{
	int attached = 0;
	bpf_program *prog;
	bpf_object__for_each_program(prog, skel->obj)
	{
		bpf_link *link = bpf_program__attach(prog);
		if (!link) {
			std::fprintf(stderr, "netmon: could not attach %s\n",
				     bpf_program__section_name(prog));
			continue;
		}
		++attached;  // links are released when the process exits
	}
	return attached;
}

}  // namespace

int main(int argc, char **argv)
{
	Options opts;
	if (!parse_options(argc, argv, opts)) {
		usage(argv[0]);
		return 2;
	}

	netmon_bpf *skel = netmon_bpf__open_and_load();
	if (!skel) {
		std::fprintf(stderr, "netmon: failed to load BPF program "
				     "(run as root?)\n");
		return 1;
	}
	if (attach(skel) == 0) {
		netmon_bpf__destroy(skel);
		return 1;
	}

	std::signal(SIGINT, on_signal);
	std::signal(SIGTERM, on_signal);

	int map_fd = bpf_map__fd(skel->maps.traffic);
	std::unordered_map<unsigned int, netmon_traffic> prev;
	collect(map_fd, prev);  // baseline: report only traffic from now on
	std::mt19937_64 rng(std::random_device{}());

	for (int report = 0; !g_stop && (!opts.count || report < opts.count);
	     ++report) {
		for (int s = 0; s < opts.interval_s && !g_stop; ++s)
			sleep(1);
		auto rows = collect(map_fd, prev);
		if (opts.sample)
			rows = sample_rows(std::move(rows), opts.sample, rng);
		if (opts.json)
			print_json(rows, opts);
		else
			print_table(rows, opts);
	}

	netmon_bpf__destroy(skel);
	return 0;
}
