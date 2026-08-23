#include "zac_native/rich_solver.hpp"

#include "zac_native/core.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <functional>
#include <limits>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>
#include <tuple>
#include <utility>
#include <vector>

namespace zac_native {
namespace {

using Clock = std::chrono::steady_clock;

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
  double forecast_nll{};
  double search_nll{};
  std::vector<double> forecast_by_depth;
  std::array<double, 3> forecast_by_category{};
};

bool evaluated_less(const Evaluated& first, const Evaluated& second) {
  if (first.fitness.feasible != second.fitness.feasible) {
    return first.fitness.feasible;
  }
  return std::tie(first.search_nll, first.fitness.move_batches,
                  first.fitness.move_time_us, first.fitness.total_distance_um,
                  first.fitness.chromosome) <
         std::tie(second.search_nll, second.fitness.move_batches,
                  second.fitness.move_time_us, second.fitness.total_distance_um,
                  second.fitness.chromosome);
}

std::optional<std::vector<std::size_t>> hungarian_assignment(
    const std::vector<std::vector<RichReturnOption>>& domains,
    const std::vector<std::size_t>& returners) {
  std::map<std::int64_t, std::size_t> site_index;
  std::vector<std::int64_t> sites;
  for (const auto eligible_index : returners) {
    for (const auto& option : domains[eligible_index]) {
      if (site_index.count(option.site_id) == 0U) {
        site_index[option.site_id] = sites.size();
        sites.push_back(option.site_id);
      }
    }
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
  }

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
    if (assignment[row] >= columns || costs[row][assignment[row]] >= kInfinity / 2) {
      return std::nullopt;
    }
    assignment[row] = static_cast<std::size_t>(sites[assignment[row]]);
  }
  return assignment;
}

