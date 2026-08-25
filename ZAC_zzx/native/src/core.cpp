#include "zac_native/core.hpp"

#include <algorithm>
#include <bitset>
#include <chrono>
#include <cmath>
#include <deque>
#include <functional>
#include <limits>
#include <map>
#include <numeric>
#include <optional>
#include <queue>
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

// ``phase_adjacency`` asks the same yes/no question for every pair of legs.
// Routing that tiny query through ``ghost_hit_atoms`` used to allocate two
// vectors and four red-black-tree nodes per pair.  Dense 2Q layers have O(n^2)
// pairs, and candidate repair evaluates the layer several times, so allocator
// traffic dominated the resident search.  The two-leg specialization below is
// the exact Cartesian-product test used by ``ghost_hit_atoms`` (two columns by
// two rows), only without materialising the temporary sets.  Repeated tracks
// are harmless because this is a boolean query.
bool pair_hits_ghost(const Leg& first, const Leg& second,
                     const std::vector<Ghost>& ghosts) {
  if (ghosts.empty()) return false;
  const std::array<const Leg*, 2> legs{&first, &second};
  const double x_min = std::min(
      {first.source.x, first.target.x, second.source.x, second.target.x});
  const double x_max = std::max(
      {first.source.x, first.target.x, second.source.x, second.target.x});
  const double y_min = std::min(
      {first.source.y, first.target.y, second.source.y, second.target.y});
  const double y_max = std::max(
      {first.source.y, first.target.y, second.source.y, second.target.y});
  for (const auto& ghost : ghosts) {
    const auto gx = ghost.position.x;
    const auto gy = ghost.position.y;
    if (gx < x_min - kEps || gx > x_max + kEps ||
        gy < y_min - kEps || gy > y_max + kEps) {
      continue;
    }
    for (const auto* column : legs) {
      const auto x = cover(column->source.x, column->target.x, gx);
      if (!x.valid) continue;
      for (const auto* row : legs) {
        const auto y = cover(row->source.y, row->target.y, gy);
        if (!y.valid) continue;
        if (x.always || y.always ||
            std::abs(x.time - y.time) < kSTolerance) {
          return true;
        }
      }
    }
  }
  return false;
}

