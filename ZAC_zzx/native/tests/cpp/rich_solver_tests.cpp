#include "zac_native/rich_solver.hpp"

#include <cassert>
#include <cmath>
#include <iostream>
#include <set>

using namespace zac_native;

namespace {
PythonRandomState rng_fixture() {
  PythonRandomState state;
  for (std::size_t index = 0; index < state.words.size(); ++index) {
    state.words[index] = static_cast<std::uint32_t>(
        0x6c078965U * static_cast<std::uint32_t>(index + 11));
  }
  state.index = 624;
  return state;
}

RichSearchConfig exact_config() {
  RichSearchConfig config;
  config.operator_profile = RichOperatorProfile::kExact;
  config.max_unique_evaluations = 256;
  return config;
}

RichH0Problem one_resident_problem() {
  RichH0Problem problem;
  problem.n_atoms = 3;
  problem.current_points = {{0.0, 0.0}, {1.0, 0.0}, {2.0, 2.0}};
  problem.participants = {0, 1};
  problem.gate_domains = {{{10, 0, 1, {0.0, 0.0}, {1.0, 0.0}, {}, {}, {}}}};
  problem.eligible = {2};
  problem.eviction_order_indices = {0};
  problem.forced_return_mask = {false};
  problem.return_domains = {{{3, {2.0, 3.0}, 1.0}}};
  problem.matched_gate_genes = {0};
  return problem;
}
}  // namespace

