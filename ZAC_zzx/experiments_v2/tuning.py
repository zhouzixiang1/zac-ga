"""Deterministic tuning contracts for the shared M3/M4 resident GA.

The module deliberately contains no compiler imports.  It freezes the circuit
split, candidate generation, and selection rule before an expensive tuning run
starts.  The same trial ledger can be ranked once for the shared main-paper
configuration and once per method for the explicitly-labelled independent
upper-bound track.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


TUNING_PROTOCOL_ID = "resident-ga-native-v2"
TUNING_QUALITY_POLICY_ID = (
    "per-circuit-method-linear-else-exponential-sensitivity-v1")
LINEAR_QUALITY_MODEL = "linear_log_fidelity"
EXPONENTIAL_QUALITY_MODEL = "exponential_sensitivity_log_fidelity"

TUNING_METHODS = ("M3", "M4")
TUNING_PHASES = ("screen", "successive_halving", "validation")
PHASE_CONTRACTS: Mapping[str, Mapping[str, Any]] = {
    "screen": {
        "circuit_split": "screen",
        "seeds": (0,),
        "candidate_count": 18,
        "expected_trials": 324,
    },
    "successive_halving": {
        "circuit_split": "train",
        "seeds": (0, 1),
        "candidate_count": 6,
        "expected_trials": 432,
    },
    "validation": {
        "circuit_split": "validation",
        "seeds": (0, 1, 2, 3, 4),
        # The default is always evaluated in addition to the two promoted
        # non-default candidates, even when it ranked in the top two.  This
        # freezes the preregistered 270-attempt validation budget.
        "candidate_count": 3,
        "expected_trials": 270,
    },
}

TUNING_SPACE: Mapping[str, tuple[Any, ...]] = {
    "population_size": (4, 6, 8, 12),
    "iterations": (4, 8, 12),
    "neighbors_per_solution": (1, 2, 3),
    "neighbor_sample_size": (12, 24, 36),
    "theta_capacity": (0.80, 0.90, 0.95),
    "box_ratio": (2, 3, 4),
    "alpha_lookahead": (0.05, 0.10, 0.20),
    "rho": (0.4, 0.6, 0.8),
    "w_resident": (0.15, 0.30, 0.50),
    "pin_radius": (1, 2, 4),
    "elite_count": (1, 2),
    "early_stop_patience": (0, 3, 5),
}

DEFAULT_CANDIDATE: Mapping[str, Any] = {
    "population_size": 6,
    "iterations": 8,
    "neighbors_per_solution": 2,
    "neighbor_sample_size": 24,
    "theta_capacity": 0.90,
    "box_ratio": 3,
    "alpha_lookahead": 0.10,
    "rho": 0.6,
    "w_resident": 0.30,
    "pin_radius": 2,
    "elite_count": 1,
    "early_stop_patience": 0,
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def candidate_id(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(_stable_json(dict(config)).encode()).hexdigest()[:16]


def circuit_family(circuit: str) -> str:
    """Return the frozen QMAP family identifier used to prevent leakage.

    QMAP filenames end in a catalogue identifier (``_130``), while variants
    often add ``-v0`` or ``-bdd``.  Removing exactly those suffixes groups the
    known benchmark variants without guessing from qubit or gate counts.
    """
    name = Path(circuit).name
    if name.endswith(".qasm"):
        name = name[:-5]
    name = re.sub(r"_transpiled$", "", name.lower())
    name = re.sub(r"_\d+$", "", name)
    name = re.sub(r"-(?:v\d+|bdd)$", "", name)
    return name


def gate_stratum(gates_2q: int) -> str:
    if gates_2q <= 300:
        return "le300"
    if gates_2q <= 1500:
        return "301_1500"
    return "gt1500"


def _manifest_name(row: Mapping[str, Any]) -> str:
    return Path(str(row["canonical_path"])).stem


def _rank_key(row: Mapping[str, Any], seed: int) -> str:
    payload = f"{seed}:{row['canonical_sha256']}:{_manifest_name(row)}"
    return hashlib.sha256(payload.encode()).hexdigest()


def build_tuning_split(
        suite: Sequence[Mapping[str, Any]], *, seed: int = 0) -> dict[str, Any]:
    """Select 18 train and 9 validation circuits without family leakage.

    Only singleton QMAP families are eligible for tuning.  Consequently all
    variants from multi-member families remain completely unseen, and the
    remaining 127 circuits are an honest holdout when the canonical suite has
    the expected 154 members.
    """
    if len(suite) < 27:
        raise ValueError("tuning split requires at least 27 circuits")
    families = Counter(circuit_family(_manifest_name(row)) for row in suite)
    selected: dict[str, dict[str, list[str]]] = {}
    used: set[str] = set()
    for stratum in ("le300", "301_1500", "gt1500"):
        eligible = [
            row for row in suite
            if gate_stratum(int(row["gates_2q"])) == stratum
            and families[circuit_family(_manifest_name(row))] == 1
        ]
        eligible.sort(key=lambda row: _rank_key(row, seed))
        if len(eligible) < 9:
            raise ValueError(
                f"stratum {stratum} has only {len(eligible)} singleton families")
        chosen = eligible[:9]
        train = [_manifest_name(row) for row in chosen[:6]]
        validation = [_manifest_name(row) for row in chosen[6:]]
        screen = train[:3]
        selected[stratum] = {
            "screen": screen,
            "train": train,
            "validation": validation,
        }
        used.update(train)
        used.update(validation)

    all_names = {_manifest_name(row) for row in suite}
    holdout = sorted(all_names - used)
    family_sets: dict[str, set[str]] = defaultdict(set)
    for row in suite:
        name = _manifest_name(row)
        split = "holdout"
        for strata in selected.values():
            if name in strata["train"]:
                split = "train"
            elif name in strata["validation"]:
                split = "validation"
        family_sets[circuit_family(name)].add(split)
    leaking = {key: sorted(value) for key, value in family_sets.items()
               if len(value) > 1}
    if leaking:
        raise AssertionError(f"circuit-family leakage in tuning split: {leaking}")

    return {
        "protocol_id": TUNING_PROTOCOL_ID,
        "seed": seed,
        "suite_size": len(suite),
        "family_rule": "strip catalogue _N, then -vN/-bdd suffix",
        "strata": selected,
        "screen": [name for value in selected.values() for name in value["screen"]],
        "train": [name for value in selected.values() for name in value["train"]],
        "validation": [
            name for value in selected.values() for name in value["validation"]],
        "holdout": holdout,
        "sha256": hashlib.sha256(_stable_json(selected).encode()).hexdigest(),
    }


def _valid_budget(config: Mapping[str, Any]) -> bool:
    return (int(config["population_size"]) * int(config["iterations"]) *
            int(config["neighbor_sample_size"]) <= 3456)


def _distance(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    terms = []
    for key, values in TUNING_SPACE.items():
        li, ri = values.index(left[key]), values.index(right[key])
        scale = max(1, len(values) - 1)
        terms.append(((li - ri) / scale) ** 2)
    return math.sqrt(sum(terms) / len(terms))


def generate_candidates(*, count: int = 18, seed: int = 0,
                        pool_size: int = 4096) -> list[dict[str, Any]]:
    """Generate a deterministic maximin sample containing the current default."""
    if count < 1:
        raise ValueError("candidate count must be positive")
    keys = tuple(TUNING_SPACE)
    eligible = []
    for values in itertools.product(*(TUNING_SPACE[key] for key in keys)):
        config = dict(zip(keys, values))
        if _valid_budget(config):
            eligible.append(config)
    rng = random.Random(seed)
    if len(eligible) > pool_size:
        pool = [eligible[index] for index in rng.sample(range(len(eligible)), pool_size)]
    else:
        pool = eligible
    default = dict(DEFAULT_CANDIDATE)
    if not _valid_budget(default):
        raise AssertionError("default tuning candidate violates the search budget")
    chosen = [default]
    remaining = {candidate_id(item): item for item in pool}
    remaining.pop(candidate_id(default), None)
    while len(chosen) < count:
        if not remaining:
            raise ValueError("candidate pool is smaller than requested sample")
        winner_id, winner = max(
            remaining.items(),
            key=lambda item: (
                min(_distance(item[1], old) for old in chosen),
                item[0],
            ),
        )
        chosen.append(winner)
        del remaining[winner_id]
    return [dict(item, candidate_id=candidate_id(item)) for item in chosen]


@dataclass(frozen=True)
class TuningTrial:
    candidate_id: str
    circuit: str
    method: str
    seed: int
    status: str
    verifier_ok: bool
    ghost_hits: int
    fallback: bool
    log_fidelity: float | None
    transition_decision_ns: int | None
    move_time_us: float | None
    move_batches: int | None
    fidelity_ood: bool = False
    exponential_sensitivity_log_fidelity: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TuningTrial":
        known = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = set(value) - known
        if unknown:
            raise ValueError(f"unknown tuning trial fields: {sorted(unknown)}")
        return cls(**value)


@dataclass(frozen=True)
class ScheduledTrial:
    """One immutable, deterministic unit in a tuning phase."""

    trial_id: str
    phase: str
    candidate_id: str
    circuit: str
    method: str
    seed: int
    ordinal: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ScheduledTrial":
        known = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = set(value) - known
        if unknown:
            raise ValueError(f"unknown scheduled trial fields: {sorted(unknown)}")
        trial = cls(**value)
        trial.validate()
        return trial

    def validate(self) -> None:
        if self.phase not in TUNING_PHASES:
            raise ValueError(f"unknown tuning phase: {self.phase!r}")
        if self.method not in TUNING_METHODS:
            raise ValueError(f"tuning only accepts M3/M4, found {self.method!r}")
        if not self.trial_id or not self.candidate_id or not self.circuit:
            raise ValueError("scheduled tuning identity may not be empty")
        if (not isinstance(self.seed, int) or isinstance(self.seed, bool)
                or self.seed < 0):
            raise ValueError("scheduled tuning seed must be a non-negative integer")
        if (not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool)
                or self.ordinal < 0):
            raise ValueError("scheduled tuning ordinal must be non-negative")


def _trial_id(phase: str, candidate: str, circuit: str,
              method: str, seed: int) -> str:
    payload = {
        "protocol_id": TUNING_PROTOCOL_ID,
        "phase": phase,
        "candidate_id": candidate,
        "circuit": circuit,
        "method": method,
        "seed": seed,
    }
    return hashlib.sha256(_stable_json(payload).encode()).hexdigest()[:24]


def build_trial_schedule(
        phase: str, *, candidate_ids: Sequence[str],
        circuits: Sequence[str], seeds: Sequence[int], schedule_seed: int = 0,
        parent_sha256: str = "") -> dict[str, Any]:
    """Build a fully materialised phase schedule with a stable self hash.

    Trial order is hash-randomised once, deterministically.  This avoids a
    candidate or method always running first while still making resume exact.
    """
    if phase not in PHASE_CONTRACTS:
        raise ValueError(f"unknown tuning phase: {phase!r}")
    candidate_values = list(candidate_ids)
    circuit_values = list(circuits)
    seed_values = list(seeds)
    if len(set(candidate_values)) != len(candidate_values) or not candidate_values:
        raise ValueError("candidate ids must be non-empty and unique")
    if len(set(circuit_values)) != len(circuit_values) or not circuit_values:
        raise ValueError("circuit ids must be non-empty and unique")
    if len(set(seed_values)) != len(seed_values) or not seed_values:
        raise ValueError("seeds must be non-empty and unique")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
           for value in seed_values):
        raise ValueError("schedule seeds must be non-negative integers")

    contract = PHASE_CONTRACTS[phase]
    if len(candidate_values) != int(contract["candidate_count"]):
        raise ValueError(
            f"{phase} requires {contract['candidate_count']} candidates, "
            f"found {len(candidate_values)}")
    if tuple(sorted(seed_values)) != tuple(contract["seeds"]):
        raise ValueError(
            f"{phase} requires seeds {contract['seeds']}, found {seed_values}")

    identities = [
        (candidate, circuit, method, seed)
        for candidate in candidate_values
        for circuit in circuit_values
        for seed in seed_values
        for method in TUNING_METHODS
    ]
    identities.sort(key=lambda row: hashlib.sha256(
        f"{schedule_seed}:{phase}:{':'.join(map(str, row))}".encode()
    ).hexdigest())
    trials = []
    for ordinal, (candidate, circuit, method, seed) in enumerate(identities):
        trials.append(asdict(ScheduledTrial(
            trial_id=_trial_id(phase, candidate, circuit, method, seed),
            phase=phase,
            candidate_id=candidate,
            circuit=circuit,
            method=method,
            seed=seed,
            ordinal=ordinal,
        )))
    expected = int(contract["expected_trials"])
    if len(trials) != expected:
        raise ValueError(
            f"{phase} schedule must contain {expected} attempts, found "
            f"{len(trials)}; verify the frozen circuit split")
    payload = {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "phase": phase,
        "schedule_seed": int(schedule_seed),
        "parent_sha256": str(parent_sha256),
        "candidate_ids": candidate_values,
        "circuits": circuit_values,
        "seeds": seed_values,
        "methods": list(TUNING_METHODS),
        "expected_trials": expected,
        "trials": trials,
    }
    payload["schedule_sha256"] = hashlib.sha256(
        _stable_json(payload).encode()).hexdigest()
    return payload


def validate_trial_schedule(payload: Mapping[str, Any]) -> list[ScheduledTrial]:
    """Validate the complete immutable schedule and return typed rows."""
    known = {
        "experiment_schema", "protocol_id", "phase", "schedule_seed",
        "parent_sha256", "candidate_ids", "circuits", "seeds", "methods",
        "expected_trials", "trials", "schedule_sha256",
    }
    unknown = set(payload) - known
    if unknown:
        raise ValueError(f"unknown tuning schedule fields: {sorted(unknown)}")
    if payload.get("experiment_schema") != 2:
        raise ValueError("tuning schedule must be Schema 2")
    if payload.get("protocol_id") != TUNING_PROTOCOL_ID:
        raise ValueError("tuning protocol id mismatch")
    expected_hash = payload.get("schedule_sha256")
    unsigned = {key: value for key, value in payload.items()
                if key != "schedule_sha256"}
    if (not isinstance(expected_hash, str) or
            hashlib.sha256(_stable_json(unsigned).encode()).hexdigest()
            != expected_hash):
        raise ValueError("tuning schedule hash mismatch")
    expected = build_trial_schedule(
        str(payload["phase"]),
        candidate_ids=[str(value) for value in payload["candidate_ids"]],
        circuits=[str(value) for value in payload["circuits"]],
        seeds=[int(value) for value in payload["seeds"]],
        schedule_seed=int(payload["schedule_seed"]),
        parent_sha256=str(payload["parent_sha256"]),
    )
    if dict(payload) != expected:
        raise ValueError("tuning schedule content differs from deterministic rebuild")
    trials = [ScheduledTrial.from_mapping(row) for row in payload["trials"]]
    if len({trial.trial_id for trial in trials}) != len(trials):
        raise ValueError("duplicate trial id in tuning schedule")
    return trials


def _median(values: Iterable[float]) -> float:
    materialized = list(values)
    if not materialized:
        raise ValueError("cannot take the median of an empty sequence")
    return float(statistics.median(materialized))


def _finite_number(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(float(value)))


def _valid_trial_evidence(row: TuningTrial) -> bool:
    """Return whether one row carries every metric needed by tuning v2.

    Every successful row binds the exponential sensitivity score, even when it
    is inside the paper's linear-coherence domain.  This is necessary because
    another candidate in the same circuit/method cohort may be OOD, in which
    case the complete cohort must be compared with one common model.
    """
    linear_evidence_ok = (
        row.log_fidelity is None if row.fidelity_ood else
        _finite_number(row.log_fidelity))
    return bool(
        row.status == "success" and row.verifier_ok and
        row.ghost_hits == 0 and not row.fallback and
        linear_evidence_ok and
        _finite_number(row.exponential_sensitivity_log_fidelity) and
        _finite_number(row.transition_decision_ns) and
        float(row.transition_decision_ns) >= 0 and
        _finite_number(row.move_time_us) and float(row.move_time_us) >= 0 and
        isinstance(row.move_batches, int) and
        not isinstance(row.move_batches, bool) and row.move_batches >= 0)


def _cohort_quality_models(
        trials: Iterable[TuningTrial], *, methods: Sequence[str],
        circuits: Sequence[str]) -> dict[tuple[str, str], str]:
    """Freeze one coherence model for every circuit/method candidate cohort."""
    rows = list(trials)
    models = {}
    for method in methods:
        for circuit in circuits:
            use_exponential = any(
                row.method == method and row.circuit == circuit and
                row.fidelity_ood for row in rows)
            models[(method, circuit)] = (
                EXPONENTIAL_QUALITY_MODEL if use_exponential
                else LINEAR_QUALITY_MODEL)
    return models


def _quality_value(row: TuningTrial, model: str) -> float:
    value = (row.exponential_sensitivity_log_fidelity
             if model == EXPONENTIAL_QUALITY_MODEL else row.log_fidelity)
    if not _finite_number(value):
        raise ValueError(
            f"tuning row lacks finite {model}: "
            f"{row.candidate_id}/{row.method}/{row.circuit}/seed-{row.seed}")
    return float(value)


def _quality_model_counts(
        models: Mapping[tuple[str, str], str], *, method: str,
        circuits: Sequence[str]) -> dict[str, int]:
    return {
        model: sum(models[(method, circuit)] == model for circuit in circuits)
        for model in (LINEAR_QUALITY_MODEL, EXPONENTIAL_QUALITY_MODEL)
    }


def rank_candidates(
        trials: Sequence[TuningTrial], *, default_id: str,
        expected_circuits: Sequence[str], expected_seeds: Sequence[int],
        near_optimal_logf: float = 0.002) -> dict[str, Any]:
    """Apply the preregistered shared and independent selection rules."""
    methods = ("M3", "M4")
    expected = {(method, circuit, seed) for method in methods
                for circuit in expected_circuits for seed in expected_seeds}
    grouped: dict[str, dict[tuple[str, str, int], TuningTrial]] = defaultdict(dict)
    for trial in trials:
        key = (trial.method, trial.circuit, trial.seed)
        if key in grouped[trial.candidate_id]:
            raise ValueError(f"duplicate tuning trial: {trial.candidate_id} {key}")
        grouped[trial.candidate_id][key] = trial
    if default_id not in grouped:
        raise ValueError("default candidate is absent from the tuning ledger")

    quality_models = _cohort_quality_models(
        trials, methods=methods, circuits=expected_circuits)

    default_rows = grouped[default_id]
    if set(default_rows) != expected:
        raise ValueError("default candidate does not cover the validation cohort")
    if not all(_valid_trial_evidence(row)
               for row in default_rows.values()):
        raise ValueError("default candidate is not a complete valid validation run")
    default_by_method_circuit = {
        (method, circuit): _median(
            _quality_value(
                default_rows[(method, circuit, seed)],
                quality_models[(method, circuit)])
            for seed in expected_seeds)
        for method in methods for circuit in expected_circuits
    }

    summaries = []
    for cid, rows in sorted(grouped.items()):
        valid = (set(rows) == expected and
                 all(_valid_trial_evidence(row) for row in rows.values()))
        per_method: dict[str, dict[str, float]] = {}
        if valid:
            for method in methods:
                deltas = []
                runtimes = []
                move_times = []
                move_batches = []
                for circuit in expected_circuits:
                    median_logf = _median(
                        _quality_value(
                            rows[(method, circuit, seed)],
                            quality_models[(method, circuit)])
                        for seed in expected_seeds)
                    deltas.append(
                        median_logf - default_by_method_circuit[(method, circuit)])
                    runtimes.append(_median(
                        float(rows[(method, circuit, seed)].transition_decision_ns)
                        for seed in expected_seeds))
                    move_times.append(_median(
                        float(rows[(method, circuit, seed)].move_time_us)
                        for seed in expected_seeds
                        if rows[(method, circuit, seed)].move_time_us is not None))
                    move_batches.append(_median(
                        float(rows[(method, circuit, seed)].move_batches)
                        for seed in expected_seeds
                        if rows[(method, circuit, seed)].move_batches is not None))
                per_method[method] = {
                    "median_delta_log_fidelity": _median(deltas),
                    "median_transition_decision_ns": _median(runtimes),
                    "median_move_time_us": _median(move_times),
                    "median_move_batches": _median(move_batches),
                    "quality_model_counts": _quality_model_counts(
                        quality_models, method=method,
                        circuits=expected_circuits),
                }
        non_regressing = valid and all(
            per_method[method]["median_delta_log_fidelity"] >= 0.0
            for method in methods)
        summaries.append({
            "candidate_id": cid,
            "valid": valid,
            "non_regressing": non_regressing,
            "methods": per_method,
            "shared_score": (min(
                per_method[m]["median_delta_log_fidelity"] for m in methods)
                if valid else None),
        })

    eligible = [row for row in summaries if row["non_regressing"]]
    shared = None
    if eligible:
        best_score = max(float(row["shared_score"]) for row in eligible)
        near = [row for row in eligible
                if best_score - float(row["shared_score"]) <= near_optimal_logf]
        shared = min(near, key=lambda row: (
            _median(row["methods"][method]["median_transition_decision_ns"]
                    for method in methods),
            _median(row["methods"][method]["median_move_time_us"]
                    for method in methods),
            _median(row["methods"][method]["median_move_batches"]
                    for method in methods),
            row["candidate_id"],
        ))

    independent: dict[str, Any] = {}
    valid_rows = [row for row in summaries if row["valid"]]
    for method in methods:
        non_regressing_rows = [
            row for row in valid_rows
            if row["methods"][method]["median_delta_log_fidelity"] >= 0.0]
        if non_regressing_rows:
            independent[method] = min(non_regressing_rows, key=lambda row: (
                -row["methods"][method]["median_delta_log_fidelity"],
                row["methods"][method]["median_transition_decision_ns"],
                row["methods"][method]["median_move_time_us"],
                row["methods"][method]["median_move_batches"],
                row["candidate_id"],
            ))["candidate_id"]
        else:
            independent[method] = None

    return {
        "protocol_id": TUNING_PROTOCOL_ID,
        "quality_policy": TUNING_QUALITY_POLICY_ID,
        "default_candidate_id": default_id,
        "shared_selected": None if shared is None else shared["candidate_id"],
        "independent_selected": independent,
        "summaries": summaries,
    }


def stage_leaderboard(
        trials: Sequence[TuningTrial], *, candidate_ids: Sequence[str],
        default_id: str, expected_circuits: Sequence[str],
        expected_seeds: Sequence[int]) -> list[dict[str, Any]]:
    """Rank one screening/halving cohort without changing final selection.

    All scheduled attempts must be present.  Compiler or verifier failures make
    only their candidate invalid; missing or duplicate ledger entries abort the
    promotion.  The deterministic score is the smaller of M3/M4's median
    per-circuit delta from the current default.  It is used solely to allocate
    the next tuning budget and never substitutes for validation selection.
    """
    candidates = list(candidate_ids)
    if len(set(candidates)) != len(candidates) or default_id not in candidates:
        raise ValueError("stage candidates must be unique and include the default")
    expected_keys = {
        (candidate, method, circuit, seed)
        for candidate in candidates for method in TUNING_METHODS
        for circuit in expected_circuits for seed in expected_seeds
    }
    observed: dict[tuple[str, str, str, int], TuningTrial] = {}
    for trial in trials:
        key = (trial.candidate_id, trial.method, trial.circuit, trial.seed)
        if key in observed:
            raise ValueError(f"duplicate stage tuning trial: {key}")
        if key not in expected_keys:
            raise ValueError(f"unexpected stage tuning trial: {key}")
        observed[key] = trial
    missing = expected_keys - set(observed)
    if missing:
        preview = sorted(missing)[:5]
        raise ValueError(
            f"stage tuning ledger is incomplete: missing {len(missing)} rows; "
            f"examples={preview}")

    quality_models = _cohort_quality_models(
        observed.values(), methods=TUNING_METHODS,
        circuits=expected_circuits)

    default_medians: dict[tuple[str, str], float] = {}
    for method in TUNING_METHODS:
        for circuit in expected_circuits:
            rows = [observed[(default_id, method, circuit, seed)]
                    for seed in expected_seeds]
            if not all(_valid_trial_evidence(row) for row in rows):
                raise ValueError(
                    "default candidate failed in a promotion cohort: "
                    f"{method}/{circuit}")
            default_medians[(method, circuit)] = _median(
                _quality_value(row, quality_models[(method, circuit)])
                for row in rows)

    leaderboard = []
    for cid in candidates:
        rows = [observed[(cid, method, circuit, seed)]
                for method in TUNING_METHODS for circuit in expected_circuits
                for seed in expected_seeds]
        valid = all(_valid_trial_evidence(row) for row in rows)
        methods: dict[str, Any] = {}
        if valid:
            for method in TUNING_METHODS:
                deltas = []
                runtimes = []
                move_times = []
                move_batches = []
                for circuit in expected_circuits:
                    circuit_rows = [
                        observed[(cid, method, circuit, seed)]
                        for seed in expected_seeds]
                    deltas.append(
                        _median(_quality_value(
                            row, quality_models[(method, circuit)])
                            for row in circuit_rows)
                        - default_medians[(method, circuit)])
                    runtimes.append(_median(
                        float(row.transition_decision_ns)
                        for row in circuit_rows))
                    move_times.append(_median(
                        float(row.move_time_us) for row in circuit_rows))
                    move_batches.append(_median(
                        float(row.move_batches) for row in circuit_rows))
                methods[method] = {
                    "median_delta_log_fidelity": _median(deltas),
                    "median_transition_decision_ns": _median(runtimes),
                    "median_move_time_us": _median(move_times),
                    "median_move_batches": _median(move_batches),
                    "quality_model_counts": _quality_model_counts(
                        quality_models, method=method,
                        circuits=expected_circuits),
                }
        leaderboard.append({
            "candidate_id": cid,
            "valid": valid,
            "quality_policy": TUNING_QUALITY_POLICY_ID,
            "shared_score": (min(
                methods[method]["median_delta_log_fidelity"]
                for method in TUNING_METHODS) if valid else None),
            "methods": methods,
        })

    def sort_key(row: Mapping[str, Any]):
        if not row["valid"]:
            return (1, 0.0, math.inf, math.inf,
                    row["candidate_id"])
        method_rows = row["methods"]
        # Screen and halving may run in parallel.  Their wall-clock transition
        # measurements remain useful diagnostics but cannot decide promotion.
        # Move metrics are deterministic properties of the emitted schedule.
        return (
            0,
            -float(row["shared_score"]),
            _median(method_rows[method]["median_move_time_us"]
                    for method in TUNING_METHODS),
            _median(method_rows[method]["median_move_batches"]
                    for method in TUNING_METHODS),
            row["candidate_id"],
        )
    ordered = sorted(leaderboard, key=sort_key)
    for rank, row in enumerate(ordered, start=1):
        row["rank"] = rank if row["valid"] else None
    return ordered


def promoted_candidates(phase: str, leaderboard: Sequence[Mapping[str, Any]],
                        *, default_id: str) -> list[str]:
    """Return exactly six halving or three validation candidate ids."""
    valid = [str(row["candidate_id"]) for row in leaderboard if row["valid"]]
    if phase == "screen":
        non_default = [cid for cid in valid if cid != default_id]
        if default_id not in valid:
            raise RuntimeError("screen default candidate is invalid")
        if len(non_default) < 5:
            raise RuntimeError(
                f"screen has only {len(valid)} valid candidates; six required")
        # Retaining the default is necessary because the next cohort adds new
        # circuits and seed 1; its per-circuit delta cannot be borrowed from the
        # smaller screen cohort.
        promoted = [default_id, *non_default[:5]]
    elif phase == "successive_halving":
        non_default = [cid for cid in valid if cid != default_id]
        if len(non_default) < 2:
            raise RuntimeError(
                "successive halving requires two valid non-default candidates")
        promoted = [default_id, *non_default[:2]]
    else:
        raise ValueError(f"phase {phase!r} cannot promote another cohort")
    if len(set(promoted)) != len(promoted):
        raise AssertionError("promotion emitted duplicate candidates")
    return promoted


def write_protocol_files(suite_manifest: Path, output_directory: Path,
                         *, seed: int = 0, count: int = 18) -> dict[str, str]:
    suite = json.loads(suite_manifest.read_text(encoding="utf-8"))
    if not isinstance(suite, list):
        raise ValueError("suite manifest must contain a JSON array")
    split = build_tuning_split(suite, seed=seed)
    candidates = generate_candidates(count=count, seed=seed)
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "split": output_directory / "split_manifest.json",
        "space": output_directory / "tuning_space.json",
        "candidates": output_directory / "candidates.json",
    }
    payloads = {
        "split": split,
        "space": {"protocol_id": TUNING_PROTOCOL_ID,
                  "space": {key: list(value) for key, value in TUNING_SPACE.items()},
                  "budget_constraint":
                      "population_size*iterations*neighbor_sample_size<=3456"},
        "candidates": {"protocol_id": TUNING_PROTOCOL_ID,
                       "default_candidate_id": candidate_id(DEFAULT_CANDIDATE),
                       "candidates": candidates},
    }
    for key, path in paths.items():
        path.write_text(_stable_json(payloads[key]) + "\n", encoding="utf-8")
    return {key: str(path) for key, path in paths.items()}


__all__ = [
    "DEFAULT_CANDIDATE", "PHASE_CONTRACTS", "ScheduledTrial",
    "TUNING_METHODS", "TUNING_PHASES", "TUNING_PROTOCOL_ID",
    "TUNING_QUALITY_POLICY_ID", "TUNING_SPACE",
    "TuningTrial",
    "build_tuning_split", "candidate_id", "circuit_family", "gate_stratum",
    "build_trial_schedule", "generate_candidates", "promoted_candidates",
    "rank_candidates", "stage_leaderboard", "validate_trial_schedule",
    "write_protocol_files",
]
