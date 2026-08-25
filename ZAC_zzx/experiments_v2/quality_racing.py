"""Streamlined, method-specific quality racing for the native resident GA.

This is the active ``resident-ga-quality-racing-v3`` protocol.  The older
screen/halving/270-validation protocol remains readable in :mod:`tuning` only
for legacy ledgers; formal M3/M4 configs point exclusively to this module's
protocol id.

The search is deliberately sequential rather than a Cartesian grid:

1. compare five search-budget profiles;
2. expand the best two with one-factor decision/RETURN variants;
3. for M4 only, expand the best two over decay pairs and 1/2/4-site rollout
   budgets;
4. validate the best two per method with seeds 0, 1, and 2.

Every ranking uses externally measured strongest-baseline log fidelity.  The
compiler fitness remains the physical objective and never sees baseline data.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zzx.algorithm_v2 import (
    FORMAL_NATIVE_TUNING_PROTOCOL_ID,
    decay_lookahead_spec,
    validate_schema2_pair,
    validate_schema2_setting,
)


PROTOCOL_ID = "resident-ga-quality-racing-v3"
if PROTOCOL_ID != FORMAL_NATIVE_TUNING_PROTOCOL_ID:
    raise RuntimeError("quality racing protocol differs from formal contract")

METHODS = ("M3", "M4")
DEVELOPMENT_SEED = (0,)
VALIDATION_SEEDS = (0, 1, 2)
RACE_BLOCK_SIZE = 5
ELIMINATION_LOGF_MARGIN = 0.005
NEAR_OPTIMAL_LOGF = 0.002
INCUMBENT_NON_DEGRADATION_TOLERANCE = 1e-12

ZAC_DEVELOPMENT = (
    "bv_n14", "bv_n19", "cat_n22", "ghz_n23", "ghz_n78", "wstate_n27",
)
QMAP_DEVELOPMENT = (
    "mini_alu_305", "ground_state_estimation_10", "aj-e11_165",
    "z4_268", "pm1_249", "cm152a_212",
    "dist_223", "hwb8_113", "hwb9_119",
)
ZAC_VALIDATION = (
    "ising_n98", "knn_n31", "multiply_n13", "qft_n29", "seca_n11",
    "swap_test_n25",
)
QMAP_VALIDATION = (
    "ex1_226", "ex3_229", "cm82a_208",
    "hwb5_53", "adr4_197", "wim_266",
    "clip_206", "root_255", "misex1_241",
)
DEVELOPMENT_CIRCUITS = ZAC_DEVELOPMENT + QMAP_DEVELOPMENT
# ``dist_223`` and ``hwb8_113`` exhaust the common 600-s M3 budget, so
# repeating them for every hyperparameter is uninformative and would make
# every candidate invalid.  Keep the larger same-family ``hwb9_119`` with its
# sibling to avoid tuning/coverage family leakage.  All three remain in the
# declared development inventory and are attempted by the selected methods
# during full QMAP154 coverage; none is removed from final result tables.
DEVELOPMENT_COVERAGE_ONLY = ("dist_223", "hwb8_113", "hwb9_119")
DEVELOPMENT_TUNING_CIRCUITS = tuple(
    circuit for circuit in DEVELOPMENT_CIRCUITS
    if circuit not in DEVELOPMENT_COVERAGE_ONLY)
VALIDATION_CIRCUITS = ZAC_VALIDATION + QMAP_VALIDATION

SEARCH_PROFILES: Mapping[str, Mapping[str, int]] = {
    "default": {
        "population_size": 6, "iterations": 8,
        "neighbor_sample_size": 24, "neighbors_per_solution": 2,
        "elite_count": 1, "early_stop_patience": 0,
        "max_unique_evaluations": 1152,
    },
    "fast": {
        "population_size": 8, "iterations": 6,
        "neighbor_sample_size": 12, "neighbors_per_solution": 2,
        "elite_count": 2, "early_stop_patience": 3,
        "max_unique_evaluations": 576,
    },
    "balanced": {
        "population_size": 12, "iterations": 10,
        "neighbor_sample_size": 16, "neighbors_per_solution": 2,
        "elite_count": 2, "early_stop_patience": 4,
        "max_unique_evaluations": 1920,
    },
    "deep": {
        "population_size": 8, "iterations": 16,
        "neighbor_sample_size": 16, "neighbors_per_solution": 3,
        "elite_count": 2, "early_stop_patience": 5,
        "max_unique_evaluations": 2048,
    },
    "quality": {
        "population_size": 16, "iterations": 12,
        "neighbor_sample_size": 24, "neighbors_per_solution": 3,
        "elite_count": 2, "early_stop_patience": 5,
        "max_unique_evaluations": 4096,
    },
}

COMMON_KNOBS: Mapping[str, tuple[Any, ...]] = {
    "theta_capacity": (0.85, 0.90, 0.95),
    "return_candidate_limit": (6, 10),
    "return_assignment_k": (4, 8),
    "crossover_rate": (0.25, 0.50),
    "local_polish_sweeps": (1, 2),
}
M4_DECAY_KNOBS: Mapping[str, tuple[Any, ...]] = {
    "alpha_lookahead": (0.10, 0.20, 0.35),
    "rho": (0.50, 0.70),
    "forecast_gate_candidate_budget": (1, 2, 4),
}
STRUCTURAL_DEFAULTS: Mapping[str, Any] = {
    "theta_capacity": 0.90,
    "return_candidate_limit": 6,
    "return_assignment_k": 4,
    "crossover_rate": 0.25,
    "local_polish_sweeps": 1,
    "direct_enumeration_limit": 512,
    # Start profile/decision racing with the bounded fast projection.  The M4
    # lookahead stage then compares 1/2/4 and may restore the full four-site
    # physical minimum when its quality gain justifies the time.  dist_223 is
    # recorded separately as a full-coverage sentinel, not ranked here.
    "forecast_gate_candidate_budget": 1,
    "alpha_lookahead": 0.10,
    "rho": 0.60,
    "max_horizon": 8,
}

# These are the only candidate-controlled fields applied to a complete formal
# setting.  Keeping one shared list lets the runner freeze the *actual* current
# M3/M4 settings as validation incumbents without accidentally omitting a knob.
FORMAL_CANDIDATE_KEYS = (
    "population_size", "iterations", "neighbor_sample_size",
    "neighbors_per_solution", "elite_count", "early_stop_patience",
    "max_unique_evaluations", "theta_capacity", "return_candidate_limit",
    "return_assignment_k", "crossover_rate", "local_polish_sweeps",
    "direct_enumeration_limit", "forecast_gate_candidate_budget",
    "alpha_lookahead",
)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def config_id(config: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in config.items()
               if key != "candidate_id"}
    return hashlib.sha256(_stable_json(payload).encode()).hexdigest()[:16]


def _with_id(config: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(config)
    value["candidate_id"] = config_id(value)
    return value


def _method_config(method: str, profile: str) -> dict[str, Any]:
    if method not in METHODS:
        raise ValueError(f"unknown tuning method {method!r}")
    if profile not in SEARCH_PROFILES:
        raise ValueError(f"unknown search profile {profile!r}")
    value = {
        "protocol_id": PROTOCOL_ID,
        "method": method,
        "stage": "search_profile",
        "search_profile": profile,
        **STRUCTURAL_DEFAULTS,
        **SEARCH_PROFILES[profile],
    }
    value["max_horizon"] = 0 if method == "M3" else 8
    return _with_id(value)


def search_profile_candidates(method: str) -> list[dict[str, Any]]:
    """Return the five preregistered search profiles in table order."""
    return [_method_config(method, profile) for profile in SEARCH_PROFILES]


def incumbent_candidate(method: str, setting: Mapping[str, Any]
                        ) -> dict[str, Any]:
    """Freeze the current formal setting as an explicit validation candidate.

    The search profiles intentionally start from a bounded, cheap projection,
    which need not equal the compiler's currently tracked setting.  This
    helper extracts the real tunable values from that setting so validation
    can enforce the registered no-regression gate.
    """
    if method not in METHODS:
        raise ValueError(f"unknown tuning method {method!r}")
    value = dict(setting)
    validate_schema2_setting(value)
    expected_method_id = "ours_nl" if method == "M3" else "ours_lk"
    if value.get("method_id") != expected_method_id:
        raise ValueError("incumbent setting belongs to another method")
    lookahead = value.get("lookahead_horizon")
    if not isinstance(lookahead, Mapping):
        raise ValueError("incumbent setting lacks decay lookahead metadata")
    expected_horizon = 0 if method == "M3" else 8
    if int(lookahead.get("max_horizon", -1)) != expected_horizon:
        raise ValueError("incumbent setting has the wrong method horizon")
    candidate = {
        "protocol_id": PROTOCOL_ID,
        "method": method,
        "stage": "incumbent_validation",
        "search_profile": "tracked_pre_tuning_incumbent",
        **{key: value[key] for key in FORMAL_CANDIDATE_KEYS},
        "rho": float(lookahead["rho"]),
        "max_horizon": expected_horizon,
    }
    return _with_id(candidate)


def decision_candidates(
        method: str, promoted: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Expand promoted profiles with a bounded one-factor decision design."""
    if method not in METHODS:
        raise ValueError(f"unknown tuning method {method!r}")
    if len(promoted) != 2:
        raise ValueError("decision expansion requires exactly two profiles")
    values: dict[str, dict[str, Any]] = {}
    for parent in promoted:
        if parent.get("method") != method:
            raise ValueError("promoted profile belongs to another method")
        base = {key: value for key, value in parent.items()
                if key != "candidate_id"}
        base["stage"] = "decision"
        base["parent_candidate_id"] = parent["candidate_id"]
        candidates = [base]
        for key, options in COMMON_KNOBS.items():
            for option in options:
                if option == base[key]:
                    continue
                trial = dict(base)
                trial[key] = option
                candidates.append(trial)
        for candidate in candidates:
            identified = _with_id(candidate)
            values[identified["candidate_id"]] = identified
    return [values[key] for key in sorted(values)]


