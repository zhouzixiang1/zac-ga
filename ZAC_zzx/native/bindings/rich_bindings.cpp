#include "zac_native/rich_bindings.hpp"

#include "zac_native/rich_solver.hpp"

#include <pybind11/stl.h>

#include <chrono>
#include <cmath>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace py = pybind11;

namespace zac_native {
namespace {

template <typename T>
std::vector<T> copy_buffer(const py::dict& buffers, const char* name) {
  if (!buffers.contains(name)) {
    throw std::invalid_argument(std::string("rich payload missing ") + name);
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

void offsets(const std::vector<std::int64_t>& values, std::size_t count,
             std::size_t upper, const char* name) {
  if (values.size() != count + 1 || values.empty() || values.front() != 0 ||
      values.back() < 0 || static_cast<std::size_t>(values.back()) != upper) {
    throw std::invalid_argument(std::string(name) + " has invalid bounds");
  }
  for (std::size_t index = 1; index < values.size(); ++index) {
    if (values[index] < values[index - 1]) {
      throw std::invalid_argument(std::string(name) + " is not monotonic");
    }
  }
}

std::size_t index_of(std::int64_t value, std::size_t upper, const char* name) {
  if (value < 0 || static_cast<std::size_t>(value) > upper) {
    throw std::invalid_argument(std::string(name) + " offset out of range");
  }
  return static_cast<std::size_t>(value);
}

RichH0Problem parse_problem(const ArchitectureSnapshot& architecture,
                            const py::dict& buffers) {
  RichH0Problem problem;
  problem.n_atoms = architecture.n_atoms();
  problem.prior_idle_time_us =
      copy_buffer<double>(buffers, "prior_idle_time_us");
  if (problem.prior_idle_time_us.empty()) {
    // Non-formal/source compatibility only.  Registered ABI7 calls must send
    // one accumulated coherence-idle value per atom.
    problem.prior_idle_time_us.assign(problem.n_atoms, 0.0);
  } else if (problem.prior_idle_time_us.size() != problem.n_atoms) {
    throw std::invalid_argument(
        "prior_idle_time_us must contain every atom or be empty");
  }
  for (const auto value : problem.prior_idle_time_us) {
    if (!std::isfinite(value) || value < 0.0) {
      throw std::invalid_argument(
          "prior_idle_time_us must be finite and non-negative");
    }
  }
  const auto scheduler_trace_end =
      copy_buffer<double>(buffers, "scheduler_trace_end_us");
  const auto scheduler_one_qubit_end =
      copy_buffer<double>(buffers, "scheduler_one_qubit_end_us");
  if (scheduler_trace_end.size() != 1 ||
      scheduler_one_qubit_end.size() != 1) {
    throw std::invalid_argument(
        "scheduler scalar clocks must each contain one value");
  }
  problem.scheduler_trace_end_us = scheduler_trace_end[0];
  problem.scheduler_active_union_us =
      copy_buffer<double>(buffers, "scheduler_active_union_us");
  problem.scheduler_aod_end_us =
      copy_buffer<double>(buffers, "scheduler_aod_end_us");
  problem.scheduler_one_qubit_end_us = scheduler_one_qubit_end[0];
  problem.scheduler_rydberg_end_us =
      copy_buffer<double>(buffers, "scheduler_rydberg_end_us");
  problem.scheduler_qubit_dependency_end_us =
      copy_buffer<double>(buffers, "scheduler_qubit_dependency_end_us");
  problem.scheduler_back_dependency_end_us =
      copy_buffer<double>(buffers, "scheduler_back_dependency_end_us");
  problem.scheduler_site_dependency_site_ids = copy_buffer<std::int64_t>(
      buffers, "scheduler_site_dependency_site_ids");
  problem.scheduler_site_dependency_activation_finish_us = copy_buffer<double>(
      buffers, "scheduler_site_dependency_activation_finish_us");
  problem.target_one_qubit_atoms =
      copy_buffer<std::int64_t>(buffers, "target_one_qubit_atoms");
  const auto physical_constants =
      copy_buffer<double>(buffers, "scheduler_physical_constants");
  if (physical_constants.size() != 6) {
    throw std::invalid_argument(
        "scheduler_physical_constants must contain six values");
  }
  problem.scheduler_one_qubit_duration_us = physical_constants[0];
  problem.scheduler_rydberg_duration_us = physical_constants[1];
  problem.scheduler_one_qubit_common_us = physical_constants[2];
  problem.scheduler_transfer_duration_us = physical_constants[3];
  problem.scheduler_accel_um_per_us2 = physical_constants[4];
  problem.coherence_t2_us = physical_constants[5];
  const auto physical_contract =
      copy_buffer<std::uint8_t>(buffers, "scheduler_physical_contract");
  if (physical_contract.size() != 1 || physical_contract[0] > 1) {
    throw std::invalid_argument("invalid scheduler physical contract flag");
  }
  problem.enforce_frozen_physical_model = physical_contract[0] != 0;
  problem.exact_current_scheduler =
      !problem.scheduler_active_union_us.empty();
  const auto geometry_mode = copy_buffer<std::uint8_t>(buffers, "geometry_mode");
  if (geometry_mode.size() != 1 || geometry_mode[0] > 1) {
    throw std::invalid_argument("invalid rich geometry mode");
  }
  const bool indexed_geometry = geometry_mode[0] == 1;
  const auto current_xy = copy_buffer<double>(buffers, "current_xy");
  const auto current_site_ids =
      copy_buffer<std::int64_t>(buffers, "current_site_ids");
  if (indexed_geometry) {
    if (!current_xy.empty() || current_site_ids.size() != problem.n_atoms) {
      throw std::invalid_argument("indexed current geometry has invalid shape");
    }
    problem.current_site_ids = current_site_ids;
    for (const auto site_id : current_site_ids) {
      if (site_id < 0 || static_cast<std::size_t>(site_id) >=
                             architecture.site_coordinates().size()) {
        throw std::invalid_argument("current site id is outside architecture");
      }
      problem.current_points.push_back(
          architecture.site_coordinates()[static_cast<std::size_t>(site_id)]);
    }
  } else {
    if (!current_site_ids.empty() || current_xy.size() != problem.n_atoms * 2) {
      throw std::invalid_argument("current_xy must contain every atom");
    }
    for (std::size_t atom = 0; atom < problem.n_atoms; ++atom) {
      problem.current_points.push_back(
          {current_xy[atom * 2], current_xy[atom * 2 + 1]});
    }
  }
  problem.participants = copy_buffer<std::int64_t>(buffers, "participants");
  const auto ghost_atoms =
      copy_buffer<std::int64_t>(buffers, "static_ghost_atoms");
  const auto ghost_xy = copy_buffer<double>(buffers, "static_ghost_xy");
  if ((!indexed_geometry && ghost_xy.size() != ghost_atoms.size() * 2) ||
      (indexed_geometry && !ghost_xy.empty())) {
    throw std::invalid_argument("static ghost arrays differ in length");
  }
  for (std::size_t index = 0; index < ghost_atoms.size(); ++index) {
    const auto atom = ghost_atoms[index];
    if (atom < 0 || static_cast<std::size_t>(atom) >= problem.n_atoms) {
      throw std::invalid_argument("static ghost atom is outside architecture");
    }
    problem.static_ghosts.push_back({
        atom,
        indexed_geometry
            ? problem.current_points[static_cast<std::size_t>(atom)]
            : Point{ghost_xy[index * 2], ghost_xy[index * 2 + 1]},
    });
  }
  problem.eligible = copy_buffer<std::int64_t>(buffers, "eligible");
  const auto minimum = copy_buffer<std::int64_t>(buffers, "min_returns");
  if (minimum.size() != 1 || minimum[0] < 0) {
    throw std::invalid_argument("min_returns must be one non-negative integer");
  }
  problem.min_returns = static_cast<std::size_t>(minimum[0]);
  const auto eviction =
      copy_buffer<std::int64_t>(buffers, "eviction_order_indices");
  for (const auto value : eviction) {
    problem.eviction_order_indices.push_back(index_of(
        value, problem.eligible.size(), "eviction_order_indices"));
  }
  const auto forced = copy_buffer<std::uint8_t>(buffers, "forced_return_mask");
  for (const auto value : forced) {
    if (value > 1) throw std::invalid_argument("forced return mask is not boolean");
    problem.forced_return_mask.push_back(value != 0);
  }
  const auto recommended =
      copy_buffer<std::uint8_t>(buffers, "recommended_return_mask");
  for (const auto value : recommended) {
    if (value > 1) {
      throw std::invalid_argument("recommended return mask is not boolean");
    }
    problem.recommended_return_mask.push_back(value != 0);
  }
  const auto recommended_stay =
      copy_buffer<std::uint8_t>(buffers, "recommended_stay_mask");
  for (const auto value : recommended_stay) {
    if (value > 1) {
      throw std::invalid_argument("recommended stay mask is not boolean");
    }
    problem.recommended_stay_mask.push_back(value != 0);
  }
  const auto policy = copy_buffer<std::uint8_t>(buffers, "decision_policy");
  if (policy.size() != 1 || policy[0] > 3) {
    throw std::invalid_argument("invalid rich decision policy");
  }
  problem.decision_policy = static_cast<RichDecisionPolicy>(policy[0]);
  problem.matched_gate_genes =
      copy_buffer<std::int64_t>(buffers, "matched_gate_genes");
  problem.occupied_storage_site_ids =
      copy_buffer<std::int64_t>(buffers, "occupied_storage_site_ids");

  const auto gate_offsets =
      copy_buffer<std::int64_t>(buffers, "gate_option_offsets");
  if (gate_offsets.empty()) throw std::invalid_argument("missing gate offsets");
  const auto gate_count = gate_offsets.size() - 1;
  const auto gate_site_ids = copy_buffer<std::int64_t>(buffers, "gate_site_ids");
  const auto gate_q1 = copy_buffer<std::int64_t>(buffers, "gate_q1");
  const auto gate_q2 = copy_buffer<std::int64_t>(buffers, "gate_q2");
  const auto gate_targets = copy_buffer<double>(buffers, "gate_targets");
  const auto gate_target_site_ids =
      copy_buffer<std::int64_t>(buffers, "gate_target_site_ids");
  const auto option_count = gate_site_ids.size();
  if (gate_q1.size() != option_count || gate_q2.size() != option_count ||
      (!indexed_geometry &&
       (gate_targets.size() != option_count * 4 ||
        !gate_target_site_ids.empty())) ||
      (indexed_geometry &&
       (!gate_targets.empty() || gate_target_site_ids.size() != option_count * 2))) {
    throw std::invalid_argument("rich gate option columns differ in length");
  }
  offsets(gate_offsets, gate_count, option_count, "gate_option_offsets");
  const auto leg_offsets =
      copy_buffer<std::int64_t>(buffers, "gate_leg_offsets");
  const auto leg_values = copy_buffer<double>(buffers, "gate_leg_values");
  if (leg_values.size() % 5 != 0) throw std::invalid_argument("bad rich leg values");
  offsets(leg_offsets, option_count, leg_values.size() / 5, "gate_leg_offsets");
  const auto owner_offsets =
      copy_buffer<std::int64_t>(buffers, "gate_owner_offsets");
  const auto owners = copy_buffer<std::int64_t>(buffers, "gate_owners");
  offsets(owner_offsets, option_count, owners.size(), "gate_owner_offsets");
  const auto seated_offsets =
      copy_buffer<std::int64_t>(buffers, "gate_seated_offsets");
  const auto seated_atoms =
      copy_buffer<std::int64_t>(buffers, "gate_seated_atoms");
  const auto seated_xy = copy_buffer<double>(buffers, "gate_seated_xy");
  if (seated_xy.size() != seated_atoms.size() * 2) {
    throw std::invalid_argument("bad seated ghost values");
  }
  offsets(seated_offsets, option_count, seated_atoms.size(), "gate_seated_offsets");
  problem.gate_domains.resize(gate_count);
  for (std::size_t gate = 0; gate < gate_count; ++gate) {
    const auto begin = index_of(gate_offsets[gate], option_count, "gate offsets");
    const auto end = index_of(gate_offsets[gate + 1], option_count, "gate offsets");
    for (auto option_index = begin; option_index < end; ++option_index) {
      RichGateOption option;
      option.site_id = gate_site_ids[option_index];
      option.q1 = gate_q1[option_index];
      option.q2 = gate_q2[option_index];
      if (indexed_geometry) {
        const auto first = gate_target_site_ids[option_index * 2];
        const auto second = gate_target_site_ids[option_index * 2 + 1];
        if (first < 0 || second < 0 ||
            static_cast<std::size_t>(first) >=
                architecture.site_coordinates().size() ||
            static_cast<std::size_t>(second) >=
                architecture.site_coordinates().size()) {
          throw std::invalid_argument("gate target id is outside architecture");
        }
        option.target1 = architecture.site_coordinates()[
            static_cast<std::size_t>(first)];
        option.target2 = architecture.site_coordinates()[
            static_cast<std::size_t>(second)];
        option.target1_site_id = first;
        option.target2_site_id = second;
      } else {
        option.target1 = {gate_targets[option_index * 4],
                          gate_targets[option_index * 4 + 1]};
        option.target2 = {gate_targets[option_index * 4 + 2],
                          gate_targets[option_index * 4 + 3]};
      }
      const auto leg_begin = index_of(leg_offsets[option_index],
                                      leg_values.size() / 5, "leg offsets");
      const auto leg_end = index_of(leg_offsets[option_index + 1],
                                    leg_values.size() / 5, "leg offsets");
      for (auto leg = leg_begin; leg < leg_end; ++leg) {
        const auto base = leg * 5;
        option.legs.push_back({leg_values[base],
                               {leg_values[base + 1], leg_values[base + 2]},
                               {leg_values[base + 3], leg_values[base + 4]}});
      }
      const auto owner_begin = index_of(owner_offsets[option_index], owners.size(),
                                        "owner offsets");
      const auto owner_end = index_of(owner_offsets[option_index + 1], owners.size(),
                                      "owner offsets");
      option.owners.assign(owners.begin() + owner_begin, owners.begin() + owner_end);
      const auto seated_begin = index_of(seated_offsets[option_index],
                                         seated_atoms.size(), "seated offsets");
      const auto seated_end = index_of(seated_offsets[option_index + 1],
                                       seated_atoms.size(), "seated offsets");
      for (auto seated = seated_begin; seated < seated_end; ++seated) {
        option.seated_ghosts.push_back(
            {seated_atoms[seated],
             {seated_xy[seated * 2], seated_xy[seated * 2 + 1]}});
      }
      if (indexed_geometry) {
        for (const auto& [atom, target] :
             {std::pair<std::int64_t, Point>{option.q1, option.target1},
              std::pair<std::int64_t, Point>{option.q2, option.target2}}) {
          if (atom < 0 || static_cast<std::size_t>(atom) >= problem.n_atoms) {
            throw std::invalid_argument("gate participant is outside architecture");
          }
          const auto& source =
              problem.current_points[static_cast<std::size_t>(atom)];
          const auto distance = std::hypot(source.x - target.x,
                                           source.y - target.y);
          if (distance > 1e-9) {
            option.legs.push_back({distance, source, target});
            option.owners.push_back(atom);
          } else {
            option.seated_ghosts.push_back({atom, target});
          }
        }
      }
      problem.gate_domains[gate].push_back(std::move(option));
    }
  }

  const auto return_offsets =
      copy_buffer<std::int64_t>(buffers, "return_option_offsets");
  const auto return_site_ids =
      copy_buffer<std::int64_t>(buffers, "return_site_ids");
  const auto return_costs = copy_buffer<double>(buffers, "return_costs");
  const auto return_xy = copy_buffer<double>(buffers, "return_xy");
  const auto return_count = return_site_ids.size();
  if (return_costs.size() != return_count ||
      (!indexed_geometry && return_xy.size() != return_count * 2) ||
      (indexed_geometry && !return_xy.empty())) {
    throw std::invalid_argument("rich RETURN option columns differ in length");
  }
  offsets(return_offsets, problem.eligible.size(), return_count,
          "return_option_offsets");
  problem.return_domains.resize(problem.eligible.size());
  for (std::size_t eligible = 0; eligible < problem.eligible.size(); ++eligible) {
    const auto begin = index_of(return_offsets[eligible], return_count,
                                "return offsets");
    const auto end = index_of(return_offsets[eligible + 1], return_count,
                              "return offsets");
    for (auto index = begin; index < end; ++index) {
      Point point;
      if (indexed_geometry) {
        const auto site_id = return_site_ids[index];
        if (site_id < 0 || static_cast<std::size_t>(site_id) >=
                               architecture.site_coordinates().size()) {
          throw std::invalid_argument("RETURN site id is outside architecture");
        }
        point = architecture.site_coordinates()[static_cast<std::size_t>(site_id)];
      } else {
        point = {return_xy[index * 2], return_xy[index * 2 + 1]};
      }
      problem.return_domains[eligible].push_back(
          {return_site_ids[index], point, return_costs[index]});
    }
  }
  const auto forecast_depths =
      copy_buffer<std::int64_t>(buffers, "forecast_depths");
  const auto forecast_kinds =
      copy_buffer<std::uint8_t>(buffers, "forecast_kinds");
  const auto forecast_categories =
      copy_buffer<std::uint8_t>(buffers, "forecast_categories");
  const auto forecast_indices =
      copy_buffer<std::int64_t>(buffers, "forecast_indices");
  const auto forecast_second_indices =
      copy_buffer<std::int64_t>(buffers, "forecast_second_indices");
  const auto forecast_selectors =
      copy_buffer<std::int64_t>(buffers, "forecast_selectors");
  const auto forecast_nll = copy_buffer<double>(buffers, "forecast_nll");
  const auto forecast_count = forecast_depths.size();
  if (forecast_kinds.size() != forecast_count ||
      forecast_categories.size() != forecast_count ||
      forecast_indices.size() != forecast_count ||
      forecast_second_indices.size() != forecast_count ||
      forecast_selectors.size() != forecast_count ||
      forecast_nll.size() != forecast_count) {
    throw std::invalid_argument("forecast columns differ in length");
  }
  for (std::size_t index = 0; index < forecast_count; ++index) {
    if (forecast_depths[index] < 0 || forecast_kinds[index] > 6 ||
        forecast_categories[index] > 3) {
      throw std::invalid_argument("invalid forecast term code");
    }
    problem.forecast_terms.push_back({
        static_cast<std::size_t>(forecast_depths[index]),
        static_cast<RichForecastKind>(forecast_kinds[index]),
        static_cast<RichForecastCategory>(forecast_categories[index]),
        forecast_indices[index],
        forecast_second_indices[index],
        forecast_selectors[index],
        forecast_nll[index],
    });
  }
  const auto future_depths =
      copy_buffer<std::int64_t>(buffers, "future_layer_depths");
  const auto future_offsets =
      copy_buffer<std::int64_t>(buffers, "future_layer_gate_offsets");
  const auto future_atoms =
      copy_buffer<std::int64_t>(buffers, "future_gate_atoms");
  if (future_atoms.size() % 2 != 0) {
    throw std::invalid_argument("future gate atom column has odd length");
  }
  offsets(future_offsets, future_depths.size(), future_atoms.size() / 2,
          "future_layer_gate_offsets");
  for (std::size_t layer = 0; layer < future_depths.size(); ++layer) {
    if (future_depths[layer] <= 0) {
      throw std::invalid_argument("future layer depth must be positive");
    }
    RichFutureLayer future;
    future.depth = static_cast<std::size_t>(future_depths[layer]);
    const auto begin = index_of(future_offsets[layer], future_atoms.size() / 2,
                                "future layer offsets");
    const auto end = index_of(future_offsets[layer + 1],
                              future_atoms.size() / 2,
                              "future layer offsets");
    for (auto gate = begin; gate < end; ++gate) {
      future.gates.emplace_back(future_atoms[gate * 2],
                                future_atoms[gate * 2 + 1]);
    }
    problem.future_layers.push_back(std::move(future));
  }
  const auto terminal_boundary =
      copy_buffer<std::uint8_t>(buffers, "terminal_boundary");
  if (terminal_boundary.size() != 1 || terminal_boundary[0] > 1) {
    throw std::invalid_argument("invalid terminal boundary flag");
  }
  problem.terminal_boundary = terminal_boundary[0] != 0;
  return problem;
}

RichSearchConfig parse_search_config(const py::dict& value) {
  RichSearchConfig config;
  if (!value.contains("operator_profile")) {
    throw std::invalid_argument("rich operator_profile must be explicit");
  }
  const auto profile = py::cast<std::string>(value["operator_profile"]);
  if (profile == "exact") {
    config.operator_profile = RichOperatorProfile::kExact;
  } else if (profile == "tuned") {
    config.operator_profile = RichOperatorProfile::kTuned;
  } else {
    throw std::invalid_argument("rich operator_profile must be exact or tuned");
  }
  config.population_size = py::cast<std::size_t>(value["population_size"]);
  config.iterations = py::cast<std::size_t>(value["iterations"]);
  config.neighbors_per_solution =
      py::cast<std::size_t>(value["neighbors_per_solution"]);
  config.neighbor_sample_size =
      py::cast<std::size_t>(value["neighbor_sample_size"]);
  config.elite_count = py::cast<std::size_t>(value["elite_count"]);
  config.early_stop_patience =
      py::cast<std::size_t>(value["early_stop_patience"]);
  config.max_unique_evaluations =
      py::cast<std::size_t>(value["max_unique_evaluations"]);
  config.direct_enumeration_limit =
      py::cast<std::size_t>(value["direct_enumeration_limit"]);
  config.crossover_rate = py::cast<double>(value["crossover_rate"]);
  config.local_polish_sweeps =
      py::cast<std::size_t>(value["local_polish_sweeps"]);
  config.return_candidate_limit =
      py::cast<std::size_t>(value["return_candidate_limit"]);
  config.return_assignment_k =
      py::cast<std::size_t>(value["return_assignment_k"]);
  config.forecast_gate_candidate_budget =
      py::cast<std::size_t>(value["forecast_gate_candidate_budget"]);
  config.exact_coloring_threshold =
      py::cast<std::size_t>(value["exact_coloring_threshold"]);
  config.enforce_single_leg_ghost =
      py::cast<bool>(value["enforce_single_leg_ghost"]);
  config.fitness_cache = py::cast<bool>(value["fitness_cache"]);
  config.forecast_mode = py::cast<std::string>(value["forecast_mode"]);
  config.forecast_policy = py::cast<std::string>(value["forecast_policy"]);
  config.decay_kind = py::cast<std::string>(value["decay_kind"]);
  config.max_horizon = py::cast<std::size_t>(value["max_horizon"]);
  config.alpha_lookahead = py::cast<double>(value["alpha_lookahead"]);
  config.decay_rho = py::cast<double>(value["decay_rho"]);
  config.decay_epsilon = py::cast<double>(value["decay_epsilon"]);
  return config;
}

PythonRandomState parse_rng_state(const py::tuple& state) {
  if (state.size() != 3 || py::cast<int>(state[0]) != 3) {
    throw std::invalid_argument("expected Python random state version 3");
  }
  const auto inner = py::cast<py::tuple>(state[1]);
  if (inner.size() != 625) {
    throw std::invalid_argument("Python MT19937 state needs 625 integers");
  }
  PythonRandomState result;
  for (std::size_t index = 0; index < 624; ++index) {
    result.words[index] = py::cast<std::uint32_t>(inner[index]);
  }
  result.index = py::cast<std::size_t>(inner[624]);
  return result;
}

py::tuple rng_state_to_python(const PythonRandomState& state,
                              const py::handle& gaussian) {
  py::tuple inner(625);
  for (std::size_t index = 0; index < 624; ++index) inner[index] = state.words[index];
  inner[624] = state.index;
  py::tuple result(3);
  result[0] = 3;
  result[1] = inner;
  result[2] = gaussian;
  return result;
}

py::dict fitness_to_dict(const FitnessResult& result) {
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
  value["candidate_idle_time_us"] = result.candidate_idle_time_us;
  return value;
}

}  // namespace

void bind_rich_solver(py::module_& module) {
  module.def(
      "solve_rich_h0",
      [](const ArchitectureSnapshot& architecture, const py::dict& buffers,
         const py::dict& config_value, const py::tuple& rng_state_value,
         const py::object& cached_value) {
        const auto parse_started = std::chrono::steady_clock::now();
        const auto problem = parse_problem(architecture, buffers);
        const auto config = parse_search_config(config_value);
        const auto rng_state = parse_rng_state(rng_state_value);
        std::optional<std::vector<std::int64_t>> cached;
        if (!cached_value.is_none()) {
          cached = py::cast<std::vector<std::int64_t>>(cached_value);
        }
        const auto parse_stopped = std::chrono::steady_clock::now();
        RichSolveResult result;
        {
          py::gil_scoped_release release;
          result = solve_rich_h0(architecture, problem, config, rng_state, cached);
        }
        const auto serialize_started = std::chrono::steady_clock::now();
        py::dict value;
        value["winner"] = fitness_to_dict(result.winner);
        value["gate_option_indices"] = result.gate_option_indices;
        value["return_assignments"] = result.return_assignments;
        value["reseat_assignments"] = result.reseat_assignments;
        value["participant_parking_assignments"] =
            result.participant_parking_assignments;
        value["rng_state"] = rng_state_to_python(result.rng_state, rng_state_value[2]);
        value["search_mode"] = result.search_mode;
        value["operator_profile"] =
            result.operator_profile == RichOperatorProfile::kExact
                ? "exact"
                : "tuned";
        py::dict stats;
        stats["evaluations"] = result.stats.evaluations;
        stats["unique_evaluations"] = result.stats.unique_evaluations;
        stats["deterministic_unique_evaluations"] =
            result.stats.deterministic_unique_evaluations;
        stats["stochastic_unique_evaluations"] =
            result.stats.stochastic_unique_evaluations;
        stats["fitness_hits"] = result.stats.fitness_hits;
        stats["decode_hits"] = result.stats.decode_hits;
        stats["return_match_hits"] = result.stats.return_match_hits;
        stats["generations"] = result.stats.generations;
        stats["iterations_completed"] = result.stats.generations;
        stats["early_stopped"] = result.stats.early_stopped;
        stats["early_stop_reason"] = result.stats.early_stop_reason;
        stats["stochastic_budget"] = result.stats.stochastic_budget;
        py::dict operator_stats;
        operator_stats["gate_mutations"] = result.stats.gate_mutations;
        operator_stats["residency_mutations"] = result.stats.residency_mutations;
        operator_stats["high_cost_gate_reselections"] =
            result.stats.high_cost_gate_reselections;
        operator_stats["conflict_cluster_swaps"] =
            result.stats.conflict_cluster_swaps;
        operator_stats["marginal_return_flips"] =
            result.stats.marginal_return_flips;
        operator_stats["cached_winner_elites"] =
            result.stats.cached_winner_elites;
        operator_stats["crossovers"] = result.stats.crossovers;
        operator_stats["local_polish_evaluations"] =
            result.stats.local_polish_evaluations;
        operator_stats["direct_lower_bound_prunes"] =
            result.stats.direct_lower_bound_prunes;
        operator_stats["forecast_state_cache_hits"] =
            result.stats.forecast_state_cache_hits;
        stats["operator_stats"] = operator_stats;
        stats["return_assignment_evaluated"] =
            result.stats.return_assignment_evaluated;
        stats["current_ghost_rejections"] =
            result.stats.current_ghost_rejections;
        stats["pre_score_reseats"] = result.stats.pre_score_reseats;
        stats["pre_score_participant_parkings"] =
            result.stats.pre_score_participant_parkings;
        stats["forecast_terms_applied"] =
            result.stats.forecast_terms_applied;
        stats["forecast_terms_skipped_cutoff"] =
            result.stats.forecast_terms_skipped_cutoff;
        value["stats"] = stats;
        value["forecast_nll"] = result.forecast_nll;
        value["search_negative_log_fidelity"] =
            result.search_negative_log_fidelity;
        value["forecast_by_depth"] = result.forecast_by_depth;
        py::dict forecast_breakdown;
        forecast_breakdown["residency"] = result.forecast_residency_nll;
        forecast_breakdown["reentry"] = result.forecast_reentry_nll;
        forecast_breakdown["terminal"] = result.forecast_terminal_nll;
        forecast_breakdown["routing"] = result.forecast_routing_nll;
        value["forecast_breakdown"] = forecast_breakdown;
        value["return_assignment_rank"] = result.return_assignment_rank;
        value["return_assignment_evaluated"] =
            result.return_assignment_evaluated;
        value["current_ghost_rejections"] =
            result.current_ghost_rejections;
        value["future_ghost_cost"] = result.forecast_routing_nll;
        value["pre_score_reseats"] = result.pre_score_reseats;
        value["pre_score_participant_parkings"] =
            result.pre_score_participant_parkings;
        value["current_gate_anchor"] = result.current_gate_anchor;
        value["current_gate_anchor_assignment_site_ids"] =
            result.current_gate_anchor_assignment_site_ids;
        value["current_gate_final_assignment_site_ids"] =
            result.current_gate_final_assignment_site_ids;
        value["current_gate_guard_branch"] =
            result.current_gate_guard_branch;
        value["current_gate_guard_cohort_size"] =
            result.current_gate_guard_cohort_size;
        value["current_gate_guard_admitted_size"] =
            result.current_gate_guard_admitted_size;
        value["current_gate_projection_source"] =
            result.current_gate_projection_source;
        value["current_gate_projection_evaluated"] =
            result.current_gate_projection_evaluated;
        py::dict timing;
        timing["normalize_ns"] = result.normalize_ns;
        timing["decode_ns"] = result.decode_ns;
        timing["return_match_ns"] = result.return_match_ns;
        timing["fitness_ns"] = result.fitness_ns;
        timing["forecast_ns"] = result.forecast_ns;
        timing["selection_ns"] = result.selection_ns;
        timing["search_ns"] = result.search_ns;
        timing["native_parse_ns"] =
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                parse_stopped - parse_started).count();
        const auto serialize_stopped = std::chrono::steady_clock::now();
        timing["native_serialize_ns"] =
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                serialize_stopped - serialize_started).count();
        value["timing"] = timing;
        return value;
      },
      py::arg("architecture"), py::arg("buffers"), py::arg("config"),
      py::arg("rng_state"), py::arg("cached_winner") = py::none());
  module.attr("solve_rich_boundary") = module.attr("solve_rich_h0");

  module.def("python_random_probe", [](const py::tuple& state_value,
                                       const py::list& operations) {
    auto state = parse_rng_state(state_value);
    PythonRandom rng(state);
    py::list values;
    for (const auto& operation_value : operations) {
      const auto operation = py::cast<py::tuple>(operation_value);
      const auto name = py::cast<std::string>(operation[0]);
      if (name == "random") {
        values.append(rng.random());
      } else if (name == "randbelow") {
        values.append(rng.randbelow(py::cast<std::size_t>(operation[1])));
      } else if (name == "getrandbits") {
        values.append(rng.getrandbits(py::cast<std::size_t>(operation[1])));
      } else {
        throw std::invalid_argument("unknown RNG probe operation");
      }
    }
    py::dict result;
    result["values"] = values;
    result["state"] = rng_state_to_python(rng.state(), state_value[2]);
    return result;
  });
}

}  // namespace zac_native
