// Network interface counters from /proc/net/dev: every byte the host's
// interfaces moved, headers included, whichever process (or none) moved it.
// Set against the bytes netmon charges to processes, they show how much of
// the traffic netmon accounts for. No BPF here, so the unit tests can drive
// it.
#pragma once

#include <cstdio>
#include <istream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "capture.h"

struct IfaceCounters {
	unsigned long long rx_bytes = 0;
	unsigned long long rx_packets = 0;
	unsigned long long tx_bytes = 0;
	unsigned long long tx_packets = 0;
	bool operator==(const IfaceCounters &) const = default;
};

// Running totals by interface name.
using IfaceTotals = std::map<std::string, IfaceCounters>;

// Parses /proc/net/dev: two header lines, then a line per interface,
//   "  eth0: rx_bytes rx_packets errs drop fifo frame compressed multicast
//    tx_bytes tx_packets ...".
// Interface names can't hold a colon. Lines it can't read are skipped.
inline IfaceTotals parse_net_dev(std::istream &in)
{
	IfaceTotals totals;
	std::string line;
	while (std::getline(in, line)) {
		auto colon = line.find(':');
		if (colon == std::string::npos)
			continue;  // a header line
		auto first = line.find_first_not_of(' ');
		if (first >= colon)
			continue;  // no name
		std::istringstream fields(line.substr(colon + 1));
		unsigned long long f[10];
		bool ok = true;
		for (auto &v : f)
			ok = ok && static_cast<bool>(fields >> v);
		if (!ok)
			continue;
		totals[line.substr(first, colon - first)] = {f[0], f[1], f[8],
							     f[9]};
	}
	return totals;
}

struct IfaceRow {
	std::string name;
	IfaceCounters delta;
};

// What each interface moved since prev, in name order, leaving out those
// that moved nothing, and replaces prev with cur. An interface new since
// prev counts from zero, as the kernel's counters start when it is made. A
// counter below its last value means it was made again (a USB adapter
// replugged), so it too counts from zero.
inline std::vector<IfaceRow> interface_deltas(const IfaceTotals &cur,
					      IfaceTotals &prev)
{
	auto since = [](unsigned long long now, unsigned long long before) {
		return now >= before ? now - before : now;
	};
	std::vector<IfaceRow> rows;
	for (const auto &[name, now] : cur) {
		IfaceCounters before;
		if (auto it = prev.find(name); it != prev.end())
			before = it->second;
		IfaceCounters d{since(now.rx_bytes, before.rx_bytes),
				since(now.rx_packets, before.rx_packets),
				since(now.tx_bytes, before.tx_bytes),
				since(now.tx_packets, before.tx_packets)};
		if (d != IfaceCounters{})
			rows.push_back({name, d});
	}
	prev = cur;
	return rows;
}

// One interface's row in a report, as a JSON line. host is already escaped.
inline std::string interface_json(long long ts, const std::string &host,
				  int interval_s, const IfaceRow &row)
{
	char counts[192];
	std::snprintf(counts, sizeof(counts),
		      "\"rx_bytes\":%llu,\"rx_packets\":%llu,"
		      "\"tx_bytes\":%llu,\"tx_packets\":%llu}\n",
		      row.delta.rx_bytes, row.delta.rx_packets,
		      row.delta.tx_bytes, row.delta.tx_packets);
	return "{\"ts\":" + std::to_string(ts) + ",\"host\":\"" + host +
	       "\",\"interval_s\":" + std::to_string(interval_s) +
	       ",\"interface\":\"" + json_escape(row.name) + "\"," + counts;
}
