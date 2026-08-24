#pragma once

#include "zac_native/python_random.hpp"
#include "zac_native/types.hpp"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace zac_native {

struct RichGateOption {
  std::int64_t site_id{};
  std::int64_t q1{};
  std::int64_t q2{};
  Point target1;
  Point target2;
  std::vector<Leg> legs;
  std::vector<std::int64_t> owners;
  std::vector<Ghost> seated_ghosts;
  // Appended to preserve the source-fixture aggregate initializer layout.
  std::int64_t target1_site_id{-1};
  std::int64_t target2_site_id{-1};
};

struct RichReturnOption {
  std::int64_t site_id{};
  Point point;
  double cost{};
};

enum class RichDecisionPolicy : std::uint8_t {
  kOptimize = 0,
  kAlwaysStay = 1,
  kAlwaysReturn = 2,
  kAdjacentOnly = 3,
};

enum class RichOperatorProfile : std::uint8_t {
  // Byte-for-byte migration target for the registered Python GA.
  kExact = 0,
  // Structural search improvements.  This profile is never selected implicitly.
  kTuned = 1,
};

enum class RichForecastKind : std::uint8_t {
  kConstant = 0,
  kStay = 1,
  kReturn = 2,
  kReturnSite = 3,
  kGateOption = 4,
  kStayPair = 5,
  kReturnPair = 6,
};

enum class RichForecastCategory : std::uint8_t {
  kResidency = 0,
  kReentry = 1,
  kTerminal = 2,
  kRouting = 3,
};

struct RichForecastTerm {
  std::size_t depth{};
  RichForecastKind kind{RichForecastKind::kConstant};
  RichForecastCategory category{RichForecastCategory::kResidency};
  std::int64_t index{-1};
  std::int64_t second_index{-1};
  std::int64_t selector{-1};
  double nll{};
};

struct RichFutureLayer {
  std::size_t depth{};
  std::vector<std::pair<std::int64_t, std::int64_t>> gates;
};

struct RichH0Problem {
  std::size_t n_atoms{};
  std::vector<Point> current_points;
  std::vector<std::int64_t> current_site_ids;
  // ABI7: accumulated scorer coherence-idle time on entry to this boundary.
  // The binding accepts an empty vector only for non-formal source fixtures.
  std::vector<double> prior_idle_time_us;
  // ABI7: compact absolute scheduler state after the fixed source
  // out/CZ/parent-1Q prefix and before the candidate source-back phase.
  bool exact_current_scheduler{false};
  double scheduler_trace_end_us{};
  std::vector<double> scheduler_active_union_us;
  std::vector<double> scheduler_aod_end_us;
  double scheduler_one_qubit_end_us{};
  std::vector<double> scheduler_rydberg_end_us;
  std::vector<double> scheduler_qubit_dependency_end_us;
  std::vector<double> scheduler_back_dependency_end_us;
  std::vector<std::int64_t> scheduler_site_dependency_site_ids;
  std::vector<double> scheduler_site_dependency_activation_finish_us;
  std::vector<std::int64_t> target_one_qubit_atoms;
  double scheduler_one_qubit_duration_us{52.0};
  double scheduler_rydberg_duration_us{0.36};
  double scheduler_one_qubit_common_us{};
  double scheduler_transfer_duration_us{15.0};
  double scheduler_accel_um_per_us2{0.00275};
  double coherence_t2_us{1.5e6};
  bool enforce_frozen_physical_model{false};
  std::vector<std::int64_t> participants;
  std::vector<std::vector<RichGateOption>> gate_domains;
  std::vector<Ghost> static_ghosts;
  std::vector<std::int64_t> eligible;
  std::size_t min_returns{};
  std::vector<std::size_t> eviction_order_indices;
  std::vector<bool> forced_return_mask;
  std::vector<bool> recommended_return_mask;
  std::vector<std::vector<RichReturnOption>> return_domains;
  std::vector<std::int64_t> occupied_storage_site_ids;
  std::vector<std::int64_t> matched_gate_genes;
  RichDecisionPolicy decision_policy{RichDecisionPolicy::kOptimize};
  std::vector<RichForecastTerm> forecast_terms;
  // ABI7 formal path: raw bounded 2Q layers are rolled out and physically
  // scored inside C++.  ``forecast_terms`` remains only for legacy regression
  // fixtures and must not be mixed with this representation.
  std::vector<RichFutureLayer> future_layers;
  // The target layer is the final circuit layer. This carries no future gate
  // content and suppresses a fictitious post-circuit Bellman cleanup.
  bool terminal_boundary{false};
};

