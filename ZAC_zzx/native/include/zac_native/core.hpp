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

// Exact colored replay with mover source/target precedence.  Unlike
// ``color_phase`` this rejects an execution for which no ghost-safe batch
// order exists and returns batches in executable order.
std::vector<std::vector<std::size_t>> replay_phase_batches_strict(
    const MovementPhase& phase, std::size_t exact_threshold = 0);

// Timing landmarks of the exact row-activation expansion used by the formal
// ZAC router for one already-colored movement batch.  Site dependencies use
// activation_finish_offset_us - deactivation_offset_us, so exposing both keeps
// the candidate scheduler and emitted router on one duration definition.
struct ExpandedBatchTiming {
  double duration_us{};
  double activation_finish_offset_us{};
  double deactivation_offset_us{};
};

ExpandedBatchTiming expanded_batch_timing(
    const std::vector<Leg>& legs,
    const std::vector<std::size_t>& members);

FitnessResult evaluate_candidate(const ArchitectureSnapshot& architecture,
                                 const CandidatePlan& candidate,
                                 const BoundaryConfig& config,
                                 const std::vector<double>&
                                     prior_idle_time_us = {});

// Search kernels compare millions of candidates but only the final winner
// needs its explicit batch membership.  This variant computes the identical
// physical objective while omitting the nested phase-batch payload.
FitnessResult evaluate_candidate_summary(
    const ArchitectureSnapshot& architecture, const CandidatePlan& candidate,
    const BoundaryConfig& config,
    const std::vector<double>& prior_idle_time_us = {});

// Exact incremental NLL used by ABI7 and the final trace scorer:
// sum_q log(1-t_q/T2) - log(1-(t_q+dt_q)/T2).  Returns +inf outside the
// linear model domain and rejects malformed vectors.
double linear_coherence_delta_nll(
    const std::vector<double>& prior_idle_time_us,
    const std::vector<double>& candidate_idle_time_us);

bool objective_less(const FitnessResult& first,
                    const FitnessResult& second) noexcept;

}  // namespace zac_native
