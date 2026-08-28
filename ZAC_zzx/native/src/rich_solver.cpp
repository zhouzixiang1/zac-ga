#include "zac_native/rich_solver.hpp"

#include "zac_native/core.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <limits>
#include <map>
#include <optional>
#include <queue>
#include <set>
#include <string>
#include <stdexcept>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace zac_native {
namespace {

using Clock = std::chrono::steady_clock;

constexpr double kFExc = 0.9975;
constexpr double kFTransfer = 0.999;
constexpr double kTransferUs = 15.0;
constexpr double kRydbergUs = 0.36;
constexpr double kOneQUs = 52.0;
constexpr double kAccelUmPerUs2 = 0.00275;
constexpr double kT2Us = 1.5e6;
// The bounded rollout is a heuristic, not an exact Bellman oracle. Requiring
// a fourfold predicted saving before spending executable current fidelity
// prevents long-distance placements from exploiting optimistic rollout error.
constexpr double kForecastSacrificeTrust = 0.25;
// A wide target layer already gives the GA a coupled gate placement.  Replaying
// every complete gate domain for every residency suffix turns the M4 safety
// guard into a second, much larger optimizer (20-gate Ising layers were
// spending minutes here).  Above this threshold the guard audits a bounded,
// deterministic neighbourhood around the GA winner and the ordinary matched
// placement.  The current gene, matched gene and the first few weight-ordered
// local candidates keep the physical anchor while avoiding a gate_count x
// full_zone_domain x suffix explosion.
constexpr std::size_t kParallelGuardGateThreshold = 8;
constexpr std::size_t kParallelGuardOptionLimit = 6;
// A long-depth profile is signalled by the registered exact-enumeration cap.
// Its layers are overwhelmingly one-to-three gates wide, so the M4 guard
// audits a stable six-option current-physics neighbourhood per gate rather
// than re-solving all 140 zone sites after every already-bounded GA call.  All
// sites remain in the search DTO and full-domain recovery still runs when the
// bounded cohort is current-infeasible.
constexpr std::size_t kLongDepthEnumerationLimit = 64;
constexpr std::size_t kLongDepthGuardOptionLimit = 6;

// The paper's linear coherence factor is undefined once any absolute idle
// time reaches T2.  That must make the *reported* linear fidelity OOD, but it
// must not make every chromosome at all later compiler boundaries
// infeasible.  The resident search therefore uses the exact linear log ratio
// while the whole candidate is in-domain and switches the whole comparison
// to the registered exponential sensitivity model after the first crossing.
// The independent trace scorer remains the authority for the final OOD flag.
double search_coherence_absolute_nll(
    const std::vector<double>& before, const std::vector<double>& after,
    double t2_us = kT2Us) {
  if (before.size() != after.size()) {
    throw std::invalid_argument("coherence vectors differ in length");
  }
  if (!std::isfinite(t2_us) || t2_us <= 0.0) {
    throw std::invalid_argument("coherence T2 must be finite and positive");
  }
  bool outside_linear_domain = false;
  for (std::size_t atom = 0; atom < before.size(); ++atom) {
    if (!std::isfinite(before[atom]) || before[atom] < 0.0 ||
        !std::isfinite(after[atom]) || after[atom] < 0.0) {
      throw std::invalid_argument(
          "absolute coherence idle times must be finite and non-negative");
    }
    outside_linear_domain = outside_linear_domain ||
                            before[atom] >= t2_us || after[atom] >= t2_us;
  }
  double total = 0.0;
  if (outside_linear_domain) {
    for (std::size_t atom = 0; atom < before.size(); ++atom) {
      total += (after[atom] - before[atom]) / t2_us;
    }
    return total;
  }
  for (std::size_t atom = 0; atom < before.size(); ++atom) {
    total += std::log1p(-before[atom] / t2_us) -
             std::log1p(-after[atom] / t2_us);
  }
  return total;
}

double search_coherence_delta_nll(
    const std::vector<double>& prior,
    const std::vector<double>& candidate_delta,
    double t2_us = kT2Us) {
  if (prior.size() != candidate_delta.size()) {
    throw std::invalid_argument("coherence vectors differ in length");
  }
  auto after = prior;
  for (std::size_t atom = 0; atom < prior.size(); ++atom) {
    if (!std::isfinite(candidate_delta[atom]) ||
        candidate_delta[atom] < -1e-12) {
      throw std::invalid_argument(
          "candidate idle-time delta must be finite and non-negative");
    }
    after[atom] += std::max(0.0, candidate_delta[atom]);
  }
  return search_coherence_absolute_nll(prior, after, t2_us);
}

template <typename T>
struct VectorHash {
  std::size_t operator()(const std::vector<T>& values) const noexcept {
    std::size_t seed = 0xcbf29ce484222325ULL;
    for (const auto& value : values) {
      const auto item = std::hash<T>{}(value);
      seed ^= item + 0x9e3779b97f4a7c15ULL + (seed << 6U) + (seed >> 2U);
    }
    return seed;
  }
};

std::int64_t elapsed_ns(Clock::time_point started) {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             Clock::now() - started)
      .count();
}

double point_distance(const Point& first, const Point& second) {
  return std::hypot(first.x - second.x, first.y - second.y);
}

ExpandedBatchTiming expanded_batch_timing_model(
    const std::vector<Leg>& legs, double transfer_us, double accel_um_per_us2) {
  if (legs.empty()) return {};
  std::map<double, std::vector<const Leg*>> rows;
  for (const auto& leg : legs) rows[leg.source.y].push_back(&leg);
  const auto row_count = rows.size();
  double duration = 0.0;
  if (legs.size() == 1) {
    duration = 2.0 * transfer_us +
               std::sqrt(point_distance(legs.front().source,
                                        legs.front().target) /
                         accel_um_per_us2);
  } else {
    duration = static_cast<double>(row_count + 1) * transfer_us;
    const auto parking_us =
        std::sqrt(std::sqrt(2.0) / accel_um_per_us2);
    if (row_count > 1) {
      duration += static_cast<double>(row_count - 1) * parking_us;
    }
    std::vector<std::pair<double, double>> row_moves;
    std::map<double, std::size_t> last_row_for_x;
    std::map<double, double> target_x_for_source;
    std::size_t row_index = 0;
    for (const auto& [source_y, row_legs] : rows) {
      row_moves.emplace_back(
          source_y + (row_index + 1 < row_count ? 1.0 : 0.0),
          row_legs.front()->target.y);
      for (const auto* leg : row_legs) {
        last_row_for_x[leg->source.x] = row_index;
        target_x_for_source.emplace(leg->source.x, leg->target.x);
      }
      ++row_index;
    }
    double longest = 0.0;
    for (const auto& [source_x, last_row] : last_row_for_x) {
      const auto column_begin =
          source_x + (last_row + 1 < row_count ? 1.0 : 0.0);
      const auto column_end = target_x_for_source.at(source_x);
      for (const auto& [row_begin, row_end] : row_moves) {
        longest = std::max(
            longest,
            std::hypot(column_end - column_begin, row_end - row_begin));
      }
    }
    duration += std::sqrt(longest / accel_um_per_us2);
  }
  const auto parking_us =
      std::sqrt(std::sqrt(2.0) / accel_um_per_us2);
  ExpandedBatchTiming result;
  result.duration_us = duration;
  result.activation_finish_offset_us =
      static_cast<double>(row_count) * transfer_us +
      static_cast<double>(row_count - 1) * parking_us;
  result.deactivation_offset_us = duration - transfer_us;
  return result;
}

bool same_point(const Point& first, const Point& second) {
  return first.x == second.x && first.y == second.y;
}

struct Coverage {
  bool valid{};
  bool always{};
  double time{};
};

Coverage cover(double begin, double end, double coordinate) {
  constexpr double kEps = 1e-9;
  if (std::abs(end - begin) < kEps) {
    const bool matches = std::abs(coordinate - begin) < kEps;
    return {matches, matches, 0.0};
  }
  const auto raw = (coordinate - begin) / (end - begin);
  if (raw < -kEps || raw > 1.0 + kEps) return {};
  return {true, false, std::clamp(raw, 0.0, 1.0)};
}

using Trajectory = std::pair<double, double>;

void beams(const std::vector<Leg>& legs, std::set<Trajectory>& columns,
           std::set<Trajectory>& rows) {
  for (const auto& leg : legs) {
    columns.emplace(leg.source.x, leg.target.x);
    rows.emplace(leg.source.y, leg.target.y);
  }
}

bool coverage_matches(const Coverage& first, const Coverage& second) {
  constexpr double kTolerance = 1e-6;
  return first.valid && second.valid &&
         (first.always || second.always ||
          std::abs(first.time - second.time) < kTolerance);
}

bool has_new_ghost_conflict(const std::vector<Leg>& existing,
                            const std::vector<Leg>& added,
                            const std::vector<Ghost>& ghosts) {
  if (added.empty() || ghosts.empty()) return false;
  std::set<Trajectory> existing_columns;
  std::set<Trajectory> existing_rows;
  std::set<Trajectory> added_columns;
  std::set<Trajectory> added_rows;
  beams(existing, existing_columns, existing_rows);
  beams(added, added_columns, added_rows);
  auto all_columns = existing_columns;
  auto all_rows = existing_rows;
  all_columns.insert(added_columns.begin(), added_columns.end());
  all_rows.insert(added_rows.begin(), added_rows.end());
  for (const auto& ghost : ghosts) {
    for (const auto& column : added_columns) {
      const auto x = cover(column.first, column.second, ghost.position.x);
      if (!x.valid) continue;
      for (const auto& row : all_rows) {
        if (coverage_matches(
                x, cover(row.first, row.second, ghost.position.y))) {
          return true;
        }
      }
    }
    for (const auto& row : added_rows) {
      const auto y = cover(row.first, row.second, ghost.position.y);
      if (!y.valid) continue;
      for (const auto& column : all_columns) {
        if (coverage_matches(
                cover(column.first, column.second, ghost.position.x), y)) {
          return true;
        }
      }
    }
  }
  return false;
}

FitnessResult infeasible_fitness(const std::vector<std::int64_t>& chromosome,
                                 std::string error) {
  const auto infinity = std::numeric_limits<double>::infinity();
  FitnessResult result;
  result.chromosome = chromosome;
  result.feasible = false;
  result.negative_log_fidelity = infinity;
  result.transfer_nll = infinity;
  result.idle_excitation_nll = infinity;
  result.coherence_nll = infinity;
  result.error = std::move(error);
  return result;
}

std::size_t positive_mod(std::int64_t value, std::size_t domain) {
  const auto signed_domain = static_cast<std::int64_t>(domain);
  auto result = value % signed_domain;
  if (result < 0) result += signed_domain;
  return static_cast<std::size_t>(result);
}

std::size_t rounded_index(std::size_t numerator, std::size_t denominator) {
  const auto quotient = numerator / denominator;
  const auto remainder = numerator % denominator;
  if (remainder * 2 < denominator) return quotient;
  if (remainder * 2 > denominator) return quotient + 1;
  return quotient % 2 == 0 ? quotient : quotient + 1;
}

struct DecodeResult {
  bool feasible{true};
  std::vector<std::size_t> option_indices;
  std::string error;
};

struct ReturnAssignment {
  std::size_t eligible_index{};
  std::int64_t site_id{};
  Point point;
};

struct ParticipantParkingAssignment {
  std::int64_t atom{};
  std::int64_t site_id{};
  Point point;
};

struct Evaluated {
  FitnessResult fitness;
  DecodeResult decoded;
  std::vector<ReturnAssignment> assignments;
  std::vector<ReturnAssignment> reseats;
  std::vector<ParticipantParkingAssignment> participant_parkings;
  double forecast_nll{};
  double search_nll{};
  std::vector<double> forecast_by_depth;
  std::array<double, 4> forecast_by_category{};
  std::vector<std::int64_t> assignment_key;
  std::size_t return_assignment_rank{};
  std::size_t return_assignment_evaluated{};
  std::size_t current_ghost_rejections{};
  std::size_t pre_score_reseats{};
  std::size_t pre_score_participant_parkings{};
  bool forecast_feasible{true};
  std::string forecast_error;
};

// Exact current-boundary replay for one chromosome.  ``current_candidates``
// contains every bounded RETURN assignment in canonical rank order, but none
// of them has consumed the (non-negative) future rollout yet.  This is an
// admissible lower-bound representation, never a publishable fitness value.
struct PreparedCurrent {
  bool complete{};
  Evaluated complete_value;
  std::vector<Evaluated> current_candidates;
  std::size_t ghost_rejections{};
  double lower_bound{std::numeric_limits<double>::infinity()};
};

struct ForecastReplay {
  double nll{};
  std::vector<double> by_depth;
  std::array<double, 4> by_category{};
  std::size_t terms_applied{};
  std::size_t terms_skipped_cutoff{};
};

