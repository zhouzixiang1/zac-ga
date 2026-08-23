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

  candidate.phases[0].ghosts = {{9, {1.0, 0.0}}};
  BoundaryConfig ghost_config;
  ghost_config.enforce_single_leg_ghost = true;
  assert(!evaluate_candidate(architecture, candidate, ghost_config).feasible);
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
