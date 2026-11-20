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
#include <optional>
#include <random>
#include <string>
#include <vector>

#include "capture.h"
#include "interfaces.h"
#include "layouts.h"
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
		"  -o  append JSON lines to dir/YYYY-MM-DD/HH.jsonl (UTC), and\n"
		"      the interfaces' counters to HH.interfaces.jsonl\n"
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
	std::printf("%8s %6s %-16s %9s %9s %9s %9s %9s %9s %9s%s\n", "PID",
		    "UID", "COMM", "TCP_TX", "TCP_RX", "UDP_TX", "UDP_RX",
		    "NFS_RD", "NFS_WR", "NFS_FETCH",
		    opts.sample ? "    SCALE" : "");
	size_t n = rows_to_print(rows, opts);
	for (size_t i = 0; i < n; ++i) {
		const Row &r = rows[i];
		std::printf("%8u %6u %-16s %9s %9s %9s %9s %9s %9s %9s", r.pid,
			    r.uid, r.comm.c_str(),
			    human(r.delta[NETMON_TCP_TX]).c_str(),
			    human(r.delta[NETMON_TCP_RX]).c_str(),
			    human(r.delta[NETMON_UDP_TX]).c_str(),
			    human(r.delta[NETMON_UDP_RX]).c_str(),
			    human(r.delta[NETMON_NFS_READ]).c_str(),
			    human(r.delta[NETMON_NFS_WRITE]).c_str(),
			    human(r.delta[NETMON_NFS_FETCHED]).c_str());
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
			"\"nfs_write\":%llu,\"nfs_fetched\":%llu,"
			"\"scale\":%.17g}\n",
			static_cast<long long>(ts), host.c_str(), interval_s,
			r.pid, r.uid, json_escape(r.comm).c_str(),
			r.delta[NETMON_TCP_TX], r.delta[NETMON_TCP_RX],
			r.delta[NETMON_UDP_TX], r.delta[NETMON_UDP_RX],
			r.delta[NETMON_NFS_READ], r.delta[NETMON_NFS_WRITE],
			r.delta[NETMON_NFS_FETCHED], r.scale);
	}
	return std::fflush(out) == 0 && !std::ferror(out);
}

// The host's interfaces: pid 1's network namespace, as the service has a
// private one of its own (PrivateNetwork=), with only a loopback in it.
constexpr const char *kNetDev = "/proc/1/net/dev";

// With -o, each report's interface counters, in dir/YYYY-MM-DD/
// HH.interfaces.jsonl: what each interface moved since the last report, or
// since netmon started for the first, as the processes' rows count.
class InterfaceLog {
public:
	explicit InterfaceLog(const std::string &dir)
	    : files_(dir, ".interfaces.jsonl")
	{
		started_ = read(prev_);
	}

	// Returns false if the write failed.
	bool write(std::time_t ts, const std::string &host, int interval_s)
	{
		IfaceTotals cur;
		if (!read(cur))
			return true;  // the next read counts these bytes too
		if (!started_) {  // counts from here, not from boot
			prev_ = std::move(cur);
			started_ = true;
			return true;
		}
		auto rows = interface_deltas(cur, prev_);
		if (rows.empty())
			return true;
		std::FILE *out = files_.at(ts);
		if (!out)
			return false;
		for (const IfaceRow &row : rows)
			std::fputs(interface_json(ts, host, interval_s, row)
					   .c_str(),
				   out);
		return std::fflush(out) == 0 && !std::ferror(out);
	}

private:
	// Says so the first time it can't read the counters.
	bool read(IfaceTotals &totals)
	{
		if (std::ifstream in(kNetDev); in) {
			totals = parse_net_dev(in);
			if (!totals.empty())
				return true;
		}
		if (!warned_)
			std::fprintf(stderr,
				     "netmon: can't read %s; not recording "
				     "interface counters\n",
				     kNetDev);
		warned_ = true;
		return false;
	}

	HourlyFiles files_;
	IfaceTotals prev_;
	bool started_ = false;
	bool warned_ = false;
};

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

