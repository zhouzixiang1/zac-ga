#include "zac_native/rich_solver.hpp"

#include "zac_native/core.hpp"

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

FitnessResult score_general_single_leg(
    const ArchitectureSnapshot& architecture, std::int64_t owner,
    const Point& source, const Point& target,
    const std::vector<Point>& positions) {
  MovementPhase phase;
  phase.legs = {{std::hypot(source.x - target.x, source.y - target.y),
                 source, target}};
  phase.owners = {owner};
  for (std::size_t atom = 0; atom < positions.size(); ++atom) {
    phase.ghosts.push_back(
        {static_cast<std::int64_t>(atom), positions[atom]});
  }
  CandidatePlan candidate;
  candidate.phases = {std::move(phase)};
  BoundaryConfig config;
  config.enforce_single_leg_ghost = true;
  return evaluate_candidate_summary(architecture, candidate, config);
}

RichSolveResult solve_single_blocker_forecast(
    const ArchitectureSnapshot& architecture,
    const std::vector<Point>& current_points) {
  RichH0Problem problem;
  problem.n_atoms = current_points.size();
  problem.current_points = current_points;
  problem.future_layers = {{1, {{0, 1}}}};
  auto config = exact_config();
  config.max_horizon = 1;
  config.alpha_lookahead = 1.0;
  config.decay_rho = 1.0;
  config.decay_epsilon = 0.0;
  config.forecast_gate_candidate_budget = 1;
  config.enforce_single_leg_ghost = true;
  return solve_rich_h0(architecture, problem, config, rng_fixture());
}
}  // namespace