int main() {
  std::size_t tests = 0;
  ArchitectureSnapshot small_architecture(
      3, {{0.0, 0.0}, {1.0, 0.0}, {2.0, 2.0}, {2.0, 3.0}}, {3});

  auto problem = one_resident_problem();
  const auto direct = solve_rich_h0(
      small_architecture, problem, exact_config(), rng_fixture());
  assert(direct.search_mode == "enumerate");
  assert(direct.winner.chromosome == std::vector<std::int64_t>({0, 1}));
  const std::vector<std::pair<std::int64_t, std::int64_t>> expected_return{{2, 3}};
  assert(direct.return_assignments == expected_return);
  ++tests;

  problem.min_returns = 1;
  problem.decision_policy = RichDecisionPolicy::kAlwaysStay;
  const auto capacity = solve_rich_h0(
      small_architecture, problem, exact_config(), rng_fixture());
  assert(capacity.winner.chromosome.back() == 1);
  ++tests;

  ArchitectureSnapshot injection_architecture(
      4, {{0.0, 0.0}, {1.0, 0.0}, {2.0, 0.0}, {3.0, 0.0}});
  RichH0Problem injection;
  injection.n_atoms = 4;
  injection.current_points = {{0.0, 0.0}, {1.0, 0.0},
                              {2.0, 0.0}, {3.0, 0.0}};
  injection.participants = {0, 1, 2, 3};
  injection.gate_domains = {
      {{10, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
       {11, 0, 1, {0.0, 0.0}, {1.0, 0.0}}},
      {{10, 2, 3, {2.0, 0.0}, {3.0, 0.0}},
       {12, 2, 3, {2.0, 0.0}, {3.0, 0.0}}},
  };
  injection.matched_gate_genes = {0, 0};
  const auto injected = solve_rich_h0(
      injection_architecture, injection, exact_config(), rng_fixture());
  assert(injected.gate_option_indices == std::vector<std::size_t>({0, 1}));
  ++tests;

  ArchitectureSnapshot fallback_architecture(
      2, {{0.0, 0.0}, {1.0, 0.0}, {0.0, 2.0}, {1.0, 2.0}}, {2, 3});
  RichH0Problem bounded_matching;
  bounded_matching.n_atoms = 2;
  bounded_matching.current_points = {{0.0, 0.0}, {1.0, 0.0}};
  bounded_matching.eligible = {0, 1};
  bounded_matching.min_returns = 2;
  bounded_matching.eviction_order_indices = {0, 1};
  bounded_matching.forced_return_mask = {true, true};
  bounded_matching.return_domains = {
      {{2, {0.0, 2.0}, 1.0}, {3, {1.0, 2.0}, 2.0}},
      {{2, {0.0, 2.0}, 1.0}, {3, {1.0, 2.0}, 2.0}}};
  bounded_matching.decision_policy = RichDecisionPolicy::kAlwaysReturn;
  const auto bounded_value = solve_rich_h0(
      fallback_architecture, bounded_matching, exact_config(), rng_fixture());
  assert(bounded_value.return_assignments.size() == 2);
  const std::set<std::int64_t> fallback_sites{
      bounded_value.return_assignments[0].second,
      bounded_value.return_assignments[1].second};
  const std::set<std::int64_t> expected_sites{2, 3};
  assert(fallback_sites == expected_sites);
  ++tests;

  ArchitectureSnapshot ghost_return_architecture(
      3, {{1.0, 1.0}, {10.0, 10.0}, {0.0, 0.0},
          {2.0, 2.0}, {2.0, 3.0}}, {3, 4});
  RichH0Problem ghost_return;
  ghost_return.n_atoms = 3;
  ghost_return.current_points = {{1.0, 1.0}, {10.0, 10.0}, {0.0, 0.0}};
  ghost_return.participants = {0, 1};
  ghost_return.gate_domains = {
      {{10, 0, 1, {1.0, 1.0}, {10.0, 10.0}}}};
  ghost_return.eligible = {2};
  ghost_return.min_returns = 1;
  ghost_return.eviction_order_indices = {0};
  ghost_return.forced_return_mask = {true};
  ghost_return.return_domains = {
      {{3, {2.0, 2.0}, 1.0}, {4, {2.0, 3.0}, 2.0}}};
  ghost_return.matched_gate_genes = {0};
  auto ghost_return_config = exact_config();
  ghost_return_config.return_assignment_k = 4;
  const auto ghost_safe = solve_rich_h0(
      ghost_return_architecture, ghost_return, ghost_return_config, rng_fixture());
  const std::vector<std::pair<std::int64_t, std::int64_t>> ghost_safe_expected{
      {2, 4}};
  assert(ghost_safe.return_assignments == ghost_safe_expected);
  assert(ghost_safe.return_assignment_rank == 2);
  assert(ghost_safe.return_assignment_evaluated == 2);
  assert(ghost_safe.current_ghost_rejections == 1);
  ++tests;

  ArchitectureSnapshot reseat_architecture(
      3, {{0.0, 0.0}, {1.0, 0.0}, {2.0, 2.0},
          {3.0, 3.0}, {2.0, 4.0}}, {4});
  RichH0Problem reseat;
  reseat.n_atoms = 3;
  reseat.current_points = {{0.0, 0.0}, {1.0, 0.0}, {2.0, 2.0}};
  reseat.participants = {0, 1};
  reseat.gate_domains = {{{10, 0, 1, {2.0, 2.0}, {1.0, 0.0}}}};
  reseat.eligible = {2};
  reseat.eviction_order_indices = {0};
  reseat.forced_return_mask = {false};
  reseat.return_domains = {{{4, {2.0, 4.0}, 1.0}}};
  reseat.matched_gate_genes = {0};
  reseat.decision_policy = RichDecisionPolicy::kAlwaysStay;
  const auto reseated = solve_rich_h0(
      reseat_architecture, reseat, exact_config(), rng_fixture());
  assert(reseated.winner.feasible);
  const std::vector<std::pair<std::int64_t, std::int64_t>> reseat_expected{
      {2, 3}};
  assert(reseated.reseat_assignments == reseat_expected);
  assert(reseated.pre_score_reseats == 1);
  ++tests;

  ArchitectureSnapshot moving_endpoint_architecture(
      2, {{0.0, 0.0}, {2.0, 0.0}});
  RichH0Problem moving_endpoint;
  moving_endpoint.n_atoms = 2;
  moving_endpoint.current_points = {{0.0, 0.0}, {2.0, 0.0}};
  moving_endpoint.participants = {0, 1};
  moving_endpoint.gate_domains = {{
      {10, 0, 1, {1.0, 0.0}, {0.5, 0.0}},
      {11, 0, 1, {0.0, 10.0}, {2.0, 10.0}},
  }};
  moving_endpoint.matched_gate_genes = {0};
  const auto endpoint_safe = solve_rich_h0(
      moving_endpoint_architecture, moving_endpoint, exact_config(),
      rng_fixture());
  assert(endpoint_safe.winner.feasible);
  assert(endpoint_safe.gate_option_indices == std::vector<std::size_t>({1}));
  moving_endpoint.gate_domains.front().resize(1);
  const auto endpoint_blocked = solve_rich_h0(
      moving_endpoint_architecture, moving_endpoint, exact_config(),
      rng_fixture());
  assert(!endpoint_blocked.winner.feasible);
  assert(endpoint_blocked.current_ghost_rejections == 1);
  ++tests;

  problem = one_resident_problem();
  problem.forced_return_mask = {true};
  problem.forecast_terms = {
      {2, RichForecastKind::kReturn, RichForecastCategory::kReentry,
       0, -1, -1, 0.5},
      {8, RichForecastKind::kConstant, RichForecastCategory::kTerminal,
       -1, -1, -1, 10.0},
  };
  auto decay_config = exact_config();
  decay_config.max_horizon = 8;
  decay_config.alpha_lookahead = 0.2;
  decay_config.decay_rho = 0.5;
  decay_config.decay_epsilon = 0.05;
  const auto decay = solve_rich_h0(
      small_architecture, problem, decay_config, rng_fixture());
  assert(std::abs(decay.forecast_nll - 0.05) < 1e-15);
  assert(std::abs(decay.search_negative_log_fidelity -
                  decay.winner.negative_log_fidelity - 0.05) < 1e-15);
  assert(decay.stats.forecast_terms_applied > 0);
  assert(decay.stats.forecast_terms_skipped_cutoff > 0);
  ++tests;

  ArchitectureSnapshot enum_architecture(
      2, {{0.0, 0.0}, {1.0, 0.0}});
  RichH0Problem enum_problem;
  enum_problem.n_atoms = 2;
  enum_problem.current_points = {{0.0, 0.0}, {1.0, 0.0}};
  enum_problem.participants = {0, 1};
  std::vector<RichGateOption> enum_domain;
  for (std::size_t option = 0; option < 256; ++option) {
    enum_domain.push_back({static_cast<std::int64_t>(1000 + option),
                           0, 1, {0.0, 0.0}, {1.0, 0.0}});
  }
  enum_problem.gate_domains = {std::move(enum_domain)};
  enum_problem.matched_gate_genes = {0};
  auto enum_config = exact_config();
  enum_config.direct_enumeration_limit = 512;
  enum_config.max_unique_evaluations = 256;
  const auto enumerated = solve_rich_h0(
      enum_architecture, enum_problem, enum_config, rng_fixture());
  assert(enumerated.search_mode == "enumerate");
  assert(enumerated.stats.unique_evaluations == 256);
  assert(enumerated.winner.chromosome == std::vector<std::int64_t>({0}));
  ++tests;

  RichH0Problem pruned_problem;
  pruned_problem.n_atoms = 2;
  pruned_problem.current_points = {{0.0, 0.0}, {1.0, 0.0}};
  pruned_problem.participants = {0, 1};
  pruned_problem.gate_domains = {{
      {20, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
      {21, 0, 1, {100.0, 100.0}, {101.0, 100.0}},
      {22, 0, 1, {200.0, 200.0}, {201.0, 200.0}},
  }};
  pruned_problem.matched_gate_genes = {0};
  auto pruned_config = exact_config();
  pruned_config.direct_enumeration_limit = 512;
  const auto pruned = solve_rich_h0(
      enum_architecture, pruned_problem, pruned_config, rng_fixture());
  assert(pruned.search_mode == "enumerate");
  assert(pruned.winner.chromosome == std::vector<std::int64_t>({0}));
  assert(pruned.stats.unique_evaluations == 3);
  assert(pruned.stats.direct_lower_bound_prunes == 2);
  ++tests;

  constexpr std::size_t kAtoms = 9;
  std::vector<Point> coordinates;
  for (std::size_t atom = 0; atom < kAtoms; ++atom) {
    coordinates.push_back({static_cast<double>(atom), 0.0});
  }
  for (std::size_t site = 0; site < 5; ++site) {
    coordinates.push_back({static_cast<double>(site + 4), 4.0});
  }
  ArchitectureSnapshot ga_architecture(kAtoms, coordinates, {9, 10, 11, 12, 13});
  RichH0Problem ga;
  ga.n_atoms = kAtoms;
  ga.current_points.assign(coordinates.begin(), coordinates.begin() + kAtoms);
  ga.participants = {0, 1, 2, 3};
  for (std::size_t gate = 0; gate < 2; ++gate) {
    std::vector<RichGateOption> domain;
    for (std::size_t option = 0; option < 10; ++option) {
      const auto q1 = static_cast<std::int64_t>(gate * 2);
      const auto q2 = q1 + 1;
      domain.push_back({static_cast<std::int64_t>(100 + gate * 20 + option),
                        q1, q2, coordinates[q1], coordinates[q2]});
    }
    ga.gate_domains.push_back(std::move(domain));
  }
  ga.eligible = {4, 5, 6, 7, 8};
  ga.eviction_order_indices = {0, 1, 2, 3, 4};
  ga.forced_return_mask = {false, false, false, false, false};
  for (std::size_t index = 0; index < ga.eligible.size(); ++index) {
    ga.return_domains.push_back({{
        static_cast<std::int64_t>(9 + index), coordinates[9 + index], 1.0}});
  }
  ga.matched_gate_genes = {0, 0};
  auto tuned_config = exact_config();
  tuned_config.operator_profile = RichOperatorProfile::kTuned;
  tuned_config.population_size = 6;
  tuned_config.iterations = 1;
  tuned_config.neighbor_sample_size = 16;
  tuned_config.neighbors_per_solution = 2;
  tuned_config.early_stop_patience = 2;
  tuned_config.max_unique_evaluations = 256;
  tuned_config.crossover_rate = 0.5;
  tuned_config.local_polish_sweeps = 2;
  std::vector<std::int64_t> cached(7, 1);
  const auto tuned = solve_rich_h0(
      ga_architecture, ga, tuned_config, rng_fixture(), cached);
  assert(tuned.stats.cached_winner_elites == 1);
  assert(tuned.stats.unique_evaluations <= 256);
  assert(tuned.stats.generations > 0 && !tuned.stats.early_stop_reason.empty());
  assert(tuned.stats.gate_mutations > 0);
  assert(tuned.stats.residency_mutations > 0);
  assert(tuned.stats.high_cost_gate_reselections > 0);
  assert(tuned.stats.conflict_cluster_swaps > 0);
  assert(tuned.stats.marginal_return_flips > 0);
  assert(tuned.stats.crossovers > 0);
  assert(tuned.stats.local_polish_evaluations > 0);
  ++tests;

  const auto repeated = solve_rich_h0(
      ga_architecture, ga, tuned_config, rng_fixture(), cached);
  assert(tuned.winner.chromosome == repeated.winner.chromosome);
  assert(tuned.return_assignments == repeated.return_assignments);
  assert(tuned.rng_state.words == repeated.rng_state.words &&
         tuned.rng_state.index == repeated.rng_state.index);
  ++tests;

  auto uncached_config = tuned_config;
  uncached_config.fitness_cache = false;
  const auto uncached = solve_rich_h0(
      ga_architecture, ga, uncached_config, rng_fixture(), cached);
  assert(tuned.winner.chromosome == uncached.winner.chromosome);
  assert(tuned.winner.feasible == uncached.winner.feasible);
  assert(tuned.winner.negative_log_fidelity ==
         uncached.winner.negative_log_fidelity);
  assert(tuned.winner.move_batches == uncached.winner.move_batches);
  assert(tuned.winner.move_time_us == uncached.winner.move_time_us);
  assert(tuned.winner.total_distance_um ==
         uncached.winner.total_distance_um);
  assert(tuned.winner.phase_batches == uncached.winner.phase_batches);
  assert(tuned.gate_option_indices == uncached.gate_option_indices);
  assert(tuned.return_assignments == uncached.return_assignments);
  assert(tuned.reseat_assignments == uncached.reseat_assignments);
  assert(tuned.rng_state.words == uncached.rng_state.words &&
         tuned.rng_state.index == uncached.rng_state.index);
  ++tests;

  std::cout << "rich_solver_tests: " << tests << " sections ok\n";
  return 0;
}