class RichSolver {
 public:
  RichSolver(const ArchitectureSnapshot& architecture, const RichH0Problem& problem,
             const RichSearchConfig& config, PythonRandomState rng_state)
      : architecture_(architecture), problem_(problem), config_(config),
        rng_(rng_state) {
    validate();
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
      evaluate(winner, false);
      search_mode = "lru";
      stats_.early_stop_reason = "lru";
    } else {
      const auto direct_space = direct_search_space();
      if (direct_space <= 64) {
        auto chromosomes = enumerate_chromosomes();
        auto scored = score_unique(chromosomes, false);
        if (scored.empty()) throw std::runtime_error("direct search has no candidate");
        winner = scored.front().fitness.chromosome;
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
              neighbors.push_back(neighbor(parent.fitness.chromosome));
            }
            auto pool = score_unique(neighbors, true);
            const auto take = std::min(config_.neighbors_per_solution, pool.size());
            for (std::size_t index = 0; index < take; ++index) {
              offspring.push_back(pool[index].fitness.chromosome);
            }
            if (stats_.stochastic_unique_evaluations >= stats_.stochastic_budget) {
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
        search_mode = (stats_.stochastic_unique_evaluations >=
                               stats_.stochastic_budget
                           ? "ga-budget"
                           : (stats_.early_stopped ? "ga-early-stop" : "ga"));
        if (stats_.early_stop_reason == "not-started") {
          stats_.early_stop_reason = "iterations-completed";
        }
      }
    }
    const auto selection_started = Clock::now();
    const auto final_value = evaluate(winner, false);
    selection_ns_ += elapsed_ns(selection_started);
    RichSolveResult result;
    result.winner = final_value.fitness;
    result.gate_option_indices = final_value.decoded.option_indices;
    for (const auto& assignment : final_value.assignments) {
      result.return_assignments.emplace_back(
          problem_.eligible[assignment.eligible_index], assignment.site_id);
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
        config_.elite_count > config_.population_size) {
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

  std::vector<ReturnAssignment> match_returns(
      const std::vector<std::size_t>& returners) {
    const auto started = Clock::now();
    const auto cached = return_cache_.find(returners);
    if (config_.fitness_cache && cached != return_cache_.end()) {
      ++stats_.return_match_hits;
      return_match_ns_ += elapsed_ns(started);
      return cached->second;
    }
    std::vector<ReturnAssignment> result;
    if (returners.empty()) {
      if (config_.fitness_cache) return_cache_[returners] = result;
      return_match_ns_ += elapsed_ns(started);
      return result;
    }
    bool independent = true;
    std::set<std::int64_t> independent_sites;
    for (const auto index : returners) {
      auto options = problem_.return_domains[index];
      std::sort(options.begin(), options.end(), [](const auto& first,
                                                  const auto& second) {
        return std::tie(first.cost, first.site_id) <
               std::tie(second.cost, second.site_id);
      });
      if (options.empty() ||
          (options.size() > 1 && options[0].cost == options[1].cost) ||
          !independent_sites.insert(options[0].site_id).second) {
        independent = false;
        break;
      }
      result.push_back({index, options[0].site_id, options[0].point});
    }
    if (!independent) {
      result.clear();
      const auto assignment = hungarian_assignment(problem_.return_domains, returners);
      if (assignment.has_value()) {
        for (std::size_t row = 0; row < returners.size(); ++row) {
          const auto eligible_index = returners[row];
          const auto site_id = static_cast<std::int64_t>((*assignment)[row]);
          const auto& domain = problem_.return_domains[eligible_index];
          const auto found = std::find_if(
              domain.begin(), domain.end(), [&](const auto& option) {
                return option.site_id == site_id;
              });
          if (found == domain.end()) {
            throw std::runtime_error("Hungarian result is absent from domain");
          }
          result.push_back({eligible_index, site_id, found->point});
        }
      } else {
        std::set<std::int64_t> taken;
        const std::set<std::int64_t> occupied(
            problem_.occupied_storage_site_ids.begin(),
            problem_.occupied_storage_site_ids.end());
        for (const auto eligible_index : returners) {
          auto options = problem_.return_domains[eligible_index];
          std::sort(options.begin(), options.end(), [](const auto& first,
                                                      const auto& second) {
            return std::tie(first.cost, first.site_id) <
                   std::tie(second.cost, second.site_id);
          });
          auto local = std::find_if(options.begin(), options.end(),
                                    [&](const auto& option) {
            return taken.count(option.site_id) == 0U;
          });
          if (local != options.end()) {
            result.push_back({eligible_index, local->site_id, local->point});
            taken.insert(local->site_id);
            continue;
          }
          std::optional<std::pair<double, std::int64_t>> best_key;
          Point best_point;
          for (const auto site_id : architecture_.storage_site_ids()) {
            if (occupied.count(site_id) != 0U || taken.count(site_id) != 0U) continue;
            const auto& point = architecture_.site_coordinates().at(
                static_cast<std::size_t>(site_id));
            const auto candidate = std::make_pair(
                std::sqrt(point_distance(problem_.current_points[
                                             problem_.eligible[eligible_index]],
                                         point)),
                site_id);
            if (!best_key.has_value() || candidate < *best_key) {
              best_key = candidate;
              best_point = point;
            }
          }
          if (!best_key.has_value()) {
            throw std::runtime_error("RETURN matching has no free fallback site");
          }
          result.push_back({eligible_index, best_key->second, best_point});
          taken.insert(best_key->second);
        }
      }
    }
    if (config_.fitness_cache) return_cache_[returners] = result;
    return_match_ns_ += elapsed_ns(started);
    return result;
  }

  void apply_forecast(Evaluated& result,
                      const std::vector<std::int64_t>& chromosome) {
    const auto started = Clock::now();
    result.forecast_by_depth.assign(config_.max_horizon + 1, 0.0);
    result.forecast_by_category.fill(0.0);
    if (config_.max_horizon == 0 || problem_.forecast_terms.empty() ||
        config_.alpha_lookahead == 0.0) {
      result.forecast_nll = 0.0;
      result.search_nll = result.fitness.negative_log_fidelity;
      forecast_ns_ += elapsed_ns(started);
      return;
    }
    const auto gate_count = problem_.gate_domains.size();
    std::map<std::size_t, std::int64_t> assigned_sites;
    for (const auto& assignment : result.assignments) {
      assigned_sites[assignment.eligible_index] = assignment.site_id;
    }
    for (const auto& term : problem_.forecast_terms) {
      const auto decay_factor = std::pow(
          config_.decay_rho, static_cast<double>(term.depth - 1));
      if (decay_factor < config_.decay_epsilon) {
        ++stats_.forecast_terms_skipped_cutoff;
        continue;
      }
      const auto weight = config_.alpha_lookahead * decay_factor;
      bool applies = false;
      const auto bit = [&](std::int64_t index) {
        return chromosome[gate_count + static_cast<std::size_t>(index)] != 0;
      };
      switch (term.kind) {
        case RichForecastKind::kConstant:
          applies = true;
          break;
        case RichForecastKind::kStay:
          applies = !bit(term.index);
          break;
        case RichForecastKind::kReturn:
          applies = bit(term.index);
          break;
        case RichForecastKind::kReturnSite: {
          const auto found = assigned_sites.find(
              static_cast<std::size_t>(term.index));
          applies = found != assigned_sites.end() &&
                    found->second == term.selector;
          break;
        }
        case RichForecastKind::kGateOption:
          applies = result.decoded.option_indices[
                        static_cast<std::size_t>(term.index)] ==
                    static_cast<std::size_t>(term.selector);
          break;
        case RichForecastKind::kStayPair:
          applies = !bit(term.index) && !bit(term.second_index);
          break;
        case RichForecastKind::kReturnPair:
          applies = bit(term.index) && bit(term.second_index);
          break;
      }
      if (!applies) continue;
      const auto contribution = weight * term.nll;
      result.forecast_nll += contribution;
      result.forecast_by_depth[term.depth] += contribution;
      result.forecast_by_category[static_cast<std::size_t>(term.category)] +=
          contribution;
      ++stats_.forecast_terms_applied;
    }
    result.search_nll = result.fitness.negative_log_fidelity +
                        result.forecast_nll;
    forecast_ns_ += elapsed_ns(started);
  }

  Evaluated evaluate(const std::vector<std::int64_t>& raw, bool stochastic) {
    ++stats_.evaluations;
    const auto chromosome = normalize(raw);
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
    try {
      result.assignments = match_returns(returners);
    } catch (const std::exception& error) {
      result.fitness = infeasible_fitness(chromosome, error.what());
      if (config_.fitness_cache) fitness_cache_[chromosome] = result;
      fitness_ns_ += elapsed_ns(started);
      return result;
    }
    auto positions_t1 = problem_.current_points;
    std::vector<Leg> back_legs;
    std::vector<std::int64_t> back_owners;
    for (const auto& assignment : result.assignments) {
      const auto q = problem_.eligible[assignment.eligible_index];
      const auto& source = problem_.current_points[q];
      const auto distance = point_distance(source, assignment.point);
      if (distance > 1e-9) {
        back_legs.push_back({distance, source, assignment.point});
        back_owners.push_back(q);
      }
      positions_t1[q] = assignment.point;
    }
    const std::set<std::int64_t> participants(problem_.participants.begin(),
                                               problem_.participants.end());
    for (std::size_t gate = 0; gate < gate_count; ++gate) {
      const auto& option =
          problem_.gate_domains[gate][result.decoded.option_indices[gate]];
      for (const auto& target : {option.target1, option.target2}) {
        for (std::size_t atom = 0; atom < positions_t1.size(); ++atom) {
          if (participants.count(static_cast<std::int64_t>(atom)) != 0U) continue;
          if (same_point(target, positions_t1[atom])) {
            result.fitness = infeasible_fitness(
                chromosome, "gate target occupied after RETURN decisions");
            if (config_.fitness_cache) fitness_cache_[chromosome] = result;
            fitness_ns_ += elapsed_ns(started);
            return result;
          }
        }
      }
    }
    std::vector<Leg> out_legs;
    std::vector<std::int64_t> out_owners;
    for (std::size_t gate = 0; gate < gate_count; ++gate) {
      const auto& option =
          problem_.gate_domains[gate][result.decoded.option_indices[gate]];
      for (const auto& [q, target] :
           {std::pair<std::int64_t, Point>{option.q1, option.target1},
            std::pair<std::int64_t, Point>{option.q2, option.target2}}) {
        const auto& source = positions_t1[q];
        const auto distance = point_distance(source, target);
        if (distance > 1e-9) {
          out_legs.push_back({distance, source, target});
          out_owners.push_back(q);
        }
      }
    }
    std::vector<Ghost> ghosts_t0;
    std::vector<Ghost> ghosts_t1;
    for (std::size_t atom = 0; atom < problem_.n_atoms; ++atom) {
      ghosts_t0.push_back({static_cast<std::int64_t>(atom),
                           problem_.current_points[atom]});
      ghosts_t1.push_back({static_cast<std::int64_t>(atom), positions_t1[atom]});
    }
    CandidatePlan candidate;
    candidate.chromosome = chromosome;
    candidate.idle_exposures = static_cast<std::int64_t>(
        problem_.eligible.size() - returners.size());
    candidate.phases = {
        {std::move(back_legs), ghosts_t0, std::move(back_owners), "phase"},
        {std::move(out_legs), ghosts_t1, std::move(out_owners), "phase"},
    };
    BoundaryConfig boundary_config;
    boundary_config.exact_coloring_threshold = config_.exact_coloring_threshold;
    boundary_config.enforce_single_leg_ghost = config_.enforce_single_leg_ghost;
    result.fitness = evaluate_candidate(architecture_, candidate, boundary_config);
    if (result.fitness.feasible) apply_forecast(result, chromosome);
    if (config_.fitness_cache) fitness_cache_[chromosome] = result;
    fitness_ns_ += elapsed_ns(started);
    return result;
  }

  bool stochastic_can_score(const std::vector<std::int64_t>& raw) {
    const auto chromosome = normalize(raw);
    return evaluated_keys_.count(chromosome) != 0U ||
           stats_.stochastic_unique_evaluations < stats_.stochastic_budget;
  }

  std::vector<Evaluated> score_unique(
      const std::vector<std::vector<std::int64_t>>& raw_values,
      bool stochastic) {
    std::vector<std::vector<std::int64_t>> unique;
    std::set<std::vector<std::int64_t>> seen;
    for (const auto& raw : raw_values) {
      auto chromosome = normalize(raw);
      if (seen.insert(chromosome).second) unique.push_back(std::move(chromosome));
    }
    std::vector<Evaluated> result;
    for (const auto& chromosome : unique) {
      if (stochastic && !stochastic_can_score(chromosome)) continue;
      result.push_back(evaluate(chromosome, stochastic));
    }
    std::sort(result.begin(), result.end(), [](const auto& first,
                                               const auto& second) {
      return evaluated_less(first, second);
    });
    return result;
  }

  std::size_t direct_search_space() const {
    std::size_t value = 1;
    for (const auto& domain : problem_.gate_domains) {
      if (value > 64 / domain.size()) return 65;
      value *= domain.size();
    }
    if (problem_.decision_policy == RichDecisionPolicy::kOptimize) {
      for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
        if (value > 32) return 65;
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
    auto best = evaluate(greedy, false);
    for (std::size_t index = 0; index < problem_.eligible.size(); ++index) {
      auto trial = greedy;
      trial[gate_count + index] ^= 1;
      trial = normalize(trial);
      const auto value = evaluate(trial, false);
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
          const auto score = evaluate(trial, false);
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
        const auto score = evaluate(trial, false);
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
    while (raw.size() < config_.population_size * 3) {
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
    std::set<std::vector<std::int64_t>> seen;
    for (const auto& chromosome : raw) {
      auto value = normalize(chromosome);
      if (!seen.insert(value).second) continue;
      population.push_back(std::move(value));
      if (population.size() == config_.population_size) break;
    }
    while (population.size() < config_.population_size) {
      population.push_back(population.empty()
                               ? std::vector<std::int64_t>(gate_count, 0)
                               : population.back());
    }
    return population;
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
    if (gate_count < 2) return separated_mutation(chromosome);
    std::size_t first = gate_count;
    std::size_t second = gate_count;
    for (std::size_t left = 0; left < gate_count && first == gate_count; ++left) {
      const auto left_option = positive_mod(
          chromosome[left], problem_.gate_domains[left].size());
      const auto left_site = problem_.gate_domains[left][left_option].site_id;
      for (std::size_t right = left + 1; right < gate_count; ++right) {
        const auto right_option = positive_mod(
            chromosome[right], problem_.gate_domains[right].size());
        if (problem_.gate_domains[right][right_option].site_id == left_site) {
          first = left;
          second = right;
          break;
        }
      }
    }
    if (first == gate_count) {
      first = rng_.randbelow(gate_count);
      second = rng_.randbelow(gate_count - 1);
      if (second >= first) ++second;
    }
    auto result = chromosome;
    std::swap(result[first], result[second]);
    ++stats_.conflict_cluster_swaps;
    return normalize(result);
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
  PythonRandom rng_;
  RichSearchStats stats_;
  std::map<std::vector<std::int64_t>, std::vector<std::int64_t>> normalize_cache_;
  std::map<std::vector<std::int64_t>, DecodeResult> decode_cache_;
  std::map<std::vector<std::size_t>, std::vector<ReturnAssignment>> return_cache_;
  std::map<std::vector<std::int64_t>, Evaluated> fitness_cache_;
  std::set<std::vector<std::int64_t>> evaluated_keys_;
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