// Exit status when this kernel can't have the sock tracepoints (no tracefs,
// too old, another layout): restarting won't help.
constexpr int kCantCount = 3;

int quiet_print(enum libbpf_print_level, const char *, va_list) { return 0; }

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
// count but NFS. The rest are optional: each is attached on its own, so a
// kernel missing one function still runs the rest, and retried, since NFS
// functions and tracepoints appear only once the nfs module loads, at the
// first mount. An NFS tracepoint with another layout is never attached.
class Probes {
public:
	explicit Probes(netmon_bpf *skel)
	    : unmatched_fd_(bpf_map__fd(skel->maps.nfs_unmatched))
	{
		bpf_program *prog;
		bpf_object__for_each_program(prog, skel->obj)
		{
			Probe p{};
			p.prog = prog;
			if (is_tracepoint(prog))
				p.layout = layout_of(
					bpf_program__section_name(prog));
			probes_.push_back(std::move(p));
		}
	}
	Probes(const Probes &) = delete;
	Probes &operator=(const Probes &) = delete;
	~Probes()
	{
		for (Probe &p : probes_)
			bpf_link__destroy(p.link);
	}

	// Attaches the tracepoints. Returns 0, or saying why: kCantCount if
	// this kernel can't have them, 1 if attaching failed and may not again.
	int attach_required()
	{
		mount_tracefs();
		const std::string root = tracefs_root();
		for (Probe &p : probes_) {
			if (!is_tracepoint(p.prog) || !p.layout.required)
				continue;
			std::string name = bpf_program__section_name(p.prog);
			std::string problem =
				check_tracepoint(root, p.layout.category,
						 p.layout.event,
						 *p.layout.fields);
			int status = kCantCount;
			if (problem.empty()) {
				p.link = bpf_program__attach(p.prog);
				if (!p.link) {
					problem = std::string(" ") +
						  std::strerror(errno);
					status = 1;
				}
			}
			if (!problem.empty()) {
				std::fprintf(stderr,
					     "netmon: can't count TCP and UDP: "
					     "%s:%s\n",
					     name.c_str(), problem.c_str());
				return status;
			}
		}
		return 0;
	}

	// Attaches the optional probes it can. Says why for those it can't the
	// first time, and when a retry succeeds.
	void attach_optional(bool retry)
	{
		libbpf_print_fn_t old_print = nullptr;
		if (retry)  // libbpf would repeat its warnings every minute
			old_print = libbpf_set_print(quiet_print);
		const std::string root = tracefs_root();
		for (Probe &p : probes_) {
			if (p.link || p.refused ||
			    (is_tracepoint(p.prog) && p.layout.required))
				continue;
			const char *name = bpf_program__section_name(p.prog);
			int err = ENOENT;
			std::string problem;
			Readiness ready =
				is_tracepoint(p.prog)
					? check_optional(root, p.layout,
							 problem)
					: Readiness::kReady;
			if (ready == Readiness::kRefused) {
				refuse(p, problem);
				continue;
			}
			if (ready == Readiness::kReady) {
				p.link = bpf_program__attach(p.prog);
				err = errno;
				if (p.link && !p.layout.group.empty())
					quiet_reports_ = 2;
			}
			if (p.link && retry)
				std::fprintf(stderr, "netmon: attached %s\n",
					     name);
			else if (!p.link && !retry)
				std::fprintf(
					stderr,
					"netmon: could not attach %s: %s; "
					"retrying every minute\n",
					name,
					err != ENOENT ? std::strerror(err)
					: is_tracepoint(p.prog)
						? "no such tracepoint in this "
						  "kernel yet"
						: "no such function in this "
						  "kernel yet");
		}
		if (retry)
			libbpf_set_print(old_print);
	}

