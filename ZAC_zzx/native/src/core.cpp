#include "zac_native/core.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <tuple>
#include <unordered_set>
#include <utility>

namespace zac_native {
namespace {

constexpr double kEps = 1e-9;
constexpr double kSTolerance = 1e-6;
constexpr double kFExc = 0.9975;
constexpr double kFTransfer = 0.999;
constexpr double kTransferUs = 15.0;
constexpr double kRydbergUs = 0.36;
constexpr double kAccelUmPerUs2 = 0.00275;
constexpr double kT2Us = 1.5e6;

double point_distance(const Point& first, const Point& second) {
  return std::hypot(first.x - second.x, first.y - second.y);
}

struct Coverage {
  bool valid{};
  bool always{};
  double time{};
};

Coverage cover(double begin, double end, double coordinate) {
  if (std::abs(end - begin) < kEps) {
    return {std::abs(coordinate - begin) < kEps,
            std::abs(coordinate - begin) < kEps, 0.0};
  }
  const double raw = (coordinate - begin) / (end - begin);
  if (raw < -kEps || raw > 1.0 + kEps) {
    return {};
  }
  return {true, false, std::clamp(raw, 0.0, 1.0)};
}

using Adjacency = std::vector<std::set<std::size_t>>;

Adjacency phase_adjacency(const MovementPhase& phase) {
  const auto size = phase.legs.size();
  Adjacency adjacency(size);
  for (std::size_t i = 0; i < size; ++i) {
    const auto& first = phase.legs[i];
    const std::array<double, 4> first_vector{
        first.source.x, first.target.x, first.source.y, first.target.y};
    for (std::size_t j = i + 1; j < size; ++j) {
      const auto& second = phase.legs[j];
      const std::array<double, 4> second_vector{
          second.source.x, second.target.x, second.source.y, second.target.y};
      bool conflict = !compatible_2d(first_vector, second_vector);
      if (!conflict && !phase.ghosts.empty()) {
        std::vector<Ghost> remaining;
        remaining.reserve(phase.ghosts.size());
        for (const auto& ghost : phase.ghosts) {
          if (!phase.owners.empty() &&
              (ghost.atom == phase.owners[i] || ghost.atom == phase.owners[j])) {
            continue;
          }
          remaining.push_back(ghost);
        }
        conflict = !ghost_hit_atoms({first, second}, remaining).empty();
      }
      if (conflict) {
        adjacency[i].insert(j);
        adjacency[j].insert(i);
      }
    }
  }
  return adjacency;
}

std::vector<int> dsatur_heuristic(const MovementPhase& phase,
                                  const Adjacency& adjacency) {
  const std::size_t size = phase.legs.size();
  std::vector<int> colors(size, -1);
  std::vector<std::set<int>> neighbor_colors(size);
  for (std::size_t step = 0; step < size; ++step) {
    std::size_t best = size;
    std::tuple<std::size_t, std::size_t, double> best_key{};
    bool have_best = false;
    for (std::size_t vertex = 0; vertex < size; ++vertex) {
      if (colors[vertex] >= 0) {
        continue;
      }
      const auto key = std::make_tuple(neighbor_colors[vertex].size(),
                                       adjacency[vertex].size(),
                                       phase.legs[vertex].distance_um);
      if (!have_best || key > best_key) {
        have_best = true;
        best = vertex;
        best_key = key;
      }
    }
    int color = 0;
    while (neighbor_colors[best].count(color) != 0U) {
      ++color;
    }
    colors[best] = color;
    for (const auto neighbor : adjacency[best]) {
      neighbor_colors[neighbor].insert(color);
    }
  }
  return colors;
}

void exact_color_recursive(const Adjacency& adjacency, std::vector<int>& colors,
                           std::vector<std::map<int, int>>& neighbor_counts,
                           int used, int& best, std::vector<int>& best_colors,
                           std::size_t& budget) {
  if (used >= best || budget == 0) {
    return;
  }
  --budget;
  const std::size_t size = colors.size();
  std::size_t vertex = size;
  std::pair<std::size_t, std::size_t> best_key{};
  bool have_best = false;
  for (std::size_t candidate = 0; candidate < size; ++candidate) {
    if (colors[candidate] >= 0) {
      continue;
    }
    const auto key = std::make_pair(neighbor_counts[candidate].size(),
                                    adjacency[candidate].size());
    if (!have_best || key > best_key) {
      have_best = true;
      best_key = key;
      vertex = candidate;
    }
  }
  if (vertex == size) {
    best = used;
    best_colors = colors;
    return;
  }
  for (int color = 0; color <= used && color < best; ++color) {
    if (neighbor_counts[vertex].count(color) != 0U) {
      continue;
    }
    colors[vertex] = color;
    for (const auto neighbor : adjacency[vertex]) {
      ++neighbor_counts[neighbor][color];
    }
    exact_color_recursive(adjacency, colors, neighbor_counts,
                          std::max(used, color + 1), best, best_colors, budget);
    for (const auto neighbor : adjacency[vertex]) {
      auto found = neighbor_counts[neighbor].find(color);
      if (--found->second == 0) {
        neighbor_counts[neighbor].erase(found);
      }
    }
    colors[vertex] = -1;
  }
}

std::vector<std::vector<std::size_t>> batches_from_colors(
    const MovementPhase& phase, const std::vector<int>& colors) {
  std::map<int, std::vector<std::size_t>> classes;
  std::vector<int> color_order;
  for (std::size_t vertex = 0; vertex < colors.size(); ++vertex) {
    if (classes.count(colors[vertex]) == 0U) {
      color_order.push_back(colors[vertex]);
    }
    classes[colors[vertex]].push_back(vertex);
  }
  std::vector<std::vector<std::size_t>> batches;
  for (const auto color : color_order) {
    auto& members = classes[color];
    std::stable_sort(members.begin(), members.end(), [&](auto first, auto second) {
      return phase.legs[first].distance_um > phase.legs[second].distance_um;
    });
    batches.push_back(std::move(members));
  }
  std::stable_sort(batches.begin(), batches.end(), [&](const auto& first,
                                                       const auto& second) {
    const auto longest = [&](const auto& members) {
      double value = 0.0;
      for (const auto member : members) {
        value = std::max(value, phase.legs[member].distance_um);
      }
      return value;
    };
    return longest(first) > longest(second);
  });
  return batches;
}

std::vector<std::vector<std::size_t>> greedy_batches(
    const MovementPhase& phase, const Adjacency& adjacency) {
  std::set<std::size_t> remaining;
  for (std::size_t index = 0; index < phase.legs.size(); ++index) {
    remaining.insert(index);
  }
  std::vector<std::size_t> priority(remaining.begin(), remaining.end());
  std::stable_sort(priority.begin(), priority.end(), [&](auto first, auto second) {
    return phase.legs[first].distance_um > phase.legs[second].distance_um;
  });
  std::vector<std::vector<std::size_t>> batches;
  while (!remaining.empty()) {
    std::vector<std::size_t> members;
    std::set<std::size_t> selected;
    for (const auto index : priority) {
      if (remaining.count(index) == 0U) {
        continue;
      }
      bool conflict = false;
      for (const auto other : selected) {
        if (adjacency[index].count(other) != 0U) {
          conflict = true;
          break;
        }
      }
      if (!conflict) {
        members.push_back(index);
        selected.insert(index);
      }
    }
    for (const auto member : members) {
      remaining.erase(member);
    }
    batches.push_back(std::move(members));
  }
  return batches;
}

double expanded_batch_time(const std::vector<Leg>& legs,
                           const std::vector<std::size_t>& members) {
  if (members.empty()) {
    return 0.0;
  }
  if (members.size() == 1) {
    const auto& leg = legs[members.front()];
    return 2.0 * kTransferUs +
           std::sqrt(point_distance(leg.source, leg.target) / kAccelUmPerUs2);
  }
  std::map<double, std::vector<const Leg*>> rows;
  for (const auto member : members) {
    rows[legs[member].source.y].push_back(&legs[member]);
  }
  const auto row_count = rows.size();
  double duration = static_cast<double>(row_count + 1) * kTransferUs;
  if (row_count > 1) {
    duration += static_cast<double>(row_count - 1) *
                std::sqrt(std::sqrt(2.0) / kAccelUmPerUs2);
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
  std::vector<std::pair<double, double>> column_moves;
  for (const auto& [source_x, last_row] : last_row_for_x) {
    column_moves.emplace_back(
        source_x + (last_row + 1 < row_count ? 1.0 : 0.0),
        target_x_for_source.at(source_x));
  }
  double longest = 0.0;
  for (const auto& [row_begin, row_end] : row_moves) {
    for (const auto& [column_begin, column_end] : column_moves) {
      longest = std::max(longest, std::hypot(column_end - column_begin,
                                             row_end - row_begin));
    }
  }
  return duration + std::sqrt(longest / kAccelUmPerUs2);
}

FitnessResult infeasible(const CandidatePlan& candidate, std::string error) {
  const auto infinity = std::numeric_limits<double>::infinity();
  FitnessResult result;
  result.chromosome = candidate.chromosome;
  result.feasible = false;
  result.negative_log_fidelity = infinity;
  result.transfer_nll = infinity;
  result.idle_excitation_nll = infinity;
  result.coherence_nll = infinity;
  result.idle_exposures = candidate.idle_exposures;
  result.error = std::move(error);
  return result;
}

}  // namespace

ArchitectureSnapshot::ArchitectureSnapshot(
    std::size_t n_atoms, std::vector<Point> site_coordinates,
    std::vector<std::int64_t> storage_site_ids)
    : n_atoms_(n_atoms), site_coordinates_(std::move(site_coordinates)),
      storage_site_ids_(std::move(storage_site_ids)) {
  if (n_atoms_ == 0) {
    throw std::invalid_argument("n_atoms must be positive");
  }
  for (const auto& point : site_coordinates_) {
    if (!std::isfinite(point.x) || !std::isfinite(point.y)) {
      throw std::invalid_argument("site coordinates must be finite");
    }
  }
  std::set<std::int64_t> seen_storage;
  for (const auto site_id : storage_site_ids_) {
    if (site_id < 0 || static_cast<std::size_t>(site_id) >= site_coordinates_.size()) {
      throw std::invalid_argument("storage site id is outside site coordinates");
    }
    if (!seen_storage.insert(site_id).second) {
      throw std::invalid_argument("storage site ids must be unique");
    }
  }
}

bool compatible_2d(const std::array<double, 4>& a,
                   const std::array<double, 4>& b) noexcept {
  if (a[0] == b[0] && a[1] != b[1]) return false;
  if (a[1] == b[1] && a[0] != b[0]) return false;
  if (a[0] < b[0] && a[1] >= b[1]) return false;
  if (a[0] > b[0] && a[1] <= b[1]) return false;
  if (a[2] == b[2] && a[3] != b[3]) return false;
  if (a[3] == b[3] && a[2] != b[2]) return false;
  if (a[2] < b[2] && a[3] >= b[3]) return false;
  if (a[2] > b[2] && a[3] <= b[3]) return false;
  return true;
}

std::vector<std::int64_t> ghost_hit_atoms(const std::vector<Leg>& legs,
                                          const std::vector<Ghost>& ghosts) {
  if (legs.empty() || ghosts.empty()) {
    return {};
  }
  std::set<std::pair<double, double>> columns;
  std::set<std::pair<double, double>> rows;
  double x_min = std::numeric_limits<double>::infinity();
  double x_max = -std::numeric_limits<double>::infinity();
  double y_min = std::numeric_limits<double>::infinity();
  double y_max = -std::numeric_limits<double>::infinity();
  for (const auto& leg : legs) {
    columns.emplace(leg.source.x, leg.target.x);
    rows.emplace(leg.source.y, leg.target.y);
    x_min = std::min({x_min, leg.source.x, leg.target.x});
    x_max = std::max({x_max, leg.source.x, leg.target.x});
    y_min = std::min({y_min, leg.source.y, leg.target.y});
    y_max = std::max({y_max, leg.source.y, leg.target.y});
  }
  std::vector<std::int64_t> hits;
  for (const auto& ghost : ghosts) {
    const auto gx = ghost.position.x;
    const auto gy = ghost.position.y;
    if (gx < x_min - kEps || gx > x_max + kEps ||
        gy < y_min - kEps || gy > y_max + kEps) {
      continue;
    }
    bool hit = false;
    for (const auto& column : columns) {
      const auto x_coverage = cover(column.first, column.second, gx);
      if (!x_coverage.valid) continue;
      for (const auto& row : rows) {
        const auto y_coverage = cover(row.first, row.second, gy);
        if (!y_coverage.valid) continue;
        if (x_coverage.always || y_coverage.always ||
            std::abs(x_coverage.time - y_coverage.time) < kSTolerance) {
          hit = true;
          break;
        }
      }
      if (hit) break;
    }
    if (hit) hits.push_back(ghost.atom);
  }
  return hits;
}

std::vector<std::vector<std::size_t>> color_phase(
    const MovementPhase& phase, std::size_t exact_threshold) {
  if (phase.legs.empty()) {
    return {};
  }
  const auto adjacency = phase_adjacency(phase);
  if (phase.batching == "greedy") {
    return greedy_batches(phase, adjacency);
  }
  if (phase.batching != "phase") {
    throw std::invalid_argument("unknown batching policy: " + phase.batching);
  }
  auto colors = dsatur_heuristic(phase, adjacency);
  const auto used = *std::max_element(colors.begin(), colors.end()) + 1;
  if (exact_threshold > 0 && phase.legs.size() <= exact_threshold && used > 1) {
    std::vector<int> trial(colors.size(), -1);
    std::vector<std::map<int, int>> neighbor_counts(colors.size());
    std::vector<int> exact;
    int best = used;
    std::size_t budget = 200000;
    exact_color_recursive(adjacency, trial, neighbor_counts, 0, best, exact, budget);
    if (!exact.empty()) colors = std::move(exact);
  }
  return batches_from_colors(phase, colors);
}

FitnessResult evaluate_candidate(const ArchitectureSnapshot& architecture,
                                 const CandidatePlan& candidate,
                                 const BoundaryConfig& config) {
  if (candidate.idle_exposures < 0) {
    throw std::invalid_argument("idle_exposures must be non-negative");
  }
  FitnessResult result;
  result.chromosome = candidate.chromosome;
  result.idle_exposures = candidate.idle_exposures;
  result.coherence_nll = -static_cast<double>(candidate.idle_exposures) *
                         std::log1p(-kRydbergUs / kT2Us);
  std::size_t movers = 0;
  for (std::size_t phase_index = 0; phase_index < candidate.phases.size();
       ++phase_index) {
    const auto& phase = candidate.phases[phase_index];
    if (!phase.owners.empty() && phase.owners.size() != phase.legs.size()) {
      throw std::invalid_argument("owners must be empty or match leg count");
    }
    for (const auto& leg : phase.legs) {
      if (!std::isfinite(leg.distance_um) || leg.distance_um < 0.0 ||
          !std::isfinite(leg.source.x) || !std::isfinite(leg.source.y) ||
          !std::isfinite(leg.target.x) || !std::isfinite(leg.target.y)) {
        throw std::invalid_argument(
            "leg values must be finite and distance non-negative");
      }
    }
    for (const auto& ghost : phase.ghosts) {
      if (!std::isfinite(ghost.position.x) ||
          !std::isfinite(ghost.position.y)) {
        throw std::invalid_argument("ghost coordinates must be finite");
      }
    }
    if (config.enforce_single_leg_ghost) {
      for (std::size_t index = 0; index < phase.legs.size(); ++index) {
        std::vector<Ghost> remaining;
        for (const auto& ghost : phase.ghosts) {
          if (!phase.owners.empty() && ghost.atom == phase.owners[index]) continue;
          remaining.push_back(ghost);
        }
        const auto hits = ghost_hit_atoms({phase.legs[index]}, remaining);
        if (!hits.empty()) {
          return infeasible(candidate, "phase " + std::to_string(phase_index) +
                                         " single-leg ghost hit");
        }
      }
    }
    if (phase.legs.size() > architecture.n_atoms()) {
      return infeasible(candidate, "phase has more movers than atoms");
    }
    auto batches = color_phase(phase, config.exact_coloring_threshold);
    double phase_time = 0.0;
    for (const auto& batch : batches) {
      phase_time += expanded_batch_time(phase.legs, batch);
    }
    const auto phase_movers = phase.legs.size();
    const auto mover_idle = std::max(0.0, phase_time - 2.0 * kTransferUs);
    if (phase_time >= kT2Us || mover_idle >= kT2Us) {
      result.feasible = false;
      result.error = "linear coherence model out of domain";
      result.negative_log_fidelity = std::numeric_limits<double>::infinity();
      result.transfer_nll = result.negative_log_fidelity;
      result.idle_excitation_nll = result.negative_log_fidelity;
      result.coherence_nll = result.negative_log_fidelity;
      result.move_batches += batches.size();
      result.move_time_us += phase_time;
      for (const auto& leg : phase.legs) {
        result.total_distance_um += leg.distance_um;
      }
      result.transfers = 2 * (movers + phase_movers);
      result.phase_batches.push_back(std::move(batches));
      return result;
    }
    result.coherence_nll -=
        static_cast<double>(architecture.n_atoms() - phase_movers) *
        std::log1p(-phase_time / kT2Us);
    result.coherence_nll -= static_cast<double>(phase_movers) *
                            std::log1p(-mover_idle / kT2Us);
    result.move_batches += batches.size();
    result.move_time_us += phase_time;
    for (const auto& leg : phase.legs) {
      result.total_distance_um += leg.distance_um;
    }
    movers += phase_movers;
    result.phase_batches.push_back(std::move(batches));
  }
  result.transfers = 2 * movers;
  result.transfer_nll = -static_cast<double>(result.transfers) *
                        std::log(kFTransfer);
  result.idle_excitation_nll = -static_cast<double>(candidate.idle_exposures) *
                               std::log(kFExc);
  result.negative_log_fidelity = result.transfer_nll +
                                 result.idle_excitation_nll +
                                 result.coherence_nll;
  return result;
}

bool objective_less(const FitnessResult& first,
                    const FitnessResult& second) noexcept {
  return std::tie(first.negative_log_fidelity, first.move_batches,
                  first.move_time_us, first.total_distance_um,
                  first.chromosome) <
         std::tie(second.negative_log_fidelity, second.move_batches,
                  second.move_time_us, second.total_distance_um,
                  second.chromosome);
}

}  // namespace zac_native
