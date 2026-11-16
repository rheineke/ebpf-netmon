// From the BPF map's running totals to per-interval rows, and when to
// report them. No BPF calls here, so the unit tests can drive it.
#pragma once

#include <algorithm>
#include <array>
#include <chrono>
#include <string>
#include <unordered_map>
#include <vector>

#include "netmon.h"

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

using Totals = std::unordered_map<unsigned int, netmon_traffic>;

// Walks the map with next_key(const unsigned *key, unsigned *next) and
// lookup(unsigned key, netmon_traffic &value), which wrap
// bpf_map_get_next_key() and bpf_map_lookup_elem(), and returns the bytes
// each process moved since prev, largest first. name(pid, bpf_comm) names
// each row. Replaces prev with the totals read.
template <class NextKey, class Lookup, class Name>
std::vector<Row> collect(NextKey next_key, Lookup lookup, Name name,
			 Totals &prev)
{
	Totals cur;
	std::vector<Row> rows;
	unsigned int key = 0, next = 0;
	const unsigned int *keyp = nullptr;
	netmon_traffic value{};

	while (next_key(keyp, &next)) {
		key = next;
		keyp = &key;
		if (!lookup(key, value))
			continue;  // evicted between the two calls
		// A hash map's walk starts over when the LRU evicts the key it
		// stands on, so a key can come round again: count it once.
		if (!cur.emplace(key, value).second)
			continue;

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
		row.comm = name(key, value.comm);
		rows.push_back(std::move(row));
	}
	prev = std::move(cur);

	std::sort(rows.begin(), rows.end(), [](const Row &a, const Row &b) {
		return a.total() > b.total();
	});
	return rows;
}

// The interval a report covers, in whole seconds: usually -i, longer for a
// report held for the clock, shorter for the last one, cut off by a stop.
inline int interval_seconds(std::chrono::steady_clock::duration since_last)
{
	auto s = std::chrono::round<std::chrono::seconds>(since_last).count();
	return static_cast<int>(std::max<decltype(s)>(1, s));
}

// A Pi has no clock battery, so it boots with its last shutdown's time.
// Holds reports at startup until the clock is synchronized, for at most
// limit, then never again. The counts keep adding up in the map meanwhile.
class ClockHold {
public:
	explicit ClockHold(std::chrono::seconds limit)
	    : limit_(limit), done_(limit.count() <= 0)
	{
	}

	// Whether to hold this report. synced() is only asked while holding.
	template <class Synced>
	bool hold(std::chrono::steady_clock::duration since_start,
		  bool stopping, Synced synced)
	{
		if (done_)
			return false;
		if (stopping || since_start >= limit_ || synced()) {
			done_ = true;
			return false;
		}
		return true;
	}

private:
	std::chrono::seconds limit_;
	bool done_;
};
