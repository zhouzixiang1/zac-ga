"""GA-LK initial-layout selection using production-physical prefix replay.

The original SA layout is generated once by the caller. All horizons use the
same finite permutation pool. H=0 scores the first 2Q layer; H>0 scores that
layer and H following layers. Candidate decisions always have internal H=0.
The actual circuit length is retained and a prefix stop is not a terminal
RETURN. No result from a complete circuit is used to select the initializer.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import random
import time
from typing import Any, Mapping, Sequence

import numpy as np

from evaluation import normalize_zair, score_trace, validate_trace_physics
from streaming.resident_transition import ResidentTransitionKernel
from streaming.zac_route_transition import ZACRouteTransitionDriver
from zzx.algorithm_v2 import (ForecastLayerProvider, decay_lookahead_spec,
                              maximum_lookahead_horizon)
from zzx.zplacer import ResidentPlacer


POLICY_ID = "physical-prefix-initial-v1"


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class InitialLookaheadConfig:
    horizon: int = 2
    candidates: int = 4
    seed: int = 0
    rho: float = 0.7
    rollout_evaluations: int = 32

    def __post_init__(self):
        for name, lower, upper in (("horizon", 0, 8), ("candidates", 1, 32),
                                   ("seed", 0, 2**63 - 1),
                                   ("rollout_evaluations", 1, 4096)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"initial_lookahead.{name} must be an integer in [{lower}, {upper}]")
        if (isinstance(self.rho, bool) or not isinstance(self.rho, (int, float))
                or not math.isfinite(self.rho) or not 0 < self.rho <= 1):
            raise ValueError("initial_lookahead.rho must be finite and in (0, 1]")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None, *, seed: int = 0):
        if value is not None and not isinstance(value, Mapping):
            raise ValueError("initial_lookahead must be an object")
        values = dict(value or {})
        unknown = set(values) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown initial_lookahead controls: {sorted(unknown)}")
        values.setdefault("seed", seed)
        return cls(**values)


def is_standard_ga_lk(setting: Mapping[str, Any]) -> bool:
    """Identify the public GA-LK path, not GA-NL or an H=0 rollout."""
    return (
        setting.get("experiment_schema") == 2
        and setting.get("method_id") == "ours_lk"
        and setting.get("placer") == "resident"
        and setting.get("engine") == "ga"
        and setting.get("init_engine", "sa") == "sa"
        and setting.get("search_policy", "ga") == "ga"
        and maximum_lookahead_horizon(setting.get("lookahead_horizon")) > 0
    )


def resolve_initial_setting(setting: Mapping[str, Any], *, historical_contract: bool = False) -> dict:
    """Resolve the current default without changing caller-owned settings.

    ABI8 and private paper-ablation contracts name historical experiments;
    their omitted initializer remains SA. New public GA-LK settings (including
    the ABI9 runtime) use the physical prefix. Explicit ``legacy`` always wins.
    Explicit ``physical_prefix`` remains available to controlled H=0 studies.
    """
    result = deepcopy(dict(setting))
    if ("init_strategy" not in result and not historical_contract
            and result.get("native_abi_version") != 8 and is_standard_ga_lk(result)):
        result["init_strategy"] = "physical_prefix"
    strategy = result.get("init_strategy", "legacy")
    if not isinstance(strategy, str) or strategy not in {"legacy", "physical_prefix"}:
        raise ValueError("init_strategy must be legacy or physical_prefix")
    if strategy == "physical_prefix":
        if (result.get("placer") != "resident" or result.get("engine") != "ga"
                or result.get("experiment_schema") != 2
                or result.get("init_engine", "sa") != "sa"):
            raise ValueError("physical_prefix requires Schema-2 resident GA and the original SA initializer")
        config = InitialLookaheadConfig.from_mapping(
            result.get("initial_lookahead"), seed=result.get("seed", 0))
        result["initial_lookahead"] = asdict(config)
    elif "initial_lookahead" in result:
        raise ValueError("initial_lookahead requires init_strategy=physical_prefix")
    return result


def architecture_spec(architecture) -> dict:
    """Reconstruct the exact adapter geometry from the live architecture."""
    def slm_spec(index):
        slm = architecture.dict_SLM[index]
        return {"id": index, "r": slm.n_r, "c": slm.n_c,
                "site_seperation": list(slm.site_seperation), "location": list(slm.location)}
    return {
        "name": architecture.name,
        "arch_range": deepcopy(architecture.arch_range),
        "rydberg_range": deepcopy(architecture.rydberg_range),
        "operation_duration": {"rydberg": architecture.time_rydberg,
                               "1qGate": architecture.time_1qGate,
                               "atom_transfer": architecture.time_atom_transfer},
        "storage_zones": [{"slms": [slm_spec(i) for i in architecture.storage_zone]}],
        "entanglement_zones": [{"zone_id": architecture.dict_SLM[indices[0]].entanglement_id,
                                "slms": [slm_spec(i) for i in sorted(indices)]}
                               for indices in architecture.entanglement_zone],
        "aods": [{"id": a.idx, "r": a.n_r, "c": a.n_c, "site_seperation": a.site_seperation}
                 for a in architecture.dict_AOD.values()],
    }


def candidate_pool(base_mapping: Sequence[Sequence[int]], *, count: int, seed: int):
    """SA plus deterministic atom-seat swaps, independent of H and cost."""
    base = tuple(tuple(int(v) for v in site) for site in base_mapping)
    if not base or any(len(site) != 3 for site in base) or len(set(base)) != len(base):
        raise ValueError("initial mapping must be nonempty, width-three and injective")
    if count < 1:
        raise ValueError("candidate count must be positive")
    result = [base]
    rng = random.Random(seed)
    pairs = [(a, b) for a in range(len(base)) for b in range(a + 1, len(base))]
    rng.shuffle(pairs)
    for a, b in pairs:
        mapping = list(base)
        mapping[a], mapping[b] = mapping[b], mapping[a]
        result.append(tuple(mapping))
        if len(result) >= count:
            break
    return tuple(result[:count])


class PrefixLayerProvider(ForecastLayerProvider):
    """Preserve full depth metadata while rejecting unrequested ordered reads."""

    def __init__(self, schedule, one_qubit):
        self.schedule = tuple(tuple(tuple(g) for g in layer) for layer in schedule)
        self.one_qubit = tuple(tuple(tuple(g) for g in layer) for layer in one_qubit)
        counts = Counter(tuple(sorted(g)) for layer in self.schedule for g in layer)
        self.frozen_interaction_graph = tuple((a, b, n) for (a, b), n in sorted(counts.items()))
        self.allowed_layer = 0
        self.reads = []
        self.one_qubit_reads = []

    @property
    def layer_count(self):
        return len(self.schedule)

    def _check(self, layer):
        if not 0 <= layer < self.layer_count or layer > self.allowed_layer:
            raise RuntimeError(f"initializer attempted out-of-prefix layer {layer}; allowed {self.allowed_layer}")

    def read_layer(self, layer):
        self._check(layer)
        self.reads.append(layer)
        return self.schedule[layer]

    def one_qubit_for_stage(self, layer):
        self._check(layer)
        self.one_qubit_reads.append(layer)
        return self.one_qubit[layer] if layer < len(self.one_qubit) else ()


def rollout_params(params: Mapping[str, Any], config: InitialLookaheadConfig) -> dict:
    """Fixed current-only bounded policy; never mutate the final GA-LK config."""
    result = deepcopy(dict(params))
    for key in ("init_strategy", "initial_lookahead", "init_engine", "init_pop", "init_gens"):
        result.pop(key, None)
    result.update(experiment_schema=2, method_id="ours_nl", engine="ga",
                  objective="physical_log_fidelity", seed=config.seed,
                  lookahead_horizon=decay_lookahead_spec(0),
                  population_size=4, elite_count=1, iterations=2,
                  neighbors_per_solution=2, neighbor_sample_size=4,
                  early_stop_patience=1, max_unique_evaluations=config.rollout_evaluations,
                  direct_enumeration_limit=config.rollout_evaluations,
                  local_polish_sweeps=0, search_policy="ga",
                  m3_search_budget_policy="fixed", m3_pin_radius_policy="fixed")
    return result


def evaluate_mapping(architecture, mapping, schedule, *, one_qubit=(), leading_one_qubit=(),
                     params: Mapping[str, Any], config: InitialLookaheadConfig) -> dict:
    """Score cumulative after-gate prefixes, then discount their true increments."""
    started = time.perf_counter_ns()
    arch = deepcopy(architecture)
    spec = architecture_spec(arch)
    provider = PrefixLayerProvider(schedule, one_qubit)
    effective = rollout_params(params, config)
    placer = ResidentPlacer(list(mapping), **effective)
    kernel = ResidentTransitionKernel(placer, arch, mapping, provider,
                                     leading_one_qubit_gates=leading_one_qubit)
    driver = ZACRouteTransitionDriver(
        arch, mapping, initial_one_qubit_gates=leading_one_qubit,
        coloring_exact_threshold=int(effective.get("coloring_exact_threshold", 24)))
    emitted = list(deepcopy(driver.initial_instructions))
    rows = []
    previous_nll = 0.0
    weighted_nll = 0.0
    stage_count = min(len(schedule), config.horizon + 1)
    for layer in range(stage_count):
        current = kernel.initial_gate_mapping if layer == 0 else current
        gates = provider.read_layer(layer)
        one_q = provider.one_qubit_for_stage(layer)
        is_terminal = layer == len(schedule) - 1
        boundary = kernel.finish().boundary_mapping if is_terminal else current
        # A fork ends immediately after this gate. The live driver remains at
        # B_layer until the following transition is actually requested. Thus
        # H=0 and H>0 have byte-identical first-layer scores, with any RETURN
        # charged to the next layer's incremental cost, not an invented finish.
        fork = ZACRouteTransitionDriver.from_state(arch, mapping, driver.state_dict())
        routed = fork.route_layer(layer, driver.current_boundary, current, boundary,
                                  gates, tuple(range(len(gates))), one_q)
        prefix = emitted + list(deepcopy(routed.instructions))
        events = tuple(normalize_zair({"instructions": prefix}, architecture=spec))
        validation = validate_trace_physics(events, n_qubits=len(mapping))
        score = score_trace(events, n_qubits=len(mapping))
        if score.ood or score.log_fidelity is None:
            raise ValueError("initial candidate prefix is outside the linear fidelity domain")
        nll = -score.log_fidelity
        increment = nll - previous_nll
        weight = config.rho ** layer
        weighted_nll += weight * increment
        rows.append({"layer": layer, "cumulative_nll": nll, "increment_nll": increment,
                     "weight": weight, "weighted_increment_nll": weight * increment,
                     "score": score.to_dict(), "validation": validation,
                     "instruction_sha256": stable_hash(prefix),
                     "scheduler_timing_sha256": fork.scheduler.timing_sha256,
                     "terminal_boundary": is_terminal})
        previous_nll = nll
        if layer + 1 < stage_count:
            provider.allowed_layer = layer + 1
            transition = kernel.advance()
            actual = driver.route_layer(layer, driver.current_boundary, current,
                                        transition.boundary_mapping, gates,
                                        tuple(range(len(gates))), one_q)
            emitted.extend(deepcopy(actual.instructions))
            current = transition.target_gate_mapping
    if stage_count == 0:
        events = tuple(normalize_zair({"instructions": emitted}, architecture=spec))
        validate_trace_physics(events, n_qubits=len(mapping))
        score = score_trace(events, n_qubits=len(mapping))
        if score.log_fidelity is None:
            raise ValueError("initial candidate has invalid fidelity")
        weighted_nll = -score.log_fidelity
    if placer.forecast.horizon != 0:
        raise AssertionError("initializer rollout enabled internal lookahead")
    return {"status": "success", "weighted_nll": weighted_nll,
            "layers_scored": stage_count, "actual_total_layers": len(schedule),
            "ordered_layer_reads": provider.reads, "one_qubit_layer_reads": provider.one_qubit_reads,
            "internal_horizon": 0, "rollout_config_sha256": stable_hash(effective),
            "rows": rows, "evaluation_ns": time.perf_counter_ns() - started}


def select_initial_mapping(architecture, base_mapping, schedule, *, one_qubit=(),
                           leading_one_qubit=(), params: Mapping[str, Any],
                           config: InitialLookaheadConfig):
    started = time.perf_counter_ns()
    pool = candidate_pool(base_mapping, count=config.candidates, seed=config.seed)
    for mapping in pool:
        if any(site[0] not in architecture.storage_zone
               or not architecture.is_valid_SLM_position(*site) for site in mapping):
            raise ValueError("initial candidates must use valid storage SLM traps")
    records = []
    # The legacy router may use Python's module RNG. Preserve its state across
    # the entire preview; candidate generation and ResidentPlacer have local RNGs.
    rng_state = random.getstate()
    numpy_rng_state = np.random.get_state()
    try:
        for index, mapping in enumerate(pool):
            random.setstate(rng_state)
            np.random.set_state(numpy_rng_state)
            record = {"candidate": index, "mapping": mapping, "mapping_sha256": stable_hash(mapping)}
            try:
                record.update(evaluate_mapping(
                    architecture, mapping, schedule, one_qubit=one_qubit,
                    leading_one_qubit=leading_one_qubit, params=params, config=config))
            except (ValueError, RuntimeError, AssertionError) as error:
                record.update(status="failed", error=f"{type(error).__name__}: {error}")
            records.append(record)
    finally:
        random.setstate(rng_state)
        np.random.set_state(numpy_rng_state)
    report = {"policy_id": POLICY_ID, "config": asdict(config),
              "candidate_pool_sha256": stable_hash(pool), "candidate_count": len(pool),
              "dynamic_config_sha256": stable_hash(dict(params)),
              "rollout_config": rollout_params(params, config),
              "candidates": records, "selection_ns": time.perf_counter_ns() - started,
              "score_semantics": "sum rho^layer times after-gate cumulative NLL increments"}
    valid = [record for record in records if record["status"] == "success"]
    if not valid:
        error = RuntimeError("no feasible initial candidate; " + json.dumps(records, default=list))
        error.initial_lookahead_report = report
        raise error
    best = min(valid, key=lambda record: (record["weighted_nll"], record["candidate"]))
    report.update(status="success", selected_candidate=best["candidate"],
                  selected_mapping_sha256=best["mapping_sha256"],
                  failed_candidates=len(records) - len(valid))
    return list(pool[best["candidate"]]), report