int main() {
  std::size_t tests = 0;
  ArchitectureSnapshot small_architecture(
      3, {{0.0, 0.0}, {1.0, 0.0}, {2.0, 2.0}, {2.0, 3.0}}, {3});

  // The public rich solve has exactly one future blocker relocation.  Its
  // routing category is therefore the allocation-free single-leg scorer,
  // subsequently replayed by the generic physical phase.  The generic result
  // is the differential oracle for all physical fields; the public result
  // exposes their aggregate NLL without adding a production test hook.
  ArchitectureSnapshot safe_single_leg_architecture(
      3,
      {{0.0, 0.0}, {1.0, 0.0}, {0.0, 4.0}, {5.0, 0.0},
       {8.0, 8.0}},
      {2, 3, 4}, {{0, 1}});
  const Point single_leg_source{0.0, 0.0};
  const Point safe_single_leg_target{0.0, 4.0};
  const std::vector<Point> safe_positions{
      {-10.0, -10.0}, {10.0, 10.0}, single_leg_source};
  const auto safe_general = score_general_single_leg(
      safe_single_leg_architecture, 2, single_leg_source,
      safe_single_leg_target, safe_positions);
  const auto safe_forecast = solve_single_blocker_forecast(
      safe_single_leg_architecture, safe_positions);
  const auto safe_expected_time =
      30.0 + std::sqrt(4.0 / 0.00275);
  const auto safe_expected_transfer_nll = -2.0 * std::log(0.999);
  const auto safe_expected_coherence_nll =
      -2.0 * std::log1p(-safe_expected_time / 1.5e6) -
      std::log1p(-(safe_expected_time - 30.0) / 1.5e6);
  assert(safe_general.feasible);
  assert(safe_general.move_batches == 1);
  assert(safe_general.move_time_us == safe_expected_time);
  assert(safe_general.total_distance_um == 4.0);
  assert(safe_general.transfers == 2);
  assert(safe_general.transfer_nll == safe_expected_transfer_nll);
  assert(safe_general.idle_excitation_nll == 0.0);
  assert(safe_general.coherence_nll == safe_expected_coherence_nll);
  assert(safe_general.negative_log_fidelity ==
         safe_general.transfer_nll + safe_general.coherence_nll);
  assert(safe_forecast.winner.feasible);
  assert(safe_forecast.forecast_routing_nll ==
         safe_general.negative_log_fidelity);
  ++tests;

  // The nearest storage leg is hit at equal x/y trajectory time.  The fast
  // scorer must reject it, continue to the next safe storage site, and produce
  // the exact same routing NLL as a generic one-leg physical phase there.
  const std::vector<Point> blocked_positions{
      {0.0, 2.0}, {10.0, 10.0}, single_leg_source};
  const auto blocked_general = score_general_single_leg(
      safe_single_leg_architecture, 2, single_leg_source,
      safe_single_leg_target, blocked_positions);
  const Point alternate_target{5.0, 0.0};
  const auto alternate_general = score_general_single_leg(
      safe_single_leg_architecture, 2, single_leg_source,
      alternate_target, blocked_positions);
  const auto ghost_avoiding_forecast = solve_single_blocker_forecast(
      safe_single_leg_architecture, blocked_positions);
  assert(!blocked_general.feasible);
  assert(std::isinf(blocked_general.negative_log_fidelity));
  assert(alternate_general.feasible);
  assert(alternate_general.move_batches == 1);
  assert(alternate_general.move_time_us ==
         30.0 + std::sqrt(5.0 / 0.00275));
  assert(alternate_general.total_distance_um == 5.0);
  assert(alternate_general.transfers == 2);
  assert(alternate_general.negative_log_fidelity >
         safe_general.negative_log_fidelity);
  assert(ghost_avoiding_forecast.winner.feasible);
  assert(ghost_avoiding_forecast.forecast_routing_nll ==
         alternate_general.negative_log_fidelity);
  ++tests;

  // A QFT-like future front can have a cyclic source/target precedence even
  // though its endpoints are legal.  The bounded rollout must execute real
  // parking/reentry legs with transfer and coherence cost, rather than turn
  // the physically recoverable forecast into infinity.
  ArchitectureSnapshot qft_interlock_architecture(
      4,
      {{0.0, 0.0}, {2.0, 0.0}, {0.0, 4.0}, {2.0, 4.0},
       {-6.0, -6.0}, {6.0, -6.0}, {-6.0, 10.0}, {6.0, 10.0},
       {0.0, -8.0}, {2.0, -8.0}},
      {4, 5, 6, 7, 8, 9}, {{0, 1}, {2, 3}});
  RichH0Problem qft_interlock;
  qft_interlock.n_atoms = 4;
  qft_interlock.current_points = {
      {4.0, 3.0}, {0.0, 2.0}, {3.0, 4.0}, {0.0, 4.0}};
  qft_interlock.future_layers = {{1, {{0, 1}, {2, 3}}}};
  qft_interlock.prior_idle_time_us = {0.0, 0.0, 0.0, 0.0};
  auto qft_interlock_config = exact_config();
  qft_interlock_config.max_horizon = 1;
  qft_interlock_config.alpha_lookahead = 1.0;
  qft_interlock_config.decay_rho = 1.0;
  qft_interlock_config.decay_epsilon = 0.0;
  qft_interlock_config.forecast_gate_candidate_budget = 4;
  const auto qft_recovered = solve_rich_h0(
      qft_interlock_architecture, qft_interlock,
      qft_interlock_config, rng_fixture());
  assert(qft_recovered.winner.feasible);
  assert(std::isfinite(qft_recovered.forecast_nll));
  assert(std::isfinite(qft_recovered.forecast_reentry_nll));
  assert(qft_recovered.forecast_reentry_nll > 0.0);
  assert(std::abs(qft_recovered.forecast_reentry_nll -
                  0.013098101931857835) < 1e-12);
  // Every endpoint-zone atom belongs to the final visible gate, so there is
  // no non-participant resident for the Bellman cleanup potential.  The old
  // per-layer terminal RETURN was both speculative and double-counted here.
  assert(qft_recovered.forecast_terminal_nll == 0.0);
  assert(std::abs(qft_recovered.forecast_nll -
                  qft_recovered.forecast_reentry_nll) < 1e-12);
  ++tests;

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

  // Two target-layer participants already occupy their selected zero-length
  // gate seats and block the straight out legs of their partners (the same
  // q5/q6 and q93/q94 pattern seen in a dense Ising boundary).  Candidate
  // evaluation must park both blockers at real storage sites in phase 0 and
  // re-enter them in phase 1, with all six physical legs scored.
  ArchitectureSnapshot participant_parking_architecture(
      4, {{0.0, 0.0}, {2.0, 0.0}, {10.0, 0.0}, {12.0, 0.0},
          {2.0, -4.0}, {12.0, -4.0}}, {4, 5});
  RichH0Problem participant_parking;
  participant_parking.n_atoms = 4;
  participant_parking.current_points = {
      {0.0, 0.0}, {2.0, 0.0}, {10.0, 0.0}, {12.0, 0.0}};
  participant_parking.participants = {0, 1, 2, 3};
  participant_parking.gate_domains = {
      {{80, 0, 1, {4.0, 0.0}, {2.0, 0.0}}},
      {{81, 2, 3, {14.0, 0.0}, {12.0, 0.0}}},
  };
  participant_parking.matched_gate_genes = {0, 0};
  const auto participant_parked = solve_rich_h0(
      participant_parking_architecture, participant_parking,
      exact_config(), rng_fixture());
  const std::vector<std::pair<std::int64_t, std::int64_t>>
      expected_participant_parkings{{1, 4}, {3, 5}};
  assert(participant_parked.winner.feasible);
  assert(participant_parked.participant_parking_assignments ==
         expected_participant_parkings);
  assert(participant_parked.pre_score_participant_parkings == 2);
  assert(participant_parked.stats.pre_score_participant_parkings >= 2);
  assert(participant_parked.winner.transfers == 12);
  assert(participant_parked.winner.phase_batches.size() == 2);
  ++tests;

  // With no real storage endpoint the same boundary remains explicitly
  // infeasible; parking must never become an unscored hidden waypoint.
  ArchitectureSnapshot no_participant_parking_architecture(
      4, {{0.0, 0.0}, {2.0, 0.0}, {10.0, 0.0}, {12.0, 0.0},
          {2.0, -4.0}, {12.0, -4.0}});
  const auto participant_parking_exhausted = solve_rich_h0(
      no_participant_parking_architecture, participant_parking,
      exact_config(), rng_fixture());
  assert(!participant_parking_exhausted.winner.feasible);
  assert(participant_parking_exhausted.participant_parking_assignments.empty());
  assert(participant_parking_exhausted.winner.error ==
         "unresolved current single-leg ghost hit");
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

  ArchitectureSnapshot indexed_forecast_architecture(
      4, {{0.0, 0.0}, {1.0, 0.0}, {2.0, 0.0}, {3.0, 0.0},
          {0.0, 2.0}, {1.0, 2.0}}, {4, 5});
  RichH0Problem indexed_forecast;
  indexed_forecast.n_atoms = 4;
  indexed_forecast.current_points = {
      {0.0, 0.0}, {1.0, 0.0}, {2.0, 0.0}, {3.0, 0.0}};
  indexed_forecast.participants = {0, 1};
  indexed_forecast.gate_domains = {{
      {10, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
      {11, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
  }};
  indexed_forecast.eligible = {2, 3};
  indexed_forecast.min_returns = 2;
  indexed_forecast.eviction_order_indices = {0, 1};
  indexed_forecast.forced_return_mask = {true, true};
  indexed_forecast.return_domains = {
      {{4, {0.0, 2.0}, 1.0}, {5, {1.0, 2.0}, 10.0}},
      {{4, {0.0, 2.0}, 10.0}, {5, {1.0, 2.0}, 1.0}},
  };
  indexed_forecast.matched_gate_genes = {0};
  indexed_forecast.forecast_terms = {
      {1, RichForecastKind::kConstant, RichForecastCategory::kTerminal,
       -1, -1, -1, 0.1},
      {1, RichForecastKind::kReturn, RichForecastCategory::kReentry,
       0, -1, -1, 0.2},
      {1, RichForecastKind::kReturn, RichForecastCategory::kReentry,
       1, -1, -1, 0.3},
      {1, RichForecastKind::kStay, RichForecastCategory::kResidency,
       0, -1, -1, 100.0},
      {2, RichForecastKind::kReturnSite, RichForecastCategory::kRouting,
       0, -1, 4, 0.3},
      {2, RichForecastKind::kReturnSite, RichForecastCategory::kRouting,
       1, -1, 5, 0.4},
      {3, RichForecastKind::kGateOption, RichForecastCategory::kResidency,
       0, -1, 0, 0.4},
      {3, RichForecastKind::kGateOption, RichForecastCategory::kResidency,
       0, -1, 1, 0.1},
      {4, RichForecastKind::kReturnPair, RichForecastCategory::kRouting,
       0, 1, -1, 0.6},
      {4, RichForecastKind::kStayPair, RichForecastCategory::kRouting,
       0, 1, -1, 100.0},
      {8, RichForecastKind::kConstant, RichForecastCategory::kTerminal,
       -1, -1, -1, 100.0},
  };
  auto indexed_forecast_config = exact_config();
  indexed_forecast_config.max_horizon = 8;
  indexed_forecast_config.alpha_lookahead = 0.2;
  indexed_forecast_config.decay_rho = 0.5;
  indexed_forecast_config.decay_epsilon = 0.05;
  const auto indexed_forecast_value = solve_rich_h0(
      indexed_forecast_architecture, indexed_forecast,
      indexed_forecast_config, rng_fixture());
  assert(indexed_forecast_value.gate_option_indices ==
         std::vector<std::size_t>({1}));
  const std::vector<std::pair<std::int64_t, std::int64_t>>
      indexed_forecast_returns{{2, 4}, {3, 5}};
  // The swapped assignment has a smaller raw forecast (0.14), but its current
  // NLL, Move batches and Move time are all dominated by the nearest physical
  // assignment.  The residency Pareto guard therefore retains the nearest
  // assignment and its complete 0.21 forecast.
  assert(indexed_forecast_value.return_assignments ==
         indexed_forecast_returns);
  assert(std::abs(indexed_forecast_value.forecast_nll - 0.21) < 1e-15);
  const std::vector<double> expected_forecast_by_depth{
      0.0, 0.12, 0.07, 0.005, 0.015, 0.0, 0.0, 0.0, 0.0};
  assert(indexed_forecast_value.forecast_by_depth.size() ==
         expected_forecast_by_depth.size());
  for (std::size_t depth = 0; depth < expected_forecast_by_depth.size();
       ++depth) {
    assert(std::abs(indexed_forecast_value.forecast_by_depth[depth] -
                    expected_forecast_by_depth[depth]) < 1e-15);
  }
  assert(std::abs(indexed_forecast_value.forecast_residency_nll - 0.005) <
         1e-15);
  assert(std::abs(indexed_forecast_value.forecast_reentry_nll - 0.10) <
         1e-15);
  assert(std::abs(indexed_forecast_value.forecast_terminal_nll - 0.02) <
         1e-15);
  assert(std::abs(indexed_forecast_value.forecast_routing_nll - 0.085) <
         1e-15);
  assert(indexed_forecast_value.current_gate_guard_branch ==
         "residency-pareto-envelope");
  assert(indexed_forecast_value.current_gate_guard_admitted_size == 2);
  assert(indexed_forecast_value.stats.forecast_terms_skipped_cutoff > 0);
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

  RichH0Problem ordered_prune_problem;
  ordered_prune_problem.n_atoms = 2;
  ordered_prune_problem.current_points = {{0.0, 0.0}, {1.0, 0.0}};
  ordered_prune_problem.participants = {0, 1};
  ordered_prune_problem.gate_domains = {{
      {30, 0, 1, {100.0, 100.0}, {101.0, 100.0}},
      {31, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
      {32, 0, 1, {200.0, 200.0}, {201.0, 200.0}},
  }};
  ordered_prune_problem.matched_gate_genes = {0};
  const auto ordered_rng = rng_fixture();
  const auto ordered_prune = solve_rich_h0(
      enum_architecture, ordered_prune_problem, pruned_config, ordered_rng);
  assert(ordered_prune.search_mode == "enumerate");
  assert(ordered_prune.winner.chromosome ==
         std::vector<std::int64_t>({1}));
  assert(ordered_prune.stats.unique_evaluations == 3);
  assert(ordered_prune.stats.deterministic_unique_evaluations == 3);
  assert(ordered_prune.stats.direct_lower_bound_prunes == 2);
  assert(ordered_prune.rng_state.words == ordered_rng.words &&
         ordered_prune.rng_state.index == ordered_rng.index);
  ++tests;

  ArchitectureSnapshot exact_current_architecture(
      3, {{0.0, 0.0}, {1.0, 0.0}, {2.0, 0.0},
          {3.0, 0.0}, {2.0, 2.0}}, {4});
  RichH0Problem exact_current_problem;
  exact_current_problem.n_atoms = 3;
  exact_current_problem.current_points = {
      {0.0, 0.0}, {1.0, 0.0}, {2.0, 0.0}};
  exact_current_problem.participants = {0, 1};
  exact_current_problem.gate_domains = {{
      {40, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
      // Its optimistic bound contains only the q1 move.  Exact current
      // replay must first RESEAT atom 2 away from q1's target, making this
      // chromosome strictly worse than the incumbent before any forecast.
      {41, 0, 1, {0.0, 0.0}, {2.0, 0.0}},
  }};
  exact_current_problem.eligible = {2};
  exact_current_problem.eviction_order_indices = {0};
  exact_current_problem.forced_return_mask = {false};
  exact_current_problem.return_domains = {{{4, {2.0, 2.0}, 1.0}}};
  exact_current_problem.matched_gate_genes = {0};
  exact_current_problem.decision_policy = RichDecisionPolicy::kAlwaysStay;
  exact_current_problem.forecast_terms = {{
      1, RichForecastKind::kGateOption,
      RichForecastCategory::kRouting, 0, -1, 0, 0.0022}};
  auto exact_current_config = exact_config();
  exact_current_config.direct_enumeration_limit = 512;
  exact_current_config.max_horizon = 1;
  exact_current_config.alpha_lookahead = 1.0;
  exact_current_config.decay_rho = 1.0;
  exact_current_config.decay_epsilon = 0.0;
  const auto exact_current_prune = solve_rich_h0(
      exact_current_architecture, exact_current_problem,
      exact_current_config, rng_fixture());
  assert(exact_current_prune.search_mode == "enumerate");
  assert(exact_current_prune.winner.chromosome ==
         std::vector<std::int64_t>({0, 0}));
  assert(exact_current_prune.stats.unique_evaluations == 2);
  assert(exact_current_prune.stats.direct_lower_bound_prunes == 0);
  // Only the incumbent reaches the direct-search forecast; option 1 passes
  // the cheap bound but is rejected by its exact current physical score.  The
  // current-gate guard then replays the incumbent once for its independent
  // cohort audit, so the public aggregate counter is two.
  assert(exact_current_prune.stats.forecast_terms_applied == 2);
  ++tests;

  RichH0Problem exact_current_tie;
  exact_current_tie.n_atoms = 2;
  exact_current_tie.current_points = {{0.0, 0.0}, {1.0, 0.0}};
  exact_current_tie.participants = {0, 1};
  exact_current_tie.gate_domains = {{
      {50, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
      {51, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
  }};
  exact_current_tie.matched_gate_genes = {0};
  exact_current_tie.forecast_terms = {{
      1, RichForecastKind::kConstant,
      RichForecastCategory::kRouting, -1, -1, -1, 0.0}};
  const auto exact_current_tied = solve_rich_h0(
      enum_architecture, exact_current_tie,
      exact_current_config, rng_fixture());
  assert(exact_current_tied.winner.chromosome ==
         std::vector<std::int64_t>({0}));
  // Equality is not pruned: both candidates receive their complete direct
  // forecast and the canonical chromosome remains the tie-break winner.  The
  // independent two-value gate-guard cohort replay raises the aggregate
  // application counter from two to four.
  assert(exact_current_tied.stats.forecast_terms_applied == 4);
  ++tests;

  // P1 gate projection: with no residency decision, decay lookahead may not
  // purchase worse executable current NLL/batches/Move time merely to improve
  // a future gate-option term.  Option 1 wins the unguarded sum, but option 0
  // is the strict current-physics anchor and must be projected back.
  ArchitectureSnapshot gate_guard_architecture(
      2, {{0.0, 0.0}, {1.0, 0.0}});
  RichH0Problem strict_gate_guard;
  strict_gate_guard.n_atoms = 2;
  strict_gate_guard.current_points = {{0.0, 0.0}, {1.0, 0.0}};
  strict_gate_guard.participants = {0, 1};
  strict_gate_guard.gate_domains = {{
      {60, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
      {61, 0, 1, {0.0, 1.0}, {1.0, 1.0}},
  }};
  strict_gate_guard.matched_gate_genes = {0};
  strict_gate_guard.forecast_terms = {{
      1, RichForecastKind::kGateOption,
      RichForecastCategory::kRouting, 0, -1, 0, 0.10}};
  auto gate_guard_config = exact_config();
  gate_guard_config.max_horizon = 1;
  gate_guard_config.alpha_lookahead = 1.0;
  gate_guard_config.decay_rho = 1.0;
  gate_guard_config.decay_epsilon = 0.0;
  const auto strict_guarded = solve_rich_h0(
      gate_guard_architecture, strict_gate_guard,
      gate_guard_config, rng_fixture());
  assert(strict_guarded.gate_option_indices ==
         std::vector<std::size_t>({0}));
  assert(strict_guarded.winner.chromosome ==
         std::vector<std::int64_t>({0}));
  assert(strict_guarded.winner.negative_log_fidelity == 0.0);
  assert(strict_guarded.forecast_nll == 0.10);
  assert(strict_guarded.winner.phase_batches.size() == 2);
  ++tests;

  // Exact current ties remain forecast-visible.  The strict eligible=0 guard
  // is therefore a safety projection, not a blanket ban on gate lookahead.
  auto tied_gate_guard = strict_gate_guard;
  tied_gate_guard.gate_domains.front()[1].target1 = {0.0, 0.0};
  tied_gate_guard.gate_domains.front()[1].target2 = {1.0, 0.0};
  const auto tied_guarded = solve_rich_h0(
      gate_guard_architecture, tied_gate_guard,
      gate_guard_config, rng_fixture());
  assert(tied_guarded.gate_option_indices ==
         std::vector<std::size_t>({1}));
  assert(tied_guarded.winner.chromosome ==
         std::vector<std::int64_t>({1}));
  assert(tied_guarded.winner.negative_log_fidelity == 0.0);
  assert(tied_guarded.forecast_nll == 0.0);
  ++tests;

  // The gate guard fixes only the normalized residency suffix.  Here future
  // terms intentionally select RETURN, while the projected gate changes from
  // the forecast-attractive moving option back to the current-safe option.
  ArchitectureSnapshot suffix_guard_architecture(
      3, {{0.0, 0.0}, {1.0, 0.0}, {4.0, 4.0}, {4.0, 5.0}}, {3});
  RichH0Problem suffix_guard;
  suffix_guard.n_atoms = 3;
  suffix_guard.current_points = {
      {0.0, 0.0}, {1.0, 0.0}, {4.0, 4.0}};
  suffix_guard.participants = {0, 1};
  suffix_guard.gate_domains = {{
      {70, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
      {71, 0, 1, {0.0, 1.0}, {1.0, 1.0}},
  }};
  suffix_guard.eligible = {2};
  suffix_guard.eviction_order_indices = {0};
  suffix_guard.forced_return_mask = {false};
  suffix_guard.return_domains = {{{3, {4.0, 5.0}, 1.0}}};
  suffix_guard.matched_gate_genes = {0};
  suffix_guard.forecast_terms = {
      {1, RichForecastKind::kGateOption,
       RichForecastCategory::kRouting, 0, -1, 0, 0.10},
      {1, RichForecastKind::kStay,
       RichForecastCategory::kResidency, 0, -1, -1, 0.20},
  };
  const auto suffix_guarded = solve_rich_h0(
      suffix_guard_architecture, suffix_guard,
      gate_guard_config, rng_fixture());
  assert(suffix_guarded.winner.chromosome ==
         std::vector<std::int64_t>({0, 1}));
  assert(suffix_guarded.gate_option_indices ==
         std::vector<std::size_t>({0}));
  const std::vector<std::pair<std::int64_t, std::int64_t>>
      suffix_guard_return{{2, 3}};
  assert(suffix_guarded.return_assignments == suffix_guard_return);
  ++tests;

  // A current Pareto tradeoff with exactly one extra moving atom (two
  // transfers) remains admissible when residency exists.  The higher-transfer
  // option has lower Move time, and forecast may legitimately choose it.
  ArchitectureSnapshot transfer_envelope_architecture(
      3, {{0.0, 0.0}, {100.0, 0.0}, {200.0, 200.0},
          {200.0, 201.0}}, {3});
  RichH0Problem transfer_envelope;
  transfer_envelope.n_atoms = 3;
  transfer_envelope.current_points = {
      {0.0, 0.0}, {100.0, 0.0}, {200.0, 200.0}};
  transfer_envelope.participants = {0, 1};
  transfer_envelope.gate_domains = {{
      {80, 0, 1, {0.0, 0.0}, {100.0, 10.0}},
      {81, 0, 1, {0.0, 1.0}, {100.0, 1.0}},
  }};
  transfer_envelope.eligible = {2};
  transfer_envelope.eviction_order_indices = {0};
  transfer_envelope.forced_return_mask = {false};
  transfer_envelope.return_domains = {{{3, {200.0, 201.0}, 1.0}}};
  transfer_envelope.matched_gate_genes = {0};
  transfer_envelope.decision_policy = RichDecisionPolicy::kAlwaysStay;
  transfer_envelope.forecast_terms = {{
      1, RichForecastKind::kGateOption,
      RichForecastCategory::kRouting, 0, -1, 0, 0.10}};
  const auto transfer_allowed = solve_rich_h0(
      transfer_envelope_architecture, transfer_envelope,
      gate_guard_config, rng_fixture());
  assert(transfer_allowed.gate_option_indices ==
         std::vector<std::size_t>({1}));
  assert(transfer_allowed.winner.transfers == 4);
  assert(transfer_allowed.winner.move_time_us <
         30.0 + std::sqrt(10.0 / 0.00275));

  auto transfer_uncached_config = gate_guard_config;
  transfer_uncached_config.fitness_cache = false;
  const auto transfer_allowed_uncached = solve_rich_h0(
      transfer_envelope_architecture, transfer_envelope,
      transfer_uncached_config, rng_fixture());
  assert(transfer_allowed.winner.chromosome ==
         transfer_allowed_uncached.winner.chromosome);
  assert(transfer_allowed.gate_option_indices ==
         transfer_allowed_uncached.gate_option_indices);
  assert(transfer_allowed.rng_state.words ==
             transfer_allowed_uncached.rng_state.words &&
         transfer_allowed.rng_state.index ==
             transfer_allowed_uncached.rng_state.index);
  ++tests;

  // Pareto status alone is insufficient: this template saves one batch but
  // spends one extra load+store pair plus a very long trajectory.  Its current
  // NLL exceeds the anchor by more than -2log(0.999), so the physical NLL
  // envelope rejects it even though the transfer-count cap alone would admit.
  ArchitectureSnapshot nll_envelope_architecture(
      5, {{0.0, 0.0}, {0.0, 20.0}, {0.0, 10.0},
          {0.0, 30.0}, {2000.0, 2000.0}, {2000.0, 2001.0}}, {5});
  RichH0Problem nll_envelope;
  nll_envelope.n_atoms = 5;
  nll_envelope.current_points = {
      {0.0, 0.0}, {0.0, 20.0}, {0.0, 10.0},
      {0.0, 30.0}, {2000.0, 2000.0}};
  nll_envelope.participants = {0, 1, 2, 3};
  nll_envelope.gate_domains = {
      {
          {84, 0, 1, {10.0, 10.0}, {0.0, 20.0}},
          {85, 0, 1, {1000.0, 0.0}, {1000.0, 20.0}},
      },
      {
          {86, 2, 3, {10.0, 0.0}, {0.0, 30.0}},
          {87, 2, 3, {1000.0, 10.0}, {0.0, 30.0}},
      },
  };
  nll_envelope.eligible = {4};
  nll_envelope.eviction_order_indices = {0};
  nll_envelope.forced_return_mask = {false};
  nll_envelope.return_domains = {{{5, {2000.0, 2001.0}, 1.0}}};
  nll_envelope.matched_gate_genes = {0, 0};
  nll_envelope.decision_policy = RichDecisionPolicy::kAlwaysStay;
  nll_envelope.forecast_terms = {
      {1, RichForecastKind::kGateOption,
       RichForecastCategory::kRouting, 0, -1, 0, 0.05},
      {1, RichForecastKind::kGateOption,
       RichForecastCategory::kRouting, 1, -1, 0, 0.05},
  };
  const auto nll_capped = solve_rich_h0(
      nll_envelope_architecture, nll_envelope,
      gate_guard_config, rng_fixture());
  assert(nll_capped.gate_option_indices ==
         std::vector<std::size_t>({0, 0}));
  assert(nll_capped.winner.transfers == 4);
  assert(nll_capped.winner.move_batches == 2);
  ++tests;

  // Across a wide gate prefix, the all-short template is current-Pareto but
  // adds two movers (four transfers) relative to the physical anchor.  The
  // explicit +2-transfer envelope rejects it; mixed templates are dominated.
  ArchitectureSnapshot transfer_cap_architecture(
      5, {{0.0, 0.0}, {100.0, 0.0}, {0.0, 100.0},
          {100.0, 100.0}, {200.0, 200.0}, {200.0, 201.0}}, {5});
  RichH0Problem transfer_cap;
  transfer_cap.n_atoms = 5;
  transfer_cap.current_points = {
      {0.0, 0.0}, {100.0, 0.0}, {0.0, 100.0},
      {100.0, 100.0}, {200.0, 200.0}};
  transfer_cap.participants = {0, 1, 2, 3};
  transfer_cap.gate_domains = {
      {
          {90, 0, 1, {0.0, 0.0}, {100.0, 10.0}},
          {91, 0, 1, {0.0, 1.0}, {100.0, 1.0}},
      },
      {
          {92, 2, 3, {0.0, 100.0}, {100.0, 110.0}},
          {93, 2, 3, {0.0, 101.0}, {100.0, 101.0}},
      },
  };
  transfer_cap.eligible = {4};
  transfer_cap.eviction_order_indices = {0};
  transfer_cap.forced_return_mask = {false};
  transfer_cap.return_domains = {{{5, {200.0, 201.0}, 1.0}}};
  transfer_cap.matched_gate_genes = {0, 0};
  transfer_cap.decision_policy = RichDecisionPolicy::kAlwaysStay;
  transfer_cap.forecast_terms = {
      {1, RichForecastKind::kGateOption,
       RichForecastCategory::kRouting, 0, -1, 0, 0.05},
      {1, RichForecastKind::kGateOption,
       RichForecastCategory::kRouting, 1, -1, 0, 0.05},
  };
  const auto transfer_capped = solve_rich_h0(
      transfer_cap_architecture, transfer_cap,
      gate_guard_config, rng_fixture());
  assert(transfer_capped.gate_option_indices ==
         std::vector<std::size_t>({0, 0}));
  assert(transfer_capped.winner.transfers == 4);
  ++tests;

  // A forecast-attractive gate with an exact current single-leg ghost hit is
  // never archived or resurrected by the final projection.
  ArchitectureSnapshot guard_ghost_architecture(
      3, {{0.0, 0.0}, {1.0, 0.0}, {0.0, 0.5}});
  RichH0Problem guard_ghost;
  guard_ghost.n_atoms = 3;
  guard_ghost.current_points = {
      {0.0, 0.0}, {1.0, 0.0}, {0.0, 0.5}};
  guard_ghost.participants = {0, 1};
  guard_ghost.gate_domains = {{
      {100, 0, 1, {0.0, 0.0}, {1.0, 0.0}},
      {101, 0, 1, {0.0, 1.0}, {1.0, 0.0}},
  }};
  guard_ghost.matched_gate_genes = {0};
  guard_ghost.forecast_terms = {{
      1, RichForecastKind::kGateOption,
      RichForecastCategory::kRouting, 0, -1, 0, 0.10}};
  const auto guard_ghost_safe = solve_rich_h0(
      guard_ghost_architecture, guard_ghost,
      gate_guard_config, rng_fixture());
  assert(guard_ghost_safe.winner.feasible);
  assert(guard_ghost_safe.gate_option_indices ==
         std::vector<std::size_t>({0}));
  ++tests;

  // H=0 clears the forecast contract before validation and therefore follows
  // the original current-only solver without consuming RNG in the gate guard.
  auto h0_gate_guard = strict_gate_guard;
  h0_gate_guard.forecast_terms.clear();
  auto h0_gate_config = gate_guard_config;
  h0_gate_config.max_horizon = 0;
  const auto h0_rng = rng_fixture();
  const auto h0_gate_value = solve_rich_h0(
      gate_guard_architecture, h0_gate_guard, h0_gate_config, h0_rng);
  assert(h0_gate_value.gate_option_indices ==
         std::vector<std::size_t>({0}));
  assert(h0_gate_value.rng_state.words == h0_rng.words &&
         h0_gate_value.rng_state.index == h0_rng.index);
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
  // All ten options of each gate share identical current geometry.  Their
  // future costs differ, so the GA lazy top-k path must forecast every exact
  // current-NLL tie before it can rank the neighbor/generation pool.
  for (std::size_t gate = 0; gate < ga.gate_domains.size(); ++gate) {
    for (std::size_t option = 0; option < ga.gate_domains[gate].size();
         ++option) {
      ga.forecast_terms.push_back({
          1, RichForecastKind::kGateOption,
          RichForecastCategory::kRouting,
          static_cast<std::int64_t>(gate), -1,
          static_cast<std::int64_t>(option),
          static_cast<double>(9 - option) * 1e-4});
    }
  }
  auto tuned_config = exact_config();
  tuned_config.operator_profile = RichOperatorProfile::kTuned;
  tuned_config.population_size = 6;
  tuned_config.iterations = 3;
  tuned_config.neighbor_sample_size = 16;
  tuned_config.neighbors_per_solution = 2;
  tuned_config.early_stop_patience = 2;
  tuned_config.max_unique_evaluations = 256;
  tuned_config.crossover_rate = 0.5;
  tuned_config.local_polish_sweeps = 2;
  tuned_config.max_horizon = 1;
  tuned_config.alpha_lookahead = 1.0;
  tuned_config.decay_rho = 1.0;
  tuned_config.decay_epsilon = 0.0;
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
  // The cache-off execution retains the old complete-score path.  Matching
  // search counters and final RNG state across multiple GA pools is a public
  // differential check that lazy top-k retained every generation's ranking.
  assert(tuned.stats.evaluations == uncached.stats.evaluations);
  assert(tuned.stats.unique_evaluations ==
         uncached.stats.unique_evaluations);
  assert(tuned.stats.deterministic_unique_evaluations ==
         uncached.stats.deterministic_unique_evaluations);
  assert(tuned.stats.stochastic_unique_evaluations ==
         uncached.stats.stochastic_unique_evaluations);
  assert(tuned.stats.generations == uncached.stats.generations);
  assert(tuned.stats.early_stopped == uncached.stats.early_stopped);
  assert(tuned.stats.early_stop_reason == uncached.stats.early_stop_reason);
  assert(tuned.stats.gate_mutations == uncached.stats.gate_mutations);
  assert(tuned.stats.residency_mutations ==
         uncached.stats.residency_mutations);
  assert(tuned.stats.high_cost_gate_reselections ==
         uncached.stats.high_cost_gate_reselections);
  assert(tuned.stats.conflict_cluster_swaps ==
         uncached.stats.conflict_cluster_swaps);
  assert(tuned.stats.marginal_return_flips ==
         uncached.stats.marginal_return_flips);
  assert(tuned.stats.crossovers == uncached.stats.crossovers);
  assert(tuned.stats.local_polish_evaluations ==
         uncached.stats.local_polish_evaluations);
  assert(tuned.stats.forecast_terms_applied <
         uncached.stats.forecast_terms_applied);
  ++tests;

  std::cout << "rich_solver_tests: " << tests << " sections ok\n";
  return 0;
}
