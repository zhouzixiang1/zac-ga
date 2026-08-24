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
constexpr double kAccelUmPerUs2 = 0.00275;
constexpr double kT2Us = 1.5e6;

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

struct Evaluated {
  FitnessResult fitness;
  DecodeResult decoded;
  std::vector<ReturnAssignment> assignments;
  std::vector<ReturnAssignment> reseats;
  double forecast_nll{};
  double search_nll{};
  std::vector<double> forecast_by_depth;
  std::array<double, 4> forecast_by_category{};
  std::vector<std::int64_t> assignment_key;
  std::size_t return_assignment_rank{};
  std::size_t return_assignment_evaluated{};
  std::size_t current_ghost_rejections{};
  std::size_t pre_score_reseats{};
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
    const std::vector<Point>& positions) {
  std::vector<std::uint64_t> key;
  key.reserve(positions.size() * 2);
  for (const auto& point : positions) {
    key.push_back(double_bits(point.x));
    key.push_back(double_bits(point.y));
  }
  return key;
}

struct PlanGeometry {
  std::vector<Point> positions_t1;
  std::vector<Leg> back_legs;
  std::vector<std::int64_t> back_owners;
  std::vector<Leg> out_legs;
  std::vector<std::int64_t> out_owners;
  std::vector<Ghost> ghosts_t0;
  std::vector<Ghost> ghosts_t1;
  std::set<std::int64_t> blockers;
  std::size_t violations{};
  std::size_t ghost_violations{};
};

