#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace zac_native {

inline constexpr int kNativeAbiVersion = 4;

struct Point {
  double x{};
  double y{};
};

struct Leg {
  double distance_um{};
  Point source;
  Point target;
};

struct Ghost {
  std::int64_t atom{};
  Point position;
};

struct MovementPhase {
  std::vector<Leg> legs;
  std::vector<Ghost> ghosts;
  std::vector<std::int64_t> owners;
  std::string batching{"phase"};
};

struct CandidatePlan {
  std::vector<std::int64_t> chromosome;
  std::vector<MovementPhase> phases;
  std::int64_t idle_exposures{};
};

struct BoundaryConfig {
  std::int64_t seed{};
  std::size_t max_unique_evaluations{};
  std::size_t exact_coloring_threshold{};
  std::string horizon_policy{"fixed"};
  std::size_t max_horizon{};
  bool enforce_single_leg_ghost{true};
};

struct FitnessResult {
  std::vector<std::int64_t> chromosome;
  bool feasible{true};
  double negative_log_fidelity{};
  double transfer_nll{};
  double idle_excitation_nll{};
  double coherence_nll{};
  std::size_t move_batches{};
  double move_time_us{};
  double total_distance_um{};
  std::int64_t idle_exposures{};
  std::size_t transfers{};
  std::vector<std::vector<std::vector<std::size_t>>> phase_batches;
  std::string error;
};

class ArchitectureSnapshot {
 public:
  ArchitectureSnapshot(std::size_t n_atoms, std::vector<Point> site_coordinates,
                       std::vector<std::int64_t> storage_site_ids = {},
                       std::vector<std::array<std::int64_t, 2>>
                           entangling_site_pairs = {});

  std::size_t n_atoms() const noexcept { return n_atoms_; }
  const std::vector<Point>& site_coordinates() const noexcept {
    return site_coordinates_;
  }
  const std::vector<std::int64_t>& storage_site_ids() const noexcept {
    return storage_site_ids_;
  }
  const std::vector<std::array<std::int64_t, 2>>&
  entangling_site_pairs() const noexcept {
    return entangling_site_pairs_;
  }

 private:
  std::size_t n_atoms_;
  std::vector<Point> site_coordinates_;
  std::vector<std::int64_t> storage_site_ids_;
  std::vector<std::array<std::int64_t, 2>> entangling_site_pairs_;
};

}  // namespace zac_native
