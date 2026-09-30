"""Opt-in permutation GA and controlled random search for physical initialization.

Both policies start from the same deterministic storage domain and population.
Only a decoded mapping crosses into the subsequent, independent dynamic search.
The legacy distance-based GA and SA-plus-fixed-pool selector remain unchanged.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import itertools
import math
from pathlib import Path
import random
import re
import time
from typing import Any, Mapping

import numpy as np

from evaluation import TraceValidationError, UnsupportedOperationError
from zzx.gainit import GAInitialPlacer
from zzx.initial_lookahead import (
    InfeasibleInitialMapping, InitialLookaheadConfig, architecture_spec,
    evaluate_mapping, rollout_params, stable_hash,
)
from zzx.native_backend import NativeBackendError


POLICY_ID = "physical-prefix-ga-initial-v1"
RANDOM_POLICY_ID = "physical-prefix-random-initial-v1"
DOMAIN_ID = "nearest-storage-row-fixed-seats-v1"
POPULATION_ID = "legacy-structured-seeds-and-perturbations-v1"


class InitialMappingInfeasible(RuntimeError):
    """Every uniquely evaluated mapping was physically infeasible; report attached."""


@dataclass(frozen=True)
class PhysicalInitialGAConfig:
    horizon: int = 2
    rho: float = .7
    rollout_evaluations: int = 32
    seed: int = 0
    search_policy: str = "ga"
    population_size: int = 8
    elite_count: int = 2
    crossover_probability: float = .25
    swap_probability: float = .4
    insert_probability: float = .4
    reverse_probability: float = .2
    max_unique_evaluations: int = 32
    max_generations: int = 6
    early_stop_patience: int = 3
    max_proposals: int = 320

    def __post_init__(self):
        bounds = (("horizon", 0, 8), ("rollout_evaluations", 1, 4096),
                  ("seed", 0, 2**63 - 1), ("population_size", 1, 4096),
                  ("elite_count", 1, 4096), ("max_unique_evaluations", 1, 65536),
                  ("max_generations", 0, 65536), ("early_stop_patience", 1, 65536),
                  ("max_proposals", 1, 1000000))
        for name, lower, upper in bounds:
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"initial_ga.{name} must be an integer in [{lower}, {upper}]")
        if self.elite_count > self.population_size:
            raise ValueError("initial_ga.elite_count cannot exceed population_size")
        if self.max_proposals < max(3, self.population_size):
            raise ValueError("initial_ga.max_proposals must cover the three seeds and initial population")
        for name in ("rho", "crossover_probability", "swap_probability",
                     "insert_probability", "reverse_probability"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError(f"initial_ga.{name} must be finite in [0, 1]")
        if self.rho == 0:
            raise ValueError("initial_ga.rho must be positive")
        if not math.isclose(self.swap_probability + self.insert_probability
                            + self.reverse_probability, 1., rel_tol=0, abs_tol=1e-12):
            raise ValueError("initial_ga mutation probabilities must sum to one")
        if self.search_policy not in ("ga", "random"):
            raise ValueError("initial_ga.search_policy must be ga or random")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None, *, seed: int = 0):
        if value is not None and not isinstance(value, Mapping):
            raise ValueError("initial_ga must be an object")
        values = dict(value or {})
        unknown = set(values) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown initial_ga controls: {sorted(unknown)}")
        values.setdefault("seed", seed)
        return cls(**values)

    def prefix_config(self):
        # The fixed-pool candidate count is not a GA control and is never used.
        return InitialLookaheadConfig(horizon=self.horizon, rho=self.rho,
                                      rollout_evaluations=self.rollout_evaluations,
                                      seed=self.seed)


def _rng(seed: int, role: str):
    return random.Random(int(stable_hash({"seed": seed, "role": role}), 16))


def enumerate_storage_seats(architecture, n_qubits: int):
    """Select n valid storage seats, independently ordering rows in each SLM.

    This preserves the old GA's single-SLM ordering where it is defined, but
    handles reverse row order, exact capacity and multiple nonconsecutive IDs.
    The occupied set is fixed thereafter; SA may instead move into empty traps.
    """
    if type(n_qubits) is not int or n_qubits < 1:
        raise ValueError("physical initialization requires a positive integer atom count")
    seats = []
    for slm_id in architecture.storage_zone:
        slm = architecture.dict_SLM[slm_id]
        first = architecture.nearest_entanglement_site_distance(slm_id, 0, 0)
        last = architecture.nearest_entanglement_site_distance(slm_id, slm.n_r - 1, 0)
        rows = range(slm.n_r) if first < last else range(slm.n_r - 1, -1, -1)
        for row in rows:
            for column in range(slm.n_c):
                site = (slm_id, row, column)
                if not architecture.is_valid_SLM_position(*site) or site in seats:
                    raise ValueError("storage domain contains an invalid or repeated trap")
                seats.append(site)
                if len(seats) == n_qubits:
                    return tuple(seats)
    raise ValueError("insufficient storage capacity for the initial mapping")


def _validate_chromosome(p, n):
    if len(p) != n or any(type(value) is not int for value in p) or sorted(p) != list(range(n)):
        raise ValueError("chromosome must be the permutation p[atom] = seat_index")


def order_crossover(first, second, rng):
    """OX on seat IDs: copy [a:b), then fill cyclically in the other order."""
    n = len(first)
    _validate_chromosome(first, n)
    _validate_chromosome(second, n)
    if n < 2:
        return tuple(first)
    a, b = sorted(rng.sample(range(n + 1), 2))
    child = [None] * n
    child[a:b] = first[a:b]
    used = set(first[a:b])
    remaining = [second[i % n] for i in range(b, b + n) if second[i % n] not in used]
    positions = [i % n for i in range(b, b + n) if child[i % n] is None]
    for index, value in zip(positions, remaining):
        child[index] = value
    _validate_chromosome(child, n)
    return tuple(child)


def mutate(p, rng, config: PhysicalInitialGAConfig):
    n = len(p)
    _validate_chromosome(p, n)
    if n < 2:
        return tuple(p)
    result = list(p)
    choice = rng.random()
    a, b = rng.sample(range(n), 2)
    if choice < config.swap_probability:
        result[a], result[b] = result[b], result[a]
    elif choice < config.swap_probability + config.insert_probability:
        result.insert(b, result.pop(a))
    else:
        a, b = sorted((a, b))
        result[a:b + 1] = reversed(result[a:b + 1])
    return tuple(result)


def _bounded_space_size(n, bound):
    """Return n! only while it can affect the bounded search."""
    result = 1
    for value in range(2, n + 1):
        result *= value
        if result > bound:
            return bound + 1
    return result


def build_initial_population(architecture, n_qubits, schedule,
                             config: PhysicalInitialGAConfig, *, generation_stats=None):
    """Return (seats, chromosomes); horizon and policy do not affect either.

    The three legacy heuristics use the full circuit to propose permutations.
    They do not provide fitness values: every seed is physically evaluated.
    """
    seats = enumerate_storage_seats(architecture, n_qubits)
    seed_builder = GAInitialPlacer({"seed": config.seed})
    weights = seed_builder._weight_matrix(n_qubits, schedule)
    raw_seeds = seed_builder._warm_starts(n_qubits, weights, schedule)
    seeds = tuple(dict.fromkeys(tuple(p) for p in raw_seeds))
    attempts = len(raw_seeds)
    target = min(config.population_size, _bounded_space_size(n_qubits, config.population_size))
    population = list(seeds[:target])
    seen = set(population)
    rng = _rng(config.seed, "initial-population-v1")
    # Bound retries even if structured perturbations repeatedly collide.
    # Reserve enough attempts for deterministic completion rather than allowing
    # duplicate perturbations to consume the entire shared proposal budget.
    for _ in range(max(0, config.max_proposals - attempts - target - len(seen))):
        if len(population) == target:
            break
        proposal = rng.choice(seeds)
        for _ in range(rng.randrange(1, max(2, n_qubits // 4))):
            proposal = mutate(proposal, rng, config)
        attempts += 1
        if proposal not in seen:
            population.append(proposal)
            seen.add(proposal)
    # Lexicographic completion is bounded by the already finite seen set:
    # at most len(seen)+target iterations are needed, even for a large n.
    if len(population) < target:
        for proposal in itertools.permutations(range(n_qubits)):
            if attempts >= config.max_proposals:
                raise ValueError("initial_ga.max_proposals cannot construct the requested population")
            attempts += 1
            if proposal not in seen:
                population.append(proposal)
                seen.add(proposal)
            if len(population) == target:
                break
    for p in population:
        _validate_chromosome(p, n_qubits)
    if generation_stats is not None:
        generation_stats["initial_generation_attempts"] = attempts
    return seats, tuple(population)


def _expected_infeasible(error):
    if isinstance(error, UnsupportedOperationError):
        return False
    if isinstance(error, (InfeasibleInitialMapping, TraceValidationError)):
        return True
    if (type(error) is ValueError
            and str(error) == "phase has no ghost-safe straight-leg batch order"):
        return True
    # These backends predate typed infeasibility. Match only their documented
    # terminal no-candidate errors; ABI/configuration/runtime errors propagate.
    if isinstance(error, NativeBackendError):
        return str(error).startswith("native rich search found no feasible candidate for boundary ")
    if type(error) is RuntimeError and str(error).startswith("鬼点硬保证层修补失败："):
        return True
    return (type(error) is RuntimeError and re.fullmatch(
        r"boundary .+ has no feasible candidate(?:: [\s\S]*)?", str(error)) is not None)


def select_initial_mapping(architecture, schedule, *, n_qubits: int,
                           params: Mapping[str, Any], config: PhysicalInitialGAConfig,
                           one_qubit=(), leading_one_qubit=()):
    """Search with a hard unique-evaluation budget and return mapping/report.

    Random search samples full domain permutations independently of scores;
    GA uses tournament selection, OX, mutation and two explicit elite slots.
    Both evaluate exactly the same initial population before adaptive proposals.
    """
    started = time.perf_counter_ns()
    generation_stats = {}
    seats, initial = build_initial_population(architecture, n_qubits, schedule, config,
                                              generation_stats=generation_stats)
    decode = lambda p: tuple(seats[index] for index in p)
    initial_mappings = tuple(decode(p) for p in initial)
    prefix_config = config.prefix_config()
    effective = rollout_params(params, prefix_config)
    dynamic_params = {key: value for key, value in params.items() if key not in
                      ("init_strategy", "init_engine", "initial_ga", "initial_lookahead", "init_pop", "init_gens")}
    context = {"architecture": architecture_spec(architecture), "schedule": schedule,
               "one_qubit": one_qubit, "leading_one_qubit": leading_one_qubit,
               "rollout_config": effective, "horizon": config.horizon, "rho": config.rho,
               "evaluation_seed": config.seed}
    source_root = Path(__file__).resolve().parents[1]
    report = {
        "policy_id": POLICY_ID if config.search_policy == "ga" else RANDOM_POLICY_ID,
        "config": asdict(config), "search_policy": config.search_policy,
        "domain_id": DOMAIN_ID, "population_id": POPULATION_ID,
        "seat_domain": seats, "seat_domain_sha256": stable_hash(seats),
        "initial_chromosomes": initial, "initial_population": initial_mappings,
        "initial_population_sha256": stable_hash(initial_mappings),
        "initial_population_hash": stable_hash(initial_mappings),
        "evaluation_context_sha256": stable_hash(context),
        "dynamic_config_sha256": stable_hash(dynamic_params),
        "compiler_config_sha256": stable_hash(dict(params)),
        "rollout_config": effective, "rollout_config_sha256": stable_hash(effective),
        "source_sha256": {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
                          for name in ("zzx/physical_initial_ga.py", "zzx/initial_lookahead.py", "zzx/gainit.py")},
        "score_semantics": "sum rho^layer times after-gate cumulative NLL increments",
        "candidates": [], "proposal_trace": [], "unique_evaluations": 0,
        "cache_hits": 0, "proposals": generation_stats["initial_generation_attempts"],
        "initial_generation_attempts": generation_stats["initial_generation_attempts"],
        "search_proposals": 0, "generations": 0,
    }
    cache = {}
    records = report["candidates"]
    rng = _rng(config.seed, "outer-search-v1")
    saved_python, saved_numpy = random.getstate(), np.random.get_state()
    eval_python = random.Random(config.seed).getstate()
    eval_numpy = np.random.RandomState(config.seed % (2**32)).get_state()
    space_size = _bounded_space_size(n_qubits, config.max_unique_evaluations)

    def evaluate(p, *, initial_candidate=False):
        _validate_chromosome(p, n_qubits)
        if not initial_candidate and report["proposals"] >= config.max_proposals:
            return None
        mapping = decode(p)
        if mapping not in cache and len(records) >= config.max_unique_evaluations:
            return None
        if not initial_candidate:
            report["proposals"] += 1
            report["search_proposals"] += 1
        if mapping in cache:
            report["cache_hits"] += 1
            record = cache[mapping]
        else:
            record = {"candidate": len(records), "chromosome": p, "mapping": mapping,
                      "mapping_sha256": stable_hash(mapping)}
            # Reset both legacy global streams before every unique replay, not
            # before generation. Cache hits cannot advance evaluator randomness.
            random.setstate(eval_python)
            np.random.set_state(eval_numpy)
            try:
                result = evaluate_mapping(
                    architecture, mapping, schedule, one_qubit=one_qubit,
                    leading_one_qubit=leading_one_qubit, params=params, config=prefix_config)
                value = result["weighted_nll"]
                if (result.get("status") != "success" or isinstance(value, bool)
                        or not isinstance(value, (int, float)) or not math.isfinite(value)):
                    raise ValueError("physical prefix evaluator returned an invalid score record")
                record.update(result)
            except Exception as error:
                if not _expected_infeasible(error):
                    record.update(status="program_error", error=f"{type(error).__name__}: {error}")
                    records.append(record)
                    report["unique_evaluations"] = len(records)
                    raise
                record.update(status="infeasible", error=f"{type(error).__name__}: {error}")
            cache[mapping] = record
            records.append(record)
            report["unique_evaluations"] = len(records)
        report["proposal_trace"].append(record["candidate"])
        return record

    def rank(p):
        record = cache[decode(p)]
        return (record.get("weighted_nll", math.inf), record["candidate"])

    def stop_reason():
        if len(records) >= space_size:
            return "search_space_exhausted"
        if len(records) >= config.max_unique_evaluations:
            return "unique_evaluation_budget"
        if report["proposals"] >= config.max_proposals:
            return "proposal_budget"
        return None

    try:
        population = []
        for p in initial:
            if evaluate(p, initial_candidate=True) is None:
                break
            population.append(p)
        reason = stop_reason()
        best_value = min((rank(p)[0] for p in population), default=math.inf)
        stall = 0
        for generation in range(config.max_generations):
            if reason:
                break
            report["generations"] = generation + 1
            ordered = sorted(population, key=rank)
            children = []
            for _ in range(config.population_size):
                if config.search_policy == "random":
                    child_list = list(range(n_qubits))
                    rng.shuffle(child_list)
                    child = tuple(child_list)
                else:
                    def tournament():
                        return min(rng.sample(ordered, min(2, len(ordered))), key=rank)
                    child = tournament()
                    if rng.random() < config.crossover_probability:
                        child = order_crossover(child, tournament(), rng)
                    child = mutate(child, rng, config)
                if evaluate(child) is None:
                    reason = stop_reason()
                    break
                children.append(child)
                reason = stop_reason()
                if reason:
                    break
            if config.search_policy == "ga":
                elites = ordered[:min(config.elite_count, len(ordered))]
                # Explicit elite slots, then ranked unique offspring; existing
                # parents only fill gaps when duplicate children reduce size.
                remainder = sorted(set(children) - set(elites), key=rank)
                remainder += [p for p in ordered if p not in elites and p not in remainder]
                population = (elites + remainder)[:config.population_size]
            else:
                # Only used to track the winner; never read for proposals.
                population = sorted(set(population + children), key=rank)[:config.population_size]
            current_best = min((rank(p)[0] for p in population), default=math.inf)
            stall = 0 if current_best < best_value else stall + 1
            best_value = min(best_value, current_best)
            # Random control spends its predeclared budget without adapting to
            # scores, including without a score-dependent patience stop.
            if not reason and config.search_policy == "ga" and stall >= config.early_stop_patience:
                reason = "early_stop_patience"
                break
        report["termination_reason"] = reason or "generation_budget"
        valid = [r for r in records if r["status"] == "success"]
        report["failed_candidates"] = len(records) - len(valid)
        if not valid:
            report["status"] = "infeasible"
            raise InitialMappingInfeasible("no feasible physical-GA initial mapping")
        best = min(valid, key=lambda r: (r["weighted_nll"], r["candidate"]))
        report.update(status="success", selected_candidate=best["candidate"],
                      selected_mapping_sha256=best["mapping_sha256"],
                      selected_weighted_nll=best["weighted_nll"])
        return list(best["mapping"]), report
    except Exception as error:
        if not isinstance(error, InitialMappingInfeasible):
            report.update(status="program_error", termination_reason="program_error")
        error.initial_ga_report = report
        error.initial_lookahead_report = report
        raise
    finally:
        random.setstate(saved_python)
        np.random.set_state(saved_numpy)
        report["selection_ns"] = time.perf_counter_ns() - started
