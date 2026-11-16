// Checks collect() against a fake BPF map, interval_seconds() and ClockHold.
#include <chrono>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "report.h"

namespace {

int failures = 0;

void check(bool ok, const char *what)
{
	std::printf("%s: %s\n", ok ? "ok  " : "FAIL", what);
	if (!ok)
		++failures;
}

netmon_traffic traffic(unsigned long long tcp_tx, const char *comm)
{
	netmon_traffic t{};
	t.bytes[NETMON_TCP_TX] = tcp_tx;
	std::strncpy(t.comm, comm, sizeof(t.comm) - 1);
	return t;
}

// A hash map as bpf_map_get_next_key() walks one: in key order here, and
// from the first key again when the key passed in is gone, as when the LRU
// has evicted it.
struct FakeMap {
	std::map<unsigned int, netmon_traffic> entries;
	unsigned int evict_after = 0;  // evict this key just after it is read
	int restarts = 0;

	bool next_key(const unsigned int *key, unsigned int *next)
	{
		auto it = entries.begin();
		if (key) {
			if (entries.count(*key))
				it = entries.upper_bound(*key);
			else
				++restarts;
		}
		if (it == entries.end())
			return false;
		*next = it->first;
		return true;
	}

	bool lookup(unsigned int key, netmon_traffic &value)
	{
		auto it = entries.find(key);
		if (it == entries.end())
			return false;
		value = it->second;
		if (key == evict_after)
			entries.erase(it);
		return true;
	}

	std::vector<Row> collect(Totals &prev)
	{
		return ::collect([this](const unsigned int *k,
					unsigned int
						*n) { return next_key(k, n); },
				 [this](unsigned int k, netmon_traffic &v) {
					 return lookup(k, v);
				 },
				 [](unsigned int, const char *comm) {
					 return std::string(comm);
				 },
				 prev);
	}
};

void test_deltas()
{
	FakeMap map;
	map.entries = {{10, traffic(100, "a")}, {20, traffic(5, "b")}};
	Totals prev;
	map.collect(prev);  // the baseline
	map.entries[10] = traffic(250, "a");
	map.entries[30] = traffic(7, "c");
	auto rows = map.collect(prev);
	check(rows.size() == 2, "a process with no new bytes has no row");
	check(rows.size() == 2 && rows[0].pid == 10 &&
		      rows[0].delta[NETMON_TCP_TX] == 150 &&
		      rows[1].pid == 30 && rows[1].delta[NETMON_TCP_TX] == 7,
	      "rows are the bytes since the last read, largest first");

	map.entries[10] = traffic(40, "a");  // evicted and re-created
	rows = map.collect(prev);
	check(rows.size() == 1 && rows[0].delta[NETMON_TCP_TX] == 40,
	      "a counter that went backwards counts from zero");
}

void test_walk_restart()
{
	FakeMap map;
	for (unsigned int pid = 1; pid <= 5; ++pid)
		map.entries[pid] = traffic(0, "p");
	Totals prev;
	map.collect(prev);
	for (unsigned int pid = 1; pid <= 5; ++pid)
		map.entries[pid] = traffic(1000ULL * pid, "p");
	map.evict_after = 3;  // so the walk starts over after key 3
	auto rows = map.collect(prev);
	unsigned long long total = 0;
	for (const Row &r : rows)
		total += r.delta[NETMON_TCP_TX];
	check(map.restarts == 1, "the fake walk did start over");
	check(rows.size() == 5 && total == 15000,
	      "a walk that starts over counts each process once");
}

void test_interval_seconds()
{
	using std::chrono::milliseconds;
	check(interval_seconds(milliseconds(10'004)) == 10,
	      "a normal interval is -i seconds");
	check(interval_seconds(milliseconds(37'000)) == 37,
	      "a held report covers the whole hold");
	check(interval_seconds(milliseconds(300)) == 1,
	      "a report cut short by a stop is at least 1 s");
}

void test_clock_hold()
{
	using std::chrono::seconds;
	bool synced = false;
	auto is_synced = [&synced] { return synced; };
	ClockHold off{seconds(0)};
	check(!off.hold(seconds(1), false, is_synced), "-w 0 never holds");

	ClockHold hold{seconds(300)};
	check(hold.hold(seconds(10), false, is_synced),
	      "holds while the clock is unsynchronized");
	synced = true;
	check(!hold.hold(seconds(20), false, is_synced),
	      "stops holding once it is synchronized");
	synced = false;
	check(!hold.hold(seconds(30), false, is_synced),
	      "and never holds again, even if it loses sync");

	ClockHold limit{seconds(300)};
	check(!limit.hold(seconds(300), false, is_synced),
	      "gives up at the limit");
	ClockHold stop{seconds(300)};
	check(!stop.hold(seconds(10), true, is_synced),
	      "reports at once when stopping");
}

}  // namespace

int main()
{
	test_deltas();
	test_walk_restart();
	test_interval_seconds();
	test_clock_hold();
	if (failures)
		std::printf("%d failed\n", failures);
	return failures ? 1 : 0;
}