std::uint64_t double_bits(double value) noexcept {
  std::uint64_t bits{};
  static_assert(sizeof(bits) == sizeof(value));
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

std::vector<std::uint64_t> forecast_state_key(
    const std::vector<Point>& positions,
    const std::vector<double>& accumulated_idle) {
  std::vector<std::uint64_t> key;
  key.reserve(positions.size() * 3);
  for (const auto& point : positions) {
    key.push_back(double_bits(point.x));
    key.push_back(double_bits(point.y));
  }
  for (const auto value : accumulated_idle) key.push_back(double_bits(value));
  return key;
}

struct PlanGeometry {
  std::vector<Point> positions_t1;
  std::vector<std::int64_t> site_ids_t1;
  std::vector<std::int64_t> final_site_ids;
  std::vector<Leg> back_legs;
  std::vector<std::int64_t> back_owners;
  std::vector<Leg> out_legs;
  std::vector<std::int64_t> out_owners;
  std::vector<Ghost> ghosts_t0;
  std::vector<Ghost> ghosts_t1;
  std::set<std::int64_t> blockers;
  std::set<std::int64_t> stationary_out_participant_blockers;
  std::size_t violations{};
  std::size_t ghost_violations{};
};

struct ReseatRepair {
  std::vector<ReturnAssignment> reseats;
  PlanGeometry geometry;
};

struct ParticipantParkingRepair {
  std::vector<ParticipantParkingAssignment> parkings;
  PlanGeometry geometry;
};

using ForecastBits = std::vector<std::uint64_t>;

struct CompiledForecastTerm {
  double contribution{};
  std::size_t depth{};
  std::size_t category{};
  bool active{};
};

void set_forecast_bit(ForecastBits& bits, std::size_t index) {
  bits[index / 64U] |= (std::uint64_t{1} << (index % 64U));
}

void merge_forecast_bits(ForecastBits& target, const ForecastBits& source) {
  if (target.size() != source.size()) {
    throw std::logic_error("forecast bitset size mismatch");
  }
  for (std::size_t word = 0; word < target.size(); ++word) {
    target[word] |= source[word];
  }
}

unsigned trailing_zeroes(std::uint64_t value) {
  unsigned result = 0;
  while ((value & std::uint64_t{1}) == 0U) {
    value >>= 1U;
    ++result;
  }
  return result;
}

std::int64_t objective_bucket(double value, double quantum) {
  if (!std::isfinite(value)) return std::numeric_limits<std::int64_t>::max();
  return static_cast<std::int64_t>(std::floor(value / quantum + 0.5));
}

bool evaluated_less(const Evaluated& first, const Evaluated& second) {
  const auto first_feasible = first.fitness.feasible &&
                              first.forecast_feasible &&
                              std::isfinite(first.search_nll);
  const auto second_feasible = second.fitness.feasible &&
                               second.forecast_feasible &&
                               std::isfinite(second.search_nll);
  if (first_feasible != second_feasible) {
    return first_feasible;
  }
  return std::make_tuple(
             objective_bucket(first.search_nll, 1e-12),
             first.fitness.move_batches,
             objective_bucket(first.fitness.move_time_us, 1e-6),
             objective_bucket(first.fitness.total_distance_um, 1e-6),
             first.fitness.chromosome, first.assignment_key) <
         std::make_tuple(
             objective_bucket(second.search_nll, 1e-12),
             second.fitness.move_batches,
             objective_bucket(second.fitness.move_time_us, 1e-6),
             objective_bucket(second.fitness.total_distance_um, 1e-6),
             second.fitness.chromosome, second.assignment_key);
}

auto current_physical_key(const Evaluated& value) {
  return std::make_tuple(
      objective_bucket(value.fitness.negative_log_fidelity, 1e-12),
      value.fitness.move_batches,
      objective_bucket(value.fitness.move_time_us, 1e-6),
      objective_bucket(value.fitness.total_distance_um, 1e-6),
      value.fitness.chromosome, value.assignment_key);
}

bool current_physical_less(const Evaluated& first, const Evaluated& second) {
  if (first.fitness.feasible != second.fitness.feasible) {
    return first.fitness.feasible;
  }
  return current_physical_key(first) < current_physical_key(second);
}

struct AssignmentSolution {
  std::vector<std::int64_t> site_ids;
  double cost{};
};

std::optional<std::vector<std::size_t>> hungarian_columns(
    const std::vector<std::vector<double>>& costs) {
  const auto rows = costs.size();
  if (rows == 0) return std::vector<std::size_t>{};
  const auto columns = costs.front().size();
  if (columns < rows) return std::nullopt;
  constexpr double kInfinity = 1e100;
  std::vector<double> u(rows + 1);
  std::vector<double> v(columns + 1);
  std::vector<std::size_t> p(columns + 1);
  std::vector<std::size_t> way(columns + 1);
  for (std::size_t row = 1; row <= rows; ++row) {
    p[0] = row;
    std::size_t column0 = 0;
    std::vector<double> min_value(columns + 1, kInfinity);
    std::vector<bool> used(columns + 1, false);
    do {
      used[column0] = true;
      const auto row0 = p[column0];
      double delta = kInfinity;
      std::size_t column1 = 0;
      for (std::size_t column = 1; column <= columns; ++column) {
        if (used[column]) continue;
        const auto current = costs[row0 - 1][column - 1] - u[row0] - v[column];
        if (current < min_value[column]) {
          min_value[column] = current;
          way[column] = column0;
        }
        if (min_value[column] < delta) {
          delta = min_value[column];
          column1 = column;
        }
      }
      if (delta >= kInfinity / 2 || column1 == 0) return std::nullopt;
      for (std::size_t column = 0; column <= columns; ++column) {
        if (used[column]) {
          u[p[column]] += delta;
          v[column] -= delta;
        } else {
          min_value[column] -= delta;
        }
      }
      column0 = column1;
    } while (p[column0] != 0);
    do {
      const auto column1 = way[column0];
      p[column0] = p[column1];
      column0 = column1;
    } while (column0 != 0);
  }
  std::vector<std::size_t> assignment(rows, columns);
  for (std::size_t column = 1; column <= columns; ++column) {
    if (p[column] != 0) assignment[p[column] - 1] = column - 1;
  }
  for (std::size_t row = 0; row < rows; ++row) {
    if (assignment[row] >= columns ||
        costs[row][assignment[row]] >= kInfinity / 2) {
      return std::nullopt;
    }
  }
  return assignment;
}

std::optional<AssignmentSolution> constrained_assignment(
    const std::vector<std::vector<RichReturnOption>>& domains,
    const std::vector<std::size_t>& returners,
    const std::map<std::size_t, std::int64_t>& fixed,
    const std::set<std::pair<std::size_t, std::int64_t>>& banned) {
  std::set<std::int64_t> site_set;
  for (const auto eligible_index : returners) {
    for (const auto& option : domains[eligible_index]) {
      site_set.insert(option.site_id);
    }
  }
  const std::vector<std::int64_t> sites(site_set.begin(), site_set.end());
  std::map<std::int64_t, std::size_t> site_index;
  for (std::size_t index = 0; index < sites.size(); ++index) {
    site_index[sites[index]] = index;
  }
  const auto rows = returners.size();
  const auto columns = sites.size();
  if (columns < rows) return std::nullopt;
  constexpr double kInfinity = 1e100;
  std::vector<std::vector<double>> costs(
      rows, std::vector<double>(columns, kInfinity));
  for (std::size_t row = 0; row < rows; ++row) {
    for (const auto& option : domains[returners[row]]) {
      auto& cost = costs[row][site_index.at(option.site_id)];
      cost = std::min(cost, option.cost);
    }
    for (std::size_t column = 0; column < columns; ++column) {
      if (banned.count({row, sites[column]}) != 0U) {
        costs[row][column] = kInfinity;
      }
    }
  }
  std::vector<std::int64_t> assignment(rows, -1);
  std::set<std::size_t> fixed_columns;
  double total_cost = 0.0;
  for (const auto& [row, site_id] : fixed) {
    const auto found = site_index.find(site_id);
    if (row >= rows || found == site_index.end() ||
        !fixed_columns.insert(found->second).second ||
        costs[row][found->second] >= kInfinity / 2) {
      return std::nullopt;
    }
    assignment[row] = site_id;
    total_cost += costs[row][found->second];
  }
  std::vector<std::size_t> free_rows;
  std::vector<std::size_t> free_columns;
  for (std::size_t row = 0; row < rows; ++row) {
    if (assignment[row] < 0) free_rows.push_back(row);
  }
  for (std::size_t column = 0; column < columns; ++column) {
    if (fixed_columns.count(column) == 0U) free_columns.push_back(column);
  }
  if (free_columns.size() < free_rows.size()) return std::nullopt;
  std::vector<std::vector<double>> reduced(
      free_rows.size(), std::vector<double>(free_columns.size(), kInfinity));
  for (std::size_t row = 0; row < free_rows.size(); ++row) {
    for (std::size_t column = 0; column < free_columns.size(); ++column) {
      reduced[row][column] = costs[free_rows[row]][free_columns[column]];
    }
  }
  const auto reduced_assignment = hungarian_columns(reduced);
  if (!reduced_assignment.has_value()) return std::nullopt;
  for (std::size_t row = 0; row < free_rows.size(); ++row) {
    const auto original_column = free_columns[(*reduced_assignment)[row]];
    assignment[free_rows[row]] = sites[original_column];
    total_cost += costs[free_rows[row]][original_column];
  }
  return AssignmentSolution{std::move(assignment), total_cost};
}

std::vector<AssignmentSolution> k_best_assignments(
    const std::vector<std::vector<RichReturnOption>>& domains,
    const std::vector<std::size_t>& returners, std::size_t limit) {
  if (returners.empty()) return {AssignmentSolution{{}, 0.0}};
  struct Node {
    std::size_t fixed_prefix{};
    std::map<std::size_t, std::int64_t> fixed;
    std::set<std::pair<std::size_t, std::int64_t>> banned;
    AssignmentSolution solution;
    std::size_t serial{};
  };
  struct Later {
    bool operator()(const Node& first, const Node& second) const {
      if (first.solution.cost != second.solution.cost) {
        return first.solution.cost > second.solution.cost;
      }
      if (first.solution.site_ids != second.solution.site_ids) {
        return first.solution.site_ids > second.solution.site_ids;
      }
      return first.serial > second.serial;
    }
  };
  const auto initial = constrained_assignment(domains, returners, {}, {});
  if (!initial.has_value()) return {};
  std::priority_queue<Node, std::vector<Node>, Later> queue;
  std::size_t serial = 0;
  queue.push({0, {}, {}, *initial, serial++});
  std::set<std::vector<std::int64_t>> seen;
  std::vector<AssignmentSolution> result;
  while (!queue.empty() && result.size() < limit) {
    auto node = queue.top();
    queue.pop();
    if (seen.insert(node.solution.site_ids).second) {
      result.push_back(node.solution);
    }
    for (std::size_t row = node.fixed_prefix; row < returners.size(); ++row) {
      auto fixed = node.fixed;
      for (std::size_t prefix = node.fixed_prefix; prefix < row; ++prefix) {
        fixed[prefix] = node.solution.site_ids[prefix];
      }
      auto banned = node.banned;
      banned.insert({row, node.solution.site_ids[row]});
      const auto solution = constrained_assignment(
          domains, returners, fixed, banned);
      if (solution.has_value()) {
        queue.push({row, std::move(fixed), std::move(banned), *solution,
                    serial++});
      }
    }
  }
  return result;
}

class RichSolver {
 public:
  RichSolver(const ArchitectureSnapshot& architecture, const RichH0Problem& problem,
             const RichSearchConfig& config, PythonRandomState rng_state)
      : architecture_(architecture), problem_(problem), config_(config),
        rng_(rng_state) {
    if (problem_.prior_idle_time_us.empty()) {
      problem_.prior_idle_time_us.assign(problem_.n_atoms, 0.0);
    }
    if (problem_.recommended_return_mask.empty()) {
      problem_.recommended_return_mask.assign(
          problem_.eligible.size(), false);
    }
    if (problem_.recommended_stay_mask.empty()) {
      problem_.recommended_stay_mask.assign(
          problem_.eligible.size(), false);
    }
    validate();
    participant_mask_.assign(problem_.n_atoms, false);
    for (const auto atom : problem_.participants) {
      participant_mask_[static_cast<std::size_t>(atom)] = true;
    }
    storage_site_ids_.insert(architecture_.storage_site_ids().begin(),
                             architecture_.storage_site_ids().end());
    for (const auto& pair : architecture_.entangling_site_pairs()) {
      const auto& first = architecture_.site_coordinates()[
          static_cast<std::size_t>(pair[0])];
      const auto& second = architecture_.site_coordinates()[
          static_cast<std::size_t>(pair[1])];
      zone_points_.insert({first.x, first.y});
      zone_points_.insert({second.x, second.y});
    }
    return_domains_ = problem_.return_domains;
    for (auto& domain : return_domains_) {
      std::sort(domain.begin(), domain.end(), [](const auto& first,
                                                 const auto& second) {
        return std::tie(first.cost, first.site_id) <
               std::tie(second.cost, second.site_id);
      });
      std::set<std::int64_t> seen;
      std::vector<RichReturnOption> limited;
      limited.reserve(std::min(config_.return_candidate_limit, domain.size()));
      for (const auto& option : domain) {
        if (!seen.insert(option.site_id).second) continue;
        limited.push_back(option);
        if (limited.size() == config_.return_candidate_limit) break;
      }
      domain = std::move(limited);
    }
    compile_forecast();
    compile_future_layers();
    stats_.stochastic_budget = config_.max_unique_evaluations;
  }

  RichSolveResult solve(
      const std::optional<std::vector<std::int64_t>>& cached_winner) {
    const auto search_started = Clock::now();
    std::vector<std::int64_t> winner;
    std::string search_mode;
    if (cached_winner.has_value() &&
        config_.operator_profile == RichOperatorProfile::kExact) {
      winner = normalize(*cached_winner);
      evaluate_normalized(winner, false);
      search_mode = "lru";
      stats_.early_stop_reason = "lru";
    } else {
      const auto direct_space = direct_search_space();
      if (direct_space <= config_.direct_enumeration_limit &&
          direct_space <= config_.max_unique_evaluations) {
        auto chromosomes = enumerate_chromosomes();
        auto scored = score_direct_exact(chromosomes);
        if (!scored.has_value()) {
          throw std::runtime_error("direct search has no candidate");
        }
        winner = scored->fitness.chromosome;
        search_mode = direct_space <= 1 ? "direct" : "enumerate";
        stats_.early_stop_reason = search_mode;
      } else {
        auto greedy = greedy_seed();
        if (config_.search_policy == "greedy_only") {
          winner = greedy;
          if (config_.operator_profile == RichOperatorProfile::kTuned &&
              config_.local_polish_sweeps != 0) {
            winner = local_polish(winner);
          }
          search_mode = "greedy-only";
          stats_.early_stop_reason = "greedy-only";
        } else {
          auto population = seed_population(greedy, cached_winner);
          auto scored = score_unique(population, false);
          if (scored.empty()) {
            throw std::runtime_error("initial population is empty");
          }
          if (scored.size() > config_.population_size) {
            scored.resize(config_.population_size);
          }
          auto prior_best = scored.front();
          std::size_t stale = 0;
          bool budget_stop = false;
          for (std::size_t generation = 0; generation < config_.iterations;
               ++generation) {
            std::vector<std::vector<std::int64_t>> offspring;
            for (const auto& parent : scored) {
              std::vector<std::vector<std::int64_t>> neighbors;
              neighbors.reserve(config_.neighbor_sample_size);
              for (std::size_t sample = 0;
                   sample < config_.neighbor_sample_size; ++sample) {
                if (config_.operator_profile == RichOperatorProfile::kTuned &&
                    scored.size() > 1 &&
                    rng_.random() < config_.crossover_rate) {
                  auto mate_index = rng_.randbelow(scored.size() - 1);
                  const auto parent_index = static_cast<std::size_t>(
                      &parent - scored.data());
                  if (mate_index >= parent_index) ++mate_index;
                  neighbors.push_back(partitioned_crossover(
                      parent.fitness.chromosome,
                      scored[mate_index].fitness.chromosome));
                } else {
                  neighbors.push_back(neighbor(parent.fitness.chromosome));
                }
              }
              auto pool = score_top_k_lazy(
                  neighbors, true, config_.neighbors_per_solution);
              const auto take =
                  std::min(config_.neighbors_per_solution, pool.size());
              for (std::size_t index = 0; index < take; ++index) {
                offspring.push_back(pool[index].fitness.chromosome);
              }
              if (stats_.unique_evaluations >= stats_.stochastic_budget) {
                budget_stop = true;
                break;
              }
            }
            std::vector<std::vector<std::int64_t>> next_raw;
            // The registered Python/reference GA carries every current parent.
            // The tuned profile deliberately uses the configured elite count.
            const auto elites =
                config_.operator_profile == RichOperatorProfile::kExact
                    ? scored.size()
                    : std::min(config_.elite_count, scored.size());
            for (std::size_t index = 0; index < elites; ++index) {
              next_raw.push_back(scored[index].fitness.chromosome);
            }
            next_raw.insert(next_raw.end(), offspring.begin(), offspring.end());
            // Keep already-evaluated, distinct parents available when offspring
            // collapse to duplicate chromosomes.  This preserves diversity
            // without spending additional fitness evaluations.
            next_raw.reserve(next_raw.size() + scored.size());
            for (const auto& parent : scored) {
              next_raw.push_back(parent.fitness.chromosome);
            }
            auto next = score_top_k_lazy(
                next_raw, false, config_.population_size);
            if (!next.empty()) scored = std::move(next);
            if (scored.size() > config_.population_size) {
              scored.resize(config_.population_size);
            }
            ++stats_.generations;
            if (evaluated_less(scored.front(), prior_best)) {
              prior_best = scored.front();
              stale = 0;
            } else {
              ++stale;
            }
            if (config_.early_stop_patience != 0 &&
                stale >= config_.early_stop_patience) {
              stats_.early_stopped = true;
              stats_.early_stop_reason = "patience";
              break;
            }
            if (budget_stop) {
              stats_.early_stop_reason = "unique-budget";
              break;
            }
          }
          winner = scored.front().fitness.chromosome;
          if (config_.operator_profile == RichOperatorProfile::kTuned &&
              config_.local_polish_sweeps != 0) {
            winner = local_polish(winner);
          }
          search_mode = (stats_.unique_evaluations >=
                                 stats_.stochastic_budget
                             ? "ga-budget"
                             : (stats_.early_stopped ? "ga-early-stop" : "ga"));
          if (stats_.early_stop_reason == "not-started") {
            stats_.early_stop_reason = "iterations-completed";
          }
        }
      }
    }
    const auto selection_started = Clock::now();
    const auto pre_guard_stats = stats_;
    const auto pre_guard_fitness_ns = fitness_ns_;
    const auto pre_guard_value = evaluate_normalized(winner, false);
    stats_ = pre_guard_stats;
    fitness_ns_ = pre_guard_fitness_ns;
    if (pre_guard_value.fitness.feasible &&
        (!pre_guard_value.forecast_feasible ||
         !std::isfinite(pre_guard_value.search_nll))) {
      // Forecast is an optimization oracle, not an executability condition.
      // If every bounded rollout fails, retain the already-proven safe current
      // boundary and let the next real boundary rebuild forecast state.
      auto current_only = pre_guard_value;
      current_only.forecast_nll = 0.0;
      current_only.search_nll = current_only.fitness.negative_log_fidelity;
      current_only.forecast_feasible = true;
      current_only.forecast_error.clear();
      std::fill(current_only.forecast_by_depth.begin(),
                current_only.forecast_by_depth.end(), 0.0);
      current_only.forecast_by_category.fill(0.0);
      guarded_final_value_ = std::move(current_only);
      search_mode += "-forecast-current-fallback";
    } else if (!pre_guard_value.fitness.feasible &&
        (pre_guard_value.fitness.error.find("ghost") != std::string::npos ||
         pre_guard_value.fitness.error.find("occupancy") !=
             std::string::npos)) {
      const auto recovered = recover_infeasible_current_gate_projection(winner);
      if (recovered.has_value()) {
        winner = recovered->fitness.chromosome;
        auto completed_recovery = *recovered;
        if (problem_.future_layers.empty() &&
            (config_.max_horizon == 0 ||
             !problem_.forecast_terms.empty())) {
          // Current-feasibility recovery intentionally scores gate geometry
          // without a forecast.  Before publication, strict H0 still needs
          // its one-element zero depth vector, and any precomputed value term
          // must be restored.  Unlike a physical future rollout, those terms
          // cannot become geometrically infeasible.  This is especially
          // important for M3: returning the raw current-only recovery either
          // produced an empty H0 vector or silently dropped its depth-zero
          // value term on the exact large-boundary path.
          apply_forecast(completed_recovery, winner);
        }
        guarded_final_value_ = std::move(completed_recovery);
        search_mode += "-current-recovery";
      }
    }
    winner = guard_forecast_gate_projection(winner);
    const auto final_value = guarded_final_value_.has_value()
                                 ? *guarded_final_value_
                                 : evaluate_normalized(winner, false);
    if (final_value.fitness.feasible &&
        (!final_value.forecast_feasible ||
         !std::isfinite(final_value.search_nll))) {
      auto details = final_value.forecast_error;
      if (details.empty()) details = final_value.fitness.error;
      throw std::runtime_error(
          "rich search has no feasible candidate" +
          (details.empty() ? std::string{} : std::string(": ") + details));
    }
    auto final_geometry = build_geometry(
        final_value.decoded, final_value.assignments, final_value.reseats,
        final_value.participant_parkings);
    const auto recorded_winner = final_value.fitness.feasible
        ? score_geometry(
              winner, std::move(final_geometry),
              final_value.assignments.size(),
              config_.enforce_single_leg_ghost, true)
        : final_value.fitness;
    const auto same_value = [](double first, double second) {
      return first == second ||
             (std::isinf(first) && std::isinf(second) &&
              std::signbit(first) == std::signbit(second));
    };
    if (recorded_winner.feasible != final_value.fitness.feasible ||
        !same_value(recorded_winner.negative_log_fidelity,
                    final_value.fitness.negative_log_fidelity) ||
        recorded_winner.move_batches != final_value.fitness.move_batches ||
        !same_value(recorded_winner.move_time_us,
                    final_value.fitness.move_time_us) ||
        !same_value(recorded_winner.total_distance_um,
                    final_value.fitness.total_distance_um)) {
      throw std::runtime_error(
          "recorded winner differs from summary fitness: nll=" +
          std::to_string(recorded_winner.negative_log_fidelity) + "/" +
          std::to_string(final_value.fitness.negative_log_fidelity) +
          ", batches=" + std::to_string(recorded_winner.move_batches) +
          "/" + std::to_string(final_value.fitness.move_batches) +
          ", time=" + std::to_string(recorded_winner.move_time_us) + "/" +
          std::to_string(final_value.fitness.move_time_us) +
          ", distance=" +
          std::to_string(recorded_winner.total_distance_um) + "/" +
          std::to_string(final_value.fitness.total_distance_um));
    }
    selection_ns_ += elapsed_ns(selection_started);
    RichSolveResult result;
    result.winner = recorded_winner;
    result.gate_option_indices = final_value.decoded.option_indices;
    for (const auto& assignment : final_value.assignments) {
      result.return_assignments.emplace_back(
          problem_.eligible[assignment.eligible_index], assignment.site_id);
    }
    for (const auto& reseat : final_value.reseats) {
      result.reseat_assignments.emplace_back(
          problem_.eligible[reseat.eligible_index], reseat.site_id);
    }
    for (const auto& parking : final_value.participant_parkings) {
      result.participant_parking_assignments.emplace_back(
          parking.atom, parking.site_id);
    }
    result.rng_state = rng_.state();
    result.search_mode = std::move(search_mode);
    result.operator_profile = config_.operator_profile;
    result.stats = stats_;
    result.forecast_nll = final_value.forecast_nll;
    result.search_negative_log_fidelity = final_value.search_nll;
    result.forecast_by_depth = final_value.forecast_by_depth;
    result.forecast_residency_nll = final_value.forecast_by_category[0];
    result.forecast_reentry_nll = final_value.forecast_by_category[1];
    result.forecast_terminal_nll = final_value.forecast_by_category[2];
    result.forecast_routing_nll = final_value.forecast_by_category[3];
    result.return_assignment_rank = final_value.return_assignment_rank;
    result.return_assignment_evaluated =
        final_value.return_assignment_evaluated;
    result.current_ghost_rejections = final_value.current_ghost_rejections;
    result.pre_score_reseats = final_value.pre_score_reseats;
    result.pre_score_participant_parkings =
        final_value.pre_score_participant_parkings;
    result.current_gate_anchor = current_gate_anchor_;
    result.current_gate_anchor_assignment_site_ids =
        current_gate_anchor_assignment_site_ids_;
    result.current_gate_final_assignment_site_ids =
        current_gate_final_assignment_site_ids_;
    result.current_gate_guard_branch = current_gate_guard_branch_;
    result.current_gate_guard_cohort_size = current_gate_guard_cohort_size_;
    result.current_gate_guard_admitted_size = current_gate_guard_admitted_size_;
    result.current_gate_projection_source = current_gate_projection_source_;
    result.current_gate_projection_evaluated =
        current_gate_projection_evaluated_;
    result.normalize_ns = normalize_ns_;
    result.decode_ns = decode_ns_;
    result.return_match_ns = return_match_ns_;
    result.fitness_ns = fitness_ns_;
    result.selection_ns = selection_ns_;
    result.forecast_ns = forecast_ns_;
    result.search_ns = elapsed_ns(search_started);
    return result;
  }

 private:
  bool forecast_gate_guard_active() const noexcept {
    // H=0 never consults either future representation.  It may nevertheless
    // activate the same exact-current projection guard when the caller's
    // one-rent-vs-round-trip audit recommends STAY.  Without this branch the
    // mask was only injected as a GA seed and the current one-way RETURN cost
    // could still win, immediately paying the omitted re-entry on the next
    // boundary.
    if (config_.max_horizon == 0) {
      return std::any_of(
          problem_.recommended_stay_mask.begin(),
          problem_.recommended_stay_mask.end(),
          [](const auto value) { return value; });
    }
    // Native rollout's endpoint Phi(s_H) is independent of alpha. A visible
    // native future therefore still needs the complete gate guard at alpha=0;
    // only legacy precomputed terms disappear with alpha.
    return !problem_.future_layers.empty() ||
           (config_.alpha_lookahead > 0.0 &&
            !problem_.forecast_terms.empty());
  }

  void archive_complete_evaluation(const Evaluated& value) {
    if (!forecast_gate_guard_active() || !value.fitness.feasible ||
        !std::isfinite(value.search_nll)) {
      return;
    }
    complete_evaluated_archive_[value.fitness.chromosome] = value;
  }

  bool same_residency_suffix(const std::vector<std::int64_t>& first,
                             const std::vector<std::int64_t>& second) const {
    const auto gate_count = problem_.gate_domains.size();
    if (first.size() != second.size() || first.size() < gate_count) return false;
    return std::equal(first.begin() + static_cast<std::ptrdiff_t>(gate_count),
                      first.end(),
                      second.begin() + static_cast<std::ptrdiff_t>(gate_count));
  }

  void complete_deferred_guard_suffix(
      const std::vector<std::int64_t>& provisional_winner) {
    if (!forecast_gate_guard_active() || partial_current_bounds_.empty()) return;
    std::vector<std::vector<std::int64_t>> deferred;
    deferred.reserve(partial_current_bounds_.size());
    for (const auto& [chromosome, lower_bound] : partial_current_bounds_) {
      (void)lower_bound;
      if (same_residency_suffix(chromosome, provisional_winner)) {
        deferred.push_back(chromosome);
      }
    }
    std::sort(deferred.begin(), deferred.end());
    for (const auto& chromosome : deferred) {
      // The lazy pool already accounted the exact-current evaluation.  Rebuild
      // that private state without changing public evaluation/cache/RNG
      // counters, then complete the non-negative forecast exactly once.
      const auto saved_stats = stats_;
      auto prepared = prepare_current_normalized(chromosome, false);
      stats_ = saved_stats;
      Evaluated value;
      if (prepared.complete) {
        value = std::move(prepared.complete_value);
        partial_current_bounds_.erase(chromosome);
      } else {
        value = complete_prepared_current(chromosome, std::move(prepared));
      }
      archive_complete_evaluation(value);
    }
  }

  std::vector<Evaluated> evaluate_guard_assignment_cohort(
      const std::vector<std::int64_t>& raw_chromosome,
      bool include_forecast = true) {
    const auto chromosome = normalize(raw_chromosome);
    Evaluated initial;
    initial.search_nll = std::numeric_limits<double>::infinity();
    initial.decoded = decode(chromosome);
    if (!initial.decoded.feasible) {
      initial.fitness = infeasible_fitness(
          chromosome, initial.decoded.error);
      return {std::move(initial)};
    }
    const auto gate_count = problem_.gate_domains.size();
    std::vector<std::size_t> returners;
    for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
      if (chromosome[gate_count + index] != 0) returners.push_back(index);
    }
    std::vector<std::vector<ReturnAssignment>> assignments;
    try {
      assignments = match_returns(returners);
    } catch (const std::exception& error) {
      initial.fitness = infeasible_fitness(chromosome, error.what());
      return {std::move(initial)};
    }
    if (assignments.empty()) {
      initial.fitness = infeasible_fitness(
          chromosome, "RETURN matching has no bounded injective assignment");
      return {std::move(initial)};
    }
    stats_.return_assignment_evaluated += assignments.size();
    std::vector<Evaluated> cohort;
    cohort.reserve(assignments.size());
    for (std::size_t rank = 0; rank < assignments.size(); ++rank) {
      auto value = evaluate_assignment(
          chromosome, initial.decoded, returners, assignments[rank], rank,
          assignments.size(), false);
      if (value.fitness.feasible) {
        if (include_forecast) {
          apply_forecast(value, chromosome);
        } else {
          // Current-physics recovery is a hard executability path.  A future
          // forecast may be infeasible or truncated, but it must not discard
          // an otherwise executable current boundary.  The ordinary M4 guard
          // may add forecast information after recovery.
          value.forecast_nll = 0.0;
          value.search_nll = value.fitness.negative_log_fidelity;
          value.forecast_feasible = true;
          value.forecast_error.clear();
        }
      }
      cohort.push_back(std::move(value));
    }
    return cohort;
  }

  std::optional<Evaluated> recover_infeasible_current_gate_projection(
      const std::vector<std::int64_t>& raw_winner) {
    const auto gate_count = problem_.gate_domains.size();
    if (gate_count == 0) return std::nullopt;
    auto chromosome = normalize(raw_winner);
    std::optional<Evaluated> best;
    std::set<std::int64_t> used_gate_sites;
    const bool stop_after_first_feasible = gate_count > 1;

    // This path is entered only after the ordinary bounded GA/exact cohort is
    // wholly current-infeasible.  It does not spend stochastic budget.  A
    // serial layer audits its complete registered gate domain and retains the
    // best exact physical value.  Wider layers use deterministic injective DFS
    // and stop at the first exact feasible leaf; this is a safety recovery,
    // not a second optimizer hidden inside fitness.
    std::function<bool(std::size_t)> visit = [&](std::size_t gate) {
      if (gate == gate_count) {
        auto cohort = evaluate_guard_assignment_cohort(
            chromosome, false);
        ++current_gate_projection_evaluated_;
        for (auto& value : cohort) {
          if (!value.fitness.feasible ||
              !std::isfinite(value.fitness.negative_log_fidelity)) {
            continue;
          }
          if (!best.has_value() || current_physical_less(value, *best)) {
            best = value;
          }
        }
        return stop_after_first_feasible && best.has_value();
      }
      std::vector<std::size_t> order;
      order.reserve(problem_.gate_domains[gate].size());
      const auto preferred = positive_mod(
          chromosome[gate], problem_.gate_domains[gate].size());
      order.push_back(preferred);
      for (std::size_t option = 0;
           option < problem_.gate_domains[gate].size(); ++option) {
        if (option != preferred) order.push_back(option);
      }
      for (const auto option : order) {
        const auto site_id = problem_.gate_domains[gate][option].site_id;
        if (used_gate_sites.count(site_id) != 0U) continue;
        const auto prior = chromosome[gate];
        chromosome[gate] = static_cast<std::int64_t>(option);
        used_gate_sites.insert(site_id);
        const auto stop = visit(gate + 1);
        used_gate_sites.erase(site_id);
        chromosome[gate] = prior;
        if (stop) return true;
      }
      return false;
    };
    visit(0);
    if (best.has_value()) {
      current_gate_guard_branch_ = "infeasible-current-full-domain-recovery";
      current_gate_projection_source_ =
          gate_count == 1 ? "infeasible-single-gate-full-domain"
                          : "infeasible-injective-gate-dfs";
      current_gate_guard_cohort_size_ = 1;
      current_gate_guard_admitted_size_ = 1;
      current_gate_anchor_ = best->fitness.chromosome;
      current_gate_anchor_assignment_site_ids_.clear();
      current_gate_final_assignment_site_ids_.clear();
      for (const auto& assignment : best->assignments) {
        current_gate_anchor_assignment_site_ids_.push_back(
            assignment.site_id);
        current_gate_final_assignment_site_ids_.push_back(
            assignment.site_id);
      }
    }
    return best;
  }

  std::vector<std::int64_t> guard_forecast_gate_projection(
      const std::vector<std::int64_t>& raw_winner) {
    if (!forecast_gate_guard_active()) return raw_winner;
    auto provisional = normalize(raw_winner);
    const auto fallback = provisional;
    std::map<std::vector<std::int64_t>, std::vector<Evaluated>> guard_values;
    const auto values_for = [&](const std::vector<std::int64_t>& raw)
        -> std::vector<Evaluated>& {
      const auto chromosome = normalize(raw);
      auto [iterator, inserted] = guard_values.try_emplace(chromosome);
      if (inserted) {
        // Gate projection is ordered solely by exact current-boundary physics.
        // Deferring the bounded rollout here avoids replaying H future layers
        // for every option inspected by the coordinate/full-domain sweep.
        // The final guard cohort below receives the complete forecast before
        // any future-aware admission or winner comparison.
        iterator->second =
            evaluate_guard_assignment_cohort(chromosome, false);
        ++current_gate_projection_evaluated_;
      }
      return iterator->second;
    };
    const auto current_min = [](std::vector<Evaluated>& values)
        -> Evaluated* {
      if (values.empty()) return nullptr;
      return &*std::min_element(
          values.begin(), values.end(), [](const auto& first,
                                           const auto& second) {
            return current_physical_less(first, second);
          });
    };

    // The GA archive is not guaranteed to contain the current-myopic gate
    // placement for every residency suffix that matters to the rolling rent
    // decision.  Serial and narrow layers retain the complete audit.  Wide
    // parallel layers use one bounded coordinate sweep and only the all-STAY,
    // provisional, and complete recommendation suffixes; per-resident
    // singleton projection there is both combinatorial and redundant with the
    // coupled suffix already selected by the GA.  Selection here uses current
    // physics only; forecast remains solely the downstream tie/Pareto selector.
    const auto gate_count = problem_.gate_domains.size();
    const bool long_depth_guard =
        gate_count > 0 &&
        config_.direct_enumeration_limit <= kLongDepthEnumerationLimit;
    const bool bounded_parallel_guard =
        gate_count >= kParallelGuardGateThreshold ||
        (long_depth_guard && gate_count > 1);
    const bool bounded_serial_guard =
        gate_count == 1 && long_depth_guard;
    const bool bounded_gate_guard =
        bounded_parallel_guard || bounded_serial_guard;
    const auto bounded_option_limit =
        bounded_parallel_guard ? kParallelGuardOptionLimit
                               : kLongDepthGuardOptionLimit;
    const bool has_return_recommendation = std::any_of(
        problem_.recommended_return_mask.begin(),
        problem_.recommended_return_mask.end(),
        [](const auto value) { return value; });
    const bool has_stay_recommendation = std::any_of(
        problem_.recommended_stay_mask.begin(),
        problem_.recommended_stay_mask.end(),
        [](const auto value) { return value; });
    current_gate_projection_source_ =
        gate_count == 0
            ? "no-current-gates"
            : (gate_count == 1
               ? (bounded_serial_guard
                      ? (has_return_recommendation
                             ? "single-gate-joint-long-depth-bounded"
                             : "single-gate-long16")
                      : (has_return_recommendation
                             ? "single-gate-joint-full-domain-singleton-reuse"
                             : "single-gate-full-domain"))
               : (bounded_parallel_guard
                      ? (long_depth_guard
                             ? "coordinate-bounded-long-depth-1-sweep"
                             : "coordinate-bounded-parallel-1-sweep")
                      : "coordinate-full-domain-2-sweep"));
    const auto project_current_gates = [&](
        const std::vector<std::int64_t>& raw)
        -> std::optional<std::vector<std::int64_t>> {
      auto projected = normalize(raw);
      auto* projected_pointer = current_min(values_for(projected));
      if (projected_pointer == nullptr ||
          !projected_pointer->fitness.feasible) {
        return std::nullopt;
      }
      auto projected_value = *projected_pointer;
      const auto sweeps = gate_count == 0
                              ? std::size_t{0}
                              : (gate_count == 1 ? std::size_t{1}
                                 : (bounded_parallel_guard
                                        ? std::size_t{1}
                                        : std::size_t{2}));
      for (std::size_t sweep = 0; sweep < sweeps; ++sweep) {
        bool changed = false;
        for (std::size_t gate = 0; gate < gate_count; ++gate) {
          auto best = projected_value;
          std::vector<std::size_t> options;
          const auto add_option = [&options](const std::size_t option) {
            if (std::find(options.begin(), options.end(), option) ==
                options.end()) {
              options.push_back(option);
            }
          };
          const auto domain_size = problem_.gate_domains[gate].size();
          if (bounded_gate_guard) {
            add_option(positive_mod(projected[gate], domain_size));
            add_option(positive_mod(problem_.matched_gate_genes[gate],
                                    domain_size));
            for (std::size_t option = 0;
                 option < domain_size &&
                 options.size() < bounded_option_limit;
                 ++option) {
              add_option(option);
            }
          } else {
            options.reserve(domain_size);
            for (std::size_t option = 0; option < domain_size; ++option) {
              options.push_back(option);
            }
          }
          for (const auto option : options) {
            auto trial = projected;
            trial[gate] = static_cast<std::int64_t>(option);
            trial = normalize(trial);
            auto* value = current_min(values_for(trial));
            if (value != nullptr && current_physical_less(*value, best)) {
              best = *value;
            }
          }
          if (best.fitness.chromosome != projected) {
            projected = best.fitness.chromosome;
            projected_value = std::move(best);
            changed = true;
          }
        }
        if (!changed) break;
      }
      return projected;
    };

    // Recommendation variants follow the seed-population contract: singleton
    // masks start from all-STAY, and the mixed mask enables every recommended
    // RETURN.  normalize() still owns forced returns, capacity repair, and
    // deterministic eviction order before any physical comparison.
    std::set<std::vector<std::int64_t>> projection_seeds{provisional};
    std::set<std::vector<std::int64_t>> serial_singleton_seeds;
    if (has_return_recommendation) {
      auto all_stay = provisional;
      std::fill(all_stay.begin() + static_cast<std::ptrdiff_t>(gate_count),
                all_stay.end(), 0);
      auto recommended = all_stay;
      for (std::size_t index = 0;
           index < problem_.recommended_return_mask.size(); ++index) {
        if (!problem_.recommended_return_mask[index]) continue;
        if (!bounded_gate_guard) {
          auto singleton = all_stay;
          singleton[gate_count + index] = 1;
          singleton = normalize(singleton);
          if (gate_count == 1) {
            serial_singleton_seeds.insert(std::move(singleton));
          } else {
            projection_seeds.insert(std::move(singleton));
          }
        }
        recommended[gate_count + index] = 1;
      }
      if (bounded_parallel_guard) {
        projection_seeds.insert(normalize(all_stay));
      }
      projection_seeds.insert(normalize(recommended));
    }
    if (has_stay_recommendation) {
      // A decayed objective can under-price RETURN by charging its current
      // half-cycle at full weight but discounting the later re-entry.  The
      // caller's full round-trip rent audit supplies one exact joint suffix
      // with those proven-cheaper atoms kept resident.  normalize() still
      // owns forced RETURNs and deterministic capacity eviction.
      auto recommended_stay = provisional;
      for (std::size_t index = 0;
           index < problem_.recommended_stay_mask.size(); ++index) {
        if (problem_.recommended_stay_mask[index]) {
          recommended_stay[gate_count + index] = 0;
        }
      }
      projection_seeds.insert(normalize(recommended_stay));
    }

    std::set<std::vector<std::int64_t>> projected_chromosomes;
    std::optional<std::vector<std::int64_t>> projected_provisional;
    for (const auto& seed : projection_seeds) {
      const auto projected = project_current_gates(seed);
      if (!projected.has_value()) continue;
      projected_chromosomes.insert(*projected);
      if (seed == provisional) projected_provisional = *projected;
    }
    if (gate_count == 1 && projected_provisional.has_value()) {
      // Ordinary serial layers retain exact singleton suffixes.  The registered
      // long-depth guard deliberately leaves this set empty: its provisional
      // GA winner plus the joint recommended RETURN/STAY suffixes already
      // contain the coupled decisions, while replaying one suffix per atom was
      // the dominant 10k-layer cost.  Every retained suffix still passes the
      // unchanged strict current assignment and ghost-safe scorer.
      for (const auto& singleton : serial_singleton_seeds) {
        if (projection_seeds.count(singleton) != 0U) continue;
        auto reused = singleton;
        reused[0] = (*projected_provisional)[0];
        reused = normalize(reused);
        auto* value = current_min(values_for(reused));
        if (value != nullptr && value->fitness.feasible) {
          projected_chromosomes.insert(value->fitness.chromosome);
        }
      }
    }
    if (projected_chromosomes.empty()) {
      current_gate_guard_branch_ = "projection-infeasible-fallback";
      return provisional;
    }
    current_gate_anchor_ = *projected_chromosomes.begin();

    // RETURN/STAY remains the lookahead decision.  Complete candidates with
    // every projected normalized suffix, then apply one exact current-physics
    // Pareto envelope across the union.  This prevents a useful rent
    // recommendation from disappearing merely because it arrived with a gate
    // prefix that was suboptimal for that suffix.
    if (!bounded_gate_guard) {
      for (const auto& projected : projected_chromosomes) {
        complete_deferred_guard_suffix(projected);
      }
    }
    const auto residency_suffix = [&](const std::vector<std::int64_t>& value) {
      return std::vector<std::int64_t>(
          value.begin() + static_cast<std::ptrdiff_t>(gate_count), value.end());
    };
    std::set<std::vector<std::int64_t>> projected_suffixes;
    for (const auto& projected : projected_chromosomes) {
      projected_suffixes.insert(residency_suffix(projected));
    }
    const auto has_projected_suffix = [&](
        const std::vector<std::int64_t>& chromosome) {
      return chromosome.size() >= gate_count &&
             projected_suffixes.find(residency_suffix(chromosome)) !=
                 projected_suffixes.end();
    };

    std::set<std::vector<std::int64_t>> guard_chromosomes;
    if (!bounded_parallel_guard) {
      for (const auto& [chromosome, value] : complete_evaluated_archive_) {
        (void)value;
        if (has_projected_suffix(chromosome)) {
          guard_chromosomes.insert(chromosome);
        }
      }
      for (const auto& [chromosome, values] : guard_values) {
        (void)values;
        if (has_projected_suffix(chromosome)) {
          guard_chromosomes.insert(chromosome);
        }
      }
    }
    guard_chromosomes.insert(projected_chromosomes.begin(),
                             projected_chromosomes.end());
    struct GuardCurrentCandidate {
      const std::vector<std::int64_t>* chromosome{};
      Evaluated* value{};
    };
    std::vector<GuardCurrentCandidate> current_cohort;
    for (const auto& chromosome : guard_chromosomes) {
      auto& values = values_for(chromosome);
      for (auto& value : values) {
        if (value.fitness.feasible) {
          current_cohort.push_back({&chromosome, &value});
        }
      }
    }
    if (current_cohort.empty()) return fallback;

    // Forced returns and the deterministic min_returns normalization are
    // authoritative.  Every other full-round-trip STAY recommendation forms
    // a trust region: among forecast-feasible candidates, first minimize how
    // many of those bits had to be overridden (for example by a joint ghost
    // conflict), then compare the physical/forecast objective.  Deriving the
    // capacity suffix through normalize(all-STAY) exactly mirrors the Python
    // semantic reference and lets hard capacity override the hint.
    auto normalized_all_stay = provisional;
    std::fill(
        normalized_all_stay.begin() +
            static_cast<std::ptrdiff_t>(gate_count),
        normalized_all_stay.end(), 0);
    normalized_all_stay = normalize(normalized_all_stay);
    std::vector<std::size_t> protected_stays;
    for (std::size_t index = 0;
         index < problem_.recommended_stay_mask.size(); ++index) {
      if (problem_.recommended_stay_mask[index] &&
          !problem_.forced_return_mask[index] &&
          normalized_all_stay[gate_count + index] == 0) {
        protected_stays.push_back(index);
      }
    }
    const auto stay_violations = [&](const GuardCurrentCandidate& candidate) {
      return static_cast<std::size_t>(std::count_if(
          protected_stays.begin(), protected_stays.end(),
          [&](const auto index) {
            return candidate.value->fitness.chromosome[gate_count + index] !=
                   0;
          }));
    };
    std::sort(current_cohort.begin(), current_cohort.end(),
              [&](const auto& first, const auto& second) {
                const auto first_violations = stay_violations(first);
                const auto second_violations = stay_violations(second);
                if (first_violations != second_violations) {
                  return first_violations < second_violations;
                }
                return current_physical_less(*first.value, *second.value);
              });

    // Projection values are deliberately current-only.  Complete candidates
    // in exact current-physics order until the first forecast-feasible value
    // establishes the guard anchor.  Every forecast contribution is a
    // non-negative physical NLL, so a later assignment whose exact current
    // NLL already strictly exceeds that complete anchor can neither become the
    // current anchor nor pass the unchanged search_nll <= anchor.search_nll
    // admission below.  Exact ties still receive the full rollout so Move and
    // canonical assignment tie-breaks remain byte-for-byte deterministic.
    std::vector<Evaluated> cohort;
    cohort.reserve(current_cohort.size());
    std::optional<Evaluated> anchor_value;
    std::size_t minimum_stay_violations = 0;
    std::size_t group_begin = 0;
    while (group_begin < current_cohort.size() && !anchor_value.has_value()) {
      const auto group_violations = stay_violations(
          current_cohort[group_begin]);
      auto group_end = group_begin + 1;
      while (group_end < current_cohort.size() &&
             stay_violations(current_cohort[group_end]) == group_violations) {
        ++group_end;
      }

      std::size_t next_current = group_begin;
      for (; next_current < group_end; ++next_current) {
        auto& current = current_cohort[next_current];
        apply_forecast(*current.value, *current.chromosome);
        if (current.value->forecast_feasible &&
            std::isfinite(current.value->search_nll)) {
          anchor_value = *current.value;
          cohort.push_back(*current.value);
          minimum_stay_violations = group_violations;
          ++next_current;
          break;
        }
      }
      if (anchor_value.has_value()) {
        for (; next_current < group_end; ++next_current) {
          auto& current = current_cohort[next_current];
          if (current.value->fitness.negative_log_fidelity >
              anchor_value->search_nll + 1e-12) {
            continue;
          }
          apply_forecast(*current.value, *current.chromosome);
          if (current.value->forecast_feasible &&
              std::isfinite(current.value->search_nll)) {
            cohort.push_back(*current.value);
          }
        }
        break;
      }
      group_begin = group_end;
    }
    if (!anchor_value.has_value()) return fallback;
    // Keep the public cohort counter on its established contract: it counts
    // exact-current assignments presented to the guard, including assignments
    // whose non-negative future lower bound proved that a rollout was
    // unnecessary.  The admitted counter below still reports the actually
    // forecast-complete subset that can affect the winner.
    current_gate_guard_cohort_size_ = current_cohort.size();

    const auto* anchor = &*std::min_element(
        cohort.begin(), cohort.end(), [](const auto& first, const auto& second) {
          return current_physical_less(first, second);
        });
    current_gate_anchor_ = anchor->fitness.chromosome;
    current_gate_anchor_assignment_site_ids_.clear();
    for (const auto& assignment : anchor->assignments) {
      current_gate_anchor_assignment_site_ids_.push_back(assignment.site_id);
    }

    std::vector<const Evaluated*> admitted;
    admitted.reserve(cohort.size());
    current_gate_guard_branch_ =
        protected_stays.empty()
            ? "trust-region-forecast-sacrifice"
            : (minimum_stay_violations == 0
                   ? "trust-region-round-trip-stay"
                   : "trust-region-round-trip-stay-relaxed");
    // A future-aware candidate may spend current fidelity only when the same
    // physical rollout certifies at least that much future saving.  This keeps
    // the current-safe anchor, but removes the fixed +2-transfer ceiling and
    // the eligible-empty exact-tie rule that prevented genuine gate relocation.
    for (const auto& value : cohort) {
      const auto current_extra = std::max(
          0.0, value.fitness.negative_log_fidelity -
                   anchor->fitness.negative_log_fidelity);
      const auto forecast_saving = std::max(
          0.0, anchor->forecast_nll - value.forecast_nll);
      if (current_extra >
          kForecastSacrificeTrust * forecast_saving + 1e-12) continue;
      if (value.search_nll > anchor->search_nll + 1e-12) continue;
      admitted.push_back(&value);
    }
    const auto* selected = anchor;
    current_gate_guard_admitted_size_ = admitted.size();
    if (!admitted.empty()) {
      selected = *std::min_element(
          admitted.begin(), admitted.end(),
          [](const auto* first, const auto* second) {
            return evaluated_less(*first, *second);
          });
    }
    guarded_final_value_ = *selected;
    current_gate_final_assignment_site_ids_.clear();
    for (const auto& assignment : selected->assignments) {
      current_gate_final_assignment_site_ids_.push_back(assignment.site_id);
    }
    return selected->fitness.chromosome;
  }

  void validate() const {
    if (problem_.n_atoms != architecture_.n_atoms() ||
        problem_.current_points.size() != problem_.n_atoms) {
      throw std::invalid_argument("rich atom count differs from architecture");
    }
    if (problem_.return_domains.size() != problem_.eligible.size() ||
        problem_.forced_return_mask.size() != problem_.eligible.size() ||
        problem_.recommended_return_mask.size() != problem_.eligible.size() ||
        problem_.recommended_stay_mask.size() != problem_.eligible.size()) {
      throw std::invalid_argument("rich eligible arrays are not aligned");
    }
    for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
      if (problem_.recommended_return_mask[index] &&
          problem_.recommended_stay_mask[index]) {
        throw std::invalid_argument(
            "rich RETURN and STAY recommendations are not disjoint");
      }
    }
    if (problem_.eviction_order_indices.size() != problem_.eligible.size()) {
      throw std::invalid_argument("rich eviction order has wrong size");
    }
    if (problem_.matched_gate_genes.size() != problem_.gate_domains.size()) {
      throw std::invalid_argument("matched gate genes have wrong size");
    }
    const std::set<std::int64_t> participant_atoms(
        problem_.participants.begin(), problem_.participants.end());
    const auto nonparticipant_eligible_count = static_cast<std::size_t>(
        std::count_if(problem_.eligible.begin(), problem_.eligible.end(),
                      [&participant_atoms](const auto atom) {
                        return participant_atoms.find(atom) ==
                               participant_atoms.end();
                      }));
    if (problem_.min_returns > nonparticipant_eligible_count) {
      throw std::invalid_argument(
          "min_returns exceeds nonparticipant eligible count");
    }
    if (problem_.exact_current_scheduler) {
      const auto finite_non_negative = [](double value) {
        return std::isfinite(value) && value >= 0.0;
      };
      const auto frozen_equal = [](double actual, double expected) {
        return std::isfinite(actual) &&
               std::abs(actual - expected) <= 1e-12;
      };
      if (!finite_non_negative(problem_.scheduler_one_qubit_duration_us) ||
          !finite_non_negative(problem_.scheduler_rydberg_duration_us) ||
          !finite_non_negative(problem_.scheduler_one_qubit_common_us) ||
          !finite_non_negative(problem_.scheduler_transfer_duration_us) ||
          !std::isfinite(problem_.scheduler_accel_um_per_us2) ||
          problem_.scheduler_accel_um_per_us2 <= 0.0 ||
          !std::isfinite(problem_.coherence_t2_us) ||
          problem_.coherence_t2_us <= 0.0) {
        throw std::invalid_argument(
            "ABI8 exact scheduler physical constants are invalid");
      }
      if (problem_.enforce_frozen_physical_model &&
          (!frozen_equal(problem_.scheduler_one_qubit_duration_us, kOneQUs) ||
          !frozen_equal(problem_.scheduler_rydberg_duration_us, kRydbergUs) ||
          !frozen_equal(problem_.scheduler_one_qubit_common_us, 0.0) ||
          !frozen_equal(problem_.scheduler_transfer_duration_us, kTransferUs) ||
          !frozen_equal(problem_.scheduler_accel_um_per_us2,
                        kAccelUmPerUs2) ||
          !frozen_equal(problem_.coherence_t2_us, kT2Us))) {
        throw std::invalid_argument(
            "ABI8 exact scheduler physical constants differ from the frozen model");
      }
      if (problem_.current_site_ids.size() != problem_.n_atoms ||
          problem_.scheduler_active_union_us.size() != problem_.n_atoms ||
          problem_.scheduler_qubit_dependency_end_us.size() != problem_.n_atoms ||
          problem_.scheduler_back_dependency_end_us.size() != problem_.n_atoms ||
          problem_.prior_idle_time_us.size() != problem_.n_atoms) {
        throw std::invalid_argument(
            "ABI8 exact scheduler atom vectors are not aligned");
      }
      // Current formal ZAC architectures own one AOD and one Rydberg zone.  The
      // compact wire intentionally fails closed until zone ids are carried per
      // gate option rather than silently guessing a multi-resource schedule.
      if (problem_.scheduler_aod_end_us.size() != 1 ||
          problem_.scheduler_rydberg_end_us.size() != 1) {
        throw std::invalid_argument(
            "ABI8 exact scheduler currently requires one AOD and one Rydberg zone");
      }
      if (!finite_non_negative(problem_.scheduler_trace_end_us) ||
          !finite_non_negative(problem_.scheduler_one_qubit_end_us)) {
        throw std::invalid_argument("ABI8 scheduler scalar clock is invalid");
      }
      for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
        const auto active = problem_.scheduler_active_union_us[atom];
        const auto ordinary =
            problem_.scheduler_qubit_dependency_end_us[atom];
        const auto back = problem_.scheduler_back_dependency_end_us[atom];
        if (!finite_non_negative(active) ||
            active > problem_.scheduler_trace_end_us + 1e-7 ||
            !finite_non_negative(ordinary) ||
            !finite_non_negative(back) || back + 1e-7 < ordinary) {
          throw std::invalid_argument("ABI8 scheduler atom clock is invalid");
        }
        const auto expected =
            std::max(0.0, problem_.scheduler_trace_end_us - active);
        if (std::abs(problem_.prior_idle_time_us[atom] - expected) > 1e-7) {
          throw std::invalid_argument(
              "ABI8 prior idle differs from absolute scheduler state");
        }
        const auto site_id = problem_.current_site_ids[atom];
        if (site_id < 0 || static_cast<std::size_t>(site_id) >=
                               architecture_.site_coordinates().size()) {
          throw std::invalid_argument("ABI8 current site id is invalid");
        }
      }
      for (const auto value : problem_.scheduler_aod_end_us) {
        if (!finite_non_negative(value)) {
          throw std::invalid_argument("ABI8 AOD clock is invalid");
        }
      }
      for (const auto value : problem_.scheduler_rydberg_end_us) {
        if (!finite_non_negative(value)) {
          throw std::invalid_argument("ABI8 Rydberg clock is invalid");
        }
      }
      if (problem_.scheduler_site_dependency_site_ids.size() !=
          problem_.scheduler_site_dependency_activation_finish_us.size()) {
        throw std::invalid_argument(
            "ABI8 scheduler site dependency columns differ in length");
      }
      std::set<std::int64_t> dependency_sites;
      for (std::size_t index = 0;
           index < problem_.scheduler_site_dependency_site_ids.size(); ++index) {
        const auto site_id =
            problem_.scheduler_site_dependency_site_ids[index];
        const auto activation =
            problem_.scheduler_site_dependency_activation_finish_us[index];
        if (site_id < 0 || static_cast<std::size_t>(site_id) >=
                               architecture_.site_coordinates().size() ||
            !dependency_sites.insert(site_id).second ||
            !finite_non_negative(activation)) {
          throw std::invalid_argument(
              "ABI8 scheduler site dependency is invalid");
        }
      }
      for (const auto atom : problem_.target_one_qubit_atoms) {
        if (atom < 0 || static_cast<std::size_t>(atom) >= problem_.n_atoms) {
          throw std::invalid_argument("ABI8 target 1Q atom is invalid");
        }
      }
      for (const auto& domain : problem_.gate_domains) {
        for (const auto& option : domain) {
          if (option.target1_site_id < 0 || option.target2_site_id < 0) {
            throw std::invalid_argument(
                "ABI8 indexed gate option lacks target site ids");
          }
        }
      }
    } else if (!problem_.scheduler_active_union_us.empty() ||
               !problem_.scheduler_aod_end_us.empty() ||
               !problem_.scheduler_rydberg_end_us.empty() ||
               !problem_.scheduler_qubit_dependency_end_us.empty() ||
               !problem_.scheduler_back_dependency_end_us.empty() ||
               !problem_.scheduler_site_dependency_site_ids.empty() ||
               !problem_.scheduler_site_dependency_activation_finish_us.empty() ||
               !problem_.target_one_qubit_atoms.empty() ||
               problem_.scheduler_trace_end_us != 0.0 ||
               problem_.scheduler_one_qubit_end_us != 0.0) {
      throw std::invalid_argument("partial scheduler snapshot is forbidden");
    }
    if ((config_.search_policy != "ga" &&
         config_.search_policy != "greedy_only") ||
        config_.population_size == 0 || config_.iterations == 0 ||
        config_.neighbors_per_solution == 0 ||
        config_.neighbor_sample_size == 0 || config_.elite_count == 0 ||
        config_.elite_count > config_.population_size ||
        config_.direct_enumeration_limit == 0 ||
        config_.return_candidate_limit == 0 ||
        config_.return_assignment_k == 0 ||
        (config_.forecast_gate_candidate_budget != 1 &&
         config_.forecast_gate_candidate_budget != 2 &&
         config_.forecast_gate_candidate_budget != 4) ||
        config_.crossover_rate < 0.0 ||
        config_.crossover_rate > 1.0) {
      throw std::invalid_argument("invalid rich GA configuration");
    }
    if (config_.max_unique_evaluations == 0) {
      throw std::invalid_argument("rich stochastic unique budget must be positive");
    }
    if (config_.forecast_mode != "decay" ||
        config_.forecast_policy != "physical_terminal_decay_v1" ||
        config_.decay_kind != "geometric") {
      throw std::invalid_argument("unsupported rich forecast specification");
    }
    if (config_.max_horizon > 8 || config_.alpha_lookahead < 0.0 ||
        config_.decay_rho <= 0.0 || config_.decay_rho > 1.0 ||
        config_.decay_epsilon < 0.0 || config_.decay_epsilon > 1.0) {
      throw std::invalid_argument("invalid bounded decay parameters");
    }
    for (const auto& domain : problem_.gate_domains) {
      if (domain.empty()) throw std::invalid_argument("empty rich gate domain");
    }
    for (const auto& term : problem_.forecast_terms) {
      const auto depth_zero_state =
          term.depth == 0 && config_.max_horizon == 0;
      if ((!depth_zero_state &&
           (term.depth == 0 || term.depth > config_.max_horizon)) ||
          term.nll < 0.0 || !std::isfinite(term.nll)) {
        throw std::invalid_argument("forecast term exceeds bounded decay contract");
      }
      const auto eligible_count = static_cast<std::int64_t>(problem_.eligible.size());
      const auto gate_count = static_cast<std::int64_t>(problem_.gate_domains.size());
      if ((term.kind == RichForecastKind::kStay ||
           term.kind == RichForecastKind::kReturn ||
           term.kind == RichForecastKind::kReturnSite ||
           term.kind == RichForecastKind::kStayPair ||
           term.kind == RichForecastKind::kReturnPair) &&
          (term.index < 0 || term.index >= eligible_count)) {
        throw std::invalid_argument("forecast eligible index is out of range");
      }
      if ((term.kind == RichForecastKind::kStayPair ||
           term.kind == RichForecastKind::kReturnPair) &&
          (term.second_index < 0 || term.second_index >= eligible_count)) {
        throw std::invalid_argument("forecast pair index is out of range");
      }
      if (term.kind == RichForecastKind::kGateOption &&
          (term.index < 0 || term.index >= gate_count || term.selector < 0 ||
           static_cast<std::size_t>(term.selector) >=
               problem_.gate_domains[static_cast<std::size_t>(term.index)].size())) {
        throw std::invalid_argument("forecast gate selector is out of range");
      }
      if (term.kind == RichForecastKind::kReturnSite && term.selector < 0) {
        throw std::invalid_argument("forecast RETURN selector is invalid");
      }
    }
    if (!problem_.forecast_terms.empty() && !problem_.future_layers.empty()) {
      throw std::invalid_argument(
          "precomputed forecast terms and native future layers are exclusive");
    }
    if (!problem_.future_layers.empty() &&
        architecture_.entangling_site_pairs().empty()) {
      throw std::invalid_argument(
          "native future rollout requires entangling site pairs");
    }
    if (problem_.terminal_boundary && !problem_.future_layers.empty()) {
      throw std::invalid_argument(
          "terminal boundary cannot contain future layers");
    }
    std::size_t previous_depth = 0;
    for (const auto& layer : problem_.future_layers) {
      if (layer.depth == 0 || layer.depth > config_.max_horizon ||
          layer.depth <= previous_depth) {
        throw std::invalid_argument(
            "native future layer exceeds bounded sorted horizon");
      }
      previous_depth = layer.depth;
      std::set<std::int64_t> atoms;
      for (const auto& gate : layer.gates) {
        if (gate.first < 0 || gate.second < 0 || gate.first == gate.second ||
            static_cast<std::size_t>(gate.first) >= problem_.n_atoms ||
            static_cast<std::size_t>(gate.second) >= problem_.n_atoms ||
            !atoms.insert(gate.first).second ||
            !atoms.insert(gate.second).second) {
          throw std::invalid_argument("invalid atom-disjoint future 2Q layer");
        }
      }
    }
    std::set<std::size_t> eviction;
    for (const auto index : problem_.eviction_order_indices) {
      if (index >= problem_.eligible.size() || !eviction.insert(index).second) {
        throw std::invalid_argument("eviction order is not a permutation");
      }
    }
  }

  std::vector<std::int64_t> normalize(
      const std::vector<std::int64_t>& chromosome) {
    const auto started = Clock::now();
    const auto cached = normalize_cache_.find(chromosome);
    if (config_.fitness_cache && cached != normalize_cache_.end()) {
      normalize_ns_ += elapsed_ns(started);
      return cached->second;
    }
    const auto gate_count = problem_.gate_domains.size();
    const auto eligible_count = problem_.eligible.size();
    if (chromosome.size() != gate_count + eligible_count) {
      throw std::invalid_argument("rich chromosome length mismatch");
    }
    std::vector<std::int64_t> value(chromosome.size());
    for (std::size_t gate = 0; gate < gate_count; ++gate) {
      value[gate] = static_cast<std::int64_t>(positive_mod(
          chromosome[gate], problem_.gate_domains[gate].size()));
    }
    std::size_t selected = 0;
    for (std::size_t index = 0; index < eligible_count; ++index) {
      bool bit = chromosome[gate_count + index] != 0;
      if (problem_.decision_policy == RichDecisionPolicy::kAlwaysStay) bit = false;
      if (problem_.decision_policy == RichDecisionPolicy::kAlwaysReturn ||
          problem_.decision_policy == RichDecisionPolicy::kAdjacentOnly) bit = true;
      if (problem_.decision_policy == RichDecisionPolicy::kOptimize &&
          problem_.forced_return_mask[index]) {
        bit = true;
      }
      value[gate_count + index] = bit ? 1 : 0;
      const auto atom = static_cast<std::size_t>(problem_.eligible[index]);
      // A participant RETURN is a back->out cycle and therefore does not
      // release target-layer entangling-zone capacity.  min_returns counts
      // only ordinary resident evictions.
      if (bit && !participant_mask_[atom]) ++selected;
    }
    if (selected < problem_.min_returns) {
      for (const auto index : problem_.eviction_order_indices) {
        const auto atom = static_cast<std::size_t>(problem_.eligible[index]);
        if (participant_mask_[atom]) continue;
        if (value[gate_count + index] == 0) {
          value[gate_count + index] = 1;
          if (++selected == problem_.min_returns) break;
        }
      }
    }
    if (config_.fitness_cache) normalize_cache_[chromosome] = value;
    normalize_ns_ += elapsed_ns(started);
    return value;
  }

  DecodeResult decode(const std::vector<std::int64_t>& chromosome) {
    const auto started = Clock::now();
    const auto gate_count = problem_.gate_domains.size();
    std::vector<std::int64_t> gate_key(chromosome.begin(),
                                       chromosome.begin() + gate_count);
    const auto cached = decode_cache_.find(gate_key);
    if (config_.fitness_cache && cached != decode_cache_.end()) {
      ++stats_.decode_hits;
      decode_ns_ += elapsed_ns(started);
      return cached->second;
    }
    DecodeResult result;
    std::set<std::int64_t> used_sites;
    std::vector<Leg> accumulated_legs;
    auto accumulated_ghosts = problem_.static_ghosts;
    for (std::size_t gate = 0; gate < gate_count; ++gate) {
      const auto& domain = problem_.gate_domains[gate];
      const auto begin = static_cast<std::size_t>(gate_key[gate]);
      std::optional<std::size_t> selected;
      for (std::size_t offset = 0; offset < domain.size(); ++offset) {
        const auto index = (begin + offset) % domain.size();
        const auto& option = domain[index];
        if (used_sites.count(option.site_id) != 0U) continue;
        if (has_new_ghost_conflict(accumulated_legs, option.legs,
                                   accumulated_ghosts)) {
          continue;
        }
        selected = index;
        break;
      }
      if (!selected.has_value()) {
        for (std::size_t offset = 0; offset < domain.size(); ++offset) {
          const auto index = (begin + offset) % domain.size();
          if (used_sites.count(domain[index].site_id) == 0U) {
            selected = index;
            break;
          }
        }
      }
      if (!selected.has_value()) {
        result.feasible = false;
        result.error = "gate menu cannot form an injection";
        break;
      }
      const auto& option = domain[*selected];
      used_sites.insert(option.site_id);
      result.option_indices.push_back(*selected);
      accumulated_legs.insert(accumulated_legs.end(), option.legs.begin(),
                              option.legs.end());
      accumulated_ghosts.insert(accumulated_ghosts.end(),
                                option.seated_ghosts.begin(),
                                option.seated_ghosts.end());
    }
    if (config_.fitness_cache) decode_cache_[gate_key] = result;
    decode_ns_ += elapsed_ns(started);
    return result;
  }

  std::vector<std::vector<ReturnAssignment>> match_returns(
      const std::vector<std::size_t>& returners) {
    const auto started = Clock::now();
    const auto cached = return_cache_.find(returners);
    if (config_.fitness_cache && cached != return_cache_.end()) {
      ++stats_.return_match_hits;
      return_match_ns_ += elapsed_ns(started);
      return cached->second;
    }
    std::vector<std::vector<ReturnAssignment>> result;
    const auto solutions = k_best_assignments(
        return_domains_, returners, config_.return_assignment_k);
    result.reserve(solutions.size());
    for (const auto& solution : solutions) {
      std::vector<ReturnAssignment> assignment;
      assignment.reserve(returners.size());
      for (std::size_t row = 0; row < returners.size(); ++row) {
        const auto eligible_index = returners[row];
        const auto site_id = solution.site_ids[row];
        const auto& domain = return_domains_[eligible_index];
        const auto found = std::find_if(
            domain.begin(), domain.end(), [&](const auto& option) {
              return option.site_id == site_id;
            });
        if (found == domain.end()) {
          throw std::runtime_error("K-best result is absent from RETURN domain");
        }
        assignment.push_back({eligible_index, site_id, found->point});
      }
      result.push_back(std::move(assignment));
    }
    if (config_.fitness_cache) return_cache_[returners] = result;
    return_match_ns_ += elapsed_ns(started);
    return result;
  }

  void compile_forecast() {
    forecast_word_count_ = (problem_.forecast_terms.size() + 63U) / 64U;
    const auto empty_bits = ForecastBits(forecast_word_count_, 0U);
    compiled_forecast_terms_.assign(
        problem_.forecast_terms.size(), CompiledForecastTerm{});
    constant_forecast_bits_ = empty_bits;
    stay_forecast_bits_.assign(problem_.eligible.size(), empty_bits);
    return_forecast_bits_.assign(problem_.eligible.size(), empty_bits);
    gate_option_forecast_bits_.resize(problem_.gate_domains.size());
    for (std::size_t gate = 0; gate < problem_.gate_domains.size(); ++gate) {
      gate_option_forecast_bits_[gate].assign(
          problem_.gate_domains[gate].size(), empty_bits);
    }

    for (std::size_t index = 0; index < problem_.forecast_terms.size();
         ++index) {
      const auto& term = problem_.forecast_terms[index];
      if (term.depth == 0) {
        // M3 depth-zero Phi(s') is already scaled by its registered weight in
        // the problem DTO.  It contains no future layer and therefore must not
        // receive alpha or geometric decay a second time.
        compiled_forecast_terms_[index] = {
            term.nll,
            0,
            static_cast<std::size_t>(term.category),
            true,
        };
      } else {
      const auto decay_factor = std::pow(
          config_.decay_rho, static_cast<double>(term.depth - 1));
      if (decay_factor < config_.decay_epsilon) {
        ++forecast_terms_skipped_cutoff_;
        continue;
      }
      compiled_forecast_terms_[index] = {
          config_.alpha_lookahead * decay_factor * term.nll,
          term.depth,
          static_cast<std::size_t>(term.category),
          true,
      };
      }
      const auto eligible_index = static_cast<std::size_t>(term.index);
      switch (term.kind) {
        case RichForecastKind::kConstant:
          set_forecast_bit(constant_forecast_bits_, index);
          break;
        case RichForecastKind::kStay:
          set_forecast_bit(stay_forecast_bits_[eligible_index], index);
          break;
        case RichForecastKind::kReturn:
          set_forecast_bit(return_forecast_bits_[eligible_index], index);
          break;
        case RichForecastKind::kReturnSite: {
          auto& bits = return_site_forecast_bits_[
              {eligible_index, term.selector}];
          if (bits.empty()) bits = empty_bits;
          set_forecast_bit(bits, index);
          break;
        }
        case RichForecastKind::kGateOption:
          set_forecast_bit(
              gate_option_forecast_bits_[eligible_index]
                                        [static_cast<std::size_t>(term.selector)],
              index);
          break;
        case RichForecastKind::kStayPair: {
          auto& bits = stay_pair_forecast_bits_[
              {eligible_index, static_cast<std::size_t>(term.second_index)}];
          if (bits.empty()) bits = empty_bits;
          set_forecast_bit(bits, index);
          break;
        }
        case RichForecastKind::kReturnPair: {
          auto& bits = return_pair_forecast_bits_[
              {eligible_index, static_cast<std::size_t>(term.second_index)}];
          if (bits.empty()) bits = empty_bits;
          set_forecast_bit(bits, index);
          break;
        }
      }
    }
  }

  void compile_future_layers() {
    const auto layer_count = problem_.future_layers.size();
    future_participant_masks_.assign(
        layer_count, std::vector<unsigned char>(problem_.n_atoms, 0U));
    future_decay_.resize(layer_count);
    for (std::size_t index = 0; index < layer_count; ++index) {
      for (const auto& gate : problem_.future_layers[index].gates) {
        future_participant_masks_[index][
            static_cast<std::size_t>(gate.first)] = 1U;
        future_participant_masks_[index][
            static_cast<std::size_t>(gate.second)] = 1U;
      }
      future_decay_[index] = std::pow(
          config_.decay_rho,
          static_cast<double>(problem_.future_layers[index].depth - 1));
    }
  }

  void apply_forecast(Evaluated& result,
                      const std::vector<std::int64_t>& chromosome) {
    const auto started = Clock::now();
    result.forecast_by_depth.assign(config_.max_horizon + 1, 0.0);
    result.forecast_by_category.fill(0.0);
    result.forecast_nll = 0.0;
    if (config_.max_horizon == 0 && problem_.forecast_terms.empty()) {
      // The reference M3 profile remains the exact current-boundary control.
      // A tuned M3 may carry explicit depth-zero Phi(s') terms, handled by the
      // same native predicate engine below without reading future_layers.
      result.search_nll = result.fitness.negative_log_fidelity;
      forecast_ns_ += elapsed_ns(started);
      return;
    }
    if (!problem_.future_layers.empty()) {
      // Rollout layer terms are alpha-weighted, but endpoint Phi(s_H) is not.
      // Do not silently remove that Bellman value when alpha is zero.
      apply_native_rollout(result);
      forecast_ns_ += elapsed_ns(started);
      return;
    }
    if (problem_.forecast_terms.empty() ||
        (config_.alpha_lookahead == 0.0 && config_.max_horizon != 0)) {
      result.search_nll = result.fitness.negative_log_fidelity;
      forecast_ns_ += elapsed_ns(started);
      return;
    }
    const auto gate_count = problem_.gate_domains.size();
    auto applicable = constant_forecast_bits_;
    for (std::size_t gate = 0; gate < gate_count; ++gate) {
      merge_forecast_bits(
          applicable,
          gate_option_forecast_bits_[gate][result.decoded.option_indices[gate]]);
    }
    for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
      const auto returned = chromosome[gate_count + index] != 0;
      merge_forecast_bits(
          applicable,
          returned ? return_forecast_bits_[index] : stay_forecast_bits_[index]);
    }
    for (const auto& assignment : result.assignments) {
      const auto found = return_site_forecast_bits_.find(
          {assignment.eligible_index, assignment.site_id});
      if (found != return_site_forecast_bits_.end()) {
        merge_forecast_bits(applicable, found->second);
      }
    }
    const auto returned = [&](std::size_t index) {
      return chromosome[gate_count + index] != 0;
    };
    for (const auto& [indices, bits] : stay_pair_forecast_bits_) {
      if (!returned(indices.first) && !returned(indices.second)) {
        merge_forecast_bits(applicable, bits);
      }
    }
    for (const auto& [indices, bits] : return_pair_forecast_bits_) {
      if (returned(indices.first) && returned(indices.second)) {
        merge_forecast_bits(applicable, bits);
      }
    }

    stats_.forecast_terms_skipped_cutoff +=
        forecast_terms_skipped_cutoff_;
    for (std::size_t word_index = 0; word_index < applicable.size();
         ++word_index) {
      auto word = applicable[word_index];
      while (word != 0U) {
        const auto bit = trailing_zeroes(word);
        const auto index = word_index * 64U + bit;
        const auto& term = compiled_forecast_terms_[index];
        if (!term.active) {
          throw std::logic_error("inactive forecast term is marked applicable");
        }
        result.forecast_nll += term.contribution;
        result.forecast_by_depth[term.depth] += term.contribution;
        result.forecast_by_category[term.category] += term.contribution;
        ++stats_.forecast_terms_applied;
        word &= word - 1U;
      }
    }
    result.search_nll = result.fitness.negative_log_fidelity +
                        result.forecast_nll;
    forecast_ns_ += elapsed_ns(started);
  }

  std::vector<Ghost> forecast_ghosts(
      const std::vector<Point>& positions) const {
    std::vector<Ghost> ghosts;
    ghosts.reserve(positions.size());
    for (std::size_t atom = 0; atom < positions.size(); ++atom) {
      ghosts.push_back({static_cast<std::int64_t>(atom), positions[atom]});
    }
    return ghosts;
  }

  FitnessResult score_forecast_phase(
      const std::vector<Leg>& legs, const std::vector<std::int64_t>& owners,
      const std::vector<Point>& positions,
      const std::vector<double>& accumulated_idle,
      std::int64_t idle_exposures = 0) const {
    CandidatePlan candidate;
    candidate.idle_exposures = idle_exposures;
    if (!legs.empty()) {
      MovementPhase phase;
      phase.legs = legs;
      phase.owners = owners;
      phase.ghosts = forecast_ghosts(positions);
      candidate.phases.push_back(std::move(phase));
    }
    BoundaryConfig boundary_config;
    boundary_config.exact_coloring_threshold = config_.exact_coloring_threshold;
    boundary_config.enforce_single_leg_ghost =
        config_.enforce_single_leg_ghost;
    auto result = evaluate_candidate_summary(
        architecture_, candidate, boundary_config, accumulated_idle);
    if (!result.feasible &&
        result.error == "linear coherence model out of domain") {
      // Recover the candidate's physical movement delta without a prior, then
      // rank it under the exponential continuation.  Ghost infeasibility and
      // every other physical failure remain hard failures.
      result = evaluate_candidate_summary(
          architecture_, candidate, boundary_config,
          std::vector<double>(architecture_.n_atoms(), 0.0));
      if (result.feasible) {
        const auto coherence_nll = search_coherence_delta_nll(
            accumulated_idle, result.candidate_idle_time_us);
        result.negative_log_fidelity +=
            coherence_nll - result.coherence_nll;
        result.coherence_nll = coherence_nll;
      }
    }
    return result;
  }

  FitnessResult score_forecast_single_leg(
      std::int64_t owner, const Point& source, const Point& target,
      const std::vector<Point>& positions,
      const std::vector<double>& accumulated_idle) const {
    FitnessResult result;
    const auto distance = point_distance(source, target);
    for (std::size_t atom = 0; atom < positions.size(); ++atom) {
      if (static_cast<std::int64_t>(atom) == owner) continue;
      const auto x = cover(source.x, target.x, positions[atom].x);
      const auto y = cover(source.y, target.y, positions[atom].y);
      if (coverage_matches(x, y)) {
        result.feasible = false;
        result.negative_log_fidelity =
            std::numeric_limits<double>::infinity();
        result.transfer_nll = result.negative_log_fidelity;
        result.idle_excitation_nll = result.negative_log_fidelity;
        result.coherence_nll = result.negative_log_fidelity;
        result.error = "phase 0 has no ghost-safe straight-leg batch order";
        return result;
      }
    }
    const auto phase_time =
        2.0 * kTransferUs + std::sqrt(distance / kAccelUmPerUs2);
    std::vector<double> candidate_idle(architecture_.n_atoms(), phase_time);
    candidate_idle[static_cast<std::size_t>(owner)] -= 2.0 * kTransferUs;
    const auto coherence_nll = search_coherence_delta_nll(
        accumulated_idle, candidate_idle);
    result.move_batches = 1;
    result.move_time_us = phase_time;
    result.total_distance_um = distance;
    result.transfers = 2;
    result.transfer_nll = -2.0 * std::log(kFTransfer);
    result.coherence_nll = coherence_nll;
    result.negative_log_fidelity =
        result.transfer_nll + result.coherence_nll;
    result.candidate_idle_time_us = std::move(candidate_idle);
    return result;
  }

  bool is_zone_point(const Point& point) const {
    return zone_points_.count({point.x, point.y}) != 0U;
  }

  bool point_occupied(const std::vector<Point>& positions, const Point& point,
                      std::int64_t except_atom = -1) const {
    for (std::size_t atom = 0; atom < positions.size(); ++atom) {
      if (static_cast<std::int64_t>(atom) == except_atom) continue;
      if (same_point(positions[atom], point)) return true;
    }
    return false;
  }

  std::optional<std::pair<std::int64_t, FitnessResult>>
  forecast_move_to_storage(std::int64_t atom,
                           std::vector<Point>& positions,
                           const std::vector<double>& accumulated_idle) const {
    const auto& source = positions[static_cast<std::size_t>(atom)];
    const auto storage_count = architecture_.storage_site_ids().size();
    std::size_t query_limit = std::min<std::size_t>(
        storage_count,
        std::max<std::size_t>(
            16, positions.size() + 4 * config_.forecast_gate_candidate_budget));
    while (query_limit != 0) {
      const auto& choices =
          architecture_.nearest_storage_site_ids(source, query_limit);
      std::optional<std::pair<std::int64_t, FitnessResult>> best;
      std::size_t tested = 0;
      for (const auto site_id : choices) {
        const auto& target = architecture_.site_coordinates()[
            static_cast<std::size_t>(site_id)];
        if (point_occupied(positions, target, atom)) continue;
        const auto distance = point_distance(source, target);
        if (distance <= 1e-9) continue;
        const auto score = score_forecast_single_leg(
            atom, source, target, positions, accumulated_idle);
        if (!score.feasible) continue;
        ++tested;
        if (!best.has_value() ||
            std::tie(score.negative_log_fidelity, score.move_batches,
                     score.move_time_us, score.total_distance_um, site_id) <
                std::tie(best->second.negative_log_fidelity,
                         best->second.move_batches, best->second.move_time_us,
                         best->second.total_distance_um, best->first)) {
          best = std::make_pair(site_id, score);
        }
        // The registered 1/2/4 rollout budget controls the number of
        // physically replayed safe parking alternatives.  Querying an exact
        // nearest prefix preserves the full-sort choice while avoiding a
        // 10,000-site vector for every transient forecast position.
        if (best.has_value() &&
            tested >= config_.forecast_gate_candidate_budget) {
          break;
        }
      }
      if (tested >= config_.forecast_gate_candidate_budget ||
          query_limit == storage_count) {
        if (best.has_value()) {
          positions[static_cast<std::size_t>(atom)] =
              architecture_.site_coordinates()[
                  static_cast<std::size_t>(best->first)];
        }
        return best;
      }
      query_limit = std::min(storage_count, query_limit * 2);
    }
    return std::nullopt;
  }

  static double finite_nll(const FitnessResult& score) {
    return score.feasible ? score.negative_log_fidelity
                          : std::numeric_limits<double>::infinity();
  }

  FitnessResult score_forecast_relocation_batch(
      const std::vector<Point>& before, const std::vector<Point>& after,
      const std::vector<std::int64_t>& atoms,
      const std::vector<double>& accumulated_idle) const {
    std::vector<Leg> legs;
    std::vector<std::int64_t> owners;
    for (const auto atom : atoms) {
      const auto index = static_cast<std::size_t>(atom);
      const auto distance = point_distance(before[index], after[index]);
      if (distance <= 1e-9) continue;
      legs.push_back({distance, before[index], after[index]});
      owners.push_back(atom);
    }
    return score_forecast_phase(legs, owners, before, accumulated_idle);
  }

  void add_weighted_forecast(Evaluated& result, std::size_t depth,
                             std::size_t category, double raw_nll) {
    const auto decay = std::pow(
        config_.decay_rho, static_cast<double>(depth - 1));
    if (decay < config_.decay_epsilon) {
      ++stats_.forecast_terms_skipped_cutoff;
      return;
    }
    const auto contribution = config_.alpha_lookahead * decay * raw_nll;
    result.forecast_nll += contribution;
    result.forecast_by_depth[depth] += contribution;
    result.forecast_by_category[category] += contribution;
  }

  void add_terminal_cleanup_potential(Evaluated& result, std::size_t depth,
                                      double raw_nll) {
    // The endpoint belongs to the same uncertain forecast as every visible
    // layer.  Weight it on that layer's physical scale instead of letting an
    // unattenuated all-RETURN proxy dominate the rolling objective.  The
    // resident-rent deadline in zplacer owns anti-procrastination progress.
    const auto decay = std::pow(
        config_.decay_rho, static_cast<double>(depth - 1));
    if (decay < config_.decay_epsilon) {
      ++stats_.forecast_terms_skipped_cutoff;
      return;
    }
    const auto contribution = config_.alpha_lookahead * decay * raw_nll;
    result.forecast_nll += contribution;
    result.forecast_by_depth[depth] += contribution;
    result.forecast_by_category[2] += contribution;
    ++stats_.forecast_terms_applied;
  }

  void apply_native_rollout(Evaluated& result) {
    if (config_.max_horizon == 0) {
      // Strict H=0 is the current-boundary control.  In particular, an
      // ordinary non-terminal boundary may still leave eligible residents in
      // the entangling zone; that state must not be charged the rollout's
      // terminal all-RETURN proxy.  Explicit depth-zero value terms are
      // handled by apply_forecast() before this physical-rollout entry point.
      result.forecast_nll = 0.0;
      result.search_nll = result.fitness.negative_log_fidelity;
      std::fill(result.forecast_by_depth.begin(),
                result.forecast_by_depth.end(), 0.0);
      result.forecast_by_category.fill(0.0);
      return;
    }
    auto positions = problem_.current_points;
    auto accumulated_idle = problem_.prior_idle_time_us;
    if (result.fitness.candidate_idle_time_us.size() != problem_.n_atoms) {
      throw std::logic_error("current ABI8 fitness lacks per-atom idle delta");
    }
    for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
      accumulated_idle[atom] += result.fitness.candidate_idle_time_us[atom];
    }
    const auto advance_idle_into = [&](std::vector<double>& idle,
                                       const FitnessResult& score) {
      if (!score.feasible) return false;
      if (score.candidate_idle_time_us.size() != problem_.n_atoms) {
        throw std::logic_error("forecast fitness lacks per-atom idle delta");
      }
      for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
        idle[atom] += score.candidate_idle_time_us[atom];
      }
      return true;
    };
    const auto advance_idle = [&](const FitnessResult& score) {
      return advance_idle_into(accumulated_idle, score);
    };
    const auto reject_forecast = [&](std::string error) {
      result.forecast_feasible = false;
      result.forecast_error = std::move(error);
      result.forecast_nll = std::numeric_limits<double>::infinity();
      result.search_nll = std::numeric_limits<double>::infinity();
      std::fill(result.forecast_by_depth.begin(),
                result.forecast_by_depth.end(), 0.0);
      result.forecast_by_category.fill(0.0);
    };

    // Select storage endpoints one atom at a time because every later choice
    // must see the sites already occupied by earlier atoms.  The exact batch
    // replay remains the preferred physical schedule.  If that joint replay
    // is more conservative than the already witnessed safe singleton order,
    // retain the witnessed order and its real transfer/coherence cost instead
    // of replacing a physically executable forecast by infinity.
    const auto relocate_to_storage = [&] (
        const std::vector<std::int64_t>& atoms) -> std::optional<double> {
      if (atoms.empty()) return 0.0;
      const auto before = positions;
      const auto idle_before = accumulated_idle;
      auto singleton_idle = accumulated_idle;
      double singleton_nll = 0.0;
      for (const auto atom : atoms) {
        const auto moved = forecast_move_to_storage(
            atom, positions, singleton_idle);
        if (!moved.has_value()) {
          positions = before;
          accumulated_idle = idle_before;
          return std::nullopt;
        }
        singleton_nll += finite_nll(moved->second);
        if (!advance_idle_into(singleton_idle, moved->second)) {
          throw std::logic_error(
              "ghost-safe forecast singleton unexpectedly became infeasible");
        }
      }
      const auto batched = score_forecast_relocation_batch(
          before, positions, atoms, idle_before);
      if (batched.feasible) {
        accumulated_idle = idle_before;
        advance_idle(batched);
        return finite_nll(batched);
      }
      accumulated_idle = std::move(singleton_idle);
      return singleton_nll;
    };

    // A future parallel front can contain a source/target precedence cycle
    // even though every requested endpoint is legal (the QFT-style swap is
    // the minimal example).  First use the strict phase replay.  When it
    // proves infeasible, park a bounded number of participants at real vacant
    // storage sites.  After every parking move retry the complete remaining
    // phase, so an executable ordered/batched solution always wins over the
    // fallback.  Every fallback leg is scored at the idle state at which it is
    // actually executed; there is no ghost proxy and no zero-cost transition.
    const auto score_reentry = [&] (
        const std::vector<std::pair<std::int64_t, Point>>& targets
        ) -> std::optional<double> {
      const auto positions_before = positions;
      const auto idle_before = accumulated_idle;
      const auto remaining_phase = [&]() {
        std::vector<Leg> legs;
        std::vector<std::int64_t> owners;
        legs.reserve(targets.size());
        owners.reserve(targets.size());
        for (const auto& [atom, target] : targets) {
          const auto index = static_cast<std::size_t>(atom);
          const auto distance = point_distance(positions[index], target);
          if (distance <= 1e-9) continue;
          legs.push_back({distance, positions[index], target});
          owners.push_back(atom);
        }
        return score_forecast_phase(
            legs, owners, positions, accumulated_idle);
      };
      const auto commit_targets = [&]() {
        for (const auto& [atom, target] : targets) {
          positions[static_cast<std::size_t>(atom)] = target;
        }
      };

      auto strict = remaining_phase();
      if (strict.feasible) {
        advance_idle(strict);
        commit_targets();
        return finite_nll(strict);
      }

      std::vector<unsigned char> parked(problem_.n_atoms, 0U);
      double recovery_nll = 0.0;
      std::size_t parking_moves = 0;
      while (parking_moves < targets.size()) {
        bool moved = false;
        for (const auto& [atom, target] : targets) {
          const auto index = static_cast<std::size_t>(atom);
          if (same_point(positions[index], target) || parked[index] != 0U) {
            continue;
          }
          const auto parked_move = forecast_move_to_storage(
              atom, positions, accumulated_idle);
          if (!parked_move.has_value()) continue;
          recovery_nll += finite_nll(parked_move->second);
          advance_idle(parked_move->second);
          parked[index] = 1U;
          ++parking_moves;
          moved = true;
          break;
        }
        if (!moved) break;

        strict = remaining_phase();
        if (strict.feasible) {
          recovery_nll += finite_nll(strict);
          advance_idle(strict);
          commit_targets();
          return recovery_nll;
        }
      }

      // The bounded parking pass normally makes the residual phase replayable.
      // Keep a final bounded singleton replay for geometries where conservative
      // combined endpoint precedence still rejects an actually safe order.
      for (std::size_t step = 0; step < targets.size(); ++step) {
        bool moved = false;
        for (const auto& [atom, target] : targets) {
          const auto index = static_cast<std::size_t>(atom);
          if (same_point(positions[index], target)) continue;
          const auto single = score_forecast_single_leg(
              atom, positions[index], target, positions, accumulated_idle);
          if (!single.feasible) continue;
          recovery_nll += finite_nll(single);
          advance_idle(single);
          positions[index] = target;
          moved = true;
          break;
        }
        if (!moved) break;
      }
      const auto complete = std::all_of(
          targets.begin(), targets.end(), [&](const auto& value) {
            return same_point(
                positions[static_cast<std::size_t>(value.first)],
                value.second);
          });
      if (!complete) {
        positions = positions_before;
        accumulated_idle = idle_before;
        return std::nullopt;
      }
      return recovery_nll;
    };
    for (const auto& assignment : result.assignments) {
      positions[static_cast<std::size_t>(
          problem_.eligible[assignment.eligible_index])] = assignment.point;
    }
    for (const auto& reseat : result.reseats) {
      positions[static_cast<std::size_t>(
          problem_.eligible[reseat.eligible_index])] = reseat.point;
    }
    for (std::size_t gate = 0; gate < problem_.gate_domains.size(); ++gate) {
      const auto& option = problem_.gate_domains[gate][
          result.decoded.option_indices[gate]];
      positions[static_cast<std::size_t>(option.q1)] = option.target1;
      positions[static_cast<std::size_t>(option.q2)] = option.target2;
    }

    const auto state_key = forecast_state_key(positions, accumulated_idle);
    if (config_.fitness_cache) {
      const auto cached = forecast_state_cache_.find(state_key);
      if (cached != forecast_state_cache_.end()) {
        result.forecast_nll = cached->second.nll;
        result.forecast_by_depth = cached->second.by_depth;
        result.forecast_by_category = cached->second.by_category;
        result.search_nll = result.fitness.negative_log_fidelity +
                            result.forecast_nll;
        stats_.forecast_terms_applied += cached->second.terms_applied;
        stats_.forecast_terms_skipped_cutoff +=
            cached->second.terms_skipped_cutoff;
        ++stats_.forecast_state_cache_hits;
        return;
      }
    }
    const auto terms_applied_before = stats_.forecast_terms_applied;
    const auto cutoff_before = stats_.forecast_terms_skipped_cutoff;
    const auto remember = [&]() {
      if (!config_.fitness_cache) return;
      forecast_state_cache_.emplace(
          state_key,
          ForecastReplay{
              result.forecast_nll,
              result.forecast_by_depth,
              result.forecast_by_category,
              stats_.forecast_terms_applied - terms_applied_before,
              stats_.forecast_terms_skipped_cutoff - cutoff_before,
          });
    };

    const auto& site_pairs = architecture_.entangling_site_pairs();
    std::vector<std::uint32_t> used_pair_epoch(site_pairs.size(), 0U);
    std::uint32_t pair_epoch = 0U;
    for (std::size_t layer_index = 0;
         layer_index < problem_.future_layers.size(); ++layer_index) {
      const auto& layer = problem_.future_layers[layer_index];
      const auto decay = future_decay_[layer_index];
      if (decay < config_.decay_epsilon) {
        ++stats_.forecast_terms_skipped_cutoff;
        continue;
      }
      const auto& participant_mask =
          future_participant_masks_[layer_index];

      struct FuturePlacement {
        std::int64_t q1{};
        std::int64_t q2{};
        Point target1;
        Point target2;
      };
      // Future gate-site selection is evaluated many times for the same
      // physical state.  Looking for blockers by scanning every atom for
      // every pair/orientation made the rollout quadratic in the number of
      // atoms.  Preserve the exact duplicate-occupancy semantics by indexing
      // every point to all atoms currently held there.
      const auto linear_occupants = positions.size() <= 64;
      std::map<std::pair<double, double>, std::vector<std::int64_t>>
          occupants_by_point;
      if (!linear_occupants) {
        for (std::size_t atom = 0; atom < positions.size(); ++atom) {
          occupants_by_point[{positions[atom].x, positions[atom].y}].push_back(
              static_cast<std::int64_t>(atom));
        }
      }
      const auto for_each_occupant = [&](const Point& target,
                                         const auto& callback) {
        if (linear_occupants) {
          for (std::size_t atom = 0; atom < positions.size(); ++atom) {
            if (same_point(positions[atom], target)) {
              callback(static_cast<std::int64_t>(atom));
            }
          }
          return;
        }
        const auto occupied = occupants_by_point.find({target.x, target.y});
        if (occupied == occupants_by_point.end()) return;
        for (const auto atom : occupied->second) callback(atom);
      };
      const auto blocker_distance = [&](const Point& target) {
        double total = 0.0;
        bool unavailable = false;
        for_each_occupant(target, [&](const auto atom_id) {
          if (participant_mask[static_cast<std::size_t>(atom_id)] != 0U) {
            return;
          }
          const auto atom = static_cast<std::size_t>(atom_id);
          const auto& ordered =
              architecture_.nearest_storage_site_ids(positions[atom], 1);
          if (ordered.empty()) {
            unavailable = true;
            return;
          }
          total += point_distance(
              positions[atom],
              architecture_.site_coordinates()[
                  static_cast<std::size_t>(ordered.front())]);
        });
        return unavailable ? std::numeric_limits<double>::infinity() : total;
      };
      std::vector<FuturePlacement> placements;
      placements.reserve(layer.gates.size());
      ++pair_epoch;
      for (const auto& gate : layer.gates) {
        std::optional<std::tuple<double, std::size_t, bool>> selected;
        const auto& first_source =
            positions[static_cast<std::size_t>(gate.first)];
        const auto& second_source =
            positions[static_cast<std::size_t>(gate.second)];
        const auto& first_distances =
            architecture_.entangling_endpoint_distances(first_source);
        const auto& second_distances =
            architecture_.entangling_endpoint_distances(second_source);
        // The old implementation sorted every pair/orientation by raw
        // movement cost before adding blocker cost.  Forecast positions are
        // highly transient, so most rankings missed the bounded cache and
        // paid O(P log P) allocation/sort work.  Scan all options once in
        // canonical (pair, reversed) order instead.  Blocker cost is
        // non-negative, so an option whose raw cost already exceeds the best
        // total cost cannot win.  The final (total, pair, reversed) key is
        // unchanged, preserving the exact winner and tie-break semantics.
        for (std::size_t pair_index = 0; pair_index < site_pairs.size();
             ++pair_index) {
          if (used_pair_epoch[pair_index] == pair_epoch) continue;
          const auto& pair = site_pairs[pair_index];
          const auto& left = architecture_.site_coordinates()[
              static_cast<std::size_t>(pair[0])];
          const auto& right = architecture_.site_coordinates()[
              static_cast<std::size_t>(pair[1])];
          for (const bool reversed : {false, true}) {
            const auto& first = reversed ? right : left;
            const auto& second = reversed ? left : right;
            const auto offset = 2 * pair_index;
            const auto raw_cost =
                (reversed ? first_distances[offset + 1]
                          : first_distances[offset]) +
                (reversed ? second_distances[offset]
                          : second_distances[offset + 1]);
            if (selected.has_value() &&
                raw_cost > std::get<0>(*selected)) {
              continue;
            }
            auto cost = raw_cost + blocker_distance(first);
            if (!same_point(first, second)) {
              cost += blocker_distance(second);
            }
            const auto key = std::make_tuple(cost, pair_index, reversed);
            if (!selected.has_value() || key < *selected) selected = key;
          }
        }
        if (!selected.has_value()) {
          reject_forecast(
              "bounded physical forecast future layer has no available "
              "entangling pair");
          return;
        }
        const auto pair_index = std::get<1>(*selected);
        const auto reversed = std::get<2>(*selected);
        used_pair_epoch[pair_index] = pair_epoch;
        const auto& pair = site_pairs[pair_index];
        const auto& left = architecture_.site_coordinates()[
            static_cast<std::size_t>(pair[0])];
        const auto& right = architecture_.site_coordinates()[
            static_cast<std::size_t>(pair[1])];
        placements.push_back({gate.first, gate.second,
                              reversed ? right : left,
                              reversed ? left : right});
      }

      std::vector<unsigned char> blocker_mask(positions.size(), 0U);
      for (const auto& placement : placements) {
        for (const auto& target : {placement.target1, placement.target2}) {
          for_each_occupant(target, [&](const auto atom_id) {
            const auto atom = static_cast<std::size_t>(atom_id);
            if (participant_mask[atom] == 0U) blocker_mask[atom] = 1U;
          });
        }
      }
      // A stationary atom intersected by any individual AOD leg is a real
      // future ghost.  Deterministically park it before the gate phase.
      for (const auto& placement : placements) {
        for (const auto& [atom, target] :
             {std::pair<std::int64_t, Point>{placement.q1, placement.target1},
              std::pair<std::int64_t, Point>{placement.q2, placement.target2}}) {
          const auto& source = positions[static_cast<std::size_t>(atom)];
          const auto distance = point_distance(source, target);
          if (distance <= 1e-9) continue;
          for (std::size_t index = 0; index < positions.size(); ++index) {
            if (participant_mask[index] != 0U) continue;
            const auto x = cover(source.x, target.x, positions[index].x);
            const auto y = cover(source.y, target.y, positions[index].y);
            if (coverage_matches(x, y)) blocker_mask[index] = 1U;
          }
        }
      }

      std::vector<std::int64_t> blockers;
      for (std::size_t atom = 0; atom < blocker_mask.size(); ++atom) {
        if (blocker_mask[atom] != 0U) {
          blockers.push_back(static_cast<std::int64_t>(atom));
        }
      }
      const auto routing_nll = relocate_to_storage(blockers);
      if (!routing_nll.has_value()) {
        reject_forecast(
            "bounded physical forecast blocker recovery exhausted safe "
            "storage moves");
        return;
      }

      std::vector<std::pair<std::int64_t, Point>> reentry_targets;
      reentry_targets.reserve(2 * placements.size());
      for (const auto& placement : placements) {
        for (const auto& [atom, target] :
             {std::pair<std::int64_t, Point>{placement.q1, placement.target1},
              std::pair<std::int64_t, Point>{placement.q2, placement.target2}}) {
          reentry_targets.emplace_back(atom, target);
        }
      }
      std::sort(reentry_targets.begin(), reentry_targets.end(),
                [](const auto& first, const auto& second) {
                  return first.first < second.first;
                });
      const auto reentry_nll = score_reentry(reentry_targets);
      if (!reentry_nll.has_value()) {
        reject_forecast(
            "bounded physical forecast reentry recovery exhausted safe "
            "parking and singleton moves");
        return;
      }

      std::int64_t idle_exposures = 0;
      for (std::size_t atom = 0; atom < positions.size(); ++atom) {
        if (participant_mask[atom] == 0U &&
            is_zone_point(positions[atom])) {
          ++idle_exposures;
        }
      }
      std::vector<double> pulse_idle(problem_.n_atoms, kRydbergUs);
      for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
        if (participant_mask[atom] != 0U) pulse_idle[atom] = 0.0;
      }
      const auto pulse_nll = search_coherence_delta_nll(
          accumulated_idle, pulse_idle);
      auto residency_nll =
          -static_cast<double>(idle_exposures) * std::log(kFExc) + pulse_nll;
      for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
        accumulated_idle[atom] += pulse_idle[atom];
      }

      add_weighted_forecast(result, layer.depth, 0, residency_nll);
      add_weighted_forecast(result, layer.depth, 1, *reentry_nll);
      add_weighted_forecast(result, layer.depth, 3, *routing_nll);
      ++stats_.forecast_terms_applied;
    }

    std::size_t endpoint_depth = 0;
    std::vector<unsigned char> endpoint_participant_mask(problem_.n_atoms, 0U);
    std::vector<unsigned char> endpoint_candidate_mask(problem_.n_atoms, 0U);
    if (!problem_.future_layers.empty()) {
      endpoint_depth = problem_.future_layers.back().depth;
      endpoint_participant_mask = future_participant_masks_.back();
      std::fill(endpoint_candidate_mask.begin(),
                endpoint_candidate_mask.end(), 1U);
    } else {
      // Strict H=0: only ordinary current eligible residents can contribute
      // to Phi(s_0).  No future representation is read or inferred.
      for (const auto atom_id : problem_.eligible) {
        const auto atom = static_cast<std::size_t>(atom_id);
        if (participant_mask_[atom] == 0U) endpoint_candidate_mask[atom] = 1U;
      }
      for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
        endpoint_participant_mask[atom] = participant_mask_[atom] ? 1U : 0U;
      }
    }
    std::vector<std::int64_t> endpoint_atoms;
    for (std::size_t atom = 0; atom < positions.size(); ++atom) {
      if (endpoint_candidate_mask[atom] == 0U ||
          endpoint_participant_mask[atom] != 0U ||
          !is_zone_point(positions[atom])) {
        continue;
      }
      endpoint_atoms.push_back(static_cast<std::int64_t>(atom));
    }
    const auto terminal_nll = relocate_to_storage(endpoint_atoms);
    if (!terminal_nll.has_value()) {
      reject_forecast(
          "bounded physical forecast terminal potential exhausted safe "
          "storage moves");
      return;
    }
    add_terminal_cleanup_potential(result, endpoint_depth, *terminal_nll);
    result.search_nll = result.fitness.negative_log_fidelity +
                        result.forecast_nll;
    remember();
  }

  PlanGeometry build_geometry(
      const DecodeResult& decoded,
      const std::vector<ReturnAssignment>& assignments,
      const std::vector<ReturnAssignment>& reseats,
      const std::vector<ParticipantParkingAssignment>& participant_parkings =
          {}) const {
    PlanGeometry geometry;
    geometry.positions_t1 = problem_.current_points;
    geometry.site_ids_t1 = problem_.current_site_ids;
    geometry.back_legs.reserve(assignments.size() + reseats.size());
    geometry.back_owners.reserve(assignments.size() + reseats.size());
    geometry.out_legs.reserve(problem_.participants.size());
    geometry.out_owners.reserve(problem_.participants.size());
    geometry.ghosts_t0.reserve(problem_.n_atoms);
    geometry.ghosts_t1.reserve(problem_.n_atoms);
    const auto append_back = [&](const ReturnAssignment& assignment) {
      const auto q = problem_.eligible[assignment.eligible_index];
      const auto& source = problem_.current_points[q];
      const auto distance = point_distance(source, assignment.point);
      if (distance > 1e-9) {
        geometry.back_legs.push_back({distance, source, assignment.point});
        geometry.back_owners.push_back(q);
      }
      geometry.positions_t1[q] = assignment.point;
      if (!geometry.site_ids_t1.empty()) {
        geometry.site_ids_t1[static_cast<std::size_t>(q)] = assignment.site_id;
      }
    };
    for (const auto& assignment : assignments) append_back(assignment);
    for (const auto& reseat : reseats) append_back(reseat);
    for (const auto& parking : participant_parkings) {
      const auto atom = static_cast<std::size_t>(parking.atom);
      const auto& source = problem_.current_points[atom];
      const auto distance = point_distance(source, parking.point);
      if (distance > 1e-9) {
        geometry.back_legs.push_back({distance, source, parking.point});
        geometry.back_owners.push_back(parking.atom);
      }
      geometry.positions_t1[atom] = parking.point;
      if (!geometry.site_ids_t1.empty()) {
        geometry.site_ids_t1[atom] = parking.site_id;
      }
    }

    constexpr std::size_t kStackAtoms = 256;
    if (geometry.positions_t1.size() <= kStackAtoms) {
      std::array<std::size_t, kStackAtoms> order{};
      for (std::size_t atom = 0; atom < geometry.positions_t1.size(); ++atom) {
        order[atom] = atom;
      }
      std::sort(order.begin(), order.begin() + geometry.positions_t1.size(),
                [&](auto first, auto second) {
                  const auto& left = geometry.positions_t1[first];
                  const auto& right = geometry.positions_t1[second];
                  return std::tie(left.x, left.y, first) <
                         std::tie(right.x, right.y, second);
                });
      for (std::size_t begin = 0; begin < geometry.positions_t1.size();) {
        std::size_t end = begin + 1;
        while (end < geometry.positions_t1.size() &&
               same_point(geometry.positions_t1[order[begin]],
                          geometry.positions_t1[order[end]])) {
          ++end;
        }
        if (end - begin > 1) {
          geometry.violations += end - begin - 1;
          for (std::size_t index = begin; index < end; ++index) {
            geometry.blockers.insert(
                static_cast<std::int64_t>(order[index]));
          }
        }
        begin = end;
      }
    } else {
      std::map<std::pair<double, double>, std::vector<std::int64_t>> occupancy;
      for (std::size_t atom = 0; atom < geometry.positions_t1.size(); ++atom) {
        const auto& point = geometry.positions_t1[atom];
        occupancy[{point.x, point.y}].push_back(
            static_cast<std::int64_t>(atom));
      }
      for (const auto& [point, atoms] : occupancy) {
        (void)point;
        if (atoms.size() <= 1) continue;
        geometry.violations += atoms.size() - 1;
        geometry.blockers.insert(atoms.begin(), atoms.end());
      }
    }

    const auto gate_count = problem_.gate_domains.size();
    for (std::size_t gate = 0; gate < gate_count; ++gate) {
      const auto& option =
          problem_.gate_domains[gate][decoded.option_indices[gate]];
      for (const auto& target : {option.target1, option.target2}) {
        for (std::size_t atom = 0; atom < geometry.positions_t1.size(); ++atom) {
          if (participant_mask_[atom]) continue;
          if (same_point(target, geometry.positions_t1[atom])) {
            ++geometry.violations;
            geometry.blockers.insert(static_cast<std::int64_t>(atom));
          }
        }
      }
      for (const auto& [q, target] :
           {std::pair<std::int64_t, Point>{option.q1, option.target1},
            std::pair<std::int64_t, Point>{option.q2, option.target2}}) {
        const auto& source = geometry.positions_t1[q];
        const auto distance = point_distance(source, target);
        if (distance > 1e-9) {
          geometry.out_legs.push_back({distance, source, target});
          geometry.out_owners.push_back(q);
        }
      }
    }

    for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
      const auto atom_id = static_cast<std::int64_t>(atom);
      geometry.ghosts_t0.push_back({atom_id, problem_.current_points[atom]});
      geometry.ghosts_t1.push_back({atom_id, geometry.positions_t1[atom]});
    }
    geometry.final_site_ids = geometry.site_ids_t1;
    if (!geometry.final_site_ids.empty()) {
      for (std::size_t gate = 0; gate < gate_count; ++gate) {
        const auto& option =
            problem_.gate_domains[gate][decoded.option_indices[gate]];
        geometry.final_site_ids[static_cast<std::size_t>(option.q1)] =
            option.target1_site_id;
        geometry.final_site_ids[static_cast<std::size_t>(option.q2)] =
            option.target2_site_id;
      }
    }
    // Geometry replay is executed for every bounded RETURN assignment.  The
    // main suites fit in the same <=256-atom regime already used by the
    // occupancy audit above, so keep the two transient mover masks on the
    // stack there.  Preserve a heap fallback for larger architectures.  This
    // changes only scratch storage: the mask values and replay order below are
    // identical to the former two vector<bool> instances.
    std::array<unsigned char, kStackAtoms> back_movers_stack{};
    std::array<unsigned char, kStackAtoms> out_movers_stack{};
    std::vector<unsigned char> back_movers_heap;
    std::vector<unsigned char> out_movers_heap;
    auto* back_movers = back_movers_stack.data();
    auto* out_movers = out_movers_stack.data();
    if (problem_.n_atoms > kStackAtoms) {
      back_movers_heap.assign(problem_.n_atoms, 0U);
      out_movers_heap.assign(problem_.n_atoms, 0U);
      back_movers = back_movers_heap.data();
      out_movers = out_movers_heap.data();
    }
    for (const auto owner : geometry.back_owners) {
      back_movers[static_cast<std::size_t>(owner)] = 1U;
    }
    for (const auto owner : geometry.out_owners) {
      out_movers[static_cast<std::size_t>(owner)] = 1U;
    }
    const auto replay_single_legs = [&](const auto& legs,
                                        const auto& ghosts,
                                        const unsigned char* movers,
                                        bool record_out_participant_blockers) {
      for (std::size_t index = 0; index < legs.size(); ++index) {
        const auto& leg = legs[index];
        for (const auto& ghost : ghosts) {
          const auto atom = static_cast<std::size_t>(ghost.atom);
          if (atom < problem_.n_atoms && movers[atom] != 0U) continue;
          const auto x = cover(leg.source.x, leg.target.x, ghost.position.x);
          const auto y = cover(leg.source.y, leg.target.y, ghost.position.y);
          if (!coverage_matches(x, y)) continue;
          ++geometry.violations;
          ++geometry.ghost_violations;
          geometry.blockers.insert(ghost.atom);
          if (record_out_participant_blockers && atom < participant_mask_.size() &&
              participant_mask_[atom] && !movers[atom]) {
            geometry.stationary_out_participant_blockers.insert(ghost.atom);
          }
        }
      }
    };
    replay_single_legs(
        geometry.back_legs, geometry.ghosts_t0, back_movers, false);
    replay_single_legs(
        geometry.out_legs, geometry.ghosts_t1, out_movers, true);
    return geometry;
  }

  FitnessResult score_geometry(
      const std::vector<std::int64_t>& chromosome,
      PlanGeometry geometry, std::size_t return_count,
      bool enforce_single_leg_ghost, bool record_batches = false) const {
    CandidatePlan candidate;
    candidate.chromosome = chromosome;
    candidate.idle_exposures = static_cast<std::int64_t>(
        problem_.eligible.size() - return_count);
    candidate.phases.reserve(2);
    MovementPhase back;
    back.legs = std::move(geometry.back_legs);
    back.ghosts = std::move(geometry.ghosts_t0);
    back.owners = std::move(geometry.back_owners);
    candidate.phases.push_back(std::move(back));
    MovementPhase out;
    out.legs = std::move(geometry.out_legs);
    out.ghosts = std::move(geometry.ghosts_t1);
    out.owners = std::move(geometry.out_owners);
    candidate.phases.push_back(std::move(out));
    BoundaryConfig boundary_config;
    boundary_config.exact_coloring_threshold = config_.exact_coloring_threshold;
    boundary_config.enforce_single_leg_ghost = enforce_single_leg_ghost;
    if (problem_.exact_current_scheduler) {
      boundary_config.production_parking_replay = true;
      // Exact scheduling needs the executable membership/order of every AOD
      // batch even for intermediate candidates.  Geometry and batching remain
      // native; Python supplies only the compact absolute prefix state.
      auto result = evaluate_candidate(
          architecture_, candidate, boundary_config, {});
      if (!result.feasible) return result;
      const auto transfer_us = problem_.scheduler_transfer_duration_us;
      const auto acceleration = problem_.scheduler_accel_um_per_us2;
      const auto rydberg_us = problem_.scheduler_rydberg_duration_us;
      const auto one_qubit_us = problem_.scheduler_one_qubit_duration_us;
      const auto one_qubit_common_us =
          problem_.scheduler_one_qubit_common_us;
      const auto coherence_t2_us = problem_.coherence_t2_us;
      result.move_time_us = 0.0;
      for (const auto& phase_batches : result.executable_phase_batches) {
        for (const auto& batch : phase_batches) {
          result.move_time_us += expanded_batch_timing_model(
              batch.legs, transfer_us, acceleration).duration_us;
        }
      }

      auto active = problem_.scheduler_active_union_us;
      auto qubit_dependency = problem_.scheduler_qubit_dependency_end_us;
      auto current_site_ids = problem_.current_site_ids;
      auto aod_end = problem_.scheduler_aod_end_us.front();
      auto one_qubit_end = problem_.scheduler_one_qubit_end_us;
      auto rydberg_end = problem_.scheduler_rydberg_end_us.front();
      auto trace_end = problem_.scheduler_trace_end_us;
      std::map<std::int64_t, double> site_dependency;
      for (std::size_t index = 0;
           index < problem_.scheduler_site_dependency_site_ids.size(); ++index) {
        site_dependency.emplace(
            problem_.scheduler_site_dependency_site_ids[index],
            problem_.scheduler_site_dependency_activation_finish_us[index]);
      }

      const auto schedule_phase = [&] (
          std::size_t phase_index,
          const std::vector<std::int64_t>& target_site_ids,
          bool source_back) {
        if (phase_index >= candidate.phases.size() ||
            phase_index >= result.executable_phase_batches.size()) {
          throw std::logic_error(
              "ABI8 candidate executable phase batches are incomplete");
        }
        const auto& phase = candidate.phases[phase_index];
        if (target_site_ids.size() != problem_.n_atoms ||
            phase.owners.size() != phase.legs.size()) {
          throw std::logic_error("ABI8 candidate phase geometry is incomplete");
        }
        const auto site_id_for_owner_target = [&](
            const std::size_t owner, const Point& point) {
          const auto& coordinates = architecture_.site_coordinates();
          // Every ordinary executable leg is a stable subset of the phase
          // geometry, so its owner-indexed endpoint has already been resolved
          // to an architecture site by build_geometry().  Keep the coordinate
          // check: if production replay ever emits a genuine waypoint rather
          // than only splitting/reordering endpoint legs, that intermediate
          // point must retain the legacy coordinate lookup below.
          const auto indexed_site = target_site_ids[owner];
          if (indexed_site >= 0 &&
              static_cast<std::size_t>(indexed_site) < coordinates.size()) {
            const auto& indexed_point =
                coordinates[static_cast<std::size_t>(indexed_site)];
            if (std::abs(indexed_point.x - point.x) < 1e-9 &&
                std::abs(indexed_point.y - point.y) < 1e-9) {
              return indexed_site;
            }
          }
          // Defensive compatibility path for a future physical waypoint or a
          // non-indexed fixture whose endpoint id is unavailable.  The scan is
          // deliberately byte-for-byte equivalent to the former hot path.
          for (std::size_t site = 0; site < coordinates.size(); ++site) {
            if (std::abs(coordinates[site].x - point.x) < 1e-9 &&
                std::abs(coordinates[site].y - point.y) < 1e-9) {
              return static_cast<std::int64_t>(site);
            }
          }
          throw std::logic_error(
              "ABI8 executable batch target is not a registered SLM site");
        };
        for (const auto& batch :
             result.executable_phase_batches[phase_index]) {
          if (batch.legs.size() != batch.owners.size()) {
            throw std::logic_error(
                "ABI8 executable batch owner geometry is incomplete");
          }
          const auto timing = expanded_batch_timing_model(
              batch.legs, transfer_us, acceleration);
          double begin = aod_end;
          std::vector<std::int64_t> batch_target_sites;
          batch_target_sites.reserve(batch.legs.size());
          for (std::size_t member = 0; member < batch.legs.size(); ++member) {
            const auto raw_owner = batch.owners[member];
            if (raw_owner < 0 ||
                static_cast<std::size_t>(raw_owner) >= problem_.n_atoms) {
              throw std::logic_error("ABI8 phase owner is invalid");
            }
            const auto owner = static_cast<std::size_t>(raw_owner);
            begin = std::max(
                begin,
                source_back
                    ? problem_.scheduler_back_dependency_end_us[owner]
                    : qubit_dependency[owner]);
            const auto target_site = site_id_for_owner_target(
                owner, batch.legs[member].target);
            batch_target_sites.push_back(target_site);
            const auto dependency = site_dependency.find(target_site);
            if (dependency != site_dependency.end()) {
              begin = std::max(
                  begin,
                  dependency->second - timing.deactivation_offset_us);
            }
          }
          const auto end = begin + timing.duration_us;
          const auto activation_finish =
              begin + timing.activation_finish_offset_us;
          for (std::size_t member = 0; member < batch.owners.size(); ++member) {
            const auto owner = static_cast<std::size_t>(batch.owners[member]);
            active[owner] += 2.0 * transfer_us;
            qubit_dependency[owner] = end;
            site_dependency[current_site_ids[owner]] = activation_finish;
            current_site_ids[owner] = batch_target_sites[member];
          }
          aod_end = end;
          trace_end = std::max(trace_end, end);
        }
      };

      schedule_phase(0, geometry.site_ids_t1, true);
      schedule_phase(1, geometry.final_site_ids, false);

      if (!problem_.gate_domains.empty()) {
        const auto before_gate_dependency = qubit_dependency;
        std::vector<unsigned char> target_one_qubit(problem_.n_atoms, 0U);
        for (const auto raw_atom : problem_.target_one_qubit_atoms) {
          target_one_qubit[static_cast<std::size_t>(raw_atom)] = 1U;
        }
        double cz_begin = rydberg_end;
        for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
          const auto participant = participant_mask_[atom];
          const auto in_zone = is_zone_point(
              architecture_.site_coordinates()[static_cast<std::size_t>(
                  current_site_ids[atom])]);
          // Router_mixin creates the CZ before the target parent-1Q block.  Its
          // resident dependency patch binds idle zone atoms except those whose
          // qubit ledger has already advanced to that later 1Q instruction.
          if (participant ||
              (in_zone && target_one_qubit[atom] == 0U)) {
            cz_begin = std::max(cz_begin, before_gate_dependency[atom]);
          }
        }
        const auto cz_end = cz_begin + rydberg_us;
        for (const auto raw_atom : problem_.participants) {
          active[static_cast<std::size_t>(raw_atom)] += rydberg_us;
        }
        rydberg_end = cz_end;
        trace_end = std::max(trace_end, cz_end);

        if (!problem_.target_one_qubit_atoms.empty()) {
          double one_qubit_begin = one_qubit_end;
          for (const auto raw_atom : problem_.target_one_qubit_atoms) {
            const auto atom = static_cast<std::size_t>(raw_atom);
            one_qubit_begin = std::max(
                one_qubit_begin,
                participant_mask_[atom] ? cz_end
                                        : before_gate_dependency[atom]);
          }
          auto cursor = one_qubit_begin;
          for (const auto raw_atom : problem_.target_one_qubit_atoms) {
            const auto atom = static_cast<std::size_t>(raw_atom);
            active[atom] += one_qubit_us;
            cursor += one_qubit_us;
          }
          one_qubit_end = cursor + one_qubit_common_us;
          trace_end = std::max(trace_end, one_qubit_end);
        }

        for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
          const auto in_zone = is_zone_point(
              architecture_.site_coordinates()[static_cast<std::size_t>(
                  current_site_ids[atom])]);
          if (target_one_qubit[atom] != 0U) {
            qubit_dependency[atom] = one_qubit_end;
          } else if (participant_mask_[atom] || in_zone) {
            qubit_dependency[atom] =
                std::max(before_gate_dependency[atom], cz_end);
          }
        }
      }

      std::vector<double> idle_after(problem_.n_atoms, 0.0);
      for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
        if (active[atom] > trace_end + 1e-7) {
          throw std::logic_error(
              "ABI8 active union exceeds candidate trace makespan");
        }
        idle_after[atom] = std::max(0.0, trace_end - active[atom]);
      }
      const auto coherence_nll = search_coherence_absolute_nll(
          problem_.prior_idle_time_us, idle_after, coherence_t2_us);
      std::int64_t idle_exposures = 0;
      for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
        if (!participant_mask_[atom] && is_zone_point(
                architecture_.site_coordinates()[static_cast<std::size_t>(
                    current_site_ids[atom])])) {
          ++idle_exposures;
        }
      }
      result.idle_exposures = idle_exposures;
      result.transfer_nll =
          -static_cast<double>(result.transfers) * std::log(kFTransfer);
      result.idle_excitation_nll =
          -static_cast<double>(idle_exposures) * std::log(kFExc);
      result.coherence_nll = coherence_nll;
      result.negative_log_fidelity = result.transfer_nll +
                                     result.idle_excitation_nll +
                                     result.coherence_nll;
      result.candidate_idle_time_us.resize(problem_.n_atoms);
      for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
        result.candidate_idle_time_us[atom] =
            idle_after[atom] - problem_.prior_idle_time_us[atom];
      }
      return result;
    }
    std::vector<double> pulse_idle(problem_.n_atoms, 0.0);
    if (!problem_.gate_domains.empty()) {
      std::fill(pulse_idle.begin(), pulse_idle.end(), kRydbergUs);
      for (const auto participant : problem_.participants) {
        pulse_idle[static_cast<std::size_t>(participant)] = 0.0;
      }
    }
    auto scoring_prior = problem_.prior_idle_time_us;
    for (std::size_t atom = 0; atom < scoring_prior.size(); ++atom) {
      scoring_prior[atom] += pulse_idle[atom];
    }
    auto result = (record_batches
                       ? evaluate_candidate(
                             architecture_, candidate, boundary_config,
                             scoring_prior)
                       : evaluate_candidate_summary(
                             architecture_, candidate, boundary_config,
                             scoring_prior));
    if (!result.feasible &&
        result.error == "linear coherence model out of domain") {
      result = (record_batches
                    ? evaluate_candidate(
                          architecture_, candidate, boundary_config,
                          std::vector<double>(problem_.n_atoms, 0.0))
                    : evaluate_candidate_summary(
                          architecture_, candidate, boundary_config,
                          std::vector<double>(problem_.n_atoms, 0.0)));
      if (result.feasible) {
        const auto movement_nll = search_coherence_delta_nll(
            scoring_prior, result.candidate_idle_time_us);
        result.negative_log_fidelity += movement_nll - result.coherence_nll;
        result.coherence_nll = movement_nll;
      }
    }
    const auto pulse_nll = search_coherence_delta_nll(
        problem_.prior_idle_time_us, pulse_idle);
    if (result.feasible) {
      result.coherence_nll += pulse_nll;
      result.negative_log_fidelity += pulse_nll;
      if (result.candidate_idle_time_us.empty()) {
        result.candidate_idle_time_us.assign(problem_.n_atoms, 0.0);
      }
      for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
        result.candidate_idle_time_us[atom] += pulse_idle[atom];
      }
    }
    return result;
  }

  ReseatRepair derive_reseats(
      const std::vector<std::int64_t>& chromosome,
      const DecodeResult& decoded,
      const std::vector<ReturnAssignment>& assignments,
      std::size_t return_count) {
    const auto gate_count = problem_.gate_domains.size();
    std::map<std::int64_t, std::size_t> movable;
    for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
      if (chromosome[gate_count + index] == 0) {
        movable[problem_.eligible[index]] = index;
      }
    }
    std::map<std::size_t, ReturnAssignment> selected;
    auto as_vector = [&]() {
      std::vector<ReturnAssignment> result;
      for (const auto& [index, assignment] : selected) {
        (void)index;
        result.push_back(assignment);
      }
      return result;
    };
    auto reseats = as_vector();
    auto geometry = build_geometry(decoded, assignments, reseats);
    for (std::size_t step = 0; step < movable.size() && geometry.violations != 0;
         ++step) {
      bool found = false;
      std::size_t best_violations = geometry.violations;
      FitnessResult best_relaxed;
      std::size_t best_index{};
      ReturnAssignment best_assignment;
      for (const auto blocker : geometry.blockers) {
        const auto movable_found = movable.find(blocker);
        if (movable_found == movable.end()) continue;
        const auto eligible_index = movable_found->second;
        const auto source = problem_.current_points[blocker];
        std::vector<std::pair<double, std::int64_t>> sites;
        for (std::size_t site_id = 0;
             site_id < architecture_.site_coordinates().size(); ++site_id) {
          if (storage_site_ids_.count(static_cast<std::int64_t>(site_id)) != 0U) {
            continue;
          }
          const auto& point = architecture_.site_coordinates()[site_id];
          sites.emplace_back(point_distance(source, point),
                             static_cast<std::int64_t>(site_id));
        }
        std::sort(sites.begin(), sites.end());
        for (const auto& [distance, site_id] : sites) {
          if (distance <= 1e-9) continue;
          auto trial = selected;
          trial[eligible_index] = {
              eligible_index, site_id,
              architecture_.site_coordinates()[static_cast<std::size_t>(site_id)]};
          std::vector<ReturnAssignment> trial_reseats;
          for (const auto& [index, assignment] : trial) {
            (void)index;
            trial_reseats.push_back(assignment);
          }
          auto trial_geometry =
              build_geometry(decoded, assignments, trial_reseats);
          const auto trial_violations = trial_geometry.violations;
          if (trial_violations >= geometry.violations) continue;
          const auto relaxed = score_geometry(
              chromosome, std::move(trial_geometry), return_count, false);
          if (!found ||
              std::tie(trial_violations,
                       relaxed.negative_log_fidelity, relaxed.move_batches,
                       relaxed.move_time_us, relaxed.total_distance_um,
                       blocker, site_id) <
                  std::tie(best_violations, best_relaxed.negative_log_fidelity,
                           best_relaxed.move_batches, best_relaxed.move_time_us,
                           best_relaxed.total_distance_um,
                           problem_.eligible[best_index],
                           best_assignment.site_id)) {
            found = true;
            best_violations = trial_violations;
            best_relaxed = relaxed;
            best_index = eligible_index;
            best_assignment = trial[eligible_index];
          }
        }
      }
      if (!found) break;
      selected[best_index] = best_assignment;
      reseats = as_vector();
      geometry = build_geometry(decoded, assignments, reseats);
    }
    return {as_vector(), std::move(geometry)};
  }

  ParticipantParkingRepair derive_participant_parkings(
      const std::vector<std::int64_t>& chromosome,
      const DecodeResult& decoded,
      const std::vector<ReturnAssignment>& assignments,
      const std::vector<ReturnAssignment>& reseats,
      std::size_t return_count) const {
    std::map<std::int64_t, ParticipantParkingAssignment> selected;
    const auto as_vector = [&]() {
      std::vector<ParticipantParkingAssignment> result;
      result.reserve(selected.size());
      for (const auto& [atom, assignment] : selected) {
        (void)atom;
        result.push_back(assignment);
      }
      return result;
    };
    auto parkings = as_vector();
    auto geometry = build_geometry(
        decoded, assignments, reseats, parkings);
    for (std::size_t step = 0;
         step < problem_.participants.size() && geometry.violations != 0;
         ++step) {
      std::set<std::int64_t> out_owners(
          geometry.out_owners.begin(), geometry.out_owners.end());
      std::set<std::int64_t> occupied_sites(
          problem_.occupied_storage_site_ids.begin(),
          problem_.occupied_storage_site_ids.end());
      for (const auto site_id : geometry.site_ids_t1) {
        if (storage_site_ids_.count(site_id) != 0U) {
          occupied_sites.insert(site_id);
        }
      }

      bool found = false;
      std::size_t best_violations = geometry.violations;
      FitnessResult best_relaxed;
      std::int64_t best_atom{};
      ParticipantParkingAssignment best_assignment;
      for (const auto atom : geometry.stationary_out_participant_blockers) {
        if (atom < 0 || static_cast<std::size_t>(atom) >= problem_.n_atoms ||
            !participant_mask_[static_cast<std::size_t>(atom)] ||
            out_owners.count(atom) != 0U || selected.count(atom) != 0U) {
          continue;
        }
        const auto& source = problem_.current_points[
            static_cast<std::size_t>(atom)];
        std::vector<std::pair<double, std::int64_t>> sites;
        sites.reserve(storage_site_ids_.size());
        for (const auto site_id : storage_site_ids_) {
          if (site_id < 0 ||
              static_cast<std::size_t>(site_id) >=
                  architecture_.site_coordinates().size() ||
              occupied_sites.count(site_id) != 0U) {
            continue;
          }
          const auto& point = architecture_.site_coordinates()[
              static_cast<std::size_t>(site_id)];
          const auto point_occupied = std::any_of(
              geometry.positions_t1.begin(), geometry.positions_t1.end(),
              [&](const auto& occupied) {
                return same_point(point, occupied);
              });
          if (point_occupied) continue;
          sites.emplace_back(point_distance(source, point), site_id);
        }
        std::sort(sites.begin(), sites.end());
        if (sites.size() > config_.return_candidate_limit) {
          sites.resize(config_.return_candidate_limit);
        }
        for (const auto& [distance, site_id] : sites) {
          if (distance <= 1e-9) continue;
          auto trial = selected;
          trial[atom] = {
              atom, site_id,
              architecture_.site_coordinates()[static_cast<std::size_t>(
                  site_id)]};
          std::vector<ParticipantParkingAssignment> trial_parkings;
          trial_parkings.reserve(trial.size());
          for (const auto& [trial_atom, assignment] : trial) {
            (void)trial_atom;
            trial_parkings.push_back(assignment);
          }
          auto trial_geometry = build_geometry(
              decoded, assignments, reseats, trial_parkings);
          const auto trial_violations = trial_geometry.violations;
          if (trial_violations >= geometry.violations) continue;
          const auto relaxed = score_geometry(
              chromosome, std::move(trial_geometry), return_count, false);
          if (!found ||
              std::tie(trial_violations,
                       relaxed.negative_log_fidelity,
                       relaxed.move_batches,
                       relaxed.move_time_us,
                       relaxed.total_distance_um,
                       atom, site_id) <
                  std::tie(best_violations,
                           best_relaxed.negative_log_fidelity,
                           best_relaxed.move_batches,
                           best_relaxed.move_time_us,
                           best_relaxed.total_distance_um,
                           best_atom, best_assignment.site_id)) {
            found = true;
            best_violations = trial_violations;
            best_relaxed = relaxed;
            best_atom = atom;
            best_assignment = trial[atom];
          }
        }
      }
      if (!found) break;
      selected[best_atom] = best_assignment;
      parkings = as_vector();
      geometry = build_geometry(decoded, assignments, reseats, parkings);
    }
    return {as_vector(), std::move(geometry)};
  }

  Evaluated evaluate_assignment(
      const std::vector<std::int64_t>& chromosome,
      const DecodeResult& decoded,
      const std::vector<std::size_t>& returners,
      const std::vector<ReturnAssignment>& assignments,
      std::size_t assignment_rank, std::size_t assignment_count,
      bool include_forecast = true) {
    Evaluated result;
    result.search_nll = std::numeric_limits<double>::infinity();
    result.assignments = assignments;
    result.return_assignment_rank = assignment_rank + 1;
    result.return_assignment_evaluated = assignment_count;
    for (const auto& assignment : assignments) {
      result.assignment_key.push_back(assignment.site_id);
    }
    result.decoded = decoded;
    auto reseat_repair = derive_reseats(
        chromosome, decoded, assignments, returners.size());
    result.reseats = std::move(reseat_repair.reseats);
    result.pre_score_reseats = result.reseats.size();
    stats_.pre_score_reseats += result.pre_score_reseats;
    if (!result.reseats.empty()) {
      result.assignment_key.push_back(-1);
      for (const auto& reseat : result.reseats) {
        result.assignment_key.push_back(
            problem_.eligible[reseat.eligible_index]);
        result.assignment_key.push_back(reseat.site_id);
      }
    }
    auto participant_parking_repair = derive_participant_parkings(
        chromosome, decoded, assignments, result.reseats, returners.size());
    result.participant_parkings = std::move(
        participant_parking_repair.parkings);
    result.pre_score_participant_parkings =
        result.participant_parkings.size();
    stats_.pre_score_participant_parkings +=
        result.pre_score_participant_parkings;
    if (!result.participant_parkings.empty()) {
      result.assignment_key.push_back(-2);
      for (const auto& parking : result.participant_parkings) {
        result.assignment_key.push_back(parking.atom);
        result.assignment_key.push_back(parking.site_id);
      }
    }
    const auto& geometry = participant_parking_repair.geometry;
    if (geometry.violations != 0 &&
        (geometry.ghost_violations == 0 ||
         config_.enforce_single_leg_ghost)) {
      result.fitness = infeasible_fitness(
          chromosome,
          geometry.ghost_violations != 0
              ? "unresolved current single-leg ghost hit"
              : "unresolved current gate occupancy");
    } else {
      result.fitness = score_geometry(
          chromosome, std::move(participant_parking_repair.geometry),
          returners.size(),
          config_.enforce_single_leg_ghost);
    }
    if (!result.fitness.feasible &&
        result.fitness.error.find("ghost") != std::string::npos) {
      result.current_ghost_rejections = 1;
      ++stats_.current_ghost_rejections;
    }
    if (result.fitness.feasible && include_forecast) {
      apply_forecast(result, chromosome);
    }
    return result;
  }

  Evaluated evaluate_normalized(
      const std::vector<std::int64_t>& chromosome, bool stochastic,
      std::optional<double> direct_incumbent_nll = std::nullopt,
      bool* exact_current_pruned = nullptr) {
    if (exact_current_pruned != nullptr) *exact_current_pruned = false;
    ++stats_.evaluations;
    const auto cached = fitness_cache_.find(chromosome);
    if (config_.fitness_cache && cached != fitness_cache_.end()) {
      ++stats_.fitness_hits;
      archive_complete_evaluation(cached->second);
      return cached->second;
    }
    const auto compact = partial_current_bounds_.find(chromosome);
    if (config_.fitness_cache && compact != partial_current_bounds_.end()) {
      // score_unique/local-polish requests a complete value for a chromosome
      // previously deferred by lazy top-k.  Rebuild the already-accounted
      // current replay, then finish its forecast without publishing or
      // ranking the partial value itself.
      const auto saved_stats = stats_;
      auto prepared = prepare_current_normalized(chromosome, stochastic);
      stats_ = saved_stats;
      ++stats_.fitness_hits;
      auto completed =
          complete_prepared_current(chromosome, std::move(prepared));
      archive_complete_evaluation(completed);
      return completed;
    }
    const bool new_key = evaluated_keys_.insert(chromosome).second;
    if (new_key) {
      ++stats_.unique_evaluations;
      if (stochastic) {
        ++stats_.stochastic_unique_evaluations;
      } else {
        ++stats_.deterministic_unique_evaluations;
      }
    }
    const auto started = Clock::now();
    Evaluated result;
    result.search_nll = std::numeric_limits<double>::infinity();
    result.decoded = decode(chromosome);
    if (!result.decoded.feasible) {
      result.fitness = infeasible_fitness(chromosome, result.decoded.error);
      if (config_.fitness_cache) fitness_cache_[chromosome] = result;
      fitness_ns_ += elapsed_ns(started);
      return result;
    }
    const auto gate_count = problem_.gate_domains.size();
    std::vector<std::size_t> returners;
    for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
      if (chromosome[gate_count + index] != 0) returners.push_back(index);
    }
    std::vector<std::vector<ReturnAssignment>> candidates;
    try {
      candidates = match_returns(returners);
    } catch (const std::exception& error) {
      result.fitness = infeasible_fitness(chromosome, error.what());
      if (config_.fitness_cache) fitness_cache_[chromosome] = result;
      fitness_ns_ += elapsed_ns(started);
      return result;
    }
    if (candidates.empty()) {
      result.fitness = infeasible_fitness(
          chromosome, "RETURN matching has no bounded injective assignment");
      if (config_.fitness_cache) fitness_cache_[chromosome] = result;
      fitness_ns_ += elapsed_ns(started);
      return result;
    }
    stats_.return_assignment_evaluated += candidates.size();
    const auto use_exact_current_prune =
        direct_incumbent_nll.has_value() &&
        std::isfinite(*direct_incumbent_nll);
    std::vector<Evaluated> current_candidates;
    std::size_t ghost_rejections = 0;
    if (use_exact_current_prune) {
      current_candidates.reserve(candidates.size());
      bool all_strictly_worse = true;
      for (std::size_t rank = 0; rank < candidates.size(); ++rank) {
        auto candidate = evaluate_assignment(
            chromosome, result.decoded, returners, candidates[rank], rank,
            candidates.size(), false);
        ghost_rejections += candidate.current_ghost_rejections;
        if (candidate.fitness.feasible &&
            !(candidate.fitness.negative_log_fidelity >
              *direct_incumbent_nll + 1e-12)) {
          all_strictly_worse = false;
        }
        current_candidates.push_back(std::move(candidate));
      }
      if (all_strictly_worse) {
        // These are exact current-boundary scores, but intentionally not full
        // current+forecast values.  Non-negative forecast contributions make
        // the strict global comparison sufficient to reject the chromosome.
        // Never publish this partial value through fitness_cache_.
        bool have_current_best = false;
        for (auto& candidate : current_candidates) {
          candidate.search_nll =
              candidate.fitness.negative_log_fidelity;
          if (!have_current_best || evaluated_less(candidate, result)) {
            result = std::move(candidate);
            have_current_best = true;
          }
        }
        result.current_ghost_rejections = ghost_rejections;
        if (exact_current_pruned != nullptr) *exact_current_pruned = true;
        fitness_ns_ += elapsed_ns(started);
        return result;
      }
    }

    bool have_best = false;
    for (std::size_t rank = 0; rank < candidates.size(); ++rank) {
      auto candidate = use_exact_current_prune
                           ? std::move(current_candidates[rank])
                           : evaluate_assignment(
                                 chromosome, result.decoded, returners,
                                 candidates[rank], rank, candidates.size(),
                                 false);
      if (!use_exact_current_prune) {
        ghost_rejections += candidate.current_ghost_rejections;
      }
      if (!candidate.fitness.feasible) {
        if (!have_best || evaluated_less(candidate, result)) {
          result = std::move(candidate);
          have_best = true;
        }
        continue;
      }
      // Every forecast contribution is a non-negative physical NLL.  Once an
      // assignment's current NLL already exceeds the incumbent's complete
      // current+forecast NLL, no future replay can make it win.  Exact ties are
      // still replayed so Move batches/time and canonical assignment order
      // retain their deterministic tie-breaking semantics.
      if (have_best && std::isfinite(result.search_nll) &&
          candidate.fitness.negative_log_fidelity >
              result.search_nll + 1e-12) {
        continue;
      }
      apply_forecast(candidate, chromosome);
      if (!have_best || evaluated_less(candidate, result)) {
        result = std::move(candidate);
        have_best = true;
      }
    }
    result.current_ghost_rejections = ghost_rejections;
    if (config_.fitness_cache) fitness_cache_[chromosome] = result;
    archive_complete_evaluation(result);
    fitness_ns_ += elapsed_ns(started);
    return result;
  }

  PreparedCurrent prepare_current_normalized(
      const std::vector<std::int64_t>& chromosome, bool stochastic) {
    ++stats_.evaluations;
    const auto cached = fitness_cache_.find(chromosome);
    if (config_.fitness_cache && cached != fitness_cache_.end()) {
      ++stats_.fitness_hits;
      PreparedCurrent prepared;
      prepared.complete = true;
      prepared.complete_value = cached->second;
      prepared.lower_bound = cached->second.search_nll;
      return prepared;
    }
    const bool new_key = evaluated_keys_.insert(chromosome).second;
    if (new_key) {
      ++stats_.unique_evaluations;
      if (stochastic) {
        ++stats_.stochastic_unique_evaluations;
      } else {
        ++stats_.deterministic_unique_evaluations;
      }
    }

    const auto started = Clock::now();
    PreparedCurrent prepared;
    Evaluated initial;
    initial.search_nll = std::numeric_limits<double>::infinity();
    initial.decoded = decode(chromosome);
    if (!initial.decoded.feasible) {
      initial.fitness = infeasible_fitness(chromosome, initial.decoded.error);
      prepared.complete = true;
      prepared.complete_value = std::move(initial);
      if (config_.fitness_cache) {
        fitness_cache_[chromosome] = prepared.complete_value;
      }
      fitness_ns_ += elapsed_ns(started);
      return prepared;
    }

    const auto gate_count = problem_.gate_domains.size();
    std::vector<std::size_t> returners;
    for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
      if (chromosome[gate_count + index] != 0) returners.push_back(index);
    }
    std::vector<std::vector<ReturnAssignment>> assignments;
    try {
      assignments = match_returns(returners);
    } catch (const std::exception& error) {
      initial.fitness = infeasible_fitness(chromosome, error.what());
      prepared.complete = true;
      prepared.complete_value = std::move(initial);
      if (config_.fitness_cache) {
        fitness_cache_[chromosome] = prepared.complete_value;
      }
      fitness_ns_ += elapsed_ns(started);
      return prepared;
    }
    if (assignments.empty()) {
      initial.fitness = infeasible_fitness(
          chromosome, "RETURN matching has no bounded injective assignment");
      prepared.complete = true;
      prepared.complete_value = std::move(initial);
      if (config_.fitness_cache) {
        fitness_cache_[chromosome] = prepared.complete_value;
      }
      fitness_ns_ += elapsed_ns(started);
      return prepared;
    }

    stats_.return_assignment_evaluated += assignments.size();
    prepared.current_candidates.reserve(assignments.size());
    bool have_feasible = false;
    for (std::size_t rank = 0; rank < assignments.size(); ++rank) {
      auto candidate = evaluate_assignment(
          chromosome, initial.decoded, returners, assignments[rank], rank,
          assignments.size(), false);
      prepared.ghost_rejections += candidate.current_ghost_rejections;
      if (candidate.fitness.feasible) {
        have_feasible = true;
        prepared.lower_bound = std::min(
            prepared.lower_bound,
            candidate.fitness.negative_log_fidelity);
      }
      prepared.current_candidates.push_back(std::move(candidate));
    }

    if (!have_feasible) {
      bool have_best = false;
      for (auto& candidate : prepared.current_candidates) {
        if (!have_best || evaluated_less(candidate, initial)) {
          initial = std::move(candidate);
          have_best = true;
        }
      }
      initial.current_ghost_rejections = prepared.ghost_rejections;
      prepared.complete = true;
      prepared.complete_value = std::move(initial);
      prepared.current_candidates.clear();
      if (config_.fitness_cache) {
        fitness_cache_[chromosome] = prepared.complete_value;
      }
    }
    fitness_ns_ += elapsed_ns(started);
    return prepared;
  }

  Evaluated complete_prepared_current(
      const std::vector<std::int64_t>& chromosome,
      PreparedCurrent prepared) {
    if (prepared.complete) return std::move(prepared.complete_value);
    const auto started = Clock::now();
    Evaluated result;
    result.search_nll = std::numeric_limits<double>::infinity();
    bool have_best = false;
    for (auto& candidate : prepared.current_candidates) {
      if (!candidate.fitness.feasible) {
        if (!have_best || evaluated_less(candidate, result)) {
          result = std::move(candidate);
          have_best = true;
        }
        continue;
      }
      // The current physical NLL is a lower bound on the complete search NLL.
      // Preserve exact ties so the downstream physical/canonical tie-breaks
      // observe the same set of assignments as the full evaluator.
      if (have_best && std::isfinite(result.search_nll) &&
          candidate.fitness.negative_log_fidelity >
              result.search_nll + 1e-12) {
        continue;
      }
      apply_forecast(candidate, chromosome);
      if (!have_best || evaluated_less(candidate, result)) {
        result = std::move(candidate);
        have_best = true;
      }
    }
    result.current_ghost_rejections = prepared.ghost_rejections;
    if (config_.fitness_cache) fitness_cache_[chromosome] = result;
    partial_current_bounds_.erase(chromosome);
    archive_complete_evaluation(result);
    fitness_ns_ += elapsed_ns(started);
    return result;
  }

  double physical_lower_bound(
      const DecodeResult& decoded,
      const std::vector<ReturnAssignment>& assignments,
      std::size_t return_count) const {
    std::size_t back_movers = 0;
    double back_longest = 0.0;
    std::vector<unsigned char> back_mover_mask(problem_.n_atoms, 0U);
    for (const auto& assignment : assignments) {
      const auto atom = static_cast<std::size_t>(
          problem_.eligible[assignment.eligible_index]);
      const auto distance = point_distance(
          problem_.current_points[atom], assignment.point);
      if (distance <= 1e-9) continue;
      ++back_movers;
      back_mover_mask[atom] = 1U;
      back_longest = std::max(back_longest, distance);
    }

    std::size_t out_movers = 0;
    double out_longest = 0.0;
    std::vector<unsigned char> out_mover_mask(problem_.n_atoms, 0U);
    for (std::size_t gate = 0; gate < problem_.gate_domains.size(); ++gate) {
      const auto& option =
          problem_.gate_domains[gate][decoded.option_indices[gate]];
      for (const auto& [atom, target] :
           {std::pair<std::int64_t, Point>{option.q1, option.target1},
            std::pair<std::int64_t, Point>{option.q2, option.target2}}) {
        const auto distance = point_distance(
            problem_.current_points[static_cast<std::size_t>(atom)], target);
        if (distance <= 1e-9) continue;
        ++out_movers;
        out_mover_mask[static_cast<std::size_t>(atom)] = 1U;
        out_longest = std::max(out_longest, distance);
      }
    }

    const auto idle_exposures = problem_.eligible.size() - return_count;
    std::vector<double> candidate_idle(problem_.n_atoms, 0.0);
    const auto add_phase = [&](const auto& mover_mask, std::size_t movers,
                               double longest) {
      if (movers == 0) return true;
      // Every legal phase must execute its longest individual trajectory.
      // Treating every leg as if it shared one batch is therefore an
      // admissible (optimistic) lower bound on the implemented phase time.
      const auto phase_time =
          2.0 * kTransferUs + std::sqrt(longest / kAccelUmPerUs2);
      for (auto& value : candidate_idle) value += phase_time;
      for (std::size_t atom = 0; atom < mover_mask.size(); ++atom) {
        if (mover_mask[atom] != 0U) {
          candidate_idle[atom] -= 2.0 * kTransferUs;
        }
      }
      return true;
    };
    if (!add_phase(back_mover_mask, back_movers, back_longest) ||
        !add_phase(out_mover_mask, out_movers, out_longest)) {
      return std::numeric_limits<double>::infinity();
    }
    if (!problem_.gate_domains.empty()) {
      for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
        if (!participant_mask_[atom]) candidate_idle[atom] += kRydbergUs;
      }
    }
    const auto coherence_nll = search_coherence_delta_nll(
        problem_.prior_idle_time_us, candidate_idle);
    const auto transfers = 2 * (back_movers + out_movers);
    const auto transfer_nll =
        -static_cast<double>(transfers) * std::log(kFTransfer);
    const auto idle_nll =
        -static_cast<double>(idle_exposures) * std::log(kFExc);
    return transfer_nll + idle_nll + coherence_nll;
  }

  std::optional<Evaluated> score_direct_exact(
      const std::vector<std::vector<std::int64_t>>& raw_values) {
    struct DirectCandidate {
      std::vector<std::int64_t> chromosome;
      // Absent means the full evaluator owns an infeasible/malformed
      // diagnostic.  Such a candidate is never removed by the physical
      // lower-bound shortcut.
      std::optional<double> physical_lower_bound;
    };

    std::vector<DirectCandidate> candidates;
    candidates.reserve(raw_values.size());
    std::unordered_set<std::vector<std::int64_t>, VectorHash<std::int64_t>> seen;
    for (const auto& raw : raw_values) {
      auto chromosome = normalize(raw);
      if (!seen.insert(chromosome).second) continue;

      std::optional<double> lower_bound;
      const auto decoded = decode(chromosome);
      if (decoded.feasible && !problem_.exact_current_scheduler) {
        const auto gate_count = problem_.gate_domains.size();
        std::vector<std::size_t> returners;
        for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
          if (chromosome[gate_count + index] != 0) {
            returners.push_back(index);
          }
        }
        try {
          const auto assignments = match_returns(returners);
          if (!assignments.empty()) {
            auto best_bound = std::numeric_limits<double>::infinity();
            for (const auto& assignment : assignments) {
              best_bound = std::min(
                  best_bound,
                  physical_lower_bound(decoded, assignment, returners.size()));
            }
            lower_bound = best_bound;
          }
        } catch (const std::exception&) {
          // Preserve the established fail-closed diagnostic path: the full
          // evaluator owns malformed/infeasible RETURN reporting.
        }
      }
      candidates.push_back({std::move(chromosome), lower_bound});
    }

    // Forecast contributions are non-negative.  Evaluating the smallest
    // admissible current-physics bound first establishes the strongest exact
    // incumbent without changing the objective, chromosome tie-break, or RNG
    // state.  Canonical chromosome order makes equal-bound ordering stable.
    std::sort(candidates.begin(), candidates.end(), [](const auto& first,
                                                       const auto& second) {
      const auto first_bound = first.physical_lower_bound.value_or(
          std::numeric_limits<double>::infinity());
      const auto second_bound = second.physical_lower_bound.value_or(
          std::numeric_limits<double>::infinity());
      return std::tie(first_bound, first.chromosome) <
             std::tie(second_bound, second.chromosome);
    });

    std::optional<Evaluated> best;
    for (const auto& candidate : candidates) {
      const auto& chromosome = candidate.chromosome;
      if (best.has_value() && std::isfinite(best->search_nll) &&
          candidate.physical_lower_bound.has_value() &&
          *candidate.physical_lower_bound > best->search_nll + 1e-12) {
        ++stats_.evaluations;
        if (evaluated_keys_.insert(chromosome).second) {
          ++stats_.unique_evaluations;
          ++stats_.deterministic_unique_evaluations;
        }
        ++stats_.direct_lower_bound_prunes;
        continue;
      }

      bool exact_current_pruned = false;
      auto value = evaluate_normalized(
          chromosome, false,
          best.has_value() && std::isfinite(best->search_nll)
              ? std::optional<double>{best->search_nll}
              : std::nullopt,
          &exact_current_pruned);
      if (exact_current_pruned) continue;
      if (!best.has_value() || evaluated_less(value, *best)) {
        best = std::move(value);
      }
    }
    return best;
  }

  bool budget_can_score_normalized(
      const std::vector<std::int64_t>& chromosome) const {
    return evaluated_keys_.count(chromosome) != 0U ||
           stats_.unique_evaluations < stats_.stochastic_budget;
  }

  std::vector<Evaluated> score_unique(
      const std::vector<std::vector<std::int64_t>>& raw_values,
      bool stochastic) {
    std::vector<std::vector<std::int64_t>> unique;
    std::unordered_set<std::vector<std::int64_t>, VectorHash<std::int64_t>> seen;
    for (const auto& raw : raw_values) {
      auto chromosome = normalize(raw);
      if (seen.insert(chromosome).second) unique.push_back(std::move(chromosome));
    }
    std::vector<Evaluated> result;
    for (const auto& chromosome : unique) {
      if (!budget_can_score_normalized(chromosome)) continue;
      result.push_back(evaluate_normalized(chromosome, stochastic));
    }
    std::sort(result.begin(), result.end(), [](const auto& first,
                                               const auto& second) {
      return evaluated_less(first, second);
    });
    return result;
  }

  std::vector<Evaluated> score_top_k_lazy(
      const std::vector<std::vector<std::int64_t>>& raw_values,
      bool stochastic, std::size_t top_k) {
    // H=0 has no rollout to defer.  The cache-off path intentionally remains
    // the complete-score oracle used by differential tests and diagnostics.
    if (top_k == 0 || config_.max_horizon == 0 ||
        config_.alpha_lookahead <= 0.0 ||
        (problem_.forecast_terms.empty() && problem_.future_layers.empty()) ||
        !config_.fitness_cache) {
      auto result = score_unique(raw_values, stochastic);
      if (result.size() > top_k) result.resize(top_k);
      return result;
    }

    std::vector<std::vector<std::int64_t>> unique;
    std::unordered_set<std::vector<std::int64_t>, VectorHash<std::int64_t>> seen;
    for (const auto& raw : raw_values) {
      auto chromosome = normalize(raw);
      if (seen.insert(chromosome).second) unique.push_back(std::move(chromosome));
    }
    if (unique.size() <= top_k) return score_unique(unique, stochastic);

    struct LazyCandidate {
      std::vector<std::int64_t> chromosome;
      double lower_bound{std::numeric_limits<double>::infinity()};
      std::optional<PreparedCurrent> prepared;
      std::optional<Evaluated> complete;
    };

    std::vector<LazyCandidate> candidates;
    candidates.reserve(unique.size());
    for (const auto& chromosome : unique) {
      if (!budget_can_score_normalized(chromosome)) continue;
      const auto full = fitness_cache_.find(chromosome);
      if (full != fitness_cache_.end()) {
        ++stats_.evaluations;
        ++stats_.fitness_hits;
        candidates.push_back(
            {chromosome, full->second.search_nll, std::nullopt,
             full->second});
        continue;
      }
      const auto compact = partial_current_bounds_.find(chromosome);
      if (compact != partial_current_bounds_.end()) {
        // A previous lazy pool evaluated this chromosome at the exact current
        // boundary but did not need its future rollout.  Count the logical
        // score-cache hit exactly once, matching a full-cache revisit, and
        // rebuild the current details only if this pool promotes it.
        ++stats_.evaluations;
        ++stats_.fitness_hits;
        candidates.push_back(
            {chromosome, compact->second, std::nullopt, std::nullopt});
        continue;
      }
      auto prepared = prepare_current_normalized(chromosome, stochastic);
      if (prepared.complete) {
        const auto bound = prepared.complete_value.search_nll;
        candidates.push_back(
            {chromosome, bound, std::nullopt,
             std::move(prepared.complete_value)});
      } else {
        const auto bound = prepared.lower_bound;
        candidates.push_back(
            {chromosome, bound, std::move(prepared), std::nullopt});
      }
    }
    if (candidates.empty()) return {};

    std::vector<std::size_t> order(candidates.size());
    for (std::size_t index = 0; index < order.size(); ++index) order[index] = index;
    std::stable_sort(order.begin(), order.end(), [&](const auto first,
                                                      const auto second) {
      return candidates[first].lower_bound < candidates[second].lower_bound;
    });

    std::vector<Evaluated> exact;
    exact.reserve(candidates.size());
    for (const auto& candidate : candidates) {
      if (candidate.complete.has_value()) exact.push_back(*candidate.complete);
    }
    const auto sort_exact = [&]() {
      std::sort(exact.begin(), exact.end(), [](const auto& first,
                                               const auto& second) {
        return evaluated_less(first, second);
      });
    };
    sort_exact();

    for (const auto index : order) {
      auto& candidate = candidates[index];
      if (candidate.complete.has_value()) continue;
      if (exact.size() >= top_k) {
        sort_exact();
        const auto& threshold = exact[top_k - 1];
        if (candidate.lower_bound > threshold.search_nll + 1e-12) {
          break;
        }
      }

      if (!candidate.prepared.has_value()) {
        // Rebuild a compact-cache hit without double-accounting its earlier
        // current-boundary evaluation.  The work is real and remains in the
        // timing counters; only public semantic counters are restored.
        const auto saved_stats = stats_;
        auto prepared = prepare_current_normalized(
            candidate.chromosome, stochastic);
        stats_ = saved_stats;
        if (prepared.complete) {
          candidate.complete = std::move(prepared.complete_value);
        } else {
          candidate.prepared = std::move(prepared);
        }
      }
      if (!candidate.complete.has_value()) {
        candidate.complete = complete_prepared_current(
            candidate.chromosome, std::move(*candidate.prepared));
        candidate.prepared.reset();
      }
      exact.push_back(*candidate.complete);
    }

    // Store only the exact current NLL for deferred candidates.  A partial is
    // deliberately never inserted into fitness_cache_ and can never enter the
    // returned top-k population or winner path.
    for (const auto& candidate : candidates) {
      if (!candidate.complete.has_value()) {
        partial_current_bounds_[candidate.chromosome] = candidate.lower_bound;
      }
    }
    sort_exact();
    if (exact.size() > top_k) exact.resize(top_k);
    return exact;
  }

  std::size_t direct_search_space() const {
    const auto limit = config_.direct_enumeration_limit;
    std::size_t value = 1;
    for (const auto& domain : problem_.gate_domains) {
      if (value > limit / domain.size()) return limit + 1;
      value *= domain.size();
    }
    if (problem_.decision_policy == RichDecisionPolicy::kOptimize) {
      for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
        if (value > limit / 2) return limit + 1;
        value *= 2;
      }
    }
    return value;
  }

  std::vector<std::vector<std::int64_t>> enumerate_chromosomes() const {
    std::vector<std::vector<std::int64_t>> values;
    std::vector<std::int64_t> current(
        problem_.gate_domains.size() + problem_.eligible.size());
    std::function<void(std::size_t)> visit = [&](std::size_t index) {
      if (index == current.size()) {
        values.push_back(current);
        return;
      }
      std::size_t domain = 1;
      if (index < problem_.gate_domains.size()) {
        domain = problem_.gate_domains[index].size();
      } else if (problem_.decision_policy == RichDecisionPolicy::kOptimize) {
        domain = 2;
      }
      for (std::size_t value = 0; value < domain; ++value) {
        current[index] = static_cast<std::int64_t>(value);
        visit(index + 1);
      }
    };
    visit(0);
    return values;
  }

  std::vector<std::int64_t> greedy_seed() {
    const auto gate_count = problem_.gate_domains.size();
    std::vector<std::int64_t> greedy = problem_.matched_gate_genes;
    greedy.insert(greedy.end(), problem_.eligible.size(), 1);
    greedy = normalize(greedy);
    auto best = evaluate_normalized(greedy, false);
    for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
      auto trial = greedy;
      trial[gate_count + index] ^= 1;
      trial = normalize(trial);
      if (!budget_can_score_normalized(trial)) break;
      const auto value = evaluate_normalized(trial, false);
      if (evaluated_less(value, best)) {
        greedy = std::move(trial);
        best = value;
      }
    }
    if (gate_count != 0) {
      const auto trials_per_gate = std::max<std::size_t>(
          2, config_.neighbor_sample_size / gate_count);
      for (std::size_t gate = 0; gate < gate_count; ++gate) {
        const auto domain = problem_.gate_domains[gate].size();
        std::set<std::size_t> values;
        if (domain <= trials_per_gate) {
          for (std::size_t value = 0; value < domain; ++value) values.insert(value);
        } else {
          for (std::size_t sample = 0; sample < trials_per_gate; ++sample) {
            values.insert(rounded_index(sample * (domain - 1),
                                        trials_per_gate - 1));
          }
          values.insert(static_cast<std::size_t>(greedy[gate]));
        }
        auto best_gene = greedy[gate];
        auto best_score = best;
        for (const auto value : values) {
          if (value == static_cast<std::size_t>(greedy[gate])) continue;
          auto trial = greedy;
          trial[gate] = static_cast<std::int64_t>(value);
          trial = normalize(trial);
          if (!budget_can_score_normalized(trial)) break;
          const auto score = evaluate_normalized(trial, false);
          if (evaluated_less(score, best_score)) {
            best_gene = trial[gate];
            best_score = score;
          }
        }
        greedy[gate] = best_gene;
        best = best_score;
      }
      for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
        auto trial = greedy;
        trial[gate_count + index] ^= 1;
        trial = normalize(trial);
        if (!budget_can_score_normalized(trial)) break;
        const auto score = evaluate_normalized(trial, false);
        if (evaluated_less(score, best)) {
          greedy = std::move(trial);
          best = score;
        }
      }
    }
    return greedy;
  }

  std::vector<std::vector<std::int64_t>> seed_population(
      const std::vector<std::int64_t>& greedy,
      const std::optional<std::vector<std::int64_t>>& cached_winner) {
    const auto gate_count = problem_.gate_domains.size();
    const auto eligible_count = problem_.eligible.size();
    std::vector<std::vector<std::int64_t>> raw;
    raw.push_back(std::vector<std::int64_t>(gate_count + eligible_count, 0));
    auto all_return = std::vector<std::int64_t>(gate_count + eligible_count, 0);
    std::fill(all_return.begin() + gate_count, all_return.end(), 1);
    raw.push_back(std::move(all_return));
    raw.push_back(greedy);
    if (std::any_of(problem_.recommended_return_mask.begin(),
                    problem_.recommended_return_mask.end(),
                    [](const auto value) { return value; })) {
      auto mixed = problem_.matched_gate_genes;
      for (const auto recommended : problem_.recommended_return_mask) {
        mixed.push_back(recommended ? 1 : 0);
      }
      raw.push_back(mixed);
      for (std::size_t index = 0; index < eligible_count; ++index) {
        if (!problem_.recommended_return_mask[index]) continue;
        auto single = problem_.matched_gate_genes;
        single.resize(gate_count + eligible_count, 0);
        single[gate_count + index] = 1;
        raw.push_back(std::move(single));
      }
    }
    if (cached_winner.has_value() &&
        config_.operator_profile == RichOperatorProfile::kTuned) {
      raw.insert(raw.begin(), *cached_winner);
      ++stats_.cached_winner_elites;
    }
    const auto target_samples = std::max<std::size_t>(
        config_.population_size * 3, config_.population_size + 8);
    while (raw.size() < target_samples) {
      std::vector<std::int64_t> chromosome;
      chromosome.reserve(gate_count + eligible_count);
      for (const auto& domain : problem_.gate_domains) {
        chromosome.push_back(static_cast<std::int64_t>(rng_.randbelow(domain.size())));
      }
      for (std::size_t index = 0; index < eligible_count; ++index) {
        chromosome.push_back(static_cast<std::int64_t>(rng_.randbelow(2)));
      }
      raw.push_back(std::move(chromosome));
    }
    std::vector<std::vector<std::int64_t>> population;
    std::unordered_set<std::vector<std::int64_t>, VectorHash<std::int64_t>> seen;
    for (const auto& chromosome : raw) {
      auto value = normalize(chromosome);
      if (!seen.insert(value).second) continue;
      population.push_back(std::move(value));
      if (population.size() == config_.population_size) break;
    }
    return population;
  }

  std::vector<std::int64_t> partitioned_crossover(
      const std::vector<std::int64_t>& first,
      const std::vector<std::int64_t>& second) {
    auto child = normalize(first);
    const auto mate = normalize(second);
    const auto gate_count = problem_.gate_domains.size();
    const auto eligible_count = problem_.eligible.size();
    const auto copy_partition_tail = [&](std::size_t begin,
                                         std::size_t length) {
      if (length == 0) return;
      const auto cut = rng_.randbelow(length + 1);
      const auto reverse = rng_.randbelow(2) != 0;
      for (std::size_t offset = 0; offset < length; ++offset) {
        const auto from_mate = reverse ? offset < cut : offset >= cut;
        if (from_mate) child[begin + offset] = mate[begin + offset];
      }
    };
    // Gate placement and residency decisions recombine independently so a
    // useful placement block is not tied to an unrelated STAY/RETURN block.
    copy_partition_tail(0, gate_count);
    copy_partition_tail(gate_count, eligible_count);
    ++stats_.crossovers;
    return normalize(child);
  }

  std::vector<std::int64_t> neighbor(
      const std::vector<std::int64_t>& chromosome) {
    if (config_.operator_profile == RichOperatorProfile::kTuned) {
      const auto operation = rng_.randbelow(4);
      if (operation == 1) return high_cost_gate_reselection(chromosome);
      if (operation == 2) return conflict_cluster_swap(chromosome);
      if (operation == 3) return marginal_return_flip(chromosome);
    }
    return separated_mutation(chromosome);
  }

  std::vector<std::int64_t> separated_mutation(
      const std::vector<std::int64_t>& chromosome) {
    auto result = chromosome;
    const auto gate_count = problem_.gate_domains.size();
    const auto eligible_count = problem_.eligible.size();
    if (gate_count != 0 &&
        (eligible_count == 0 || rng_.random() < 0.5)) {
      const auto gate = rng_.randbelow(gate_count);
      const auto random_gene = static_cast<std::int64_t>(
          rng_.randbelow(problem_.gate_domains[gate].size()));
      const std::array<std::int64_t, 3> choices{
          result[gate] + 1, result[gate] - 1, random_gene};
      result[gate] = choices[rng_.randbelow(choices.size())];
      ++stats_.gate_mutations;
    } else if (eligible_count != 0) {
      const auto index = rng_.randbelow(eligible_count);
      result[gate_count + index] ^= 1;
      ++stats_.residency_mutations;
    }
    return normalize(result);
  }

  std::vector<std::int64_t> high_cost_gate_reselection(
      const std::vector<std::int64_t>& chromosome) {
    if (problem_.gate_domains.empty()) return separated_mutation(chromosome);
    std::size_t expensive_gate = 0;
    double expensive_cost = -1.0;
    for (std::size_t gate = 0; gate < problem_.gate_domains.size(); ++gate) {
      const auto option_index = positive_mod(
          chromosome[gate], problem_.gate_domains[gate].size());
      const auto& option = problem_.gate_domains[gate][option_index];
      const auto cost = point_distance(problem_.current_points[option.q1],
                                       option.target1) +
                        point_distance(problem_.current_points[option.q2],
                                       option.target2);
      if (cost > expensive_cost) {
        expensive_cost = cost;
        expensive_gate = gate;
      }
    }
    auto result = chromosome;
    const auto& domain = problem_.gate_domains[expensive_gate];
    std::size_t best = 0;
    double best_cost = std::numeric_limits<double>::infinity();
    for (std::size_t option_index = 0; option_index < domain.size(); ++option_index) {
      const auto& option = domain[option_index];
      const auto cost = point_distance(problem_.current_points[option.q1],
                                       option.target1) +
                        point_distance(problem_.current_points[option.q2],
                                       option.target2);
      if (std::tie(cost, option_index) < std::tie(best_cost, best)) {
        best_cost = cost;
        best = option_index;
      }
    }
    result[expensive_gate] = static_cast<std::int64_t>(best);
    ++stats_.high_cost_gate_reselections;
    return normalize(result);
  }

  std::vector<std::int64_t> conflict_cluster_swap(
      const std::vector<std::int64_t>& chromosome) {
    const auto gate_count = problem_.gate_domains.size();
    if (gate_count == 0 || problem_.eligible.empty()) {
      return separated_mutation(chromosome);
    }
    std::size_t expensive_gate = 0;
    double expensive_cost = -1.0;
    for (std::size_t gate = 0; gate < gate_count; ++gate) {
      const auto option_index = positive_mod(
          chromosome[gate], problem_.gate_domains[gate].size());
      const auto& option = problem_.gate_domains[gate][option_index];
      const auto cost = point_distance(problem_.current_points[option.q1],
                                       option.target1) +
                        point_distance(problem_.current_points[option.q2],
                                       option.target2);
      if (std::tie(cost, gate) > std::tie(expensive_cost, expensive_gate)) {
        expensive_cost = cost;
        expensive_gate = gate;
      }
    }
    auto result = chromosome;
    const auto current_option = positive_mod(
        chromosome[expensive_gate],
        problem_.gate_domains[expensive_gate].size());
    const auto& domain = problem_.gate_domains[expensive_gate];
    if (domain.size() > 1) {
      std::size_t replacement = current_option;
      double replacement_cost = std::numeric_limits<double>::infinity();
      for (std::size_t option_index = 0; option_index < domain.size();
           ++option_index) {
        if (option_index == current_option) continue;
        const auto& option = domain[option_index];
        const auto cost = point_distance(problem_.current_points[option.q1],
                                         option.target1) +
                          point_distance(problem_.current_points[option.q2],
                                         option.target2);
        if (std::tie(cost, option_index) <
            std::tie(replacement_cost, replacement)) {
          replacement = option_index;
          replacement_cost = cost;
        }
      }
      result[expensive_gate] = static_cast<std::int64_t>(replacement);
    }
    const auto& active_option = domain[positive_mod(
        result[expensive_gate], domain.size())];
    std::size_t related = 0;
    double related_distance = std::numeric_limits<double>::infinity();
    for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
      const auto atom = static_cast<std::size_t>(problem_.eligible[index]);
      const auto distance = std::min(
          point_distance(problem_.current_points[atom], active_option.target1),
          point_distance(problem_.current_points[atom], active_option.target2));
      if (std::tie(distance, index) <
          std::tie(related_distance, related)) {
        related_distance = distance;
        related = index;
      }
    }
    result[gate_count + related] ^= 1;
    ++stats_.conflict_cluster_swaps;
    return normalize(result);
  }

  std::vector<std::int64_t> local_polish(
      const std::vector<std::int64_t>& raw_winner) {
    auto winner = normalize(raw_winner);
    // The stochastic archive has already consumed the complete unique-fitness
    // budget.  Building the O(gate-domain * residency) polish neighbourhood
    // cannot admit one more uncached candidate, and every cached candidate is
    // already represented in the ranked archive that produced ``winner``.
    // Long serial QMAP circuits otherwise allocate and discard millions of
    // vectors here after the search is irrevocably finished.
    if (stats_.unique_evaluations >= stats_.stochastic_budget) {
      return winner;
    }
    auto best = evaluate_normalized(winner, false);
    const auto gate_count = problem_.gate_domains.size();
    for (std::size_t sweep = 0; sweep < config_.local_polish_sweeps; ++sweep) {
      std::vector<std::vector<std::int64_t>> neighbors;
      for (std::size_t gate = 0; gate < gate_count; ++gate) {
        const auto current = positive_mod(
            winner[gate], problem_.gate_domains[gate].size());
        for (std::size_t option = 0;
             option < problem_.gate_domains[gate].size(); ++option) {
          if (option == current) continue;
          auto trial = winner;
          trial[gate] = static_cast<std::int64_t>(option);
          neighbors.push_back(std::move(trial));
        }
      }
      if (problem_.decision_policy == RichDecisionPolicy::kOptimize) {
        // A serial layer has one coupled placement/residency decision.  A
        // gate-only or residency-only descent can therefore be trapped even
        // when changing both is strictly better (including a participant's
        // RETURN->re-entry cycle).  Enumerate this bounded two-gene
        // neighbourhood in stable eligible/option order.  score_unique()
        // performs normalization, de-duplication, and the same strict unique
        // evaluation-budget check as every other polish candidate.
        if (gate_count == 1) {
          const auto current = positive_mod(
              winner[0], problem_.gate_domains[0].size());
          for (std::size_t index = 0;
               index < problem_.eligible.size(); ++index) {
            for (std::size_t option = 0;
                 option < problem_.gate_domains[0].size(); ++option) {
              if (option == current) continue;
              auto trial = winner;
              trial[0] = static_cast<std::int64_t>(option);
              trial[gate_count + index] ^= 1;
              neighbors.push_back(std::move(trial));
            }
          }
        }
        for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
          auto trial = winner;
          trial[gate_count + index] ^= 1;
          neighbors.push_back(std::move(trial));
        }
      }
      const auto before = stats_.unique_evaluations;
      auto scored = score_unique(neighbors, true);
      stats_.local_polish_evaluations +=
          stats_.unique_evaluations - before;
      if (scored.empty() || !evaluated_less(scored.front(), best)) break;
      best = scored.front();
      winner = best.fitness.chromosome;
      if (stats_.unique_evaluations >= stats_.stochastic_budget) break;
    }
    return winner;
  }

  std::vector<std::int64_t> marginal_return_flip(
      const std::vector<std::int64_t>& chromosome) {
    const auto gate_count = problem_.gate_domains.size();
    if (problem_.eligible.empty()) return separated_mutation(chromosome);
    std::size_t best_index = 0;
    double best_margin = -1.0;
    for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
      double nearest = std::numeric_limits<double>::infinity();
      for (const auto& option : problem_.return_domains[index]) {
        nearest = std::min(nearest, option.cost);
      }
      if (!std::isfinite(nearest)) nearest = 1e100;
      // Prefer reconsidering the decision whose physical RETURN edge is most
      // extreme.  The full physical objective still ranks the resulting child.
      const auto margin = chromosome[gate_count + index] == 0
                              ? 1.0 / (1.0 + nearest)
                              : nearest;
      if (margin > best_margin) {
        best_margin = margin;
        best_index = index;
      }
    }
    auto result = chromosome;
    result[gate_count + best_index] ^= 1;
    ++stats_.marginal_return_flips;
    return normalize(result);
  }

  const ArchitectureSnapshot& architecture_;
  RichH0Problem problem_;
  const RichSearchConfig& config_;
  std::vector<std::vector<RichReturnOption>> return_domains_;
  std::unordered_set<std::int64_t> storage_site_ids_;
  std::set<std::pair<double, double>> zone_points_;
  std::vector<bool> participant_mask_;
  PythonRandom rng_;
  RichSearchStats stats_;
  std::unordered_map<std::vector<std::int64_t>, std::vector<std::int64_t>,
                     VectorHash<std::int64_t>> normalize_cache_;
  std::unordered_map<std::vector<std::int64_t>, DecodeResult,
                     VectorHash<std::int64_t>> decode_cache_;
  std::unordered_map<std::vector<std::size_t>,
                     std::vector<std::vector<ReturnAssignment>>,
                     VectorHash<std::size_t>> return_cache_;
  std::unordered_map<std::vector<std::int64_t>, Evaluated,
                     VectorHash<std::int64_t>> fitness_cache_;
  std::map<std::vector<std::int64_t>, Evaluated> complete_evaluated_archive_;
  std::optional<Evaluated> guarded_final_value_;
  std::vector<std::int64_t> current_gate_anchor_;
  std::vector<std::int64_t> current_gate_anchor_assignment_site_ids_;
  std::vector<std::int64_t> current_gate_final_assignment_site_ids_;
  std::string current_gate_guard_branch_{"inactive"};
  std::size_t current_gate_guard_cohort_size_{};
  std::size_t current_gate_guard_admitted_size_{};
  std::string current_gate_projection_source_{"inactive"};
  std::size_t current_gate_projection_evaluated_{};
  std::unordered_map<std::vector<std::int64_t>, double,
                     VectorHash<std::int64_t>> partial_current_bounds_;
  std::unordered_map<std::vector<std::uint64_t>, ForecastReplay,
                     VectorHash<std::uint64_t>> forecast_state_cache_;
  std::unordered_set<std::vector<std::int64_t>, VectorHash<std::int64_t>>
      evaluated_keys_;
  std::size_t forecast_word_count_{};
  std::size_t forecast_terms_skipped_cutoff_{};
  std::vector<CompiledForecastTerm> compiled_forecast_terms_;
  ForecastBits constant_forecast_bits_;
  std::vector<ForecastBits> stay_forecast_bits_;
  std::vector<ForecastBits> return_forecast_bits_;
  std::vector<std::vector<unsigned char>> future_participant_masks_;
  std::vector<double> future_decay_;
  std::vector<std::vector<ForecastBits>> gate_option_forecast_bits_;
  std::map<std::pair<std::size_t, std::int64_t>, ForecastBits>
      return_site_forecast_bits_;
  std::map<std::pair<std::size_t, std::size_t>, ForecastBits>
      stay_pair_forecast_bits_;
  std::map<std::pair<std::size_t, std::size_t>, ForecastBits>
      return_pair_forecast_bits_;
  std::int64_t normalize_ns_{};
  std::int64_t decode_ns_{};
  std::int64_t return_match_ns_{};
  std::int64_t fitness_ns_{};
  std::int64_t forecast_ns_{};
  std::int64_t selection_ns_{};
};

}  // namespace

RichSolveResult solve_rich_h0(
    const ArchitectureSnapshot& architecture, const RichH0Problem& problem,
    const RichSearchConfig& config, PythonRandomState rng_state,
    const std::optional<std::vector<std::int64_t>>& cached_winner) {
  return RichSolver(architecture, problem, config, rng_state).solve(cached_winner);
}

}  // namespace zac_native
