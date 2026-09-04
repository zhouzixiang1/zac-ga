"""Deterministic SA-vs-GA initial-placement validation protocol.

Initial placement is deliberately outside the ICCAD transition-stage timing
comparison.  This protocol decides only whether M3 and M4 may *jointly* switch
from the current SA initializer to ``GAInitialPlacer`` before tuning.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


INITIAL_PLACEMENT_PROTOCOL_ID = "sa-vs-ga-initial-v2"
INITIAL_PLACEMENT_QUALITY_POLICY = (
    "per-circuit-linear-else-exponential-sensitivity-v1")
INITIAL_ENGINES = ("sa", "ga")
METHODS = ("M3", "M4")


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


@dataclass(frozen=True)
class InitialPlacementTrial:
    circuit: str
    method: str
    engine: str
    seed: int
    status: str
    verifier_ok: bool
    ghost_hits: int
    fallback: bool
    log_fidelity: float | None
    initial_placement_ns: int | None
    full_compile_ns: int | None
    fidelity_ood: bool = False
    exponential_sensitivity_log_fidelity: float | None = None

    def validate(self) -> None:
        if not self.circuit or self.method not in METHODS:
            raise ValueError("invalid initial-placement trial identity")
        if self.engine not in INITIAL_ENGINES:
            raise ValueError(f"unknown initial-placement engine: {self.engine}")
        if (not isinstance(self.seed, int) or isinstance(self.seed, bool)
                or self.seed < 0):
            raise ValueError("initial-placement seed must be non-negative")
        if self.status == "success":
            linear_evidence_ok = (
                self.log_fidelity is None if self.fidelity_ood else
                self.log_fidelity is not None and
                math.isfinite(float(self.log_fidelity)))
            exponential_evidence_ok = (
                self.exponential_sensitivity_log_fidelity is not None and
                math.isfinite(float(
                    self.exponential_sensitivity_log_fidelity)))
            if (not self.verifier_ok or self.ghost_hits != 0 or self.fallback
                    or not linear_evidence_ok
                    or not exponential_evidence_ok
                    or self.initial_placement_ns is None
                    or self.initial_placement_ns < 0
                    or self.full_compile_ns is None
                    or self.full_compile_ns < 0):
                raise ValueError(
                    "successful initial-placement trial lacks strict evidence")


def build_initial_placement_schedule(
        circuits: Sequence[str], *, seeds: Sequence[int] = (0, 1, 2, 3, 4),
        schedule_seed: int = 0) -> dict[str, Any]:
    circuits = tuple(str(value) for value in circuits)
    seeds = tuple(int(value) for value in seeds)
    if (not circuits or len(set(circuits)) != len(circuits)
            or len(circuits) != 9):
        raise ValueError("initial-placement validation requires nine unique circuits")
    if seeds != (0, 1, 2, 3, 4):
        raise ValueError("initial-placement validation requires seeds 0-4")
    jobs = [
        {"circuit": circuit, "method": method,
         "engine": engine, "seed": seed}
        for circuit in circuits for method in METHODS
        for engine in INITIAL_ENGINES for seed in seeds
    ]
    random.Random(schedule_seed).shuffle(jobs)
    for ordinal, job in enumerate(jobs):
        job["ordinal"] = ordinal
    payload = {
        "experiment_schema": 2,
        "protocol_id": INITIAL_PLACEMENT_PROTOCOL_ID,
        "schedule_seed": int(schedule_seed),
        "circuits": list(circuits),
        "methods": list(METHODS),
        "engines": list(INITIAL_ENGINES),
        "seeds": list(seeds),
        "expected_trials": 180,
        "jobs": jobs,
    }
    payload["sha256"] = hashlib.sha256(
        _stable_json(payload).encode()).hexdigest()
    return payload


def _median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median requires values")
    return float(statistics.median(values))


def _geometric_mean(values: Sequence[float]) -> float:
    if not values or any(value <= 0 or not math.isfinite(value)
                         for value in values):
        raise ValueError("geometric mean requires finite positive values")
    return math.exp(math.fsum(math.log(value) for value in values) / len(values))


def select_initial_placement_engine(
        trials: Sequence[InitialPlacementTrial], *,
        expected_circuits: Sequence[str],
        expected_seeds: Sequence[int] = (0, 1, 2, 3, 4)) -> dict[str, Any]:
    circuits = tuple(expected_circuits)
    seeds = tuple(expected_seeds)
    expected = {
        (circuit, method, engine, seed)
        for circuit in circuits for method in METHODS
        for engine in INITIAL_ENGINES for seed in seeds
    }
    observed: dict[tuple[str, str, str, int], InitialPlacementTrial] = {}
    for trial in trials:
        trial.validate()
        key = (trial.circuit, trial.method, trial.engine, trial.seed)
        if key in observed:
            raise ValueError(f"duplicate initial-placement trial: {key}")
        if key not in expected:
            raise ValueError(f"unexpected initial-placement trial: {key}")
        observed[key] = trial
    if set(observed) != expected:
        raise ValueError(
            f"incomplete initial-placement ledger: {len(observed)}/{len(expected)}")

    def valid(row: InitialPlacementTrial) -> bool:
        return bool(
            row.status == "success" and row.verifier_ok and
            row.ghost_hits == 0 and not row.fallback and
            ((row.fidelity_ood and row.log_fidelity is None) or
             (not row.fidelity_ood and row.log_fidelity is not None and
              math.isfinite(float(row.log_fidelity)))) and
            row.exponential_sensitivity_log_fidelity is not None and
            math.isfinite(float(
                row.exponential_sensitivity_log_fidelity)) and
            row.initial_placement_ns is not None and
            row.initial_placement_ns > 0 and
            row.full_compile_ns is not None)

    complete = all(valid(row) for row in observed.values())
    method_summary: dict[str, Mapping[str, Any]] = {}
    if complete:
        for method in METHODS:
            quality_deltas = []
            time_speedups = []
            quality_model_counts = {
                "linear_log_fidelity": 0,
                "exponential_sensitivity_log_fidelity": 0,
            }
            for circuit in circuits:
                circuit_rows = [
                    observed[(circuit, method, engine, seed)]
                    for engine in INITIAL_ENGINES for seed in seeds
                ]
                # Never compare two different coherence models.  If any
                # SA/GA seed is outside the paper's linear T2 domain, compare
                # the complete circuit/method block with the exponential
                # sensitivity score that every successful manifest records.
                use_exponential = any(
                    row.fidelity_ood for row in circuit_rows)
                quality_model = (
                    "exponential_sensitivity_log_fidelity"
                    if use_exponential else "linear_log_fidelity")
                quality_model_counts[quality_model] += 1
                engine_values = {}
                for engine in INITIAL_ENGINES:
                    rows = [observed[(circuit, method, engine, seed)]
                            for seed in seeds]
                    quality_values = [
                        (row.exponential_sensitivity_log_fidelity
                         if use_exponential else row.log_fidelity)
                        for row in rows
                    ]
                    engine_values[engine] = {
                        "logf": _median([
                            float(value) for value in quality_values]),
                        "initial_ns": _median([
                            float(row.initial_placement_ns) for row in rows]),
                    }
                quality_deltas.append(
                    engine_values["ga"]["logf"] - engine_values["sa"]["logf"])
                time_speedups.append(
                    engine_values["sa"]["initial_ns"] /
                    engine_values["ga"]["initial_ns"])
            method_summary[method] = {
                "median_delta_quality_log_fidelity_ga_minus_sa":
                    _median(quality_deltas),
                "geometric_mean_initial_speedup_sa_over_ga":
                    _geometric_mean(time_speedups),
                "quality_non_regressing": _median(quality_deltas) >= 0.0,
                "ga_faster": _geometric_mean(time_speedups) > 1.0,
                "quality_model_counts": quality_model_counts,
            }
    switch = bool(complete and all(
        method_summary[method]["quality_non_regressing"] and
        method_summary[method]["ga_faster"] for method in METHODS))
    return {
        "experiment_schema": 2,
        "protocol_id": INITIAL_PLACEMENT_PROTOCOL_ID,
        "quality_policy": INITIAL_PLACEMENT_QUALITY_POLICY,
        "complete": complete,
        "expected_trials": len(expected),
        "methods": method_summary,
        "selected_engine": "ga" if switch else "sa",
        "switch_to_ga": switch,
        "reason": ("both_methods_non_regressing_and_faster" if switch else
                   "retain_sa_until_both_methods_pass"),
        "trials_sha256": hashlib.sha256(_stable_json([
            asdict(observed[key]) for key in sorted(observed)
        ]).encode()).hexdigest(),
    }


__all__ = [
    "INITIAL_ENGINES", "INITIAL_PLACEMENT_PROTOCOL_ID",
    "INITIAL_PLACEMENT_QUALITY_POLICY", "METHODS",
    "InitialPlacementTrial", "build_initial_placement_schedule",
    "select_initial_placement_engine",
]
