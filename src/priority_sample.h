// Priority sampling: Duffield, Lund and Thorup, "Priority sampling for
// estimation of arbitrary subset sums", JACM 54(6), 2007.
//
// Keeps k of n weighted records. Each kept record i carries an estimate
// max(w_i, tau) of its weight, where tau is the (k+1)th largest priority, so
// the sum of estimates over any subset of the kept records is an unbiased
// estimate of that subset's true weight.
#pragma once

#include <algorithm>
#include <cstddef>
#include <numeric>
#include <random>
#include <vector>

struct PrioritySample {
	std::size_t index;  // into the weights passed in
	double scale;  // estimate / weight; multiply every counter by it
};

// Weights must be positive. Returns all records with scale 1 when n <= k.
inline std::vector<PrioritySample>
priority_sample(const std::vector<double> &weights, std::size_t k,
		std::mt19937_64 &rng)
{
	std::size_t n = weights.size();
	std::vector<PrioritySample> out;
	if (n <= k) {
		for (std::size_t i = 0; i < n; ++i)
			out.push_back({i, 1.0});
		return out;
	}

	// Priority w / u with u uniform on (0, 1].
	std::uniform_real_distribution<double> uniform(0.0, 1.0);
	std::vector<double> priority(n);
	for (std::size_t i = 0; i < n; ++i)
		priority[i] = weights[i] / (1.0 - uniform(rng));

	std::vector<std::size_t> order(n);
	std::iota(order.begin(), order.end(), 0);
	std::nth_element(order.begin(), order.begin() + k, order.end(),
			 [&](std::size_t a, std::size_t b) {
				 return priority[a] > priority[b];
			 });
	double tau = priority[order[k]];

	for (std::size_t j = 0; j < k; ++j) {
		std::size_t i = order[j];
		out.push_back({i, std::max(weights[i], tau) / weights[i]});
	}
	return out;
}