def lookahead_candidates(
        promoted: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Expand two M4 candidates over decay pairs and rollout support budgets."""
    if len(promoted) != 2:
        raise ValueError("lookahead expansion requires exactly two candidates")
    values: dict[str, dict[str, Any]] = {}
    for parent in promoted:
        if parent.get("method") != "M4":
            raise ValueError("lookahead expansion only accepts M4 candidates")
        for alpha in M4_DECAY_KNOBS["alpha_lookahead"]:
            for rho in M4_DECAY_KNOBS["rho"]:
                for gate_budget in M4_DECAY_KNOBS[
                        "forecast_gate_candidate_budget"]:
                    value = {key: item for key, item in parent.items()
                             if key != "candidate_id"}
                    value.update({
                        "stage": "lookahead",
                        "parent_candidate_id": parent["candidate_id"],
                        "alpha_lookahead": alpha,
                        "rho": rho,
                        "forecast_gate_candidate_budget": gate_budget,
                        "max_horizon": 8,
                    })
                    identified = _with_id(value)
                    values[identified["candidate_id"]] = identified
    return [values[key] for key in sorted(values)]


@dataclass(frozen=True, slots=True)
class RacingTrial:
    candidate_id: str
    method: str
    dataset: str
    circuit: str
    seed: int
    status: str
    verifier_ok: bool
    ghost_hits: int
    fallback: bool
    log_fidelity: float | None
    baseline_log_fidelity: float | None
    move_time_us: float | None
    move_batches: int | None
    transition_decision_ns: int | None
    fidelity_ood: bool = False
    exponential_sensitivity_log_fidelity: float | None = None
    baseline_fidelity_ood: bool = False
    baseline_exponential_sensitivity_log_fidelity: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RacingTrial":
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown quality-racing fields: {sorted(unknown)}")
        return cls(**value)

    @property
    def valid(self) -> bool:
        return self.valid_for("exponential" if (
            self.fidelity_ood or self.baseline_fidelity_ood) else "linear")

    def valid_for(self, model: str) -> bool:
        if model not in {"linear", "exponential"}:
            raise ValueError(f"unknown racing quality model {model!r}")
        quality = (
            (self.exponential_sensitivity_log_fidelity,
             self.baseline_exponential_sensitivity_log_fidelity)
            if model == "exponential"
            else (self.log_fidelity, self.baseline_log_fidelity))
        numeric = (*quality, self.move_time_us, self.transition_decision_ns)
        return bool(
            self.status == "success" and self.verifier_ok and
            self.ghost_hits == 0 and not self.fallback and
            all(value is not None and math.isfinite(float(value))
                for value in numeric) and
            float(self.move_time_us) >= 0 and
            float(self.transition_decision_ns) >= 0 and
            isinstance(self.move_batches, int) and
            not isinstance(self.move_batches, bool) and
            self.move_batches >= 0)

    @property
    def delta_log_fidelity(self) -> float:
        model = ("exponential" if (
            self.fidelity_ood or self.baseline_fidelity_ood) else "linear")
        return self.delta_for(model)

    def delta_for(self, model: str) -> float:
        if not self.valid_for(model):
            raise ValueError("invalid racing row has no quality delta")
        if model == "exponential":
            return (float(self.exponential_sensitivity_log_fidelity)
                    - float(self.baseline_exponential_sensitivity_log_fidelity))
        return float(self.log_fidelity) - float(self.baseline_log_fidelity)


def _median(values: Iterable[float]) -> float:
    materialized = list(values)
    if not materialized:
        raise ValueError("cannot take median of empty values")
    return float(statistics.median(materialized))


def _lower_quantile(values: Iterable[float], probability: float) -> float:
    """Return a deterministic linearly interpolated lower-tail quantile."""
    materialized = sorted(float(value) for value in values)
    if not materialized:
        raise ValueError("cannot take quantile of empty values")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("quantile probability is outside [0, 1]")
    position = probability * (len(materialized) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return materialized[lower]
    fraction = position - lower
    return float(
        materialized[lower] * (1.0 - fraction)
        + materialized[upper] * fraction)


def summarize_candidates(
        trials: Sequence[RacingTrial], *, candidate_ids: Sequence[str],
        method: str, circuits: Sequence[str], seeds: Sequence[int]
) -> list[dict[str, Any]]:
    """Summarize a complete paired cohort at the circuit level."""
    expected = {(candidate, circuit, seed)
                for candidate in candidate_ids for circuit in circuits
                for seed in seeds}
    observed: dict[tuple[str, str, int], RacingTrial] = {}
    for row in trials:
        if row.method != method:
            continue
        key = (row.candidate_id, row.circuit, row.seed)
        if key not in expected:
            continue
        if key in observed:
            raise ValueError(f"duplicate quality-racing trial {key}")
        observed[key] = row
    summaries = []
    quality_models = {
        circuit: ("exponential" if any(
            row.circuit == circuit and (
                row.fidelity_ood or row.baseline_fidelity_ood)
            for row in observed.values()) else "linear")
        for circuit in circuits
    }
    for candidate in candidate_ids:
        keys = {(candidate, circuit, seed)
                for circuit in circuits for seed in seeds}
        complete = keys <= set(observed)
        rows = [observed[key] for key in sorted(keys) if key in observed]
        valid = complete and all(
            row.valid_for(quality_models[row.circuit]) for row in rows)
        summary: dict[str, Any] = {
            "candidate_id": candidate,
            "valid": valid,
            "coverage": f"{sum(row.valid_for(quality_models[row.circuit]) for row in rows)}/{len(keys)}",
        }
        if valid:
            per_circuit = []
            for circuit in circuits:
                circuit_rows = [observed[(candidate, circuit, seed)]
                                for seed in seeds]
                per_circuit.append({
                    "circuit": circuit,
                    "delta_log_fidelity": _median(
                        row.delta_for(quality_models[circuit])
                        for row in circuit_rows),
                    "quality_model": quality_models[circuit],
                    "move_time_us": _median(
                        float(row.move_time_us) for row in circuit_rows),
                    "move_batches": _median(
                        float(row.move_batches) for row in circuit_rows),
                    "transition_decision_ns": _median(
                        float(row.transition_decision_ns)
                        for row in circuit_rows),
                })
            deltas = [row["delta_log_fidelity"] for row in per_circuit]
            summary.update({
                # The final fidelity geometric mean is exactly exp(mean ΔlogF).
                # Keep median for robustness reporting, but never use it as the
                # primary configuration-selection objective.
                "mean_delta_log_fidelity": float(statistics.fmean(deltas)),
                "p10_delta_log_fidelity": _lower_quantile(deltas, 0.10),
                "worst_delta_log_fidelity": min(deltas),
                "median_delta_log_fidelity": _median(deltas),
                "median_move_time_us": _median(
                    row["move_time_us"] for row in per_circuit),
                "median_move_batches": _median(
                    row["move_batches"] for row in per_circuit),
                "median_transition_decision_ns": _median(
                    row["transition_decision_ns"] for row in per_circuit),
                "per_circuit": per_circuit,
            })
        summaries.append(summary)
    return summaries


def _quality_order(row: Mapping[str, Any]) -> tuple:
    if not row["valid"]:
        return (1, math.inf, math.inf, math.inf, math.inf,
                row["candidate_id"])
    return (
        0,
        -float(row["mean_delta_log_fidelity"]),
        -float(row["p10_delta_log_fidelity"]),
        -float(row["worst_delta_log_fidelity"]),
        float(row["median_transition_decision_ns"]),
        float(row["median_move_time_us"]),
        float(row["median_move_batches"]),
        row["candidate_id"],
    )


def race_checkpoint(
        trials: Sequence[RacingTrial], *, active_candidate_ids: Sequence[str],
        method: str, completed_circuits: Sequence[str]
) -> dict[str, Any]:
    """Eliminate candidates only at five-circuit cumulative checkpoints."""
    if not completed_circuits:
        raise ValueError("race checkpoints require completed circuits")
    summaries = summarize_candidates(
        trials, candidate_ids=active_candidate_ids, method=method,
        circuits=completed_circuits, seeds=DEVELOPMENT_SEED)
    valid = sorted((row for row in summaries if row["valid"]),
                   key=_quality_order)
    if not valid:
        raise RuntimeError("quality race has no valid candidate")
    leader = valid[0]
    eliminated = []
    retained = []
    for row in summaries:
        if not row["valid"]:
            eliminated.append({
                "candidate_id": row["candidate_id"],
                "reason": "invalid_or_incomplete",
            })
            continue
        quality_gap = (
            float(leader["mean_delta_log_fidelity"])
            - float(row["mean_delta_log_fidelity"]))
        if quality_gap > ELIMINATION_LOGF_MARGIN:
            eliminated.append({
                "candidate_id": row["candidate_id"],
                "reason": "quality_margin_without_move_advantage",
                "quality_gap": quality_gap,
            })
        else:
            retained.append(row["candidate_id"])
    return {
        "protocol_id": PROTOCOL_ID,
        "method": method,
        "completed_circuits": list(completed_circuits),
        "leader": leader["candidate_id"],
        "retained": retained,
        "eliminated": eliminated,
        "summaries": summaries,
    }


def select_top(
        trials: Sequence[RacingTrial], *, candidate_ids: Sequence[str],
        method: str, circuits: Sequence[str], seeds: Sequence[int],
        count: int = 2) -> dict[str, Any]:
    """Select mean-logF leaders, then prefer tail safety and runtime."""
    summaries = summarize_candidates(
        trials, candidate_ids=candidate_ids, method=method,
        circuits=circuits, seeds=seeds)
    valid = [row for row in summaries if row["valid"]]
    if len(valid) < count:
        raise RuntimeError(
            f"{method} has only {len(valid)} valid candidates; {count} required")
    remaining = list(valid)
    selected = []
    while remaining and len(selected) < count:
        best_quality = max(float(row["mean_delta_log_fidelity"])
                           for row in remaining)
        near = [row for row in remaining
                if best_quality - float(row["mean_delta_log_fidelity"])
                <= NEAR_OPTIMAL_LOGF]
        winner = min(near, key=lambda row: (
            -float(row["p10_delta_log_fidelity"]),
            -float(row["worst_delta_log_fidelity"]),
            float(row["median_transition_decision_ns"]),
            float(row["median_move_time_us"]),
            float(row["median_move_batches"]),
            row["candidate_id"],
        ))
        selected.append(winner["candidate_id"])
        remaining = [row for row in remaining
                     if row["candidate_id"] != winner["candidate_id"]]
    return {
        "protocol_id": PROTOCOL_ID,
        "method": method,
        "selected": selected,
        "summaries": sorted(summaries, key=_quality_order),
    }


def select_non_degrading(
        trials: Sequence[RacingTrial], *, candidate_ids: Sequence[str],
        incumbent_candidate_id: str, method: str, circuits: Sequence[str],
        seeds: Sequence[int]) -> dict[str, Any]:
    """Select one winner after hard overall and per-dataset quality gates.

    Runtime and Move metrics may break a quality tie only after a candidate has
    demonstrated that its circuit-level mean delta-log-fidelity is no lower
    than the frozen incumbent both overall and within every represented
    dataset.  In particular, a QMAP improvement may not purchase a ZAC
    regression (or vice versa), and the ordinary 0.002 near-optimal runtime
    band cannot purchase a quality regression relative to the current
    implementation.
    """
    ids = list(dict.fromkeys(str(value) for value in candidate_ids))
    if incumbent_candidate_id not in ids:
        raise ValueError("validation cohort does not contain the incumbent")
    selected_circuits = set(str(value) for value in circuits)
    datasets_by_circuit: dict[str, set[str]] = {
        circuit: set() for circuit in selected_circuits}
    for row in trials:
        if (row.method == method and row.circuit in selected_circuits
                and row.candidate_id in ids and row.seed in seeds):
            datasets_by_circuit[row.circuit].add(str(row.dataset))
    malformed = {
        circuit: sorted(datasets) for circuit, datasets in datasets_by_circuit.items()
        if len(datasets) != 1
    }
    if malformed:
        raise ValueError(
            f"validation circuits do not map to exactly one dataset: {malformed}")
    circuits_by_dataset: dict[str, list[str]] = {}
    for circuit in circuits:
        dataset = next(iter(datasets_by_circuit[str(circuit)]))
        circuits_by_dataset.setdefault(dataset, []).append(str(circuit))

    summaries = summarize_candidates(
        trials, candidate_ids=ids, method=method,
        circuits=circuits, seeds=seeds)
    by_id = {str(row["candidate_id"]): row for row in summaries}
    incumbent = by_id[incumbent_candidate_id]
    if not incumbent["valid"]:
        raise RuntimeError(f"{method} incumbent is invalid or incomplete")
    incumbent_quality = float(incumbent["mean_delta_log_fidelity"])
    dataset_summaries = {
        dataset: summarize_candidates(
            trials, candidate_ids=ids, method=method,
            circuits=dataset_circuits, seeds=seeds)
        for dataset, dataset_circuits in sorted(circuits_by_dataset.items())
    }
    dataset_by_id = {
        dataset: {str(row["candidate_id"]): row for row in values}
        for dataset, values in dataset_summaries.items()
    }
    incumbent_quality_by_dataset = {}
    for dataset, values in dataset_by_id.items():
        row = values[incumbent_candidate_id]
        if not row["valid"]:
            raise RuntimeError(
                f"{method} incumbent is invalid or incomplete on {dataset}")
        incumbent_quality_by_dataset[dataset] = float(
            row["mean_delta_log_fidelity"])

    def non_degrading(candidate_id: str) -> bool:
        if (not by_id[candidate_id]["valid"]
                or float(by_id[candidate_id]["mean_delta_log_fidelity"])
                + INCUMBENT_NON_DEGRADATION_TOLERANCE < incumbent_quality):
            return False
        return all(
            dataset_by_id[dataset][candidate_id]["valid"]
            and float(dataset_by_id[dataset][candidate_id][
                "mean_delta_log_fidelity"])
            + INCUMBENT_NON_DEGRADATION_TOLERANCE
            >= incumbent_quality_by_dataset[dataset]
            for dataset in dataset_by_id
        )

    eligible = [
        candidate_id for candidate_id in ids
        if non_degrading(candidate_id)
    ]
    if incumbent_candidate_id not in eligible:
        raise AssertionError("valid incumbent was removed by its own quality gate")
    ranked = select_top(
        trials, candidate_ids=eligible, method=method,
        circuits=circuits, seeds=seeds, count=1)
    winner_id = str(ranked["selected"][0])
    winner_quality = float(by_id[winner_id]["mean_delta_log_fidelity"])
    if (winner_quality + INCUMBENT_NON_DEGRADATION_TOLERANCE
            < incumbent_quality):
        raise AssertionError("selected validation winner degrades the incumbent")
    winner_quality_by_dataset = {
        dataset: float(values[winner_id]["mean_delta_log_fidelity"])
        for dataset, values in dataset_by_id.items()
    }
    incumbent_median = float(incumbent["median_delta_log_fidelity"])
    winner_median = float(by_id[winner_id]["median_delta_log_fidelity"])
    incumbent_median_by_dataset = {
        dataset: float(values[incumbent_candidate_id][
            "median_delta_log_fidelity"])
        for dataset, values in dataset_by_id.items()
    }
    winner_median_by_dataset = {
        dataset: float(values[winner_id]["median_delta_log_fidelity"])
        for dataset, values in dataset_by_id.items()
    }
    if any(
            winner_quality_by_dataset[dataset]
            + INCUMBENT_NON_DEGRADATION_TOLERANCE
            < incumbent_quality_by_dataset[dataset]
            for dataset in dataset_by_id):
        raise AssertionError(
            "selected validation winner degrades a dataset incumbent")
    return {
        "protocol_id": PROTOCOL_ID,
        "method": method,
        "selected": [winner_id],
        "incumbent_candidate_id": incumbent_candidate_id,
        "incumbent_mean_delta_log_fidelity": incumbent_quality,
        "winner_mean_delta_log_fidelity": winner_quality,
        "incumbent_mean_delta_log_fidelity_by_dataset":
            incumbent_quality_by_dataset,
        "winner_mean_delta_log_fidelity_by_dataset":
            winner_quality_by_dataset,
        # Retain the actual medians for robustness reporting and old readers;
        # they are descriptive only and no longer drive selection.
        "incumbent_median_delta_log_fidelity": incumbent_median,
        "winner_median_delta_log_fidelity": winner_median,
        "incumbent_median_delta_log_fidelity_by_dataset":
            incumbent_median_by_dataset,
        "winner_median_delta_log_fidelity_by_dataset":
            winner_median_by_dataset,
        "non_degradation_tolerance": INCUMBENT_NON_DEGRADATION_TOLERANCE,
        "non_degradation_passed": True,
        "eligible_candidate_ids": eligible,
        "degraded_or_invalid_candidate_ids": [
            candidate_id for candidate_id in ids
            if candidate_id not in eligible
        ],
        "dataset_summaries": dataset_summaries,
        "summaries": summaries,
    }


def shared_forward_pair(m4_config: Mapping[str, Any]
                        ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a horizon-only pair for the small 30-circuit forward check."""
    if m4_config.get("method") != "M4":
        raise ValueError("shared forward pair must be derived from M4")
    common = {key: value for key, value in m4_config.items()
              if key not in {"candidate_id", "method", "max_horizon"}}
    m3 = _with_id({**common, "method": "M3", "max_horizon": 0,
                   "stage": "shared_forward_check"})
    m4 = _with_id({**common, "method": "M4", "max_horizon": 8,
                   "stage": "shared_forward_check"})
    return m3, m4


def materialize_formal_setting(base: Mapping[str, Any],
                               candidate: Mapping[str, Any], *, seed: int,
                               output_dir: str) -> dict[str, Any]:
    """Apply one selected candidate to a complete formal Schema-2 setting."""
    method = candidate["method"]
    method_id = "ours_nl" if method == "M3" else "ours_lk"
    setting = dict(base)
    for key in FORMAL_CANDIDATE_KEYS:
        setting[key] = candidate[key]
    setting.update({
        "method_id": method_id,
        "seed": int(seed),
        "dir": output_dir,
        "tuning_protocol_id": PROTOCOL_ID,
        "lookahead_horizon": decay_lookahead_spec(
            int(candidate["max_horizon"]), rho=float(candidate["rho"])),
    })
    validate_schema2_setting(setting)
    return setting


def validate_shared_formal_settings(m3: Mapping[str, Any],
                                    m4: Mapping[str, Any]) -> None:
    validate_schema2_pair(dict(m3), dict(m4))


def split_manifest() -> dict[str, Any]:
    payload = {
        "experiment_schema": 2,
        "protocol_id": PROTOCOL_ID,
        "development": {
            "ZAC18": list(ZAC_DEVELOPMENT),
            "QMAP154": list(QMAP_DEVELOPMENT),
        },
        "development_tuning": {
            "ZAC18": [circuit for circuit in ZAC_DEVELOPMENT
                      if circuit not in DEVELOPMENT_COVERAGE_ONLY],
            "QMAP154": [circuit for circuit in QMAP_DEVELOPMENT
                        if circuit not in DEVELOPMENT_COVERAGE_ONLY],
        },
        "development_coverage_only": {
            "ZAC18": [circuit for circuit in DEVELOPMENT_COVERAGE_ONLY
                      if circuit in ZAC_DEVELOPMENT],
            "QMAP154": [circuit for circuit in DEVELOPMENT_COVERAGE_ONLY
                        if circuit in QMAP_DEVELOPMENT],
        },
        "coverage_only_rule": (
            "measured 600-s timeout sentinels and their same-family sibling "
            "are excluded from hyperparameter ranking but retained for "
            "selected-method full coverage"),
        "validation": {
            "ZAC18": list(ZAC_VALIDATION),
            "QMAP154": list(QMAP_VALIDATION),
        },
        "holdout_rule": "all remaining ZAC18/QMAP154 circuits",
        "development_seeds": list(DEVELOPMENT_SEED),
        "validation_seeds": list(VALIDATION_SEEDS),
    }
    payload["sha256"] = hashlib.sha256(
        _stable_json(payload).encode()).hexdigest()
    return payload


def search_space_manifest() -> dict[str, Any]:
    payload = {
        "experiment_schema": 2,
        "protocol_id": PROTOCOL_ID,
        "search_profiles": {
            key: dict(value) for key, value in SEARCH_PROFILES.items()},
        "common_knobs": {
            key: list(value) for key, value in COMMON_KNOBS.items()},
        "m4_decay_knobs": {
            key: list(value) for key, value in M4_DECAY_KNOBS.items()},
        "direct_enumeration_limit": 512,
        "design": "sequential-profile-then-one-factor-then-m4-decay",
        "race_block_size": RACE_BLOCK_SIZE,
        "elimination_logf_margin": ELIMINATION_LOGF_MARGIN,
        "near_optimal_logf": NEAR_OPTIMAL_LOGF,
    }
    payload["sha256"] = hashlib.sha256(
        _stable_json(payload).encode()).hexdigest()
    return payload


def write_protocol_files(output_directory: Path) -> dict[str, str]:
    output_directory.mkdir(parents=True, exist_ok=True)
    payloads = {
        "split_manifest.json": split_manifest(),
        "tuning_space.json": search_space_manifest(),
    }
    paths = {}
    for name, payload in payloads.items():
        path = output_directory / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        paths[name] = str(path)
    return paths


__all__ = [
    "COMMON_KNOBS", "DEVELOPMENT_CIRCUITS", "DEVELOPMENT_COVERAGE_ONLY",
    "DEVELOPMENT_SEED", "DEVELOPMENT_TUNING_CIRCUITS",
    "ELIMINATION_LOGF_MARGIN", "FORMAL_CANDIDATE_KEYS",
    "INCUMBENT_NON_DEGRADATION_TOLERANCE", "M4_DECAY_KNOBS", "METHODS",
    "PROTOCOL_ID", "QMAP_DEVELOPMENT", "QMAP_VALIDATION",
    "RACE_BLOCK_SIZE", "RacingTrial", "SEARCH_PROFILES",
    "STRUCTURAL_DEFAULTS", "VALIDATION_CIRCUITS", "VALIDATION_SEEDS",
    "ZAC_DEVELOPMENT", "ZAC_VALIDATION", "config_id",
    "decision_candidates", "incumbent_candidate", "lookahead_candidates",
    "materialize_formal_setting", "race_checkpoint", "search_profile_candidates",
    "search_space_manifest", "select_non_degrading", "select_top", "shared_forward_pair",
    "split_manifest", "summarize_candidates", "validate_shared_formal_settings",
    "write_protocol_files",
]
