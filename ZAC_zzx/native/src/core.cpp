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

namespace {
struct ReplayBatches {
  bool feasible{true};
  std::vector<std::vector<std::size_t>> batches;
};

struct PhaseEvaluation {
  bool feasible{true};
  std::size_t batch_count{};
  double phase_time{};
  std::vector<std::vector<std::size_t>> batches;
};

double single_batch_time(const Leg& leg) {
  return 2.0 * kTransferUs +
         std::sqrt(point_distance(leg.source, leg.target) / kAccelUmPerUs2);
}

constexpr std::size_t kFastPhaseLegs = 64;
constexpr std::size_t kFastPhasePositions = 512;

std::size_t mask_members(
    const MovementPhase& phase, std::uint64_t mask,
    std::array<std::size_t, kFastPhaseLegs>& members) {
  std::size_t count = 0;
  for (std::size_t index = 0; index < phase.legs.size(); ++index) {
    if ((mask & (std::uint64_t{1} << index)) != 0U) members[count++] = index;
  }
  // Every source batch is stable-sorted by descending distance before replay.
  for (std::size_t index = 1; index < count; ++index) {
    const auto value = members[index];
    std::size_t insert = index;
    while (insert > 0 &&
           phase.legs[members[insert - 1]].distance_um <
               phase.legs[value].distance_um) {
      members[insert] = members[insert - 1];
      --insert;
    }
    members[insert] = value;
  }
  return count;
}

double mask_longest(const MovementPhase& phase, std::uint64_t mask) {
  double value = 0.0;
  for (std::size_t index = 0; index < phase.legs.size(); ++index) {
    if ((mask & (std::uint64_t{1} << index)) != 0U) {
      value = std::max(value, phase.legs[index].distance_um);
    }
  }
  return value;
}

double expanded_batch_time_mask(const MovementPhase& phase,
                                std::uint64_t mask) {
  std::array<std::size_t, kFastPhaseLegs> members{};
  const auto member_count = mask_members(phase, mask, members);
  if (member_count == 0) return 0.0;
  if (member_count == 1) return single_batch_time(phase.legs[members[0]]);

  std::array<double, kFastPhaseLegs> row_keys{};
  std::size_t row_count = 0;
  for (std::size_t offset = 0; offset < member_count; ++offset) {
    const auto key = phase.legs[members[offset]].source.y;
    bool seen = false;
    for (std::size_t row = 0; row < row_count; ++row) {
      if (row_keys[row] == key) {
        seen = true;
        break;
      }
    }
    if (!seen) row_keys[row_count++] = key;
  }
  std::sort(row_keys.begin(), row_keys.begin() + row_count);
  double duration = static_cast<double>(row_count + 1) * kTransferUs;
  if (row_count > 1) {
    duration += static_cast<double>(row_count - 1) *
                std::sqrt(std::sqrt(2.0) / kAccelUmPerUs2);
  }

  std::array<std::pair<double, double>, kFastPhaseLegs> row_moves{};
  std::array<double, kFastPhaseLegs> source_x{};
  std::array<double, kFastPhaseLegs> target_x{};
  std::array<std::size_t, kFastPhaseLegs> last_row{};
  std::size_t column_count = 0;
  for (std::size_t row = 0; row < row_count; ++row) {
    const auto source_y = row_keys[row];
    const Leg* first = nullptr;
    for (std::size_t offset = 0; offset < member_count; ++offset) {
      const auto& leg = phase.legs[members[offset]];
      if (leg.source.y != source_y) continue;
      if (first == nullptr) first = &leg;
      std::size_t column = column_count;
      for (std::size_t existing = 0; existing < column_count; ++existing) {
        if (source_x[existing] == leg.source.x) {
          column = existing;
          break;
        }
      }
      if (column == column_count) {
        source_x[column_count] = leg.source.x;
        target_x[column_count] = leg.target.x;
        ++column_count;
      }
      last_row[column] = row;
    }
    row_moves[row] = {
        source_y + (row + 1 < row_count ? 1.0 : 0.0), first->target.y};
  }

  std::array<std::size_t, kFastPhaseLegs> column_order{};
  for (std::size_t column = 0; column < column_count; ++column) {
    column_order[column] = column;
  }
  std::stable_sort(
      column_order.begin(), column_order.begin() + column_count,
      [&](auto left, auto right) { return source_x[left] < source_x[right]; });
  double longest = 0.0;
  for (std::size_t row = 0; row < row_count; ++row) {
    for (std::size_t ordered = 0; ordered < column_count; ++ordered) {
      const auto column = column_order[ordered];
      const auto column_begin =
          source_x[column] +
          (last_row[column] + 1 < row_count ? 1.0 : 0.0);
      longest = std::max(
          longest,
          std::hypot(target_x[column] - column_begin,
                     row_moves[row].second - row_moves[row].first));
    }
  }
  return duration + std::sqrt(longest / kAccelUmPerUs2);
}

bool mask_ghost_hit(
    const MovementPhase& phase, std::uint64_t mask,
    const std::int64_t* atoms, const Point* positions,
    std::size_t position_count) {
  if (mask == 0U || position_count == 0) return false;
  std::array<std::pair<double, double>, kFastPhaseLegs> columns{};
  std::array<std::pair<double, double>, kFastPhaseLegs> rows{};
  std::size_t column_count = 0;
  std::size_t row_count = 0;
  double x_min = std::numeric_limits<double>::infinity();
  double x_max = -std::numeric_limits<double>::infinity();
  double y_min = std::numeric_limits<double>::infinity();
  double y_max = -std::numeric_limits<double>::infinity();
  const auto append_unique = [](auto& values, std::size_t& size,
                                const auto& value) {
    for (std::size_t index = 0; index < size; ++index) {
      if (values[index] == value) return;
    }
    values[size++] = value;
  };
  for (std::size_t index = 0; index < phase.legs.size(); ++index) {
    if ((mask & (std::uint64_t{1} << index)) == 0U) continue;
    const auto& leg = phase.legs[index];
    append_unique(columns, column_count,
                  std::pair<double, double>{leg.source.x, leg.target.x});
    append_unique(rows, row_count,
                  std::pair<double, double>{leg.source.y, leg.target.y});
    x_min = std::min({x_min, leg.source.x, leg.target.x});
    x_max = std::max({x_max, leg.source.x, leg.target.x});
    y_min = std::min({y_min, leg.source.y, leg.target.y});
    y_max = std::max({y_max, leg.source.y, leg.target.y});
  }
  for (std::size_t ghost = 0; ghost < position_count; ++ghost) {
    bool moving = false;
    if (!phase.owners.empty()) {
      for (std::size_t index = 0; index < phase.legs.size(); ++index) {
        if ((mask & (std::uint64_t{1} << index)) != 0U &&
            phase.owners[index] == atoms[ghost]) {
          moving = true;
          break;
        }
      }
    }
    if (moving) continue;
    const auto& point = positions[ghost];
    if (point.x < x_min - kEps || point.x > x_max + kEps ||
        point.y < y_min - kEps || point.y > y_max + kEps) {
      continue;
    }
    for (std::size_t column = 0; column < column_count; ++column) {
      const auto x = cover(columns[column].first, columns[column].second,
                           point.x);
      if (!x.valid) continue;
      for (std::size_t row = 0; row < row_count; ++row) {
        const auto y = cover(rows[row].first, rows[row].second, point.y);
        if (!y.valid) continue;
        if (x.always || y.always || std::abs(x.time - y.time) < kSTolerance) {
          return true;
        }
      }
    }
  }
  return false;
}

PhaseEvaluation evaluate_fast_phase(const MovementPhase& phase,
                                    bool enforce_single_leg_ghost,
                                    bool record_batches) {
  PhaseEvaluation result;
  const auto size = phase.legs.size();
  if (size == 0) return result;
  if (phase.batching != "phase" && phase.batching != "greedy") {
    throw std::invalid_argument("unknown batching policy: " + phase.batching);
  }

  std::array<std::uint64_t, kFastPhaseLegs> adjacency{};
  std::array<std::int64_t, kFastPhasePositions> raw_atoms{};
  std::array<Point, kFastPhasePositions> raw_points{};
  const auto raw_count = phase.ghosts.size();
  for (std::size_t ghost = 0; ghost < raw_count; ++ghost) {
    raw_atoms[ghost] = phase.ghosts[ghost].atom;
    raw_points[ghost] = phase.ghosts[ghost].position;
  }
  for (std::size_t first = 0; first < size; ++first) {
    const auto& a = phase.legs[first];
    const std::array<double, 4> av{
        a.source.x, a.target.x, a.source.y, a.target.y};
    for (std::size_t second = first + 1; second < size; ++second) {
      const auto& b = phase.legs[second];
      const std::array<double, 4> bv{
          b.source.x, b.target.x, b.source.y, b.target.y};
      const auto pair_mask = (std::uint64_t{1} << first) |
                             (std::uint64_t{1} << second);
      const bool conflict =
          !compatible_2d(av, bv) ||
          (raw_count != 0 &&
           mask_ghost_hit(
               phase, pair_mask, raw_atoms.data(), raw_points.data(),
               raw_count));
      if (conflict) {
        adjacency[first] |= std::uint64_t{1} << second;
        adjacency[second] |= std::uint64_t{1} << first;
      }
    }
  }

  std::array<std::uint64_t, kFastPhaseLegs> batches{};
  std::size_t batch_count = 0;
  if (phase.batching == "greedy") {
    std::array<std::size_t, kFastPhaseLegs> priority{};
    for (std::size_t index = 0; index < size; ++index) priority[index] = index;
    std::stable_sort(priority.begin(), priority.begin() + size,
                     [&](auto left, auto right) {
                       return phase.legs[left].distance_um >
                              phase.legs[right].distance_um;
                     });
    const auto all = (size == 64 ? ~std::uint64_t{0}
                                 : (std::uint64_t{1} << size) - 1U);
    auto remaining = all;
    while (remaining != 0U) {
      std::uint64_t selected = 0;
      for (std::size_t offset = 0; offset < size; ++offset) {
        const auto index = priority[offset];
        const auto bit = std::uint64_t{1} << index;
        if ((remaining & bit) == 0U ||
            (adjacency[index] & selected) != 0U) {
          continue;
        }
        selected |= bit;
      }
      batches[batch_count++] = selected;
      remaining &= ~selected;
    }
  } else {
    std::array<int, kFastPhaseLegs> colors{};
    colors.fill(-1);
    std::array<std::uint64_t, kFastPhaseLegs> neighbor_colors{};
    for (std::size_t step = 0; step < size; ++step) {
      std::size_t best = size;
      std::tuple<int, int, double> best_key{};
      bool have_best = false;
      for (std::size_t vertex = 0; vertex < size; ++vertex) {
        if (colors[vertex] >= 0) continue;
        const auto key = std::make_tuple(
            __builtin_popcountll(neighbor_colors[vertex]),
            __builtin_popcountll(adjacency[vertex]),
            phase.legs[vertex].distance_um);
        if (!have_best || key > best_key) {
          best = vertex;
          best_key = key;
          have_best = true;
        }
      }
      int color = 0;
      while ((neighbor_colors[best] &
              (std::uint64_t{1} << static_cast<unsigned>(color))) != 0U) {
        ++color;
      }
      colors[best] = color;
      auto neighbors = adjacency[best];
      while (neighbors != 0U) {
        const auto neighbor = static_cast<std::size_t>(
            __builtin_ctzll(neighbors));
        neighbor_colors[neighbor] |=
            std::uint64_t{1} << static_cast<unsigned>(color);
        neighbors &= neighbors - 1U;
      }
    }
    std::array<int, kFastPhaseLegs> color_order{};
    std::size_t color_count = 0;
    for (std::size_t vertex = 0; vertex < size; ++vertex) {
      const auto color = colors[vertex];
      std::size_t slot = color_count;
      for (std::size_t index = 0; index < color_count; ++index) {
        if (color_order[index] == color) {
          slot = index;
          break;
        }
      }
      if (slot == color_count) {
        color_order[color_count] = color;
        batches[color_count++] = 0U;
      }
      batches[slot] |= std::uint64_t{1} << vertex;
    }
    batch_count = color_count;
    for (std::size_t index = 1; index < batch_count; ++index) {
      const auto value = batches[index];
      const auto longest = mask_longest(phase, value);
      std::size_t insert = index;
      while (insert > 0 &&
             mask_longest(phase, batches[insert - 1]) < longest) {
        batches[insert] = batches[insert - 1];
        --insert;
      }
      batches[insert] = value;
    }
  }

  if (!enforce_single_leg_ghost) {
    result.batch_count = batch_count;
    for (std::size_t batch = 0; batch < batch_count; ++batch) {
      result.phase_time += expanded_batch_time_mask(phase, batches[batch]);
      if (record_batches) {
        std::array<std::size_t, kFastPhaseLegs> members{};
        const auto count = mask_members(phase, batches[batch], members);
        result.batches.emplace_back(members.begin(), members.begin() + count);
      }
    }
    return result;
  }

  std::array<std::int64_t, kFastPhaseLegs> owner_atoms{};
  std::array<std::size_t, kFastPhaseLegs> owner_legs{};
  std::size_t owner_count = 0;
  for (std::size_t leg = 0; leg < phase.owners.size(); ++leg) {
    for (std::size_t index = 0; index < owner_count; ++index) {
      if (owner_atoms[index] == phase.owners[leg]) {
        throw std::invalid_argument("phase owner appears in multiple legs");
      }
    }
    owner_atoms[owner_count] = phase.owners[leg];
    owner_legs[owner_count] = leg;
    ++owner_count;
  }
  std::array<std::int64_t, kFastPhasePositions> static_atoms{};
  std::array<Point, kFastPhasePositions> static_points{};
  std::size_t static_count = 0;
  for (std::size_t ghost = 0; ghost < raw_count; ++ghost) {
    bool moving = false;
    for (std::size_t owner = 0; owner < owner_count; ++owner) {
      if (owner_atoms[owner] == raw_atoms[ghost]) {
        moving = true;
        break;
      }
    }
    if (moving) continue;
    static_atoms[static_count] = raw_atoms[ghost];
    static_points[static_count] = raw_points[ghost];
    ++static_count;
  }

  while (true) {
    std::array<std::size_t, kFastPhaseLegs> owner_batches{};
    for (std::size_t owner = 0; owner < owner_count; ++owner) {
      const auto leg_bit = std::uint64_t{1} << owner_legs[owner];
      for (std::size_t batch = 0; batch < batch_count; ++batch) {
        if ((batches[batch] & leg_bit) != 0U) {
          owner_batches[owner] = batch;
          break;
        }
      }
    }
    std::array<std::uint64_t, kFastPhaseLegs> outgoing{};
    std::uint64_t blocked = 0U;
    for (std::size_t batch = 0; batch < batch_count; ++batch) {
      const auto mask = batches[batch];
      if (static_count != 0U &&
          mask_ghost_hit(phase, mask, static_atoms.data(),
                         static_points.data(), static_count)) {
        blocked |= std::uint64_t{1} << batch;
        continue;
      }
      for (std::size_t owner = 0; owner < owner_count; ++owner) {
        const auto other = owner_batches[owner];
        if (other == batch) continue;
        const auto leg = owner_legs[owner];
        const std::array<std::int64_t, 1> atom{owner_atoms[owner]};
        const std::array<Point, 1> source{phase.legs[leg].source};
        const std::array<Point, 1> target{phase.legs[leg].target};
        if (mask_ghost_hit(
                phase, mask, atom.data(), source.data(), 1U)) {
          outgoing[other] |= std::uint64_t{1} << batch;
        }
        if (mask_ghost_hit(
                phase, mask, atom.data(), target.data(), 1U)) {
          outgoing[batch] |= std::uint64_t{1} << other;
        }
      }
    }
    std::array<std::size_t, kFastPhaseLegs> indegree{};
    for (std::size_t batch = 0; batch < batch_count; ++batch) {
      auto neighbors = outgoing[batch];
      while (neighbors != 0U) {
        const auto neighbor = static_cast<std::size_t>(
            __builtin_ctzll(neighbors));
        ++indegree[neighbor];
        neighbors &= neighbors - 1U;
      }
    }
    std::array<std::size_t, kFastPhaseLegs> order{};
    std::size_t order_count = 0;
    std::uint64_t remaining =
        (batch_count == 64 ? ~std::uint64_t{0}
                           : (std::uint64_t{1} << batch_count) - 1U);
    while (remaining != 0U) {
      std::size_t ready = batch_count;
      for (std::size_t batch = 0; batch < batch_count; ++batch) {
        const auto bit = std::uint64_t{1} << batch;
        if ((remaining & bit) != 0U && (blocked & bit) == 0U &&
            indegree[batch] == 0U) {
          ready = batch;
          break;
        }
      }
      if (ready == batch_count) break;
      order[order_count++] = ready;
      remaining &= ~(std::uint64_t{1} << ready);
      auto neighbors = outgoing[ready];
      while (neighbors != 0U) {
        const auto neighbor = static_cast<std::size_t>(
            __builtin_ctzll(neighbors));
        --indegree[neighbor];
        neighbors &= neighbors - 1U;
      }
    }
    if (order_count == batch_count) {
      result.batch_count = batch_count;
      for (std::size_t offset = 0; offset < order_count; ++offset) {
        const auto mask = batches[order[offset]];
        result.phase_time += expanded_batch_time_mask(phase, mask);
        if (record_batches) {
          std::array<std::size_t, kFastPhaseLegs> members{};
          const auto count = mask_members(phase, mask, members);
          result.batches.emplace_back(
              members.begin(), members.begin() + count);
        }
      }
      return result;
    }

    std::size_t split = batch_count;
    for (std::size_t batch = 0; batch < batch_count; ++batch) {
      if (__builtin_popcountll(batches[batch]) > 1) {
        split = batch;
        break;
      }
    }
    if (split == batch_count) {
      result.feasible = false;
      return result;
    }
    std::array<std::size_t, kFastPhaseLegs> members{};
    const auto member_count = mask_members(phase, batches[split], members);
    const auto added = member_count - 1;
    for (std::size_t index = batch_count; index-- > split + 1;) {
      batches[index + added] = batches[index];
    }
    for (std::size_t index = 0; index < member_count; ++index) {
      batches[split + index] = std::uint64_t{1} << members[index];
    }
    batch_count += added;
  }
}

ReplayBatches replay_phase_batches(const MovementPhase& phase,
                                   std::size_t exact_threshold) {
  auto pending = color_phase(phase, exact_threshold);
  const auto ordered = [&](const auto& batches)
      -> std::optional<std::vector<std::size_t>> {
    std::map<std::int64_t, std::size_t> batch_by_owner;
    if (!phase.owners.empty()) {
      for (std::size_t batch = 0; batch < batches.size(); ++batch) {
        for (const auto leg : batches[batch]) {
          const auto [it, inserted] =
              batch_by_owner.emplace(phase.owners[leg], batch);
          (void)it;
          if (!inserted) {
            throw std::invalid_argument(
                "phase owner appears in multiple legs");
          }
        }
      }
    }
    std::vector<Ghost> static_ghosts;
    static_ghosts.reserve(phase.ghosts.size());
    for (const auto& ghost : phase.ghosts) {
      if (batch_by_owner.count(ghost.atom) == 0U) {
        static_ghosts.push_back(ghost);
      }
    }
    std::vector<std::set<std::size_t>> outgoing(batches.size());
    std::vector<bool> blocked(batches.size(), false);
    for (std::size_t batch = 0; batch < batches.size(); ++batch) {
      std::vector<Leg> legs;
      legs.reserve(batches[batch].size());
      for (const auto index : batches[batch]) {
        legs.push_back(phase.legs[index]);
      }
      if (!ghost_hit_atoms(legs, static_ghosts).empty()) {
        blocked[batch] = true;
        continue;
      }
      for (std::size_t index = 0; index < phase.owners.size(); ++index) {
        const auto owner = phase.owners[index];
        const auto other = batch_by_owner.at(owner);
        if (other == batch) continue;
        if (!ghost_hit_atoms(
                 legs, std::vector<Ghost>{{owner, phase.legs[index].source}})
                 .empty()) {
          outgoing[other].insert(batch);
        }
        if (!ghost_hit_atoms(
                 legs, std::vector<Ghost>{{owner, phase.legs[index].target}})
                 .empty()) {
          outgoing[batch].insert(other);
        }
      }
    }
    std::vector<std::size_t> indegree(batches.size(), 0U);
    for (const auto& neighbors : outgoing) {
      for (const auto neighbor : neighbors) ++indegree[neighbor];
    }
    std::set<std::size_t> remaining;
    for (std::size_t index = 0; index < batches.size(); ++index) {
      remaining.insert(index);
    }
    std::vector<std::size_t> result;
    result.reserve(batches.size());
    while (!remaining.empty()) {
      const auto found = std::find_if(
          remaining.begin(), remaining.end(), [&](const auto batch) {
            return !blocked[batch] && indegree[batch] == 0U;
          });
      if (found == remaining.end()) return std::nullopt;
      const auto batch = *found;
      result.push_back(batch);
      remaining.erase(found);
      for (const auto neighbor : outgoing[batch]) --indegree[neighbor];
    }
    return result;
  };

  while (true) {
    if (const auto order = ordered(pending); order.has_value()) {
      ReplayBatches result;
      result.batches.reserve(order->size());
      for (const auto batch : *order) result.batches.push_back(pending[batch]);
      return result;
    }
    const auto found = std::find_if(
        pending.begin(), pending.end(),
        [](const auto& members) { return members.size() > 1; });
    if (found == pending.end()) {
      ReplayBatches result;
      result.feasible = false;
      return result;
    }
    const auto split_index = static_cast<std::size_t>(
        std::distance(pending.begin(), found));
    auto members = *found;
    pending.erase(found);
    std::vector<std::vector<std::size_t>> singles;
    singles.reserve(members.size());
    for (const auto index : members) singles.push_back({index});
    pending.insert(
        pending.begin() + static_cast<std::ptrdiff_t>(split_index),
        singles.begin(), singles.end());
  }
}

PhaseEvaluation evaluate_phase(const MovementPhase& phase,
                               std::size_t exact_threshold,
                               bool enforce_single_leg_ghost,
                               bool record_batches) {
  if (exact_threshold == 0 && phase.legs.size() <= kFastPhaseLegs &&
      phase.ghosts.size() + phase.owners.size() <= kFastPhasePositions) {
    return evaluate_fast_phase(
        phase, enforce_single_leg_ghost, record_batches);
  }
  auto replay = (enforce_single_leg_ghost
                     ? replay_phase_batches(phase, exact_threshold)
                     : ReplayBatches{true, color_phase(phase, exact_threshold)});
  PhaseEvaluation result;
  result.feasible = replay.feasible;
  if (!result.feasible) return result;
  result.batch_count = replay.batches.size();
  for (const auto& batch : replay.batches) {
    result.phase_time += expanded_batch_time(phase.legs, batch);
  }
  if (record_batches) result.batches = std::move(replay.batches);
  return result;
}
}  // namespace