struct RichSearchConfig {
  RichOperatorProfile operator_profile{RichOperatorProfile::kExact};
  std::size_t population_size{6};
  std::size_t iterations{8};
  std::size_t neighbors_per_solution{2};
  std::size_t neighbor_sample_size{24};
  std::size_t elite_count{1};
  std::size_t early_stop_patience{};
  std::size_t max_unique_evaluations{};
  std::size_t direct_enumeration_limit{512};
  double crossover_rate{0.25};
  std::size_t local_polish_sweeps{1};
  std::size_t return_candidate_limit{6};
  std::size_t return_assignment_k{4};
  std::size_t forecast_gate_candidate_budget{1};
  std::size_t exact_coloring_threshold{};
  bool enforce_single_leg_ghost{true};
  bool fitness_cache{true};
  std::string forecast_mode{"decay"};
  std::string forecast_policy{"physical_terminal_decay_v1"};
  std::string decay_kind{"geometric"};
  std::size_t max_horizon{};
  double alpha_lookahead{0.1};
  double decay_rho{0.6};
  double decay_epsilon{0.05};
};

struct RichSearchStats {
  std::size_t evaluations{};
  std::size_t unique_evaluations{};
  std::size_t deterministic_unique_evaluations{};
  std::size_t stochastic_unique_evaluations{};
  std::size_t fitness_hits{};
  std::size_t decode_hits{};
  std::size_t return_match_hits{};
  std::size_t generations{};
  bool early_stopped{};
  std::size_t stochastic_budget{};
  std::string early_stop_reason{"not-started"};
  std::size_t gate_mutations{};
  std::size_t residency_mutations{};
  std::size_t high_cost_gate_reselections{};
  std::size_t conflict_cluster_swaps{};
  std::size_t marginal_return_flips{};
  std::size_t cached_winner_elites{};
  std::size_t crossovers{};
  std::size_t local_polish_evaluations{};
  std::size_t direct_lower_bound_prunes{};
  std::size_t return_assignment_evaluated{};
  std::size_t current_ghost_rejections{};
  std::size_t pre_score_reseats{};
  std::size_t pre_score_participant_parkings{};
  std::size_t forecast_terms_applied{};
  std::size_t forecast_terms_skipped_cutoff{};
  std::size_t forecast_state_cache_hits{};
};

struct RichSolveResult {
  FitnessResult winner;
  std::vector<std::size_t> gate_option_indices;
  std::vector<std::pair<std::int64_t, std::int64_t>> return_assignments;
  std::vector<std::pair<std::int64_t, std::int64_t>> reseat_assignments;
  std::vector<std::pair<std::int64_t, std::int64_t>>
      participant_parking_assignments;
  PythonRandomState rng_state;
  std::string search_mode;
  RichOperatorProfile operator_profile{RichOperatorProfile::kExact};
  RichSearchStats stats;
  double forecast_nll{};
  double search_negative_log_fidelity{};
  std::vector<double> forecast_by_depth;
  double forecast_residency_nll{};
  double forecast_reentry_nll{};
  double forecast_terminal_nll{};
  double forecast_routing_nll{};
  std::size_t return_assignment_rank{};
  std::size_t return_assignment_evaluated{};
  std::size_t current_ghost_rejections{};
  std::size_t pre_score_reseats{};
  std::size_t pre_score_participant_parkings{};
  std::vector<std::int64_t> current_gate_anchor;
  std::vector<std::int64_t> current_gate_anchor_assignment_site_ids;
  std::vector<std::int64_t> current_gate_final_assignment_site_ids;
  std::string current_gate_guard_branch{"inactive"};
  std::size_t current_gate_guard_cohort_size{};
  std::size_t current_gate_guard_admitted_size{};
  std::string current_gate_projection_source{"inactive"};
  std::size_t current_gate_projection_evaluated{};
  std::int64_t normalize_ns{};
  std::int64_t decode_ns{};
  std::int64_t return_match_ns{};
  std::int64_t fitness_ns{};
  std::int64_t selection_ns{};
  std::int64_t forecast_ns{};
  std::int64_t search_ns{};
};

RichSolveResult solve_rich_h0(
    const ArchitectureSnapshot& architecture, const RichH0Problem& problem,
    const RichSearchConfig& config, PythonRandomState rng_state,
    const std::optional<std::vector<std::int64_t>>& cached_winner = std::nullopt);

}  // namespace zac_native
