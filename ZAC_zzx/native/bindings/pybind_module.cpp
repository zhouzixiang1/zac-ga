#include "zac_native/core.hpp"
#include "zac_native/rich_bindings.hpp"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

namespace py = pybind11;
using namespace zac_native;

namespace {

template <typename T>
std::vector<T> copy_typed_buffer(const py::dict& buffers, const char* name) {
  if (!buffers.contains(name)) {
    throw std::invalid_argument(std::string("flat payload missing ") + name);
  }
  const auto buffer = py::cast<py::buffer>(buffers[name]);
  const auto info = buffer.request();
  if (info.ndim != 1 || info.itemsize != static_cast<py::ssize_t>(sizeof(T)) ||
      info.strides[0] != static_cast<py::ssize_t>(sizeof(T)) ||
      info.format != py::format_descriptor<T>::format()) {
    throw std::invalid_argument(std::string(name) +
                                " must be a contiguous typed 1-D buffer");
  }
  const auto* begin = static_cast<const T*>(info.ptr);
  return {begin, begin + info.size};
}

std::size_t checked_offset(std::int64_t value, std::size_t upper,
                           const char* name) {
  if (value < 0 || static_cast<std::uint64_t>(value) > upper) {
    throw std::invalid_argument(std::string(name) + " contains an invalid offset");
  }
  return static_cast<std::size_t>(value);
}

void validate_offsets(const std::vector<std::int64_t>& offsets,
                      std::size_t expected_size, std::size_t upper,
                      const char* name) {
  if (offsets.size() != expected_size || offsets.empty() || offsets.front() != 0) {
    throw std::invalid_argument(std::string(name) + " has an invalid shape");
  }
  std::int64_t prior = 0;
  for (const auto value : offsets) {
    if (value < prior || value < 0 ||
        static_cast<std::uint64_t>(value) > upper) {
      throw std::invalid_argument(std::string(name) +
                                  " must be monotonic and in range");
    }
    prior = value;
  }
  if (static_cast<std::size_t>(offsets.back()) != upper) {
    throw std::invalid_argument(std::string(name) + " does not consume its buffer");
  }
}

std::vector<CandidatePlan> parse_flat_candidates(const py::dict& buffers,
                                                 std::size_t limit = 0) {
  const auto chromosome_offsets =
      copy_typed_buffer<std::int64_t>(buffers, "chromosome_offsets");
  const auto chromosome_values =
      copy_typed_buffer<std::int64_t>(buffers, "chromosome_values");
  const auto candidate_phase_offsets =
      copy_typed_buffer<std::int64_t>(buffers, "candidate_phase_offsets");
  const auto idle_exposures =
      copy_typed_buffer<std::int64_t>(buffers, "idle_exposures");
  const auto phase_leg_offsets =
      copy_typed_buffer<std::int64_t>(buffers, "phase_leg_offsets");
  const auto phase_ghost_offsets =
      copy_typed_buffer<std::int64_t>(buffers, "phase_ghost_offsets");
  const auto phase_owner_offsets =
      copy_typed_buffer<std::int64_t>(buffers, "phase_owner_offsets");
  const auto batching = copy_typed_buffer<std::uint8_t>(buffers, "batching");
  const auto leg_values = copy_typed_buffer<double>(buffers, "leg_values");
  const auto ghost_atoms =
      copy_typed_buffer<std::int64_t>(buffers, "ghost_atoms");
  const auto ghost_xy = copy_typed_buffer<double>(buffers, "ghost_xy");
  const auto owners = copy_typed_buffer<std::int64_t>(buffers, "owners");

  const auto candidate_count = idle_exposures.size();
  if (candidate_count == 0) throw std::invalid_argument("flat batch is empty");
  if (leg_values.size() % 5 != 0 || ghost_xy.size() % 2 != 0) {
    throw std::invalid_argument("flat coordinate buffers have invalid lengths");
  }
  const auto phase_count = batching.size();
  const auto leg_count = leg_values.size() / 5;
  const auto ghost_count = ghost_atoms.size();
  if (ghost_xy.size() / 2 != ghost_count) {
    throw std::invalid_argument("ghost atom and coordinate counts differ");
  }
  validate_offsets(chromosome_offsets, candidate_count + 1,
                   chromosome_values.size(), "chromosome_offsets");
  validate_offsets(candidate_phase_offsets, candidate_count + 1,
                   phase_count, "candidate_phase_offsets");
  validate_offsets(phase_leg_offsets, phase_count + 1,
                   leg_count, "phase_leg_offsets");
  validate_offsets(phase_ghost_offsets, phase_count + 1,
                   ghost_count, "phase_ghost_offsets");
  validate_offsets(phase_owner_offsets, phase_count + 1,
                   owners.size(), "phase_owner_offsets");

  const auto output_count = limit == 0 ? candidate_count
                                       : std::min(limit, candidate_count);
  std::vector<CandidatePlan> candidates;
  candidates.reserve(output_count);
  for (std::size_t candidate_index = 0; candidate_index < output_count;
       ++candidate_index) {
    CandidatePlan candidate;
    const auto chromosome_begin = checked_offset(
        chromosome_offsets[candidate_index], chromosome_values.size(),
        "chromosome_offsets");
    const auto chromosome_end = checked_offset(
        chromosome_offsets[candidate_index + 1], chromosome_values.size(),
        "chromosome_offsets");
    candidate.chromosome.assign(chromosome_values.begin() + chromosome_begin,
                                chromosome_values.begin() + chromosome_end);
    candidate.idle_exposures = idle_exposures[candidate_index];
    const auto phase_begin = checked_offset(
        candidate_phase_offsets[candidate_index], phase_count,
        "candidate_phase_offsets");
    const auto phase_end = checked_offset(
        candidate_phase_offsets[candidate_index + 1], phase_count,
        "candidate_phase_offsets");
    candidate.phases.reserve(phase_end - phase_begin);
    for (auto phase_index = phase_begin; phase_index < phase_end; ++phase_index) {
      MovementPhase phase;
      const auto leg_begin = checked_offset(
          phase_leg_offsets[phase_index], leg_count, "phase_leg_offsets");
      const auto leg_end = checked_offset(
          phase_leg_offsets[phase_index + 1], leg_count, "phase_leg_offsets");
      phase.legs.reserve(leg_end - leg_begin);
      for (auto leg_index = leg_begin; leg_index < leg_end; ++leg_index) {
        const auto base = leg_index * 5;
        phase.legs.push_back({
            leg_values[base],
            {leg_values[base + 1], leg_values[base + 2]},
            {leg_values[base + 3], leg_values[base + 4]},
        });
      }
      const auto ghost_begin = checked_offset(
          phase_ghost_offsets[phase_index], ghost_count, "phase_ghost_offsets");
      const auto ghost_end = checked_offset(
          phase_ghost_offsets[phase_index + 1], ghost_count,
          "phase_ghost_offsets");
      phase.ghosts.reserve(ghost_end - ghost_begin);
      for (auto ghost_index = ghost_begin; ghost_index < ghost_end; ++ghost_index) {
        phase.ghosts.push_back({
            ghost_atoms[ghost_index],
            {ghost_xy[ghost_index * 2], ghost_xy[ghost_index * 2 + 1]},
        });
      }
      const auto owner_begin = checked_offset(
          phase_owner_offsets[phase_index], owners.size(), "phase_owner_offsets");
      const auto owner_end = checked_offset(
          phase_owner_offsets[phase_index + 1], owners.size(),
          "phase_owner_offsets");
      phase.owners.assign(owners.begin() + owner_begin, owners.begin() + owner_end);
      const auto policy = batching[phase_index];
      if (policy > 1) throw std::invalid_argument("unknown flat batching code");
      phase.batching = policy == 0 ? "phase" : "greedy";
      candidate.phases.push_back(std::move(phase));
    }
    candidates.push_back(std::move(candidate));
  }
  return candidates;
}

Point parse_point(const py::handle& value) {
  const auto point = py::cast<std::vector<double>>(value);
  if (point.size() != 2) throw std::invalid_argument("point must have two values");
  return {point[0], point[1]};
}

Leg parse_leg(const py::handle& value) {
  const auto leg = py::cast<std::vector<double>>(value);
  if (leg.size() != 5) throw std::invalid_argument("leg must have five values");
  return {leg[0], {leg[1], leg[2]}, {leg[3], leg[4]}};
}

Ghost parse_ghost(const py::handle& value) {
  const auto ghost = py::cast<py::sequence>(value);
  if (ghost.size() != 3) throw std::invalid_argument("ghost must have three values");
  return {py::cast<std::int64_t>(ghost[0]),
          {py::cast<double>(ghost[1]), py::cast<double>(ghost[2])}};
}

MovementPhase parse_phase(const py::dict& value) {
  MovementPhase phase;
  for (const auto& item : py::cast<py::list>(value["legs"])) {
    phase.legs.push_back(parse_leg(item));
  }
  for (const auto& item : py::cast<py::list>(value["ghosts"])) {
    phase.ghosts.push_back(parse_ghost(item));
  }
  phase.owners = py::cast<std::vector<std::int64_t>>(value["owners"]);
  phase.batching = py::cast<std::string>(value["batching"]);
  return phase;
}

CandidatePlan parse_candidate(const py::dict& value) {
  CandidatePlan candidate;
  candidate.chromosome =
      py::cast<std::vector<std::int64_t>>(value["chromosome"]);
  for (const auto& item : py::cast<py::list>(value["phases"])) {
    candidate.phases.push_back(parse_phase(py::cast<py::dict>(item)));
  }
  candidate.idle_exposures = py::cast<std::int64_t>(value["idle_exposures"]);
  return candidate;
}

BoundaryConfig parse_config(const py::dict& value) {
  BoundaryConfig config;
  config.seed = py::cast<std::int64_t>(value["seed"]);
  config.max_unique_evaluations =
      py::cast<std::size_t>(value["max_unique_evaluations"]);
  config.exact_coloring_threshold =
      py::cast<std::size_t>(value["exact_coloring_threshold"]);
  config.horizon_policy = py::cast<std::string>(value["horizon_policy"]);
  config.max_horizon = py::cast<std::size_t>(value["max_horizon"]);
  config.enforce_single_leg_ghost =
      py::cast<bool>(value["enforce_single_leg_ghost"]);
  if (config.horizon_policy != "fixed" && config.horizon_policy != "dynamic") {
    throw std::invalid_argument("unknown horizon policy");
  }
  return config;
}

py::dict result_to_dict(const FitnessResult& result) {
  py::dict value;
  value["chromosome"] = result.chromosome;
  value["feasible"] = result.feasible;
  value["negative_log_fidelity"] = result.negative_log_fidelity;
  value["transfer_nll"] = result.transfer_nll;
  value["idle_excitation_nll"] = result.idle_excitation_nll;
  value["coherence_nll"] = result.coherence_nll;
  value["move_batches"] = result.move_batches;
  value["move_time_us"] = result.move_time_us;
  value["total_distance_um"] = result.total_distance_um;
  value["idle_exposures"] = result.idle_exposures;
  value["transfers"] = result.transfers;
  value["phase_batches"] = result.phase_batches;
  value["error"] = result.error.empty() ? py::none() : py::cast(result.error);
  return value;
}

py::tuple result_to_flat_row(const FitnessResult& result) {
  py::tuple value(12);
  value[0] = result.feasible;
  value[1] = result.negative_log_fidelity;
  value[2] = result.transfer_nll;
  value[3] = result.idle_excitation_nll;
  value[4] = result.coherence_nll;
  value[5] = result.move_batches;
  value[6] = result.move_time_us;
  value[7] = result.total_distance_um;
  value[8] = result.idle_exposures;
  value[9] = result.transfers;
  value[10] = py::cast(result.phase_batches);
  value[11] = result.error.empty() ? py::none() : py::cast(result.error);
  return value;
}

std::vector<CandidatePlan> parse_candidates(const py::list& values) {
  std::vector<CandidatePlan> candidates;
  candidates.reserve(values.size());
  for (const auto& value : values) {
    candidates.push_back(parse_candidate(py::cast<py::dict>(value)));
  }
  return candidates;
}

}  // namespace