namespace {
FitnessResult evaluate_candidate_impl(
    const ArchitectureSnapshot& architecture, const CandidatePlan& candidate,
    const BoundaryConfig& config, bool record_batches) {
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
    if (phase.legs.size() > architecture.n_atoms()) {
      return infeasible(candidate, "phase has more movers than atoms");
    }
    auto evaluated = evaluate_phase(
        phase, config.exact_coloring_threshold,
        config.enforce_single_leg_ghost, record_batches);
    if (!evaluated.feasible) {
      return infeasible(
          candidate, "phase " + std::to_string(phase_index) +
                         " has no ghost-safe straight-leg batch order");
    }
    const auto phase_time = evaluated.phase_time;
    const auto phase_movers = phase.legs.size();
    const auto mover_idle = std::max(0.0, phase_time - 2.0 * kTransferUs);
    if (phase_time >= kT2Us || mover_idle >= kT2Us) {
      result.feasible = false;
      result.error = "linear coherence model out of domain";
      result.negative_log_fidelity = std::numeric_limits<double>::infinity();
      result.transfer_nll = result.negative_log_fidelity;
      result.idle_excitation_nll = result.negative_log_fidelity;
      result.coherence_nll = result.negative_log_fidelity;
      result.move_batches += evaluated.batch_count;
      result.move_time_us += phase_time;
      for (const auto& leg : phase.legs) {
        result.total_distance_um += leg.distance_um;
      }
      result.transfers = 2 * (movers + phase_movers);
      if (record_batches) {
        result.phase_batches.push_back(std::move(evaluated.batches));
      }
      return result;
    }
    result.coherence_nll -=
        static_cast<double>(architecture.n_atoms() - phase_movers) *
        std::log1p(-phase_time / kT2Us);
    result.coherence_nll -= static_cast<double>(phase_movers) *
                            std::log1p(-mover_idle / kT2Us);
    result.move_batches += evaluated.batch_count;
    result.move_time_us += phase_time;
    for (const auto& leg : phase.legs) {
      result.total_distance_um += leg.distance_um;
    }
    movers += phase_movers;
    if (record_batches) {
      result.phase_batches.push_back(std::move(evaluated.batches));
    }
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
}  // namespace

std::vector<std::vector<std::size_t>> replay_phase_batches_strict(
    const MovementPhase& phase, std::size_t exact_threshold) {
  auto replay = replay_phase_batches(phase, exact_threshold);
  if (!replay.feasible) {
    throw std::runtime_error(
        "phase has no ghost-safe straight-leg batch order");
  }
  return replay.batches;
}

FitnessResult evaluate_candidate(const ArchitectureSnapshot& architecture,
                                 const CandidatePlan& candidate,
                                 const BoundaryConfig& config) {
  return evaluate_candidate_impl(architecture, candidate, config, true);
}

FitnessResult evaluate_candidate_summary(
    const ArchitectureSnapshot& architecture, const CandidatePlan& candidate,
    const BoundaryConfig& config) {
  return evaluate_candidate_impl(architecture, candidate, config, false);
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
