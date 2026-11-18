// Checks parse_net_dev(), interface_deltas() and interface_json().
#include <cstdio>
#include <sstream>
#include <string>

#include "interfaces.h"

namespace {

int failures = 0;

void check(bool ok, const char *what)
{
	std::printf("%s: %s\n", ok ? "ok  " : "FAIL", what);
	if (!ok)
		++failures;
}

// As a Pi 4 shows it, with eth0 renamed end0 on newer releases.
const char *kNetDev =
	"Inter-|   Receive                                                |  "
	"Transmit\n"
	" face |bytes    packets errs drop fifo frame compressed multicast|"
	"bytes    packets errs drop fifo colls carrier compressed\n"
	"    lo: 100169906  449622    0    0    0     0          0         0 "
	"100169906  449622    0    0    0     0       0          0\n"
	"  end0: 27373385407 27300268    0  447    0     0          0    "
	"242628 9670624498 15491871    0    0    0     0       0          0\n"
	" wlan0:       0       0    0    0    0     0          0         0 "
	"       0       0    0    0    0     0       0          0\n";

IfaceTotals parse(const std::string &text)
{
	std::istringstream in(text);
	return parse_net_dev(in);
}

void test_parse()
{
	IfaceTotals t = parse(kNetDev);
	check(t.size() == 3, "one entry per interface, none for the headers");
	check(t["lo"] == IfaceCounters{100169906, 449622, 100169906, 449622},
	      "lo's bytes and packets");
	check(t["end0"] == IfaceCounters{27373385407ULL, 27300268,
					 9670624498ULL, 15491871},
	      "end0's counters, past 32 bits, skipping errs to multicast");
	check(t.count("wlan0") == 1 && t["wlan0"] == IfaceCounters{},
	      "an idle interface is there, all zeros");
	// A name made of the characters Linux allows, one too short a line,
	// and one with no name.
	IfaceTotals odd = parse("v-eth.1@x: 1 2 0 0 0 0 0 0 3 4 0 0 0 0 0 0\n"
				"short: 1 2 3\n"
				"   : 1 2 0 0 0 0 0 0 3 4 0 0 0 0 0 0\n");
	check(odd.size() == 1 && odd["v-eth.1@x"] == IfaceCounters{1, 2, 3, 4},
	      "odd names kept, short and nameless lines skipped");
	check(parse("").empty(), "nothing in, nothing out");
}

void test_deltas()
{
	IfaceTotals prev = {{"eth0", {1000, 10, 500, 5}},
			    {"lo", {100, 1, 100, 1}},
			    {"wlan0", {0, 0, 0, 0}}};
	IfaceTotals cur = {{"eth0", {1500, 14, 800, 8}},
			   {"lo", {100, 1, 100, 1}},
			   {"wlan0", {0, 0, 0, 0}},
			   {"usb0", {70, 1, 0, 0}}};
	auto rows = interface_deltas(cur, prev);
	check(rows.size() == 2, "interfaces that moved nothing are left out");
	check(rows.size() == 2 && rows[0].name == "eth0" &&
		      rows[0].delta == IfaceCounters{500, 4, 300, 3},
	      "eth0 moved the difference");
	check(rows.size() == 2 && rows[1].name == "usb0" &&
		      rows[1].delta == IfaceCounters{70, 1, 0, 0},
	      "a new interface counts from zero");
	check(prev == cur, "prev becomes cur");

	// eth0 made again: its counters start over, below their last values.
	IfaceTotals again = {{"eth0", {200, 2, 900, 9}}};
	rows = interface_deltas(again, prev);
	check(rows.size() == 1 &&
		      rows[0].delta == IfaceCounters{200, 2, 100, 1},
	      "a counter that went down counts from zero");
	check(prev == again, "gone interfaces drop out of prev");
}

void test_json()
{
	IfaceRow row{"we\"ird", {1, 2, 3, 4}};
	check(interface_json(1792666260, "pi", 10, row) ==
		      "{\"ts\":1792666260,\"host\":\"pi\",\"interval_s\":10,"
		      "\"interface\":\"we\\\"ird\",\"rx_bytes\":1,"
		      "\"rx_packets\":2,\"tx_bytes\":3,\"tx_packets\":4}\n",
	      "a JSON line, the name escaped");
	IfaceRow big{"end0",
		     {18446744073709551615ULL, 1, 18446744073709551615ULL, 1}};
	check(interface_json(0, "pi", 10, big)
			      .find("\"rx_bytes\":18446744073709551615,") !=
		      std::string::npos,
	      "64-bit counts are written whole");
}

}  // namespace

int main()
{
	test_parse();
	test_deltas();
	test_json();
	if (failures)
		std::printf("%d failed\n", failures);
	return failures ? 1 : 0;
}