PYBIND11_MODULE(zac_native_core, module) {
  module.doc() = "Deterministic native resident-search kernels";
  module.attr("NATIVE_ABI_VERSION") = kNativeAbiVersion;
  module.attr("FLAT_WIRE_VERSION") = 1;
  module.attr("RICH_H0_WIRE_VERSION") = 1;
  module.attr("RICH_BOUNDARY_WIRE_VERSION") = 2;
  module.attr("RNG_VERSION") = "python-random-mt19937-v1";
  module.def("build_info", []() {
    py::dict value;
    value["native_abi_version"] = kNativeAbiVersion;
    value["flat_wire_version"] = 1;
    value["rich_h0_wire_version"] = 1;
    value["rich_boundary_wire_version"] = 3;
    value["version"] = VERSION_INFO;
    value["compiler_id"] = ZAC_CXX_COMPILER_ID;
    value["compiler_version"] = ZAC_CXX_COMPILER_VERSION;
    value["build_type"] = ZAC_BUILD_TYPE;
    value["cxx_standard"] = 17;
    value["openmp"] = false;
    value["fast_math"] = false;
    value["rng_version"] = "python-random-mt19937-v1";
    return value;
  });

  py::class_<ArchitectureSnapshot>(module, "ArchitectureSnapshot")
      .def(py::init([](std::size_t n_atoms,
                       const std::vector<std::vector<double>>& coordinates,
                       const std::vector<std::int64_t>& storage_site_ids,
                       const std::vector<std::array<std::int64_t, 2>>&
                           entangling_site_pairs) {
        std::vector<Point> points;
        points.reserve(coordinates.size());
        for (const auto& coordinate : coordinates) {
          if (coordinate.size() != 2) {
            throw std::invalid_argument("architecture point must have two values");
          }
          points.push_back({coordinate[0], coordinate[1]});
        }
        return ArchitectureSnapshot(
            n_atoms, std::move(points), storage_site_ids,
            entangling_site_pairs);
      }), py::arg("n_atoms"), py::arg("site_coordinates"),
          py::arg("storage_site_ids") = std::vector<std::int64_t>{},
          py::arg("entangling_site_pairs") =
              std::vector<std::array<std::int64_t, 2>>{})
      .def_property_readonly("n_atoms", &ArchitectureSnapshot::n_atoms)
      .def_property_readonly(
          "site_coordinates", [](const ArchitectureSnapshot& architecture) {
            std::vector<std::array<double, 2>> result;
            for (const auto& point : architecture.site_coordinates()) {
              result.push_back({point.x, point.y});
            }
            return result;
          })
      .def_property_readonly(
          "storage_site_ids", &ArchitectureSnapshot::storage_site_ids)
      .def_property_readonly(
          "entangling_site_pairs",
          &ArchitectureSnapshot::entangling_site_pairs);

  module.def("compatible_2d", &compatible_2d, py::arg("first"), py::arg("second"));
  module.def("ghost_hits", [](const py::list& legs, const py::list& ghosts) {
    std::vector<Leg> native_legs;
    std::vector<Ghost> native_ghosts;
    for (const auto& value : legs) native_legs.push_back(parse_leg(value));
    for (const auto& value : ghosts) native_ghosts.push_back(parse_ghost(value));
    return ghost_hit_atoms(native_legs, native_ghosts);
  });
  module.def("color_phase", [](const py::dict& phase, std::size_t threshold) {
    return color_phase(parse_phase(phase), threshold);
  }, py::arg("phase"), py::arg("exact_threshold") = 0);
  module.def("replay_phase_batches", [](const py::dict& phase,
                                         std::size_t threshold) {
    return replay_phase_batches_strict(parse_phase(phase), threshold);
  }, py::arg("phase"), py::arg("exact_threshold") = 0);
  module.def("replay_phase_batches_raw", [](const py::sequence& legs,
                                             const py::sequence& ghosts,
                                             const std::vector<std::int64_t>& owners,
                                             std::size_t threshold) {
    MovementPhase phase;
    phase.legs.reserve(legs.size());
    phase.ghosts.reserve(ghosts.size());
    for (const auto& value : legs) phase.legs.push_back(parse_leg(value));
    for (const auto& value : ghosts) phase.ghosts.push_back(parse_ghost(value));
    phase.owners = owners;
    phase.batching = "phase";
    return replay_phase_batches_strict(phase, threshold);
  }, py::arg("legs"), py::arg("ghosts"), py::arg("owners"),
     py::arg("exact_threshold") = 0);

  module.def("evaluate_many", [](const ArchitectureSnapshot& architecture,
                                  const py::list& values,
                                  const py::dict& config_value) {
    const auto config = parse_config(config_value);
    const auto candidates = parse_candidates(values);
    py::list result;
    {
      py::gil_scoped_release release;
      std::vector<FitnessResult> evaluated;
      evaluated.reserve(candidates.size());
      for (const auto& candidate : candidates) {
        evaluated.push_back(evaluate_candidate(architecture, candidate, config));
      }
      py::gil_scoped_acquire acquire;
      for (const auto& value : evaluated) result.append(result_to_dict(value));
    }
    return result;
  }, py::arg("architecture"), py::arg("candidates"), py::arg("config"));

  module.def("evaluate_many_flat", [](const ArchitectureSnapshot& architecture,
                                       const py::dict& buffers,
                                       const py::dict& config_value) {
    const auto parse_started = std::chrono::steady_clock::now();
    const auto config = parse_config(config_value);
    const auto candidates = parse_flat_candidates(buffers);
    const auto parse_stopped = std::chrono::steady_clock::now();
    const auto fitness_started = parse_stopped;
    std::vector<FitnessResult> evaluated;
    {
      py::gil_scoped_release release;
      evaluated.reserve(candidates.size());
      for (const auto& candidate : candidates) {
        evaluated.push_back(evaluate_candidate(architecture, candidate, config));
      }
    }
    const auto fitness_stopped = std::chrono::steady_clock::now();
    const auto serialize_started = fitness_stopped;
    py::list rows;
    for (const auto& result : evaluated) rows.append(result_to_flat_row(result));
    py::dict value;
    value["rows"] = rows;
    const auto serialize_stopped = std::chrono::steady_clock::now();
    value["native_parse_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        parse_stopped - parse_started).count();
    value["fitness_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        fitness_stopped - fitness_started).count();
    value["native_serialize_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        serialize_stopped - serialize_started).count();
    return value;
  }, py::arg("architecture"), py::arg("buffers"), py::arg("config"));

  module.def("solve_boundary", [](const ArchitectureSnapshot& architecture,
                                   const py::list& values,
                                   const py::dict& config_value) {
    const auto parse_started = std::chrono::steady_clock::now();
    const auto config = parse_config(config_value);
    auto candidates = parse_candidates(values);
    if (config.max_unique_evaluations != 0 &&
        candidates.size() > config.max_unique_evaluations) {
      candidates.resize(config.max_unique_evaluations);
    }
    if (candidates.empty()) throw std::runtime_error("no candidates to evaluate");
    const auto parse_stopped = std::chrono::steady_clock::now();
    const auto fitness_started = parse_stopped;
    std::vector<FitnessResult> evaluated;
    {
      py::gil_scoped_release release;
      evaluated.reserve(candidates.size());
      for (const auto& candidate : candidates) {
        evaluated.push_back(evaluate_candidate(architecture, candidate, config));
      }
    }
    const auto fitness_stopped = std::chrono::steady_clock::now();
    const auto selection_started = fitness_stopped;
    const FitnessResult* winner = nullptr;
    for (const auto& candidate : evaluated) {
      if (!candidate.feasible) continue;
      if (winner == nullptr || objective_less(candidate, *winner)) winner = &candidate;
    }
    if (winner == nullptr) throw std::runtime_error("no feasible candidate");
    const auto selection_stopped = std::chrono::steady_clock::now();
    const auto serialize_started = selection_stopped;
    py::list output;
    for (const auto& result : evaluated) output.append(result_to_dict(result));
    py::dict value;
    value["winner"] = result_to_dict(*winner);
    value["evaluated"] = output;
    value["evaluations"] = evaluated.size();
    value["unique_evaluations"] = evaluated.size();
    const auto serialize_stopped = std::chrono::steady_clock::now();
    value["native_parse_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        parse_stopped - parse_started).count();
    value["fitness_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        fitness_stopped - fitness_started).count();
    value["selection_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        selection_stopped - selection_started).count();
    value["native_serialize_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        serialize_stopped - serialize_started).count();
    value["search_kernel_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        selection_stopped - fitness_started).count();
    return value;
  }, py::arg("architecture"), py::arg("candidates"), py::arg("config"));

  module.def("solve_boundary_flat", [](const ArchitectureSnapshot& architecture,
                                        const py::dict& buffers,
                                        const py::dict& config_value) {
    const auto parse_started = std::chrono::steady_clock::now();
    const auto config = parse_config(config_value);
    const auto candidates = parse_flat_candidates(
        buffers, config.max_unique_evaluations);
    const auto parse_stopped = std::chrono::steady_clock::now();
    const auto fitness_started = parse_stopped;
    std::vector<FitnessResult> evaluated;
    {
      py::gil_scoped_release release;
      evaluated.reserve(candidates.size());
      for (const auto& candidate : candidates) {
        evaluated.push_back(evaluate_candidate(architecture, candidate, config));
      }
    }
    const auto fitness_stopped = std::chrono::steady_clock::now();
    const auto selection_started = fitness_stopped;
    std::size_t winner_index = evaluated.size();
    for (std::size_t index = 0; index < evaluated.size(); ++index) {
      if (!evaluated[index].feasible) continue;
      if (winner_index == evaluated.size() ||
          objective_less(evaluated[index], evaluated[winner_index])) {
        winner_index = index;
      }
    }
    if (winner_index == evaluated.size()) {
      throw std::runtime_error("no feasible candidate");
    }
    const auto selection_stopped = std::chrono::steady_clock::now();
    const auto serialize_started = selection_stopped;
    py::list rows;
    for (const auto& result : evaluated) rows.append(result_to_flat_row(result));
    py::dict value;
    value["winner_index"] = winner_index;
    value["rows"] = rows;
    value["evaluations"] = evaluated.size();
    value["unique_evaluations"] = evaluated.size();
    const auto serialize_stopped = std::chrono::steady_clock::now();
    value["native_parse_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        parse_stopped - parse_started).count();
    value["fitness_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        fitness_stopped - fitness_started).count();
    value["selection_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        selection_stopped - selection_started).count();
    value["native_serialize_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        serialize_stopped - serialize_started).count();
    value["search_kernel_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
        selection_stopped - fitness_started).count();
    return value;
  }, py::arg("architecture"), py::arg("buffers"), py::arg("config"));

  bind_rich_solver(module);
}
