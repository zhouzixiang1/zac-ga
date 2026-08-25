#include "zac_native/core.hpp"
#include "zac_native/python_random.hpp"

#include <cassert>
#include <cmath>
#include <iostream>
#include <stdexcept>

using namespace zac_native;

namespace {
PythonRandomState rng_fixture() {
  PythonRandomState state;
  for (std::size_t index = 0; index < state.words.size(); ++index) {
    state.words[index] = static_cast<std::uint32_t>(
        0x9e3779b9U * static_cast<std::uint32_t>(index + 1));
  }
  state.index = 624;
  return state;
}
}  // namespace

int main() {
  std::size_t tests = 0;
  assert(compatible_2d({0.0, 1.0, 0.0, 1.0},
                       {2.0, 3.0, 2.0, 3.0}));
  assert(!compatible_2d({0.0, 2.0, 0.0, 2.0},
                        {1.0, 1.0, 1.0, 1.0}));
  assert(!compatible_2d({0.0, 2.0, 0.0, 2.0},
                        {2.0, 0.0, 2.0, 0.0}));
  ++tests;

  Leg diagonal{std::sqrt(8.0), {0.0, 0.0}, {2.0, 2.0}};
  assert(ghost_hit_atoms({diagonal}, {{7, {1.0, 1.0}}}) ==
         std::vector<std::int64_t>{7});
  assert(ghost_hit_atoms({diagonal}, {{8, {3.0, 3.0}}}).empty());
  ++tests;

  MovementPhase conflicting;
  conflicting.legs = {
      {std::sqrt(8.0), {0.0, 0.0}, {2.0, 2.0}},
      {std::sqrt(8.0), {2.0, 2.0}, {0.0, 0.0}},
  };
  assert(color_phase(conflicting).size() == 2);
  assert(color_phase(conflicting, 8).size() == 2);
  conflicting.batching = "greedy";
  assert(color_phase(conflicting).size() == 2);
  ++tests;

  ArchitectureSnapshot architecture(4, {{0.0, 0.0}, {2.0, 0.0}});
  CandidatePlan candidate;
  candidate.chromosome = {0, 1};
  candidate.phases = {{{{2.0, {0.0, 0.0}, {2.0, 0.0}}}, {}, {0}, "phase"}};
  candidate.idle_exposures = 1;
  const auto score = evaluate_candidate(architecture, candidate, {});
  assert(score.feasible && score.transfers == 2 && score.move_batches == 1);
  assert(std::abs(score.negative_log_fidelity -
                  (score.transfer_nll + score.idle_excitation_nll +
                   score.coherence_nll)) < 1e-15);
  ++tests;

  // Reaching T2 makes the final linear score OOD, but native candidate search
  // must remain feasible and rank this boundary with the exponential
  // sensitivity increment instead of terminating the compiler.
  const std::vector<double> ood_prior{1.5e6 - 1.0, 10.0, 20.0, 30.0};
  const auto continued_score = evaluate_candidate(
      architecture, candidate, {}, ood_prior);
  assert(continued_score.feasible);
  double expected_ood_coherence = 0.0;
  for (const auto delta : continued_score.candidate_idle_time_us) {
    expected_ood_coherence += delta / 1.5e6;
  }
  assert(std::abs(continued_score.coherence_nll -
                  expected_ood_coherence) < 1e-15);
  ++tests;

  ArchitectureSnapshot nearest_architecture(
      2,
      {{0.0, 0.0}, {4.0, 0.0}, {1.0, 0.0}, {0.0, 2.0}, {3.0, 0.0},
       {-1.0, 0.0}},
      {1, 2, 3, 4, 5});
  const auto& all_nearest =
      nearest_architecture.storage_site_ids_by_distance({0.0, 0.0});
  const auto& first_three =
      nearest_architecture.nearest_storage_site_ids({0.0, 0.0}, 3);
  assert(first_three == std::vector<std::int64_t>(
                            all_nearest.begin(), all_nearest.begin() + 3));
  assert(nearest_architecture.nearest_storage_site_ids({0.0, 0.0}, 3) ==
         first_three);
  assert(nearest_architecture.nearest_storage_site_ids({0.0, 0.0}, 99) ==
         all_nearest);
  ++tests;

  candidate.phases[0].ghosts = {{9, {1.0, 0.0}}};
  BoundaryConfig ghost_config;
  ghost_config.enforce_single_leg_ghost = true;
  assert(!evaluate_candidate(architecture, candidate, ghost_config).feasible);
  ++tests;

  CandidatePlan precedence;
  precedence.chromosome = {0};
  precedence.phases = {{{
      {2.0, {0.0, 0.0}, {2.0, 0.0}},
      {std::sqrt(181.0), {10.0, 10.0}, {1.0, 0.0}},
  }, {{1, {0.0, 0.0}}, {2, {10.0, 10.0}}}, {1, 2}, "phase"}};
  const auto precedence_score =
      evaluate_candidate(architecture, precedence, ghost_config);
  assert(precedence_score.feasible && precedence_score.move_batches == 2);
  assert(precedence_score.phase_batches.size() == 1);
  assert((precedence_score.phase_batches[0] ==
          std::vector<std::vector<std::size_t>>{{0}, {1}}));
  ++tests;

  // The ordinary three-round color/defer replay can commit a bad prefix for
  // this compact source-seat interlock.  Fresh endpoint precedence first
  // selects owner 0 and then {2, 1}; the latter is expanded-unsafe and must be
  // split, with every singleton physically audited in stable member order.
  ArchitectureSnapshot production_fallback_architecture(3, {});
  CandidatePlan production_fallback;
  production_fallback.chromosome = {0};
  production_fallback.phases = {{{
      {4.0, {2.0, 3.0}, {6.0, 3.0}},
      {2.0, {1.0, 0.0}, {3.0, 0.0}},
      {std::sqrt(18.0), {2.0, 6.0}, {5.0, 3.0}},
  }, {{0, {2.0, 3.0}}, {1, {1.0, 0.0}}, {2, {2.0, 6.0}}},
      {0, 1, 2}, "phase"}};
  auto production_fallback_config = ghost_config;
  production_fallback_config.exact_coloring_threshold = 24;
  production_fallback_config.production_parking_replay = true;
  const auto production_fallback_score = evaluate_candidate(
      production_fallback_architecture, production_fallback,
      production_fallback_config);
  assert(production_fallback_score.feasible);
  assert(production_fallback_score.phase_batches.size() == 1);
  assert((production_fallback_score.phase_batches[0] ==
          std::vector<std::vector<std::size_t>>{{0}, {2}, {1}}));
  ++tests;

  // The strict fallback colors {1, 2} together after mover 0.  Their expanded
  // batch is unsafe, but blindly trying singleton 1 first is also unsafe while
  // owner 2 remains at (2, 3).  Exact deterministic singleton ordering must
  // backtrack to 0 -> 2 -> 1 instead of reporting a false ghost failure.
  ArchitectureSnapshot expanded_split_architecture(3, {});
  CandidatePlan expanded_split;
  expanded_split.chromosome = {0};
  expanded_split.phases = {{{
      {std::sqrt(10.0), {3.0, 1.0}, {0.0, 2.0}},
      {3.0, {2.0, 4.0}, {2.0, 1.0}},
      {3.0, {2.0, 3.0}, {2.0, 0.0}},
  }, {{0, {3.0, 1.0}}, {1, {2.0, 4.0}}, {2, {2.0, 3.0}}},
      {0, 1, 2}, "phase"}};
  const auto expanded_split_score = evaluate_candidate(
      expanded_split_architecture, expanded_split,
      production_fallback_config);
  assert(expanded_split_score.feasible);
  assert(expanded_split_score.phase_batches.size() == 1);
  assert((expanded_split_score.phase_batches[0] ==
          std::vector<std::vector<std::size_t>>{{0}, {2}, {1}}));
  ++tests;

  bool rejected = false;
  try {
    ArchitectureSnapshot invalid(1, {{0.0, 0.0}}, {0, 0});
  } catch (const std::invalid_argument&) {
    rejected = true;
  }
  assert(rejected);
  ++tests;

  PythonRandom first(rng_fixture());
  PythonRandom second(rng_fixture());
  for (std::size_t index = 0; index < 128; ++index) {
    assert(first.random() == second.random());
    assert(first.randbelow(37) == second.randbelow(37));
    assert(first.getrandbits(17) == second.getrandbits(17));
  }
  assert(first.state().words == second.state().words &&
         first.state().index == second.state().index);
  ++tests;

  std::cout << "zac_native_tests: " << tests << " sections ok\n";
  return 0;
}