Adjacency phase_adjacency(const MovementPhase& phase) {
  const auto size = phase.legs.size();
  Adjacency adjacency(size);
  std::set<std::int64_t> moving_owners;
  if (!phase.owners.empty()) {
    moving_owners.insert(phase.owners.begin(), phase.owners.end());
  }
  std::vector<Ghost> static_ghosts;
  static_ghosts.reserve(phase.ghosts.size());
  for (const auto& ghost : phase.ghosts) {
    if (moving_owners.count(ghost.atom) == 0U) {
      static_ghosts.push_back(ghost);
    }
  }
  for (std::size_t i = 0; i < size; ++i) {
    const auto& first = phase.legs[i];
    const std::array<double, 4> first_vector{
        first.source.x, first.target.x, first.source.y, first.target.y};
    for (std::size_t j = i + 1; j < size; ++j) {
      const auto& second = phase.legs[j];
      const std::array<double, 4> second_vector{
          second.source.x, second.target.x, second.source.y, second.target.y};
      bool conflict = !compatible_2d(first_vector, second_vector);
      if (!conflict && !static_ghosts.empty()) {
        // A third owner in this same phase is not a stationary ghost: it may
        // join the batch or move in an earlier/later batch.  The exact replay
        // below derives those source/target precedence edges after coloring.
        // Only atoms that never move in the phase are valid pairwise ghost
        // edges here; treating every other mover as static over-colors dense
        // layers and can manufacture an otherwise avoidable dependency cycle.
        conflict = pair_hits_ghost(first, second, static_ghosts);
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

// QMAP154 can expose every atom in one movement phase.  Keep enough headroom
// for resident RETURN/PARK bookkeeping without falling back to tree adjacency
// at 129 legs; two 256x256-bit matrices consume only 16 KiB on the stack.
constexpr std::size_t kDenseColorCapacity = 256;

// Allocation-free adjacency/DSATUR for large current layers.  Exact coloring
// is attempted only at or below ``exact_threshold``; above that threshold the
// registered semantics are precisely the heuristic below.  The generic path's
// ``std::set`` representation is useful to the recursive exact colorer but is
// unnecessarily expensive for the 96-leg ising boundary, where every fitness
// evaluation rebuilds thousands of tiny tree nodes.
std::vector<std::vector<std::size_t>> color_phase_dense_heuristic(
    const MovementPhase& phase) {
  const auto size = phase.legs.size();
  std::array<std::bitset<kDenseColorCapacity>, kDenseColorCapacity>
      adjacency{};
  std::set<std::int64_t> moving_owners;
  if (!phase.owners.empty()) {
    moving_owners.insert(phase.owners.begin(), phase.owners.end());
  }
  std::vector<Ghost> static_ghosts;
  static_ghosts.reserve(phase.ghosts.size());
  for (const auto& ghost : phase.ghosts) {
    if (moving_owners.count(ghost.atom) == 0U) {
      static_ghosts.push_back(ghost);
    }
  }
  // A dense layer reuses each leg/ghost track in O(n) different pair tests.
  // Cache the one-dimensional coverage once instead of recomputing divisions
  // in every O(n^2) pair.  The later 2x2 Cartesian-product check is exactly
  // the column/row set semantics of ``ghost_hit_atoms``.
  const auto static_count = static_ghosts.size();
  std::vector<Coverage> x_coverage(size * static_count);
  std::vector<Coverage> y_coverage(size * static_count);
  std::vector<double> leg_x_min(size);
  std::vector<double> leg_x_max(size);
  std::vector<double> leg_y_min(size);
  std::vector<double> leg_y_max(size);
  for (std::size_t leg = 0; leg < size; ++leg) {
    leg_x_min[leg] = std::min(
        phase.legs[leg].source.x, phase.legs[leg].target.x);
    leg_x_max[leg] = std::max(
        phase.legs[leg].source.x, phase.legs[leg].target.x);
    leg_y_min[leg] = std::min(
        phase.legs[leg].source.y, phase.legs[leg].target.y);
    leg_y_max[leg] = std::max(
        phase.legs[leg].source.y, phase.legs[leg].target.y);
    for (std::size_t ghost = 0; ghost < static_count; ++ghost) {
      const auto offset = leg * static_count + ghost;
      x_coverage[offset] = cover(
          phase.legs[leg].source.x, phase.legs[leg].target.x,
          static_ghosts[ghost].position.x);
      y_coverage[offset] = cover(
          phase.legs[leg].source.y, phase.legs[leg].target.y,
          static_ghosts[ghost].position.y);
    }
  }
  const auto pair_static_conflict = [&](std::size_t first,
                                        std::size_t second) {
    const std::array<std::size_t, 2> legs{first, second};
    const auto x_min = std::min(leg_x_min[first], leg_x_min[second]);
    const auto x_max = std::max(leg_x_max[first], leg_x_max[second]);
    const auto y_min = std::min(leg_y_min[first], leg_y_min[second]);
    const auto y_max = std::max(leg_y_max[first], leg_y_max[second]);
    for (std::size_t ghost = 0; ghost < static_count; ++ghost) {
      const auto& point = static_ghosts[ghost].position;
      if (point.x < x_min - kEps || point.x > x_max + kEps ||
          point.y < y_min - kEps || point.y > y_max + kEps) {
        continue;
      }
      for (const auto column : legs) {
        const auto& x = x_coverage[column * static_count + ghost];
        if (!x.valid) continue;
        for (const auto row : legs) {
          const auto& y = y_coverage[row * static_count + ghost];
          if (!y.valid) continue;
          if (x.always || y.always ||
              std::abs(x.time - y.time) < kSTolerance) {
            return true;
          }
        }
      }
    }
    return false;
  };
  for (std::size_t first = 0; first < size; ++first) {
    const auto& a = phase.legs[first];
    const std::array<double, 4> av{
        a.source.x, a.target.x, a.source.y, a.target.y};
    for (std::size_t second = first + 1; second < size; ++second) {
      const auto& b = phase.legs[second];
      const std::array<double, 4> bv{
          b.source.x, b.target.x, b.source.y, b.target.y};
      if (!compatible_2d(av, bv) || pair_static_conflict(first, second)) {
        adjacency[first].set(second);
        adjacency[second].set(first);
      }
    }
  }

  if (phase.batching == "greedy") {
    std::vector<std::size_t> priority(size);
    std::iota(priority.begin(), priority.end(), 0U);
    std::stable_sort(priority.begin(), priority.end(),
                     [&](auto first, auto second) {
                       return phase.legs[first].distance_um >
                              phase.legs[second].distance_um;
                     });
    std::bitset<kDenseColorCapacity> remaining;
    for (std::size_t index = 0; index < size; ++index) remaining.set(index);
    std::vector<std::vector<std::size_t>> batches;
    while (remaining.any()) {
      std::bitset<kDenseColorCapacity> selected;
      std::vector<std::size_t> members;
      for (const auto index : priority) {
        if (!remaining.test(index) ||
            (adjacency[index] & selected).any()) {
          continue;
        }
        selected.set(index);
        members.push_back(index);
      }
      remaining &= ~selected;
      batches.push_back(std::move(members));
    }
    return batches;
  }
  if (phase.batching != "phase") {
    throw std::invalid_argument("unknown batching policy: " + phase.batching);
  }

  std::vector<int> colors(size, -1);
  std::array<std::bitset<kDenseColorCapacity>, kDenseColorCapacity>
      neighbor_colors{};
  for (std::size_t step = 0; step < size; ++step) {
    std::size_t best = size;
    std::tuple<std::size_t, std::size_t, double> best_key{};
    bool have_best = false;
    for (std::size_t vertex = 0; vertex < size; ++vertex) {
      if (colors[vertex] >= 0) continue;
      const auto key = std::make_tuple(
          neighbor_colors[vertex].count(), adjacency[vertex].count(),
          phase.legs[vertex].distance_um);
      if (!have_best || key > best_key) {
        have_best = true;
        best = vertex;
        best_key = key;
      }
    }
    std::size_t color = 0;
    while (neighbor_colors[best].test(color)) ++color;
    colors[best] = static_cast<int>(color);
    for (std::size_t neighbor = 0; neighbor < size; ++neighbor) {
      if (adjacency[best].test(neighbor)) {
        neighbor_colors[neighbor].set(color);
      }
    }
  }
  return batches_from_colors(phase, colors);
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

ExpandedBatchTiming expanded_batch_timing(
    const std::vector<Leg>& legs,
    const std::vector<std::size_t>& members) {
  if (members.empty()) return {};
  std::set<double> rows;
  for (const auto member : members) {
    if (member >= legs.size()) {
      throw std::invalid_argument("expanded batch member is outside leg table");
    }
    rows.insert(legs[member].source.y);
  }
  const auto row_count = rows.size();
  const auto parking_us =
      std::sqrt(std::sqrt(2.0) / kAccelUmPerUs2);
  ExpandedBatchTiming result;
  result.duration_us = expanded_batch_time(legs, members);
  result.activation_finish_offset_us =
      static_cast<double>(row_count) * kTransferUs +
      static_cast<double>(row_count - 1) * parking_us;
  result.deactivation_offset_us = result.duration_us - kTransferUs;
  return result;
}

ArchitectureSnapshot::ArchitectureSnapshot(
    std::size_t n_atoms, std::vector<Point> site_coordinates,
    std::vector<std::int64_t> storage_site_ids,
    std::vector<std::array<std::int64_t, 2>> entangling_site_pairs)
    : n_atoms_(n_atoms), site_coordinates_(std::move(site_coordinates)),
      storage_site_ids_(std::move(storage_site_ids)),
      entangling_site_pairs_(std::move(entangling_site_pairs)) {
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
  storage_site_ids_by_x_ = storage_site_ids_;
  std::sort(storage_site_ids_by_x_.begin(), storage_site_ids_by_x_.end(),
            [&](const auto first, const auto second) {
              const auto& first_point =
                  site_coordinates_[static_cast<std::size_t>(first)];
              const auto& second_point =
                  site_coordinates_[static_cast<std::size_t>(second)];
              return std::tie(first_point.x, first_point.y, first) <
                     std::tie(second_point.x, second_point.y, second);
            });
  std::set<std::array<std::int64_t, 2>> seen_pairs;
  std::set<std::int64_t> storage(storage_site_ids_.begin(),
                                 storage_site_ids_.end());
  for (const auto& pair : entangling_site_pairs_) {
    if (pair[0] < 0 || pair[1] < 0 || pair[0] == pair[1] ||
        static_cast<std::size_t>(pair[0]) >= site_coordinates_.size() ||
        static_cast<std::size_t>(pair[1]) >= site_coordinates_.size()) {
      throw std::invalid_argument(
          "entangling site pair is outside site coordinates");
    }
    if (storage.count(pair[0]) != 0U || storage.count(pair[1]) != 0U) {
      throw std::invalid_argument(
          "entangling site pair cannot contain a storage site");
    }
    if (!seen_pairs.insert(pair).second) {
      throw std::invalid_argument("entangling site pairs must be unique");
    }
  }
}

const std::vector<std::int64_t>&
ArchitectureSnapshot::storage_site_ids_by_distance(
    const Point& source) const {
  const auto key = std::make_pair(source.x, source.y);
  const auto cached = storage_distance_cache_.find(key);
  if (cached != storage_distance_cache_.end()) return cached->second;
  auto ordered = storage_site_ids_;
  std::sort(ordered.begin(), ordered.end(), [&](const auto first,
                                                const auto second) {
    const auto first_distance = point_distance(
        source, site_coordinates_[static_cast<std::size_t>(first)]);
    const auto second_distance = point_distance(
        source, site_coordinates_[static_cast<std::size_t>(second)]);
    return std::tie(first_distance, first) <
           std::tie(second_distance, second);
  });
  return storage_distance_cache_.emplace(key, std::move(ordered))
      .first->second;
}

const std::vector<std::int64_t>&
ArchitectureSnapshot::nearest_storage_site_ids(
    const Point& source, std::size_t limit) const {
  if (limit == 0 || limit >= storage_site_ids_.size()) {
    return storage_site_ids_by_distance(source);
  }
  const auto cache_key = std::make_tuple(source.x, source.y, limit);
  const auto cached = nearest_storage_cache_.find(cache_key);
  if (cached != nearest_storage_cache_.end()) return cached->second;

  using DistanceSite = std::pair<double, std::int64_t>;
  std::priority_queue<DistanceSite> nearest;
  const auto split = std::lower_bound(
      storage_site_ids_by_x_.begin(), storage_site_ids_by_x_.end(), source.x,
      [&](const auto site_id, const double x) {
        return site_coordinates_[static_cast<std::size_t>(site_id)].x < x;
      });
  auto left = static_cast<std::ptrdiff_t>(
                  std::distance(storage_site_ids_by_x_.begin(), split)) -
              1;
  auto right = static_cast<std::size_t>(
      std::distance(storage_site_ids_by_x_.begin(), split));
  const auto infinity = std::numeric_limits<double>::infinity();
  while (left >= 0 || right < storage_site_ids_by_x_.size()) {
    const auto left_dx = left >= 0
                             ? std::abs(source.x - site_coordinates_[
                                   static_cast<std::size_t>(
                                       storage_site_ids_by_x_[
                                           static_cast<std::size_t>(left)])]
                                   .x)
                             : infinity;
    const auto right_dx = right < storage_site_ids_by_x_.size()
                              ? std::abs(source.x - site_coordinates_[
                                    static_cast<std::size_t>(
                                        storage_site_ids_by_x_[right])]
                                    .x)
                              : infinity;
    if (nearest.size() == limit &&
        std::min(left_dx, right_dx) > nearest.top().first) {
      break;
    }
    std::int64_t site_id{};
    if (left_dx <= right_dx) {
      site_id = storage_site_ids_by_x_[static_cast<std::size_t>(left)];
      --left;
    } else {
      site_id = storage_site_ids_by_x_[right];
      ++right;
    }
    const auto distance = point_distance(
        source, site_coordinates_[static_cast<std::size_t>(site_id)]);
    const DistanceSite candidate{distance, site_id};
    if (nearest.size() < limit) {
      nearest.push(candidate);
    } else if (candidate < nearest.top()) {
      nearest.pop();
      nearest.push(candidate);
    }
  }

  std::vector<DistanceSite> ordered;
  ordered.reserve(nearest.size());
  while (!nearest.empty()) {
    ordered.push_back(nearest.top());
    nearest.pop();
  }
  std::sort(ordered.begin(), ordered.end());
  std::vector<std::int64_t> result;
  result.reserve(ordered.size());
  for (const auto& [distance, site_id] : ordered) {
    static_cast<void>(distance);
    result.push_back(site_id);
  }
  return nearest_storage_cache_.emplace(cache_key, std::move(result))
      .first->second;
}

const std::vector<RankedEntanglingOption>&
ArchitectureSnapshot::ranked_entangling_options(
    const Point& first, const Point& second) const {
  const auto key =
      std::make_tuple(first.x, first.y, second.x, second.y);
  const auto cached = entangling_rank_cache_.find(key);
  if (cached != entangling_rank_cache_.end()) return cached->second;
  // Bound the persistent architecture cache.  Clearing only changes which
  // exact ranking is recomputed; it cannot change the deterministic order.
  if (entangling_rank_cache_.size() >= 4096) {
    entangling_rank_cache_.clear();
  }
  std::vector<RankedEntanglingOption> ranked;
  ranked.reserve(2 * entangling_site_pairs_.size());
  for (std::size_t pair_index = 0;
       pair_index < entangling_site_pairs_.size(); ++pair_index) {
    const auto& pair = entangling_site_pairs_[pair_index];
    const auto& left =
        site_coordinates_[static_cast<std::size_t>(pair[0])];
    const auto& right =
        site_coordinates_[static_cast<std::size_t>(pair[1])];
    ranked.push_back({point_distance(first, left) +
                          point_distance(second, right),
                      pair_index, false});
    ranked.push_back({point_distance(first, right) +
                          point_distance(second, left),
                      pair_index, true});
  }
  std::sort(ranked.begin(), ranked.end(), [](const auto& left,
                                             const auto& right) {
    return std::tie(left.movement_cost, left.pair_index, left.reversed) <
           std::tie(right.movement_cost, right.pair_index, right.reversed);
  });
  return entangling_rank_cache_.emplace(key, std::move(ranked))
      .first->second;
}

const std::vector<double>&
ArchitectureSnapshot::entangling_endpoint_distances(
    const Point& source) const {
  const auto key = std::make_pair(source.x, source.y);
  const auto cached = entangling_endpoint_distance_cache_.find(key);
  if (cached != entangling_endpoint_distance_cache_.end()) {
    return cached->second;
  }
  // Every formal source is one of the finite architecture coordinates.  Keep
  // a defensive bound for raw/non-indexed callers; eviction only recomputes
  // the same std::hypot values and therefore cannot change semantics.
  constexpr std::size_t kEndpointDistanceCacheLimit = 16384;
  if (entangling_endpoint_distance_cache_.size() >=
      kEndpointDistanceCacheLimit) {
    entangling_endpoint_distance_cache_.clear();
  }
  std::vector<double> distances;
  distances.reserve(2 * entangling_site_pairs_.size());
  for (const auto& pair : entangling_site_pairs_) {
    distances.push_back(point_distance(
        source, site_coordinates_[static_cast<std::size_t>(pair[0])]));
    distances.push_back(point_distance(
        source, site_coordinates_[static_cast<std::size_t>(pair[1])]));
  }
  return entangling_endpoint_distance_cache_.emplace(
      key, std::move(distances)).first->second;
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
  if (phase.legs.size() <= kDenseColorCapacity &&
      (exact_threshold == 0 || phase.legs.size() > exact_threshold)) {
    return color_phase_dense_heuristic(phase);
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
  std::vector<ExecutableBatch> executable_batches;
};

double single_batch_time(const Leg& leg) {
  return 2.0 * kTransferUs +
         std::sqrt(point_distance(leg.source, leg.target) / kAccelUmPerUs2);
}

constexpr std::size_t kFastPhaseLegs = 64;
constexpr std::size_t kFastPhasePositions = 512;

template <std::size_t LegCapacity>
std::size_t mask_members(
    const MovementPhase& phase, std::uint64_t mask,
    std::array<std::size_t, LegCapacity>& members) {
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

template <std::size_t LegCapacity>
double expanded_batch_time_mask(const MovementPhase& phase,
                                std::uint64_t mask) {
  std::array<std::size_t, LegCapacity> members{};
  const auto member_count = mask_members(phase, mask, members);
  if (member_count == 0) return 0.0;
  if (member_count == 1) return single_batch_time(phase.legs[members[0]]);

  std::array<double, LegCapacity> row_keys{};
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

  std::array<std::pair<double, double>, LegCapacity> row_moves{};
  std::array<double, LegCapacity> source_x{};
  std::array<double, LegCapacity> target_x{};
  std::array<std::size_t, LegCapacity> last_row{};
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

  std::array<std::size_t, LegCapacity> column_order{};
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

template <std::size_t LegCapacity>
bool mask_ghost_hit(
    const MovementPhase& phase, std::uint64_t mask,
    const std::int64_t* atoms, const Point* positions,
    std::size_t position_count) {
  if (mask == 0U || position_count == 0) return false;
  std::array<std::pair<double, double>, LegCapacity> columns{};
  std::array<std::pair<double, double>, LegCapacity> rows{};
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

template <std::size_t LegCapacity, std::size_t PositionCapacity>
PhaseEvaluation evaluate_fast_phase_impl(const MovementPhase& phase,
                                         bool enforce_single_leg_ghost,
                                         bool record_batches) {
  PhaseEvaluation result;
  const auto size = phase.legs.size();
  if (size == 0) return result;
  if (phase.batching != "phase" && phase.batching != "greedy") {
    throw std::invalid_argument("unknown batching policy: " + phase.batching);
  }

  std::array<std::uint64_t, LegCapacity> adjacency{};
  std::array<std::int64_t, PositionCapacity> raw_atoms{};
  std::array<Point, PositionCapacity> raw_points{};
  const auto raw_count = phase.ghosts.size();
  for (std::size_t ghost = 0; ghost < raw_count; ++ghost) {
    raw_atoms[ghost] = phase.ghosts[ghost].atom;
    raw_points[ghost] = phase.ghosts[ghost].position;
  }
  // Match the generic phase-adjacency contract: a mover elsewhere in this
  // phase is dynamic, not a stationary pairwise ghost.  Its source/target
  // precedence is derived after coloring below.  The old fast path excluded
  // only the two owners in the candidate pair and therefore over-colored
  // dense phases differently from the reference/generic implementation.
  std::array<std::int64_t, PositionCapacity> adjacency_static_atoms{};
  std::array<Point, PositionCapacity> adjacency_static_points{};
  std::size_t adjacency_static_count = 0;
  for (std::size_t ghost = 0; ghost < raw_count; ++ghost) {
    const auto moving = std::find(
        phase.owners.begin(), phase.owners.end(), raw_atoms[ghost]) !=
        phase.owners.end();
    if (moving) continue;
    adjacency_static_atoms[adjacency_static_count] = raw_atoms[ghost];
    adjacency_static_points[adjacency_static_count] = raw_points[ghost];
    ++adjacency_static_count;
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
          (adjacency_static_count != 0 &&
           mask_ghost_hit<LegCapacity>(
               phase, pair_mask, adjacency_static_atoms.data(),
               adjacency_static_points.data(), adjacency_static_count));
      if (conflict) {
        adjacency[first] |= std::uint64_t{1} << second;
        adjacency[second] |= std::uint64_t{1} << first;
      }
    }
  }

  std::array<std::uint64_t, LegCapacity> batches{};
  std::size_t batch_count = 0;
  if (phase.batching == "greedy") {
    std::array<std::size_t, LegCapacity> priority{};
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
    std::array<int, LegCapacity> colors{};
    colors.fill(-1);
    std::array<std::uint64_t, LegCapacity> neighbor_colors{};
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
    std::array<int, LegCapacity> color_order{};
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
      result.phase_time += expanded_batch_time_mask<LegCapacity>(
          phase, batches[batch]);
      if (record_batches) {
        std::array<std::size_t, LegCapacity> members{};
        const auto count = mask_members(phase, batches[batch], members);
        result.batches.emplace_back(members.begin(), members.begin() + count);
      }
    }
    return result;
  }

  std::array<std::int64_t, LegCapacity> owner_atoms{};
  std::array<std::size_t, LegCapacity> owner_legs{};
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
  std::array<std::int64_t, PositionCapacity> static_atoms{};
  std::array<Point, PositionCapacity> static_points{};
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
    std::array<std::size_t, LegCapacity> owner_batches{};
    for (std::size_t owner = 0; owner < owner_count; ++owner) {
      const auto leg_bit = std::uint64_t{1} << owner_legs[owner];
      for (std::size_t batch = 0; batch < batch_count; ++batch) {
        if ((batches[batch] & leg_bit) != 0U) {
          owner_batches[owner] = batch;
          break;
        }
      }
    }
    std::array<std::uint64_t, LegCapacity> outgoing{};
    std::uint64_t blocked = 0U;
    for (std::size_t batch = 0; batch < batch_count; ++batch) {
      const auto mask = batches[batch];
      if (static_count != 0U &&
          mask_ghost_hit<LegCapacity>(phase, mask, static_atoms.data(),
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
        if (mask_ghost_hit<LegCapacity>(
                phase, mask, atom.data(), source.data(), 1U)) {
          outgoing[other] |= std::uint64_t{1} << batch;
        }
        if (mask_ghost_hit<LegCapacity>(
                phase, mask, atom.data(), target.data(), 1U)) {
          outgoing[batch] |= std::uint64_t{1} << other;
        }
      }
    }
    std::array<std::size_t, LegCapacity> indegree{};
    for (std::size_t batch = 0; batch < batch_count; ++batch) {
      auto neighbors = outgoing[batch];
      while (neighbors != 0U) {
        const auto neighbor = static_cast<std::size_t>(
            __builtin_ctzll(neighbors));
        ++indegree[neighbor];
        neighbors &= neighbors - 1U;
      }
    }
    std::array<std::size_t, LegCapacity> order{};
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
        result.phase_time += expanded_batch_time_mask<LegCapacity>(phase, mask);
        if (record_batches) {
          std::array<std::size_t, LegCapacity> members{};
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
    std::array<std::size_t, LegCapacity> members{};
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

PhaseEvaluation evaluate_fast_phase(const MovementPhase& phase,
                                    bool enforce_single_leg_ghost,
                                    bool record_batches) {
  constexpr std::size_t kSmallFastPhasePositions = 64;
  constexpr std::size_t kSmallFastPhaseLegs = 8;
  if (phase.legs.size() <= kSmallFastPhaseLegs &&
      phase.ghosts.size() + phase.owners.size() <=
          kSmallFastPhasePositions) {
    return evaluate_fast_phase_impl<kSmallFastPhaseLegs,
                                    kSmallFastPhasePositions>(
        phase, enforce_single_leg_ghost, record_batches);
  }
  if (phase.ghosts.size() + phase.owners.size() <=
      kSmallFastPhasePositions) {
    return evaluate_fast_phase_impl<kFastPhaseLegs,
                                    kSmallFastPhasePositions>(
        phase, enforce_single_leg_ghost, record_batches);
  }
  return evaluate_fast_phase_impl<kFastPhaseLegs, kFastPhasePositions>(
      phase, enforce_single_leg_ghost, record_batches);
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

struct GhostTracks {
  std::pair<double, double> column;
  std::pair<double, double> row;
};

bool same_point(const Point& first, const Point& second) {
  return std::abs(first.x - second.x) < kEps &&
         std::abs(first.y - second.y) < kEps;
}

std::optional<GhostTracks> first_ghost_tracks(
    const std::vector<Leg>& legs,
    const std::vector<std::int64_t>& owners,
    const std::map<std::int64_t, Point>& positions) {
  if (legs.empty() || positions.empty()) return std::nullopt;
  std::set<std::int64_t> moving(owners.begin(), owners.end());
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
  for (const auto& [atom, point] : positions) {
    if (moving.count(atom) != 0U || point.x < x_min - kEps ||
        point.x > x_max + kEps || point.y < y_min - kEps ||
        point.y > y_max + kEps) {
      continue;
    }
    for (const auto& column : columns) {
      const auto x = cover(column.first, column.second, point.x);
      if (!x.valid) continue;
      for (const auto& row : rows) {
        const auto y = cover(row.first, row.second, point.y);
        if (!y.valid) continue;
        if (x.always || y.always ||
            std::abs(x.time - y.time) < kSTolerance) {
          return GhostTracks{column, row};
        }
      }
    }
  }
  return std::nullopt;
}

std::set<std::size_t> expanded_batch_conflict_members(
    const MovementPhase& phase,
    const std::vector<std::size_t>& members,
    const std::map<std::int64_t, Point>& replay_positions) {
  std::map<std::int64_t, std::size_t> member_by_owner;
  std::map<std::int64_t, Point> source_by_owner;
  std::map<std::int64_t, Point> target_by_owner;
  std::map<double, std::vector<std::int64_t>> rows;
  for (const auto member : members) {
    if (member >= phase.legs.size() || member >= phase.owners.size()) {
      throw std::invalid_argument(
          "expanded replay member is outside phase geometry");
    }
    const auto owner = phase.owners[member];
    if (!member_by_owner.emplace(owner, member).second) {
      throw std::invalid_argument("expanded replay repeats a phase owner");
    }
    source_by_owner.emplace(owner, phase.legs[member].source);
    target_by_owner.emplace(owner, phase.legs[member].target);
    rows[phase.legs[member].source.y].push_back(owner);
  }
  for (auto& [source_y, owners] : rows) {
    (void)source_y;
    std::sort(owners.begin(), owners.end());
  }

  std::map<std::int64_t, Point> physical = source_by_owner;
  std::set<std::int64_t> held;
  std::set<double> activated_columns;

  const auto audit_move = [&](const std::map<std::int64_t, Point>& next)
      -> std::set<std::size_t> {
    std::vector<Leg> detail_legs;
    std::vector<std::int64_t> detail_owners;
    for (const auto owner : held) {
      const auto current = physical.at(owner);
      const auto target = next.at(owner);
      if (same_point(current, target)) continue;
      detail_legs.push_back(
          Leg{point_distance(current, target), current, target});
      detail_owners.push_back(owner);
    }
    for (std::size_t left = 0; left < detail_legs.size(); ++left) {
      const auto& first = detail_legs[left];
      const std::array<double, 4> first_vector{
          first.source.x, first.target.x, first.source.y, first.target.y};
      for (std::size_t right = left + 1; right < detail_legs.size(); ++right) {
        const auto& second = detail_legs[right];
        const std::array<double, 4> second_vector{
            second.source.x, second.target.x,
            second.source.y, second.target.y};
        if (!compatible_2d(first_vector, second_vector)) {
          return {member_by_owner.at(detail_owners[left]),
                  member_by_owner.at(detail_owners[right])};
        }
      }
    }
    std::map<std::int64_t, Point> positions = replay_positions;
    positions.insert(physical.begin(), physical.end());
    for (const auto& [owner, point] : physical) positions[owner] = point;
    const auto hit = first_ghost_tracks(
        detail_legs, detail_owners, positions);
    if (!hit.has_value()) return {};
    std::set<std::size_t> bad;
    for (std::size_t index = 0; index < detail_legs.size(); ++index) {
      const auto& leg = detail_legs[index];
      if (std::pair<double, double>{leg.source.x, leg.target.x} ==
              hit->column ||
          std::pair<double, double>{leg.source.y, leg.target.y} == hit->row) {
        bad.insert(member_by_owner.at(detail_owners[index]));
      }
    }
    if (bad.empty() && !detail_owners.empty()) {
      bad.insert(member_by_owner.at(detail_owners.front()));
    }
    return bad;
  };

  std::size_t row_index = 0;
  for (const auto& [source_y, row_owners] : rows) {
    std::map<std::int64_t, Point> shifted = physical;
    for (const auto owner : row_owners) {
      const auto source_x = source_by_owner.at(owner).x;
      if (activated_columns.count(source_x) == 0U) continue;
      for (const auto held_owner : held) {
        if (source_by_owner.at(held_owner).x == source_x) {
          shifted[held_owner].x = source_x;
        }
      }
    }
    if (const auto bad = audit_move(shifted); !bad.empty()) return bad;
    physical = std::move(shifted);
    held.insert(row_owners.begin(), row_owners.end());
    for (const auto owner : row_owners) {
      activated_columns.insert(source_by_owner.at(owner).x);
    }

    if (row_index + 1 < rows.size()) {
      std::set<double> parked_columns;
      for (const auto owner : row_owners) {
        parked_columns.insert(source_by_owner.at(owner).x);
      }
      std::map<std::int64_t, Point> parked = physical;
      for (const auto owner : held) {
        if (parked_columns.count(source_by_owner.at(owner).x) != 0U) {
          parked[owner].x += 1.0;
        }
      }
      for (const auto owner : row_owners) {
        parked[owner].y = source_y + 1.0;
      }
      if (const auto bad = audit_move(parked); !bad.empty()) return bad;
      physical = std::move(parked);
    }
    ++row_index;
  }

  std::map<std::int64_t, Point> target = physical;
  for (const auto owner : held) target[owner] = target_by_owner.at(owner);
  if (const auto bad = audit_move(target); !bad.empty()) return bad;
  return {};
}

ExecutableBatch executable_batch(
    const MovementPhase& phase, const std::vector<std::size_t>& members,
    const std::vector<std::size_t>& canonical_to_original) {
  ExecutableBatch result;
  result.original_members.reserve(members.size());
  result.legs.reserve(members.size());
  result.owners.reserve(members.size());
  for (const auto member : members) {
    result.original_members.push_back(canonical_to_original.at(member));
    result.legs.push_back(phase.legs.at(member));
    result.owners.push_back(phase.owners.at(member));
  }
  return result;
}

struct ProductionReplay {
  bool feasible{true};
  std::vector<ExecutableBatch> batches;
};

ProductionReplay replay_production_phase_batches(
    const MovementPhase& phase, std::size_t exact_threshold) {
  if (phase.owners.size() != phase.legs.size()) {
    throw std::invalid_argument(
        "production replay requires one owner per movement leg");
  }
  // The production resident router canonicalizes movers by descending travel
  // distance before graph construction/coloring.  Keep original indices only
  // for the public batch audit payload consumed by Python.
  std::vector<std::size_t> canonical_to_original(phase.legs.size());
  std::iota(canonical_to_original.begin(), canonical_to_original.end(), 0U);
  std::stable_sort(
      canonical_to_original.begin(), canonical_to_original.end(),
      [&](const auto first, const auto second) {
        return phase.legs[first].distance_um > phase.legs[second].distance_um;
      });
  MovementPhase routed;
  routed.batching = phase.batching;
  routed.ghosts = phase.ghosts;
  routed.legs.reserve(phase.legs.size());
  routed.owners.reserve(phase.owners.size());
  for (const auto original : canonical_to_original) {
    routed.legs.push_back(phase.legs[original]);
    routed.owners.push_back(phase.owners[original]);
  }

  std::map<std::int64_t, Point> positions;
  for (const auto& ghost : routed.ghosts) {
    if (!positions.emplace(ghost.atom, ghost.position).second) {
      throw std::invalid_argument("production replay repeats a ghost atom");
    }
  }
  for (std::size_t index = 0; index < routed.owners.size(); ++index) {
    positions.emplace(routed.owners[index], routed.legs[index].source);
  }

  const auto audit_pass = [&](
      const std::vector<std::vector<std::size_t>>& input,
      std::map<std::int64_t, Point>& replay_positions,
      std::vector<ExecutableBatch>& clean,
      std::vector<std::size_t>& deferred) -> bool {
    std::deque<std::vector<std::size_t>> queue;
    for (const auto& batch : input) queue.push_back(batch);
    while (!queue.empty()) {
      auto pending = std::move(queue.front());
      queue.pop_front();
      while (true) {
        std::vector<Leg> legs;
        std::vector<std::int64_t> owners;
        legs.reserve(pending.size());
        owners.reserve(pending.size());
        for (const auto member : pending) {
          legs.push_back(routed.legs.at(member));
          owners.push_back(routed.owners.at(member));
        }
        const auto hit = first_ghost_tracks(legs, owners, replay_positions);
        if (!hit.has_value()) break;
        if (pending.size() == 1) {
          // The stationary blocker may itself move in an earlier clean batch.
          // Production defers this singleton and retries against the updated
          // position map before considering a waypoint/failure.
          deferred.push_back(pending.front());
          pending.clear();
          break;
        }
        std::set<std::size_t> bad;
        for (const auto member : pending) {
          const auto& leg = routed.legs[member];
          if (std::pair<double, double>{leg.source.x, leg.target.x} ==
                  hit->column ||
              std::pair<double, double>{leg.source.y, leg.target.y} == hit->row) {
            bad.insert(member);
          }
        }
        if (bad.empty()) bad.insert(pending.front());
        deferred.insert(deferred.end(), bad.begin(), bad.end());
        pending.erase(std::remove_if(
            pending.begin(), pending.end(), [&](const auto member) {
              return bad.count(member) != 0U;
            }), pending.end());
        if (pending.empty()) break;
      }
      if (pending.empty()) continue;

      const auto expanded_bad = expanded_batch_conflict_members(
          routed, pending, replay_positions);
      if (!expanded_bad.empty()) {
        if (pending.size() == 1) {
          deferred.push_back(pending.front());
          continue;
        }
        std::vector<std::size_t> keep;
        std::vector<std::size_t> bad;
        for (const auto member : pending) {
          (expanded_bad.count(member) == 0U ? keep : bad).push_back(member);
        }
        if (!keep.empty() && !bad.empty()) {
          queue.push_front(std::move(keep));
          deferred.insert(deferred.end(), bad.begin(), bad.end());
          continue;
        }
        std::map<double, std::vector<std::size_t>> row_groups;
        for (const auto member : pending) {
          row_groups[routed.legs[member].source.y].push_back(member);
        }
        if (row_groups.size() == 1) {
          for (auto it = pending.rbegin(); it != pending.rend(); ++it) {
            queue.push_front({*it});
          }
        } else {
          for (auto it = row_groups.rbegin(); it != row_groups.rend(); ++it) {
            queue.push_front(std::move(it->second));
          }
        }
        continue;
      }
      clean.push_back(executable_batch(
          routed, pending, canonical_to_original));
      for (const auto member : pending) {
        replay_positions[routed.owners[member]] = routed.legs[member].target;
      }
    }
    return true;
  };

  ProductionReplay result;
  auto initial = color_phase(routed, exact_threshold);
  std::vector<std::size_t> deferred;
  if (!audit_pass(initial, positions, result.batches, deferred)) {
    result.feasible = false;
    return result;
  }
  for (std::size_t round = 0; round < 3 && !deferred.empty(); ++round) {
    std::sort(deferred.begin(), deferred.end());
    deferred.erase(std::unique(deferred.begin(), deferred.end()), deferred.end());
    std::vector<std::vector<std::size_t>> batches;
    if (round == 2) {
      for (const auto member : deferred) batches.push_back({member});
    } else {
      MovementPhase subset;
      subset.batching = routed.batching;
      for (const auto member : deferred) {
        subset.legs.push_back(routed.legs[member]);
      }
      const auto local = color_phase(subset, exact_threshold);
      for (const auto& batch : local) {
        std::vector<std::size_t> translated;
        for (const auto member : batch) translated.push_back(deferred[member]);
        batches.push_back(std::move(translated));
      }
    }
    deferred.clear();
    if (!audit_pass(batches, positions, result.batches, deferred)) {
      result.feasible = false;
      return result;
    }
  }
  // Dense source-seat handoffs can leave the heuristic replay in a bad partial
  // order even though the exact endpoint-precedence replay has a valid order.
  // When that happens, discard the heuristic prefix and replay the complete
  // strict order from the original positions.  Every strict batch still goes
  // through the expanded physical audit.  When any group is unsafe, a
  // deterministic full-suffix search prefers each remaining intact group and
  // then its stable singletons; failed moved-subsets are memoized.  A moved
  // subset uniquely determines the physical source/target positions, so the
  // memo is exact and avoids factorial retry blow-ups.
  if (!deferred.empty()) {
    const auto strict = replay_phase_batches(routed, exact_threshold);
    if (!strict.feasible) {
      result.feasible = false;
      return result;
    }
    positions.clear();
    for (const auto& ghost : routed.ghosts) {
      positions.emplace(ghost.atom, ghost.position);
    }
    for (std::size_t index = 0; index < routed.owners.size(); ++index) {
      positions[routed.owners[index]] = routed.legs[index].source;
    }
    result.batches.clear();
    const auto physically_safe = [&](const std::vector<std::size_t>& members) {
      std::vector<Leg> legs;
      std::vector<std::int64_t> owners;
      legs.reserve(members.size());
      owners.reserve(members.size());
      for (const auto member : members) {
        legs.push_back(routed.legs.at(member));
        owners.push_back(routed.owners.at(member));
      }
      if (first_ghost_tracks(legs, owners, positions).has_value() ||
          !expanded_batch_conflict_members(
               routed, members, positions).empty()) {
        return false;
      }
      return true;
    };
    // Search the complete remaining strict suffix, not merely the member
    // order inside one expanded-unsafe batch.  Moving a singleton from a later
    // strict group can be the only way to vacate a source that blocks an
    // earlier group.  Each moved mask uniquely fixes all source/target
    // positions, making failed-mask memoization exact.
    const auto word_count = (routed.legs.size() + 63U) / 64U;
    std::vector<std::uint64_t> moved_mask(word_count, 0U);
    std::size_t moved_count = 0;
    std::set<std::vector<std::uint64_t>> failed_masks;
    std::vector<std::vector<std::size_t>> selected_batches;
    selected_batches.reserve(strict.batches.size());
    const auto is_moved = [&](std::size_t member) {
      return (moved_mask[member / 64U] &
              (std::uint64_t{1} << (member % 64U))) != 0U;
    };
    const auto set_moved = [&](std::size_t member, bool value) {
      auto& word = moved_mask[member / 64U];
      const auto bit = std::uint64_t{1} << (member % 64U);
      if (value) {
        word |= bit;
      } else {
        word &= ~bit;
      }
    };
    std::function<bool()> exact_suffix = [&]() {
      if (moved_count == routed.legs.size()) return true;
      if (failed_masks.count(moved_mask) != 0U) return false;
      const auto attempt = [&](const std::vector<std::size_t>& pending,
                               const auto& recurse) {
        if (!physically_safe(pending)) return false;
        std::vector<Point> previous;
        previous.reserve(pending.size());
        for (const auto member : pending) {
          const auto owner = routed.owners[member];
          previous.push_back(positions.at(owner));
          positions[owner] = routed.legs[member].target;
          set_moved(member, true);
        }
        moved_count += pending.size();
        selected_batches.push_back(pending);
        if (recurse()) return true;
        selected_batches.pop_back();
        moved_count -= pending.size();
        for (std::size_t index = 0; index < pending.size(); ++index) {
          const auto member = pending[index];
          positions[routed.owners[member]] = previous[index];
          set_moved(member, false);
        }
        return false;
      };
      for (const auto& strict_batch : strict.batches) {
        std::vector<std::size_t> remaining;
        remaining.reserve(strict_batch.size());
        for (const auto member : strict_batch) {
          if (!is_moved(member)) remaining.push_back(member);
        }
        if (remaining.empty()) continue;
        if (remaining.size() > 1 &&
            attempt(remaining, exact_suffix)) {
          return true;
        }
        for (const auto member : remaining) {
          if (attempt({member}, exact_suffix)) return true;
        }
      }
      failed_masks.insert(moved_mask);
      return false;
    };
    if (!exact_suffix()) {
      result.feasible = false;
      return result;
    }
    for (const auto& batch : selected_batches) {
      result.batches.push_back(executable_batch(
          routed, batch, canonical_to_original));
    }
  }
  return result;
}

PhaseEvaluation evaluate_phase(const ArchitectureSnapshot& architecture,
                               const MovementPhase& phase,
                               std::size_t exact_threshold,
                               bool enforce_single_leg_ghost,
                               bool record_batches,
                               bool production_parking_replay) {
  if (production_parking_replay && enforce_single_leg_ghost &&
      !phase.owners.empty()) {
    auto replay = replay_production_phase_batches(phase, exact_threshold);
    PhaseEvaluation result;
    result.feasible = replay.feasible;
    if (!result.feasible) return result;
    result.executable_batches = std::move(replay.batches);
    result.batch_count = result.executable_batches.size();
    for (const auto& batch : result.executable_batches) {
      std::vector<std::size_t> local(batch.legs.size());
      for (std::size_t index = 0; index < local.size(); ++index) {
        local[index] = index;
      }
      result.phase_time += expanded_batch_time(batch.legs, local);
      if (record_batches) result.batches.push_back(batch.original_members);
    }
    (void)architecture;
    return result;
  }
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

double linear_coherence_delta_nll(
    const std::vector<double>& prior_idle_time_us,
    const std::vector<double>& candidate_idle_time_us) {
  if (prior_idle_time_us.size() != candidate_idle_time_us.size()) {
    throw std::invalid_argument("coherence vectors differ in length");
  }
  double total = 0.0;
  for (std::size_t atom = 0; atom < prior_idle_time_us.size(); ++atom) {
    const auto prior = prior_idle_time_us[atom];
    const auto delta = candidate_idle_time_us[atom];
    if (!std::isfinite(prior) || prior < 0.0) {
      throw std::invalid_argument(
          "prior idle time must be finite and non-negative");
    }
    if (!std::isfinite(delta) || delta < -1e-12) {
      throw std::invalid_argument(
          "candidate idle-time delta must be finite and non-negative");
    }
    const auto after = prior + delta;
    if (prior >= kT2Us || after >= kT2Us) {
      return std::numeric_limits<double>::infinity();
    }
    total += std::log1p(-prior / kT2Us) -
             std::log1p(-after / kT2Us);
  }
  return total;
}

namespace {
FitnessResult evaluate_candidate_impl(
    const ArchitectureSnapshot& architecture, const CandidatePlan& candidate,
    const BoundaryConfig& config, bool record_batches,
    const std::vector<double>& prior_idle_time_us) {
  if (candidate.idle_exposures < 0) {
    throw std::invalid_argument("idle_exposures must be non-negative");
  }
  if (!prior_idle_time_us.empty() &&
      prior_idle_time_us.size() != architecture.n_atoms()) {
    throw std::invalid_argument(
        "prior idle time must contain every atom or be empty");
  }
  const auto exact_owners = std::all_of(
      candidate.phases.begin(), candidate.phases.end(), [](const auto& phase) {
        return phase.legs.empty() || !phase.owners.empty();
      });
  if (!prior_idle_time_us.empty() && !exact_owners) {
    throw std::invalid_argument(
        "formal coherence accounting requires one owner per movement leg");
  }
  const auto prior_idle = prior_idle_time_us.empty()
                              ? std::vector<double>(architecture.n_atoms(), 0.0)
                              : prior_idle_time_us;
  std::vector<double> candidate_idle(architecture.n_atoms(), 0.0);
  FitnessResult result;
  result.chromosome = candidate.chromosome;
  result.idle_exposures = candidate.idle_exposures;
  // The target CZ/1Q durations are candidate-independent and are committed by
  // the scheduler once.  Only location-dependent idle excitation and movement
  // coherence belong in the boundary objective.  Owner-less legacy fixtures
  // retain the pre-ABI7 aggregate approximation for source compatibility.
  result.coherence_nll =
      exact_owners
          ? 0.0
          : -static_cast<double>(candidate.idle_exposures) *
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
        architecture, phase, config.exact_coloring_threshold,
        config.enforce_single_leg_ghost, record_batches,
        config.production_parking_replay);
    if (!evaluated.feasible) {
      return infeasible(
          candidate, "phase " + std::to_string(phase_index) +
                         " has no ghost-safe straight-leg batch order");
    }
    const auto phase_time = evaluated.phase_time;
    std::size_t phase_movers = phase.legs.size();
    if (!evaluated.executable_batches.empty()) {
      phase_movers = 0;
      for (const auto& batch : evaluated.executable_batches) {
        phase_movers += batch.owners.size();
      }
    }
    const auto mover_idle = std::max(0.0, phase_time - 2.0 * kTransferUs);
    double updated_coherence = result.coherence_nll;
    if (exact_owners) {
      std::vector<unsigned char> seen(architecture.n_atoms(), 0U);
      for (const auto owner : phase.owners) {
        if (owner < 0 ||
            static_cast<std::size_t>(owner) >= architecture.n_atoms()) {
          throw std::invalid_argument(
              "movement owner is outside the architecture");
        }
        if (seen[static_cast<std::size_t>(owner)] != 0U) {
          throw std::invalid_argument("a movement phase cannot repeat an owner");
        }
        seen[static_cast<std::size_t>(owner)] = 1U;
      }
      if (evaluated.executable_batches.empty()) {
        for (auto& value : candidate_idle) value += phase_time;
        for (const auto owner : phase.owners) {
          candidate_idle[static_cast<std::size_t>(owner)] -=
              2.0 * kTransferUs;
        }
      } else {
        for (const auto& batch : evaluated.executable_batches) {
          std::vector<std::size_t> local(batch.legs.size());
          for (std::size_t index = 0; index < local.size(); ++index) {
            local[index] = index;
          }
          const auto batch_time = expanded_batch_time(batch.legs, local);
          for (auto& value : candidate_idle) value += batch_time;
          for (const auto owner : batch.owners) {
            candidate_idle[static_cast<std::size_t>(owner)] -=
                2.0 * kTransferUs;
          }
        }
      }
      updated_coherence =
          linear_coherence_delta_nll(prior_idle, candidate_idle);
      if (!std::isfinite(updated_coherence)) {
        // Search-time continuation only: once the final linear factor is OOD,
        // rank the whole boundary with the registered exponential sensitivity
        // increment.  The independent trace scorer still reports linear OOD.
        updated_coherence = 0.0;
        for (const auto delta : candidate_idle) {
          updated_coherence += delta / kT2Us;
        }
      }
    } else if (phase_time >= kT2Us || mover_idle >= kT2Us) {
      // Owner-less fixtures have no absolute per-atom ledger; preserve their
      // aggregate model and continue this OOD phase exponentially.
      updated_coherence +=
          (static_cast<double>(architecture.n_atoms() - phase_movers) *
               phase_time +
           static_cast<double>(phase_movers) * mover_idle) /
          kT2Us;
    } else {
      updated_coherence -=
          static_cast<double>(architecture.n_atoms() - phase_movers) *
          std::log1p(-phase_time / kT2Us);
      updated_coherence -= static_cast<double>(phase_movers) *
                           std::log1p(-mover_idle / kT2Us);
    }
    if (!std::isfinite(updated_coherence)) {
      result.feasible = false;
      result.error = "linear coherence model out of domain";
      result.negative_log_fidelity = std::numeric_limits<double>::infinity();
      result.transfer_nll = result.negative_log_fidelity;
      result.idle_excitation_nll = result.negative_log_fidelity;
      result.coherence_nll = result.negative_log_fidelity;
      result.move_batches += evaluated.batch_count;
      result.move_time_us += phase_time;
      if (evaluated.executable_batches.empty()) {
        for (const auto& leg : phase.legs) {
          result.total_distance_um += leg.distance_um;
        }
      } else {
        for (const auto& batch : evaluated.executable_batches) {
          for (const auto& leg : batch.legs) {
            result.total_distance_um += leg.distance_um;
          }
        }
      }
      result.transfers = 2 * (movers + phase_movers);
      if (exact_owners) result.candidate_idle_time_us = candidate_idle;
      if (record_batches) {
        result.phase_batches.push_back(std::move(evaluated.batches));
      }
      result.executable_phase_batches.push_back(
          std::move(evaluated.executable_batches));
      return result;
    }
    result.coherence_nll = updated_coherence;
    if (exact_owners) result.candidate_idle_time_us = candidate_idle;
    result.move_batches += evaluated.batch_count;
    result.move_time_us += phase_time;
    if (evaluated.executable_batches.empty()) {
      for (const auto& leg : phase.legs) {
        result.total_distance_um += leg.distance_um;
      }
    } else {
      for (const auto& batch : evaluated.executable_batches) {
        for (const auto& leg : batch.legs) {
          result.total_distance_um += leg.distance_um;
        }
      }
    }
    movers += phase_movers;
    if (record_batches) {
      result.phase_batches.push_back(std::move(evaluated.batches));
    }
    result.executable_phase_batches.push_back(
        std::move(evaluated.executable_batches));
  }
  result.transfers = 2 * movers;
  result.transfer_nll = -static_cast<double>(result.transfers) *
                        std::log(kFTransfer);
  result.idle_excitation_nll = -static_cast<double>(candidate.idle_exposures) *
                               std::log(kFExc);
  result.negative_log_fidelity = result.transfer_nll +
                                 result.idle_excitation_nll +
                                 result.coherence_nll;
  if (exact_owners) result.candidate_idle_time_us = candidate_idle;
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
                                 const BoundaryConfig& config,
                                 const std::vector<double>& prior_idle_time_us) {
  return evaluate_candidate_impl(
      architecture, candidate, config, true, prior_idle_time_us);
}

FitnessResult evaluate_candidate_summary(
    const ArchitectureSnapshot& architecture, const CandidatePlan& candidate,
    const BoundaryConfig& config,
    const std::vector<double>& prior_idle_time_us) {
  return evaluate_candidate_impl(
      architecture, candidate, config, false, prior_idle_time_us);
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