struct ReseatRepair {
  std::vector<ReturnAssignment> reseats;
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
  if (first.fitness.feasible != second.fitness.feasible) {
    return first.fitness.feasible;
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
        auto population = seed_population(greedy, cached_winner);
        auto scored = score_unique(population, false);
        if (scored.empty()) throw std::runtime_error("initial population is empty");
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
            for (std::size_t sample = 0; sample < config_.neighbor_sample_size;
                 ++sample) {
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
            auto pool = score_unique(neighbors, true);
            const auto take = std::min(config_.neighbors_per_solution, pool.size());
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
          const auto elites = config_.operator_profile == RichOperatorProfile::kExact
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
          auto next = score_unique(next_raw, false);
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
    const auto selection_started = Clock::now();
    const auto final_value = evaluate_normalized(winner, false);
    auto final_geometry = build_geometry(
        final_value.decoded, final_value.assignments, final_value.reseats);
    const auto recorded_winner = score_geometry(
        winner, std::move(final_geometry), final_value.assignments.size(),
        config_.enforce_single_leg_ghost, true);
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
  void validate() const {
    if (problem_.n_atoms != architecture_.n_atoms() ||
        problem_.current_points.size() != problem_.n_atoms) {
      throw std::invalid_argument("rich atom count differs from architecture");
    }
    if (problem_.return_domains.size() != problem_.eligible.size() ||
        problem_.forced_return_mask.size() != problem_.eligible.size()) {
      throw std::invalid_argument("rich eligible arrays are not aligned");
    }
    if (problem_.eviction_order_indices.size() != problem_.eligible.size()) {
      throw std::invalid_argument("rich eviction order has wrong size");
    }
    if (problem_.matched_gate_genes.size() != problem_.gate_domains.size()) {
      throw std::invalid_argument("matched gate genes have wrong size");
    }
    if (problem_.min_returns > problem_.eligible.size()) {
      throw std::invalid_argument("min_returns exceeds eligible count");
    }
    if (config_.population_size == 0 || config_.iterations == 0 ||
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
      if (term.depth == 0 || term.depth > config_.max_horizon || term.nll < 0.0 ||
          !std::isfinite(term.nll)) {
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
      if (bit) ++selected;
    }
    if (selected < problem_.min_returns) {
      for (const auto index : problem_.eviction_order_indices) {
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

  void apply_forecast(Evaluated& result,
                      const std::vector<std::int64_t>& chromosome) {
    const auto started = Clock::now();
    result.forecast_by_depth.assign(config_.max_horizon + 1, 0.0);
    result.forecast_by_category.fill(0.0);
    result.forecast_nll = 0.0;
    if (config_.max_horizon == 0 ||
        (problem_.forecast_terms.empty() && problem_.future_layers.empty()) ||
        config_.alpha_lookahead == 0.0) {
      result.search_nll = result.fitness.negative_log_fidelity;
      forecast_ns_ += elapsed_ns(started);
      return;
    }
    if (!problem_.future_layers.empty()) {
      apply_native_rollout(result);
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
      const std::vector<Point>& positions, std::int64_t idle_exposures = 0) const {
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
    return evaluate_candidate_summary(
        architecture_, candidate, boundary_config);
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
                           std::vector<Point>& positions) const {
    const auto& source = positions[static_cast<std::size_t>(atom)];
    const auto& choices =
        architecture_.storage_site_ids_by_distance(source);
    std::optional<std::pair<std::int64_t, FitnessResult>> best;
    std::size_t tested = 0;
    for (const auto site_id : choices) {
      const auto& target = architecture_.site_coordinates()[
          static_cast<std::size_t>(site_id)];
      if (point_occupied(positions, target, atom)) continue;
      const auto distance = point_distance(source, target);
      if (distance <= 1e-9) continue;
      std::vector<Leg> legs;
      if (distance > 1e-9) legs.push_back({distance, source, target});
      const auto score = score_forecast_phase(
          legs, legs.empty() ? std::vector<std::int64_t>{}
                             : std::vector<std::int64_t>{atom},
          positions);
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
      // The registered 1/2/4 rollout budget now controls the number of
      // physically replayed safe parking alternatives as well as the future
      // gate support.  This keeps M4 bounded without a proxy ghost penalty.
      if (best.has_value() &&
          tested >= config_.forecast_gate_candidate_budget) {
        break;
      }
    }
    if (best.has_value()) {
      positions[static_cast<std::size_t>(atom)] =
          architecture_.site_coordinates()[
              static_cast<std::size_t>(best->first)];
    }
    return best;
  }

  static double finite_nll(const FitnessResult& score) {
    return score.feasible ? score.negative_log_fidelity
                          : std::numeric_limits<double>::infinity();
  }

  FitnessResult score_forecast_relocation_batch(
      const std::vector<Point>& before, const std::vector<Point>& after,
      const std::set<std::int64_t>& atoms) const {
    std::vector<Leg> legs;
    std::vector<std::int64_t> owners;
    for (const auto atom : atoms) {
      const auto index = static_cast<std::size_t>(atom);
      const auto distance = point_distance(before[index], after[index]);
      if (distance <= 1e-9) continue;
      legs.push_back({distance, before[index], after[index]});
      owners.push_back(atom);
    }
    return score_forecast_phase(legs, owners, before);
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

  void apply_native_rollout(Evaluated& result) {
    auto positions = problem_.current_points;
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

    const auto state_key = forecast_state_key(positions);
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
    for (std::size_t layer_index = 0;
         layer_index < problem_.future_layers.size(); ++layer_index) {
      const auto& layer = problem_.future_layers[layer_index];
      const auto decay = std::pow(
          config_.decay_rho, static_cast<double>(layer.depth - 1));
      if (decay < config_.decay_epsilon) {
        ++stats_.forecast_terms_skipped_cutoff;
        continue;
      }
      std::set<std::int64_t> participants;
      for (const auto& gate : layer.gates) {
        participants.insert(gate.first);
        participants.insert(gate.second);
      }

      struct FuturePlacement {
        std::int64_t q1{};
        std::int64_t q2{};
        Point target1;
        Point target2;
      };
      std::vector<FuturePlacement> placements;
      std::set<std::size_t> used_pairs;
      for (const auto& gate : layer.gates) {
        std::optional<std::tuple<double, std::size_t, bool>> selected;
        for (std::size_t pair_index = 0; pair_index < site_pairs.size();
             ++pair_index) {
          if (used_pairs.count(pair_index) != 0U) continue;
          const auto& pair = site_pairs[pair_index];
          const auto& left = architecture_.site_coordinates()[
              static_cast<std::size_t>(pair[0])];
          const auto& right = architecture_.site_coordinates()[
              static_cast<std::size_t>(pair[1])];
          for (const auto reversed : {false, true}) {
            const auto& first = reversed ? right : left;
            const auto& second = reversed ? left : right;
            auto cost = point_distance(
                            positions[static_cast<std::size_t>(gate.first)],
                            first) +
                        point_distance(
                            positions[static_cast<std::size_t>(gate.second)],
                            second);
            for (std::size_t atom = 0; atom < positions.size(); ++atom) {
              const auto atom_id = static_cast<std::int64_t>(atom);
              if (participants.count(atom_id) != 0U) continue;
              if (same_point(positions[atom], first) ||
                  same_point(positions[atom], second)) {
                // This is only a deterministic placement ordering key.  The
                // selected blocker is physically moved and scored below.
                auto nearest_storage = std::numeric_limits<double>::infinity();
                for (const auto site_id : architecture_.storage_site_ids()) {
                  nearest_storage = std::min(
                      nearest_storage,
                      point_distance(
                          positions[atom],
                          architecture_.site_coordinates()[
                              static_cast<std::size_t>(site_id)]));
                }
                cost += nearest_storage;
              }
            }
            const auto key = std::make_tuple(cost, pair_index, reversed);
            if (!selected.has_value() || key < *selected) selected = key;
          }
        }
        if (!selected.has_value()) {
          result.forecast_nll = std::numeric_limits<double>::infinity();
          result.search_nll = std::numeric_limits<double>::infinity();
          remember();
          return;
        }
        const auto pair_index = std::get<1>(*selected);
        const auto reversed = std::get<2>(*selected);
        used_pairs.insert(pair_index);
        const auto& pair = site_pairs[pair_index];
        const auto& left = architecture_.site_coordinates()[
            static_cast<std::size_t>(pair[0])];
        const auto& right = architecture_.site_coordinates()[
            static_cast<std::size_t>(pair[1])];
        placements.push_back({gate.first, gate.second,
                              reversed ? right : left,
                              reversed ? left : right});
      }

      std::set<std::int64_t> blockers;
      for (const auto& placement : placements) {
        for (std::size_t atom = 0; atom < positions.size(); ++atom) {
          const auto atom_id = static_cast<std::int64_t>(atom);
          if (participants.count(atom_id) != 0U) continue;
          if (same_point(positions[atom], placement.target1) ||
              same_point(positions[atom], placement.target2)) {
            blockers.insert(atom_id);
          }
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
          const auto hits = ghost_hit_atoms(
              {{distance, source, target}}, forecast_ghosts(positions));
          for (const auto hit : hits) {
            if (participants.count(hit) == 0U) blockers.insert(hit);
          }
        }
      }

      const auto before_blockers = positions;
      double routing_nll = 0.0;
      for (const auto blocker : blockers) {
        const auto moved = forecast_move_to_storage(blocker, positions);
        if (!moved.has_value()) {
          routing_nll = std::numeric_limits<double>::infinity();
          break;
        }
        routing_nll += finite_nll(moved->second);
      }
      if (std::isfinite(routing_nll) && !blockers.empty()) {
        const auto batched = score_forecast_relocation_batch(
            before_blockers, positions, blockers);
        if (batched.feasible) routing_nll = finite_nll(batched);
      }

      std::vector<Leg> out_legs;
      std::vector<std::int64_t> out_owners;
      for (const auto& placement : placements) {
        for (const auto& [atom, target] :
             {std::pair<std::int64_t, Point>{placement.q1, placement.target1},
              std::pair<std::int64_t, Point>{placement.q2, placement.target2}}) {
          const auto& source = positions[static_cast<std::size_t>(atom)];
          const auto distance = point_distance(source, target);
          if (distance > 1e-9) {
            out_legs.push_back({distance, source, target});
            out_owners.push_back(atom);
          }
        }
      }
      const auto out_score = score_forecast_phase(
          out_legs, out_owners, positions);
      const auto reentry_nll = finite_nll(out_score);
      for (const auto& placement : placements) {
        positions[static_cast<std::size_t>(placement.q1)] = placement.target1;
        positions[static_cast<std::size_t>(placement.q2)] = placement.target2;
      }

      std::int64_t idle_exposures = 0;
      for (std::size_t atom = 0; atom < positions.size(); ++atom) {
        if (participants.count(static_cast<std::int64_t>(atom)) == 0U &&
            is_zone_point(positions[atom])) {
          ++idle_exposures;
        }
      }
      const auto idle_score = score_forecast_phase(
          {}, {}, positions, idle_exposures);
      const auto residency_nll = finite_nll(idle_score);

      std::set<std::int64_t> later_use;
      for (std::size_t later = layer_index + 1;
           later < problem_.future_layers.size(); ++later) {
        for (const auto& gate : problem_.future_layers[later].gates) {
          later_use.insert(gate.first);
          later_use.insert(gate.second);
        }
      }
      double terminal_nll = 0.0;
      std::set<std::int64_t> terminal_atoms;
      const auto before_terminal = positions;
      for (std::size_t atom = 0; atom < positions.size(); ++atom) {
        const auto atom_id = static_cast<std::int64_t>(atom);
        if (!is_zone_point(positions[atom]) ||
            later_use.count(atom_id) != 0U) {
          continue;
        }
        terminal_atoms.insert(atom_id);
        const auto moved = forecast_move_to_storage(atom_id, positions);
        if (!moved.has_value()) {
          terminal_nll = std::numeric_limits<double>::infinity();
          break;
        }
        terminal_nll += finite_nll(moved->second);
      }
      if (std::isfinite(terminal_nll) && !terminal_atoms.empty()) {
        const auto batched = score_forecast_relocation_batch(
            before_terminal, positions, terminal_atoms);
        if (batched.feasible) terminal_nll = finite_nll(batched);
      }

      add_weighted_forecast(result, layer.depth, 0, residency_nll);
      add_weighted_forecast(result, layer.depth, 1, reentry_nll);
      add_weighted_forecast(result, layer.depth, 2, terminal_nll);
      add_weighted_forecast(result, layer.depth, 3, routing_nll);
      ++stats_.forecast_terms_applied;
    }
    result.search_nll = result.fitness.negative_log_fidelity +
                        result.forecast_nll;
    remember();
  }

  PlanGeometry build_geometry(
      const DecodeResult& decoded,
      const std::vector<ReturnAssignment>& assignments,
      const std::vector<ReturnAssignment>& reseats) const {
    PlanGeometry geometry;
    geometry.positions_t1 = problem_.current_points;
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
    };
    for (const auto& assignment : assignments) append_back(assignment);
    for (const auto& reseat : reseats) append_back(reseat);

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
    std::vector<bool> back_movers(problem_.n_atoms, false);
    std::vector<bool> out_movers(problem_.n_atoms, false);
    for (const auto owner : geometry.back_owners) {
      back_movers[static_cast<std::size_t>(owner)] = true;
    }
    for (const auto owner : geometry.out_owners) {
      out_movers[static_cast<std::size_t>(owner)] = true;
    }
    const auto replay_single_legs = [&](const auto& legs,
                                        const auto& ghosts,
                                        const auto& movers) {
      for (std::size_t index = 0; index < legs.size(); ++index) {
        const auto& leg = legs[index];
        for (const auto& ghost : ghosts) {
          const auto atom = static_cast<std::size_t>(ghost.atom);
          if (atom < movers.size() && movers[atom]) continue;
          const auto x = cover(leg.source.x, leg.target.x, ghost.position.x);
          const auto y = cover(leg.source.y, leg.target.y, ghost.position.y);
          if (!coverage_matches(x, y)) continue;
          ++geometry.violations;
          ++geometry.ghost_violations;
          geometry.blockers.insert(ghost.atom);
        }
      }
    };
    replay_single_legs(
        geometry.back_legs, geometry.ghosts_t0, back_movers);
    replay_single_legs(
        geometry.out_legs, geometry.ghosts_t1, out_movers);
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
    return (record_batches
                ? evaluate_candidate(architecture_, candidate, boundary_config)
                : evaluate_candidate_summary(
                      architecture_, candidate, boundary_config));
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
          if (trial_violations >= best_violations) continue;
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
    const auto& geometry = reseat_repair.geometry;
    if (geometry.violations != 0) {
      result.fitness = infeasible_fitness(
          chromosome,
          geometry.ghost_violations != 0
              ? "unresolved current single-leg ghost hit"
              : "unresolved current gate occupancy");
    } else {
      result.fitness = score_geometry(
          chromosome, std::move(reseat_repair.geometry), returners.size(),
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
      const std::vector<std::int64_t>& chromosome, bool stochastic) {
    ++stats_.evaluations;
    const auto cached = fitness_cache_.find(chromosome);
    if (config_.fitness_cache && cached != fitness_cache_.end()) {
      ++stats_.fitness_hits;
      return cached->second;
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
    bool have_best = false;
    std::size_t ghost_rejections = 0;
    for (std::size_t rank = 0; rank < candidates.size(); ++rank) {
      auto candidate = evaluate_assignment(
          chromosome, result.decoded, returners, candidates[rank], rank,
          candidates.size(), false);
      ghost_rejections += candidate.current_ghost_rejections;
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
    fitness_ns_ += elapsed_ns(started);
    return result;
  }

  double physical_lower_bound(
      const DecodeResult& decoded,
      const std::vector<ReturnAssignment>& assignments,
      std::size_t return_count) const {
    std::size_t back_movers = 0;
    double back_longest = 0.0;
    for (const auto& assignment : assignments) {
      const auto atom = static_cast<std::size_t>(
          problem_.eligible[assignment.eligible_index]);
      const auto distance = point_distance(
          problem_.current_points[atom], assignment.point);
      if (distance <= 1e-9) continue;
      ++back_movers;
      back_longest = std::max(back_longest, distance);
    }

    std::size_t out_movers = 0;
    double out_longest = 0.0;
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
        out_longest = std::max(out_longest, distance);
      }
    }

    const auto idle_exposures = problem_.eligible.size() - return_count;
    double coherence_nll = -static_cast<double>(idle_exposures) *
                           std::log1p(-kRydbergUs / kT2Us);
    const auto add_phase = [&](std::size_t movers, double longest) {
      if (movers == 0) return true;
      // Every legal phase must execute its longest individual trajectory.
      // Treating every leg as if it shared one batch is therefore an
      // admissible (optimistic) lower bound on the implemented phase time.
      const auto phase_time =
          2.0 * kTransferUs + std::sqrt(longest / kAccelUmPerUs2);
      const auto mover_idle = std::max(0.0, phase_time - 2.0 * kTransferUs);
      if (phase_time >= kT2Us || mover_idle >= kT2Us) return false;
      coherence_nll -= static_cast<double>(problem_.n_atoms - movers) *
                       std::log1p(-phase_time / kT2Us);
      coherence_nll -= static_cast<double>(movers) *
                       std::log1p(-mover_idle / kT2Us);
      return true;
    };
    if (!add_phase(back_movers, back_longest) ||
        !add_phase(out_movers, out_longest)) {
      return std::numeric_limits<double>::infinity();
    }
    const auto transfers = 2 * (back_movers + out_movers);
    const auto transfer_nll =
        -static_cast<double>(transfers) * std::log(kFTransfer);
    const auto idle_nll =
        -static_cast<double>(idle_exposures) * std::log(kFExc);
    return transfer_nll + idle_nll + coherence_nll;
  }

  std::optional<Evaluated> score_direct_exact(
      const std::vector<std::vector<std::int64_t>>& raw_values) {
    std::optional<Evaluated> best;
    std::unordered_set<std::vector<std::int64_t>, VectorHash<std::int64_t>> seen;
    for (const auto& raw : raw_values) {
      const auto chromosome = normalize(raw);
      if (!seen.insert(chromosome).second) continue;

      if (best.has_value() && std::isfinite(best->search_nll)) {
        const auto decoded = decode(chromosome);
        bool can_improve = !decoded.feasible;
        if (decoded.feasible) {
          const auto gate_count = problem_.gate_domains.size();
          std::vector<std::size_t> returners;
          for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
            if (chromosome[gate_count + index] != 0) {
              returners.push_back(index);
            }
          }
          try {
            const auto assignments = match_returns(returners);
            can_improve = assignments.empty();
            for (const auto& assignment : assignments) {
              if (physical_lower_bound(decoded, assignment, returners.size()) <=
                  best->search_nll + 1e-12) {
                can_improve = true;
                break;
              }
            }
          } catch (const std::exception&) {
            // Preserve the established fail-closed diagnostic path: the full
            // evaluator owns malformed/infeasible RETURN reporting.
            can_improve = true;
          }
        }
        if (!can_improve) {
          ++stats_.evaluations;
          if (evaluated_keys_.insert(chromosome).second) {
            ++stats_.unique_evaluations;
            ++stats_.deterministic_unique_evaluations;
          }
          ++stats_.direct_lower_bound_prunes;
          continue;
        }
      }

      auto value = evaluate_normalized(chromosome, false);
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
  const RichH0Problem& problem_;
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
