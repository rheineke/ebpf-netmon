// netmon: print per-process TCP/UDP/NFS bytes every interval, like top.
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <getopt.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/timex.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdarg>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <random>
#include <string>
#include <vector>

#include "capture.h"
#include "netmon.h"
#include "netmon.skel.h"
#include "priority_sample.h"
#include "report.h"
#include "tracefs.h"

namespace {

volatile std::sig_atomic_t g_stop = 0;

void on_signal(int) { g_stop = 1; }

struct Options {
	int interval_s = 2;
	int count = 0;  // 0 = run until interrupted
	int top = 10;
	bool top_given = false;
	int sample = 0;  // priority-sample this many rows; 0 = off
	int wait_clock_s = 0;  // hold reports until the clock is set
	bool json = false;
	std::string out_dir;  // hourly JSON-lines files instead of stdout
};

void usage(const char *prog)
{
	std::fprintf(
		stderr,
		"usage: %s [-i seconds] [-c count] [-n top | -k sample] [-j]\n"
		"          [-o dir] [-w seconds]\n"
		"  -i  interval between reports (default 2)\n"
		"  -c  number of reports, then exit (default: until ^C)\n"
		"  -n  rows per report (default 10, or all with -o; 0 = all)\n"
		"  -k  priority-sample this many rows per report, with\n"
		"      unbiased estimates (see README); overrides -n\n"
		"  -j  JSON lines instead of a table\n"
		"  -o  append JSON lines to dir/YYYY-MM-DD/HH.jsonl (UTC)\n"
		"  -w  at start, hold reports until the clock is synchronized,\n"
		"      for at most this long; the counts keep adding up\n",
		prog);
}

bool parse_options(int argc, char **argv, Options &opts)
{
	int c;
	while ((c = getopt(argc, argv, "i:c:n:k:o:w:jh")) != -1) {
		switch (c) {
		case 'i':
			opts.interval_s = std::max(1, std::atoi(optarg));
			break;
		case 'c': opts.count = std::max(0, std::atoi(optarg)); break;
		case 'n':
			opts.top = std::max(0, std::atoi(optarg));
			opts.top_given = true;
			break;
		case 'k': opts.sample = std::max(0, std::atoi(optarg)); break;
		case 'j': opts.json = true; break;
		case 'o':
			opts.out_dir = optarg;
			opts.json = true;
			break;
		case 'w':
			opts.wait_clock_s = std::max(0, std::atoi(optarg));
			break;
		default: return false;
		}
	}
	// Files are totals for later queries, so keep every row unless asked.
	if (!opts.out_dir.empty() && !opts.top_given)
		opts.top = 0;
	return optind == argc;
}

// The BPF side reports pids as the kernel's initial pid namespace sees them.
// In a container with a pid namespace of its own, /proc/PID is some other
// process, or none.
bool in_initial_pid_namespace()
{
	constexpr ino_t kProcPidInitIno = 0xEFFFFFFCU;  // PROC_PID_INIT_INO
	struct stat st{};
	return stat("/proc/self/ns/pid", &st) == 0 &&
	       st.st_ino == kProcPidInitIno;
}

// The BPF comm is the name of whichever thread last sent or received, so
// prefer the process name when /proc shows the same pids as the kernel.
std::string process_name(unsigned int pid, const char *bpf_comm, bool use_proc)
{
	if (use_proc) {
		std::ifstream in("/proc/" + std::to_string(pid) + "/comm");
		std::string name;
		if (in && std::getline(in, name) && !name.empty())
			return name;
	}
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

// The bytes each process moved since the previous call, read from the map.
std::vector<Row> collect_map(int map_fd, Totals &prev, bool use_proc)
{
	return collect(
		[map_fd](const unsigned int *key, unsigned int *next) {
			return bpf_map_get_next_key(map_fd, key, next) == 0;
		},
		[map_fd](unsigned int key, netmon_traffic &value) {
			return bpf_map_lookup_elem(map_fd, &key, &value) == 0;
		},
		[use_proc](unsigned int pid, const char *bpf_comm) {
			return process_name(pid, bpf_comm, use_proc);
		},
		prev);
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

void print_table(const std::vector<Row> &rows, const Options &opts,
		 int interval_s)
{
	std::time_t now = std::time(nullptr);
	char ts[32];
	std::strftime(ts, sizeof(ts), "%F %T", std::localtime(&now));
	std::printf("\n%s  bytes in the last %ds\n", ts, interval_s);
	std::printf("%8s %6s %-16s %9s %9s %9s %9s %9s %9s%s\n", "PID", "UID",
		    "COMM", "TCP_TX", "TCP_RX", "UDP_TX", "UDP_RX", "NFS_RD",
		    "NFS_WR", opts.sample ? "    SCALE" : "");
	size_t n = rows_to_print(rows, opts);
	for (size_t i = 0; i < n; ++i) {
		const Row &r = rows[i];
		std::printf("%8u %6u %-16s %9s %9s %9s %9s %9s %9s", r.pid,
			    r.uid, r.comm.c_str(),
			    human(r.delta[NETMON_TCP_TX]).c_str(),
			    human(r.delta[NETMON_TCP_RX]).c_str(),
			    human(r.delta[NETMON_UDP_TX]).c_str(),
			    human(r.delta[NETMON_UDP_RX]).c_str(),
			    human(r.delta[NETMON_NFS_READ]).c_str(),
			    human(r.delta[NETMON_NFS_WRITE]).c_str());
		if (opts.sample)
			std::printf(" %8.2f", r.scale);
		std::printf("\n");
	}
	std::fflush(stdout);
}

// Returns false if the write failed, e.g. the disk is full.
bool print_json(std::FILE *out, std::time_t ts, const std::string &host,
		const std::vector<Row> &rows, const Options &opts,
		int interval_s)
{
	size_t n = rows_to_print(rows, opts);
	for (size_t i = 0; i < n; ++i) {
		const Row &r = rows[i];
		std::fprintf(
			out,
			"{\"ts\":%lld,\"host\":\"%s\",\"interval_s\":%d,"
			"\"pid\":%u,\"uid\":%u,"
			"\"comm\":\"%s\",\"tcp_tx\":%llu,\"tcp_rx\":%llu,"
			"\"udp_tx\":%llu,\"udp_rx\":%llu,\"nfs_read\":%llu,"
			"\"nfs_write\":%llu,\"scale\":%.17g}\n",
			static_cast<long long>(ts), host.c_str(), interval_s,
			r.pid, r.uid, json_escape(r.comm).c_str(),
			r.delta[NETMON_TCP_TX], r.delta[NETMON_TCP_RX],
			r.delta[NETMON_UDP_TX], r.delta[NETMON_UDP_RX],
			r.delta[NETMON_NFS_READ], r.delta[NETMON_NFS_WRITE],
			r.scale);
	}
	return std::fflush(out) == 0 && !std::ferror(out);
}

std::string host_name()
{
	char buf[256] = {};
	if (gethostname(buf, sizeof(buf) - 1) != 0)
		return "unknown";
	return json_escape(buf);
}

// adjtimex() says whether NTP (systemd-timesyncd, chrony) has set the clock.
// A Pi has no battery-backed clock, so until then it shows the last
// shutdown's time. If the call is refused, assume the clock is fine.
bool clock_synced()
{
	timex tx{};
	return adjtimex(&tx) != TIME_ERROR;
}

// Sleeps until the next multiple of interval_s on the wall clock, or a stop,
// and returns that time: the report's timestamp. If the clock steps back,
// it waits for the next boundary from there; if it steps forward, the
// report takes the latest boundary passed.
std::time_t sleep_to_boundary(int interval_s)
{
	timespec now{};
	clock_gettime(CLOCK_REALTIME, &now);
	std::time_t due = next_boundary(now.tv_sec, interval_s);
	while (!g_stop) {
		clock_gettime(CLOCK_REALTIME, &now);
		double left = static_cast<double>(due - now.tv_sec) -
			      static_cast<double>(now.tv_nsec) / 1e9;
		if (left <= 0)
			break;
		if (left > interval_s) {  // the clock stepped back
			due = next_boundary(now.tv_sec, interval_s);
			continue;
		}
		// At most a second at a time, so a stop is seen at once.
		double nap = std::min(left, 1.0);
		timespec ts{static_cast<std::time_t>(nap),
			    static_cast<long>((nap - std::floor(nap)) * 1e9)};
		nanosleep(&ts, nullptr);
	}
	clock_gettime(CLOCK_REALTIME, &now);
	return std::max(due, now.tv_sec / interval_s * interval_s);
}

// Exit status when the sock tracepoints can't attach: restarting won't help.
constexpr int kCantCount = 3;

int quiet_print(enum libbpf_print_level, const char *, va_list) { return 0; }

// The fields of the sock tracepoints that netmon.bpf.c reads.
const std::vector<TracepointField> kSockMsgLength = {
	{"family", offsetof(netmon_sock_msg_length, family), 2},
	{"protocol", offsetof(netmon_sock_msg_length, protocol), 2},
	{"ret", offsetof(netmon_sock_msg_length, ret), 4},
	{"flags", offsetof(netmon_sock_msg_length, flags), 4},
};

// The sock tracepoints need tracefs, which a container's own /sys lacks.
// Without CAP_SYS_ADMIN, as under systemd (which mounts it), this fails
// harmlessly.
void mount_tracefs()
{
	if (tracefs_root().empty())
		mount("nodev", "/sys/kernel/tracing", "tracefs", 0, nullptr);
}

bool is_tracepoint(bpf_program *prog)
{
	return bpf_program__type(prog) == BPF_PROG_TYPE_TRACEPOINT;
}

// The sock tracepoints are required: without them there is nothing to
// count but NFS. The kprobes are optional: each is attached on its own, so a
// kernel missing one function still runs the rest, and retried, since NFS
// functions appear only once the nfs module loads, at the first mount.
class Probes {
public:
	explicit Probes(netmon_bpf *skel)
	{
		bpf_program *prog;
		bpf_object__for_each_program(prog, skel->obj)
			probes_.push_back({prog});
	}
	Probes(const Probes &) = delete;
	Probes &operator=(const Probes &) = delete;
	~Probes()
	{
		for (Probe &p : probes_)
			bpf_link__destroy(p.link);
	}

	// Attaches the tracepoints; false, saying why, if one can't attach.
	bool attach_required()
	{
		mount_tracefs();
		const std::string root = tracefs_root();
		for (Probe &p : probes_) {
			if (!is_tracepoint(p.prog))
				continue;
			// "tracepoint/sock/sock_send_length"
			std::string name = bpf_program__section_name(p.prog);
			auto event = name.rfind('/');
			auto category = name.rfind('/', event - 1);
			std::string problem =
				check_tracepoint(root,
						 name.substr(category + 1,
							     event - category -
								     1),
						 name.substr(event + 1),
						 kSockMsgLength);
			if (problem.empty()) {
				p.link = bpf_program__attach(p.prog);
				if (!p.link)
					problem = std::string(" ") +
						  std::strerror(errno);
			}
			if (!problem.empty()) {
				std::fprintf(stderr,
					     "netmon: can't count TCP and UDP: "
					     "%s:%s\n",
					     name.c_str(), problem.c_str());
				return false;
			}
		}
		return true;
	}

	// Attaches the kprobes it can. Says why for those it can't the first
	// time, and when a retry succeeds.
	void attach_optional(bool retry)
	{
		libbpf_print_fn_t old_print = nullptr;
		if (retry)  // libbpf would repeat its warnings every minute
			old_print = libbpf_set_print(quiet_print);
		for (Probe &p : probes_) {
			if (p.link || is_tracepoint(p.prog))
				continue;
			const char *name = bpf_program__section_name(p.prog);
			p.link = bpf_program__attach(p.prog);
			int err = errno;
			if (p.link && retry)
				std::fprintf(stderr, "netmon: attached %s\n",
					     name);
			else if (!p.link && !retry)
				std::fprintf(
					stderr,
					"netmon: could not attach %s: %s; "
					"retrying every minute\n",
					name,
					err == ENOENT
						? "no such function in this "
						  "kernel yet"
						: std::strerror(err));
		}
		if (retry)
			libbpf_set_print(old_print);
	}

	// Logs kretprobes that lost calls since the last check. The kernel
	// tracks only so many callers inside a function at once (10 on a
	// 4-core Pi); the bytes of any more go uncounted.
	void report_missed()
	{
		for (Probe &p : probes_) {
			if (!p.link)
				continue;
			bpf_link_info info{};
			__u32 len = sizeof(info);
			if (bpf_link_get_info_by_fd(bpf_link__fd(p.link), &info,
						    &len) != 0 ||
			    info.type != BPF_LINK_TYPE_PERF_EVENT ||
			    info.perf_event.type != BPF_PERF_EVENT_KRETPROBE)
				continue;
			unsigned long long missed =
				info.perf_event.kprobe.missed;
			if (missed > p.missed)
				std::fprintf(
					stderr,
					"netmon: %s missed %llu calls, too "
					"many at once; their bytes are not "
					"counted\n",
					bpf_program__section_name(p.prog),
					missed - p.missed);
			p.missed = missed;
		}
	}

private:
	struct Probe {
		bpf_program *prog;
		bpf_link *link = nullptr;
		unsigned long long missed = 0;
	};

	std::vector<Probe> probes_;
};

int run(netmon_bpf *skel, const Options &opts)
{
	Probes probes(skel);
	if (!probes.attach_required())
		return kCantCount;
	probes.attach_optional(false);

	std::signal(SIGINT, on_signal);
	std::signal(SIGTERM, on_signal);

	int map_fd = bpf_map__fd(skel->maps.traffic);
	const bool use_proc = in_initial_pid_namespace();
	Totals prev;
	collect_map(map_fd, prev, use_proc);  // report only traffic from now on
	std::mt19937_64 rng(std::random_device{}());
	const std::string host = host_name();
	HourlyFiles files(opts.out_dir);

	using Clock = std::chrono::steady_clock;
	const auto started = Clock::now();
	auto last_retry = started, last_report = started;
	ClockHold clock_hold{std::chrono::seconds(opts.wait_clock_s)};
	bool said_holding = false;
	int reports = 0;
	while (!g_stop && (!opts.count || reports < opts.count)) {
		std::time_t due = sleep_to_boundary(opts.interval_s);
		if (Clock::now() - last_retry >= std::chrono::minutes(1)) {
			probes.attach_optional(true);
			last_retry = Clock::now();
		}
		probes.report_missed();
		if (clock_hold.hold(Clock::now() - started, g_stop,
				    clock_synced)) {
			if (!said_holding)
				std::fprintf(stderr,
					     "netmon: holding reports until "
					     "the clock is synchronized, at "
					     "most %d s\n",
					     opts.wait_clock_s);
			said_holding = true;
			continue;  // the map keeps counting
		}
		++reports;
		auto rows = collect_map(map_fd, prev, use_proc);
		int interval_s = interval_seconds(Clock::now() - last_report);
		last_report = Clock::now();
		if (opts.sample)
			rows = sample_rows(std::move(rows), opts.sample, rng);
		if (!opts.json) {
			print_table(rows, opts, interval_s);
			continue;
		}
		// A stop's last report is cut short: stamp it when it was made.
		std::time_t ts = g_stop ? std::time(nullptr) : due;
		std::FILE *out = opts.out_dir.empty() ? stdout : files.at(ts);
		if (!out ||
		    !print_json(out, ts, host, rows, opts, interval_s)) {
			std::fprintf(stderr, "netmon: write failed\n");
			return 1;
		}
	}
	return 0;
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
	int rc = run(skel, opts);
	netmon_bpf__destroy(skel);
	return rc;
}