	// Logs NFS READ replies that found no recorded reader since the last
	// check, after the first two reports: until then, replies to READs
	// started before the tracepoints attached find none either.
	void report_unmatched()
	{
		std::vector<netmon_nfs_unmatched> cpus(
			std::max(libbpf_num_possible_cpus(), 1));
		unsigned int zero = 0;
		if (bpf_map_lookup_elem(unmatched_fd_, &zero, cpus.data()) != 0)
			return;
		netmon_nfs_unmatched sum{};
		for (const netmon_nfs_unmatched &c : cpus) {
			sum.replies += c.replies;
			sum.bytes += c.bytes;
		}
		if (quiet_reports_ > 0)
			--quiet_reports_;
		else if (sum.replies > unmatched_.replies)
			std::fprintf(stderr,
				     "netmon: %llu NFS READ replies (%llu "
				     "bytes) found no recorded reader, too "
				     "many READs in flight at once; their "
				     "bytes are not counted as nfs_fetched\n",
				     sum.replies - unmatched_.replies,
				     sum.bytes - unmatched_.bytes);
		unmatched_ = sum;
	}

	// Logs calls each probe lost since the last check. The kernel tracks
	// only so many callers inside a kretprobe's function at once (10 on a
	// 4-core Pi), and skips a BPF program that fires while another runs on
	// the same CPU (from an interrupt, say). The bytes of either go
	// uncounted.
	void report_missed()
	{
		for (Probe &p : probes_) {
			if (!p.link)
				continue;
			const char *name = bpf_program__section_name(p.prog);
			bpf_prog_info prog{};
			__u32 prog_len = sizeof(prog);
			if (bpf_prog_get_info_by_fd(bpf_program__fd(p.prog),
						    &prog, &prog_len) == 0) {
				if (prog.recursion_misses > p.skipped)
					std::fprintf(
						stderr,
						"netmon: %s skipped %llu calls "
						"while another BPF program ran "
						"on its CPU; their bytes are not "
						"counted\n",
						name,
						static_cast<unsigned long long>(
							prog.recursion_misses -
							p.skipped));
				p.skipped = prog.recursion_misses;
			}
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
					name, missed - p.missed);
			p.missed = missed;
		}
	}

private:
	struct Probe {
		bpf_program *prog;
		bpf_link *link = nullptr;
		unsigned long long missed = 0;  // by a kretprobe
		unsigned long long skipped = 0;  // recursion_misses
		TracepointLayout layout;  // for tracepoints
		bool refused = false;  // a tracepoint with another layout
	};

	// Never attaches p, with another layout than netmon.bpf.c reads, nor
	// the rest of its group, which would count nothing without it.
	void refuse(Probe &p, const std::string &problem)
	{
		std::fprintf(stderr, "netmon: not attaching %s:%s\n",
			     bpf_program__section_name(p.prog),
			     problem.c_str());
		for (Probe &q : probes_) {
			if (&q != &p && (p.layout.group.empty() ||
					 q.layout.group != p.layout.group))
				continue;
			q.refused = true;
			bpf_link__destroy(q.link);
			q.link = nullptr;
		}
		if (!p.layout.group.empty())
			std::fprintf(stderr, "netmon: not counting %s\n",
				     p.layout.group.c_str());
	}

	std::vector<Probe> probes_;
	int unmatched_fd_;
	netmon_nfs_unmatched unmatched_{};
	int quiet_reports_ = 0;  // reports before logging unmatched replies
};

int run(netmon_bpf *skel, const Options &opts)
{
	Probes probes(skel);
	if (int status = probes.attach_required())
		return status;
	probes.attach_optional(false);

	std::signal(SIGINT, on_signal);
	std::signal(SIGTERM, on_signal);

	int map_fd = bpf_map__fd(skel->maps.traffic);
	const bool use_proc = in_initial_pid_namespace();
	Totals prev;
	// Report only traffic from now on. A table starts at a boundary, so its
	// first report covers a whole interval; files count from the start.
	if (!opts.json)
		sleep_to_boundary(opts.interval_s);
	collect_map(map_fd, prev, use_proc);
	std::optional<InterfaceLog> interfaces;
	if (!opts.out_dir.empty())
		interfaces.emplace(opts.out_dir);
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
		probes.report_unmatched();
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
		    !print_json(out, ts, host, rows, opts, interval_s) ||
		    (interfaces && !interfaces->write(ts, host, interval_s))) {
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
