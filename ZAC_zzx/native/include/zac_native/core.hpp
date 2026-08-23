#pragma once

#include "zac_native/types.hpp"

#include <array>
#include <cstdint>
#include <vector>

namespace zac_native {

bool compatible_2d(const std::array<double, 4>& first,
                   const std::array<double, 4>& second) noexcept;

std::vector<std::int64_t> ghost_hit_atoms(const std::vector<Leg>& legs,
                                          const std::vector<Ghost>& ghosts);

std::vector<std::vector<std::size_t>> color_phase(
    const MovementPhase& phase, std::size_t exact_threshold = 0);

FitnessResult evaluate_candidate(const ArchitectureSnapshot& architecture,
                                 const CandidatePlan& candidate,
                                 const BoundaryConfig& config);

// Search kernels compare millions of candidates but only the final winner
// needs its explicit batch membership.  This variant computes the identical
// physical objective while omitting the nested phase-batch payload.
FitnessResult evaluate_candidate_summary(
    const ArchitectureSnapshot& architecture, const CandidatePlan& candidate,
    const BoundaryConfig& config);

bool objective_less(const FitnessResult& first,
                    const FitnessResult& second) noexcept;

}  // namespace zac_native
