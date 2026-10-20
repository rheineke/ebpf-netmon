// Checks priority_sample() keeps k records and gives unbiased subset sums.
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "priority_sample.h"

namespace {

int failures = 0;

void check(bool ok, const char *what)
{
	std::printf("%s: %s\n", ok ? "ok  " : "FAIL", what);
	if (!ok)
		++failures;
}

void test_keeps_all_when_n_le_k()
{
	std::mt19937_64 rng(1);
	auto s = priority_sample({5, 1, 3}, 3, rng);
	bool ok = s.size() == 3;
	for (auto &p : s)
		ok = ok && p.scale == 1.0;
	check(ok, "n <= k keeps every record unscaled");
}

void test_unbiased_subset_sums()
{
	// Heavy-tailed, like per-process traffic: a few big talkers, many small.
	std::vector<double> w;
	for (int i = 1; i <= 60; ++i)
		w.push_back(1e6 / (i * i) + 100);
	double total = 0, odd = 0;
	for (std::size_t i = 0; i < w.size(); ++i) {
		total += w[i];
		if (i % 2)
			odd += w[i];
	}

	const std::size_t k = 8;
	const int trials = 40000;
	std::mt19937_64 rng(42);
	double est_total = 0, est_odd = 0;
	bool sizes_ok = true, scales_ok = true;
	for (int t = 0; t < trials; ++t) {
		auto s = priority_sample(w, k, rng);
		sizes_ok = sizes_ok && s.size() == k;
		for (auto &p : s) {
			scales_ok = scales_ok && p.scale >= 1.0;
			double e = w[p.index] * p.scale;
			est_total += e;
			if (p.index % 2)
				est_odd += e;
		}
	}
	est_total /= trials;
	est_odd /= trials;
	std::printf("      total %.0f est %.0f; odd %.0f est %.0f\n", total,
		    est_total, odd, est_odd);

	check(sizes_ok, "keeps exactly k records");
	check(scales_ok, "every scale is >= 1");
	check(std::fabs(est_total / total - 1) < 0.01,
	      "mean estimated total within 1%");
	check(std::fabs(est_odd / odd - 1) < 0.02,
	      "mean estimated subset sum within 2%");
}

}  // namespace

int main()
{
	test_keeps_all_when_n_le_k();
	test_unbiased_subset_sums();
	return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}
