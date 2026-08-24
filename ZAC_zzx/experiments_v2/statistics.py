"""Pre-registered Schema-2 statistics and publication claim gates.

Coverage, quality, and timing records are always evaluated in separate
``run_kind`` cohorts.  Claims are disabled unless the frozen circuit list is
supplied explicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from .contracts import RunManifest, RunStatus, load_run_manifest
from .protocol import (ghost_policy_for_method,
                       FORMAL_QUALITY_SEEDS, FORMAL_TIMING_REPETITIONS,
                       physicalization_policy_for_method,
                       trace_protocol_for_method)


METHODS = ("M1", "M2", "M3", "M4")
QUALITY_SEEDS = frozenset(FORMAL_QUALITY_SEEDS)
TIMING_REPETITIONS = frozenset(range(FORMAL_TIMING_REPETITIONS))
TERMINAL_STATUSES = tuple(item.value for item in RunStatus)


def geometric_mean_from_logs(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("geometric mean requires observations")
    return math.exp(math.fsum(values) / len(values))


def wilson_interval(successes: int, attempts: int,
                    z: float = 1.959963984540054) -> Tuple[float, float]:
    if attempts <= 0 or not 0 <= successes <= attempts:
        raise ValueError("invalid coverage counts")
    p = successes / attempts
    denominator = 1.0 + z * z / attempts
    center = (p + z * z / (2.0 * attempts)) / denominator
    radius = z * math.sqrt(
        (p * (1.0 - p) + z * z / (4.0 * attempts)) / attempts) / denominator
    return center - radius, center + radius


def holm_adjust(p_values: Mapping[str, float]) -> Dict[str, float]:
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    adjusted: Dict[str, float] = {}
    running = 0.0
    total = len(ordered)
    for rank, (name, value) in enumerate(ordered):
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"invalid p-value for {name}: {value}")
        running = max(running, min(1.0, (total - rank) * value))
        adjusted[name] = running
    return adjusted


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("empty percentile")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("percentile probability must be in [0,1]")
    position = (len(sorted_values) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def _stratified_samples(values: Mapping[str, float], strata: Mapping[str, str],
                        *, iterations: int, seed: int) -> List[float]:
    if iterations <= 0:
        raise ValueError("bootstrap iterations must be positive")
    if set(values) - set(strata):
        raise ValueError("missing stratum labels")
    grouped: MutableMapping[str, List[float]] = defaultdict(list)
    for circuit, value in values.items():
        if not math.isfinite(float(value)):
            raise ValueError(f"non-finite bootstrap value for {circuit}")
        grouped[strata[circuit]].append(float(value))
    if not grouped:
        raise ValueError("empty paired cohort")
    rng = random.Random(seed)
    samples: List[float] = []
    for _ in range(iterations):
        picked: List[float] = []
        for label in sorted(grouped):
            group = grouped[label]
            picked.extend(group[rng.randrange(len(group))] for _ in group)
        samples.append(math.fsum(picked) / len(picked))
    samples.sort()
    return samples


def stratified_bootstrap_log_ratio(
        differences: Mapping[str, float], strata: Mapping[str, str],
        *, iterations: int = 10_000, seed: int = 0,
        simultaneous_family_size: int = 1) -> Mapping[str, float | int | str]:
    """Return ordinary 95% and conservative simultaneous ratio intervals."""
    if simultaneous_family_size <= 0:
        raise ValueError("simultaneous family size must be positive")
    samples = _stratified_samples(
        differences, strata, iterations=iterations, seed=seed)
    point_log = math.fsum(differences.values()) / len(differences)
    alpha = 0.05 / simultaneous_family_size
    return {
        "ratio": math.exp(point_log),
        "ci95_low": math.exp(_percentile(samples, 0.025)),
        "ci95_high": math.exp(_percentile(samples, 0.975)),
        "simultaneous95_low": math.exp(_percentile(samples, alpha / 2.0)),
        "simultaneous95_high": math.exp(_percentile(samples, 1.0 - alpha / 2.0)),
        "log_difference": point_log,
        "iterations": iterations,
        "simultaneous_method": (
            f"Bonferroni 95% family-wise interval over {simultaneous_family_size} "
            f"comparisons ({100 * (1 - alpha):.1f}% per comparison)"),
    }


def paired_wilcoxon(differences: Sequence[float]) -> Mapping[str, float | int | str]:
    if any(not math.isfinite(value) for value in differences):
        raise ValueError("Wilcoxon differences must be finite")
    nonzero = [value for value in differences if abs(value) > 1e-15]
    wins = sum(value > 1e-15 for value in differences)
    losses = sum(value < -1e-15 for value in differences)
    ties = len(differences) - wins - losses
    ranked = sorted(enumerate(nonzero), key=lambda item: abs(item[1]))
    signed_ranks = [0.0] * len(nonzero)
    cursor = 0
    while cursor < len(ranked):
        end = cursor + 1
        while end < len(ranked) and abs(ranked[end][1]) == abs(ranked[cursor][1]):
            end += 1
        average_rank = ((cursor + 1) + end) / 2.0
        for original_index, _ in ranked[cursor:end]:
            signed_ranks[original_index] = average_rank
        cursor = end
    positive = math.fsum(rank for rank, value in zip(signed_ranks, nonzero) if value > 0)
    negative = math.fsum(rank for rank, value in zip(signed_ranks, nonzero) if value < 0)
    effect = (positive - negative) / (positive + negative) if positive + negative else 0.0
    test_name = "Wilcoxon signed-rank, one-sided greater"
    if nonzero:
        try:
            import scipy
            from scipy.stats import wilcoxon
            statistic, p_value = wilcoxon(
                nonzero, alternative="greater", zero_method="wilcox")
            statistic, p_value = float(statistic), float(p_value)
        except ImportError as error:
            raise RuntimeError(
                "formal paired Wilcoxon requires scipy; statistical fallback is forbidden"
            ) from error
        except ValueError as error:
            raise RuntimeError(
                "scipy.stats.wilcoxon rejected the pre-registered paired sample"
            ) from error
        scipy_version = str(scipy.__version__)
    else:
        statistic, p_value = 0.0, 1.0
        try:
            import scipy
        except ImportError as error:
            raise RuntimeError(
                "formal paired Wilcoxon requires scipy even for an all-tie sample"
            ) from error
        scipy_version = str(scipy.__version__)
    return {
        "test": test_name, "statistic": statistic, "p_value": p_value,
        "rank_biserial": effect, "wins": wins, "ties": ties, "losses": losses,
        "implementation": "scipy.stats.wilcoxon",
        "scipy_version": scipy_version,
        "alternative": "greater", "zero_method": "wilcox",
    }


def _suite_hash(circuits: Sequence[str]) -> str:
    payload = json.dumps(list(circuits), separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _status_counts(runs: Sequence[RunManifest], missing: int = 0,
                   duplicates: int = 0) -> Dict[str, int]:
    counts = Counter(run.status for run in runs)
    result = {status: int(counts.get(status, 0)) for status in TERMINAL_STATUSES}
    result.update(missing=int(missing), duplicate=int(duplicates))
    return result


def _coverage_threshold(dataset: str) -> float:
    lowered = dataset.lower()
    if "qmap" in lowered:
        return 0.95
    return 1.0


def _default_stratum(dataset: str, manifest: RunManifest) -> str:
    if dataset.lower().startswith(("zac", "hpca")) and manifest.qubits is not None:
        return ("le32" if manifest.qubits <= 32 else
                "33to64" if manifest.qubits <= 64 else "gt64")
    gates = manifest.expected_gates_2q
    if gates is not None:
        return "le300" if gates <= 300 else "301to1500" if gates <= 1500 else "gt1500"
    return "unstratified"


def _provenance_errors(manifests: Sequence[RunManifest],
                       circuits: Sequence[str]) -> List[str]:
    errors: List[str] = []
    if any(run.git_dirty for run in manifests):
        errors.append("formal aggregate contains a dirty Git run")
    commits = {run.git_commit for run in manifests}
    if "" in commits or "unknown" in commits or len(commits) != 1:
        errors.append(f"Git commit is not frozen: {sorted(commits)}")
    for field_name in ("architecture_sha256", "model_sha256"):
        values = {getattr(run, field_name) for run in manifests}
        if "" in values or len(values) != 1:
            errors.append(f"{field_name} is not frozen: {sorted(values)}")
    for circuit in circuits:
        values = {run.input_sha256 for run in manifests if run.circuit == circuit}
        if values and ("" in values or len(values) != 1):
            errors.append(f"input_sha256 differs for {circuit}: {sorted(values)}")
    # Resolved configs intentionally include the algorithm seed.  They may
    # differ between seeds, but must be identical across circuits/repetitions
    # for a fixed (run kind, method, seed).
    for key in sorted({(run.run_kind, run.method, run.seed) for run in manifests}):
        values = {run.config_sha256 for run in manifests
                  if (run.run_kind, run.method, run.seed) == key}
        if "" in values or len(values) != 1:
            errors.append(f"config_sha256 is not frozen for {key}: {sorted(values)}")
    return errors


def _coverage_report(runs: Sequence[RunManifest], circuits: Sequence[str],
                     dataset: str) -> Mapping[str, object]:
    if not runs:
        return {"available": False, "valid": 0, "N": len(circuits),
                "status": "not_run", "methods": {}, "gate": {"passed": False}}
    groups: MutableMapping[Tuple[str, str], List[RunManifest]] = defaultdict(list)
    protocol_errors: List[str] = []
    for run in runs:
        groups[(run.circuit, run.method)].append(run)
        if run.seed != 0 or run.repetition != 0:
            protocol_errors.append(
                f"coverage must be seed=0 repetition=0: {run.circuit}/{run.method}/{run.run_id}")
    method_reports: Dict[str, object] = {}
    threshold = _coverage_threshold(dataset)
    for method in METHODS:
        successes = missing = duplicates = 0
        selected: List[RunManifest] = []
        for circuit in circuits:
            records = groups[(circuit, method)]
            selected.extend(records)
            if not records:
                missing += 1
            elif len(records) > 1:
                duplicates += len(records) - 1
            elif (records[0].seed == 0 and records[0].repetition == 0 and
                  records[0].status == RunStatus.SUCCESS.value):
                successes += 1
        total = len(circuits)
        interval = wilson_interval(successes, total) if total else (0.0, 0.0)
        rate = successes / total if total else 0.0
        method_reports[method] = {
            "valid": successes, "N": total,
            "status": "complete" if missing == 0 and duplicates == 0 else "incomplete",
            "attempt_records": len(selected), "rate": rate,
            "wilson95": list(interval), "meets_dataset_threshold": rate >= threshold,
            "status_counts": _status_counts(selected, missing, duplicates),
        }
    m3_rate = float(method_reports["M3"]["rate"])
    m4_rate = float(method_reports["M4"]["rate"])
    explicit_terminal_attempts = all(
        method_reports[method]["status"] == "complete" for method in METHODS)
    passed = (not protocol_errors and explicit_terminal_attempts and
              all(bool(method_reports[m]["meets_dataset_threshold"]) for m in METHODS) and
              m4_rate >= m3_rate)
    return {
        "available": True,
        "valid": min(int(method_reports[m]["valid"]) for m in METHODS),
        "N": len(circuits), "status": "pass" if passed else "fail",
        "dataset_threshold": threshold, "methods": method_reports,
        "protocol_errors": protocol_errors,
        "gate": {
            "passed": passed,
            "all_attempts_have_one_explicit_terminal_record": explicit_terminal_attempts,
            "M4_not_below_M3": m4_rate >= m3_rate,
        },
    }


def _quality_group_valid(method: str,
                         records: Sequence[RunManifest]) -> Tuple[bool, str]:
    if method in ("M1", "M2"):
        if len(records) != 1:
            return False, f"requires exactly one quality record, found {len(records)}"
        if records[0].seed != 0 or records[0].repetition != 0:
            return False, "baseline quality record must be seed=0 repetition=0"
    else:
        if len(records) != len(FORMAL_QUALITY_SEEDS):
            return False, (
                f"requires exactly {len(FORMAL_QUALITY_SEEDS)} quality records, "
                f"found {len(records)}")
        seeds = [record.seed for record in records]
        if set(seeds) != QUALITY_SEEDS or len(set(seeds)) != len(seeds):
            return False, (
                f"requires paired seeds {list(FORMAL_QUALITY_SEEDS)} exactly "
                f"once, found {sorted(seeds)}")
        if any(record.repetition != 0 for record in records):
            return False, "quality repetitions must be zero"
    if any(record.status != RunStatus.SUCCESS.value for record in records):
        return False, "one or more quality attempts failed"
    if any(record.fidelity_ood or record.log_fidelity is None for record in records):
        return False, "linear ZAC fidelity is OOD or missing"
    for metric in ("move_batches", "move_time_us", "compiler_time_ns"):
        if any(getattr(record, metric) is None for record in records):
            return False, f"missing {metric}"
    return True, "valid"


def _median_metric(records: Sequence[RunManifest], field_name: str) -> float:
    return float(statistics.median(float(getattr(record, field_name)) for record in records))


def _per_stratum_ratio(differences: Mapping[str, float],
                       labels: Mapping[str, str]) -> Dict[str, Mapping[str, float | int]]:
    grouped: MutableMapping[str, List[float]] = defaultdict(list)
    for circuit, difference in differences.items():
        grouped[labels[circuit]].append(difference)
    return {label: {"ratio": math.exp(math.fsum(values) / len(values)), "n": len(values)}
            for label, values in sorted(grouped.items())}


def _bootstrap_cost_ratio(ratios: Mapping[str, float], labels: Mapping[str, str],
                          *, iterations: int, seed: int) -> Mapping[str, object]:
    if any(not math.isfinite(value) or value < 0 for value in ratios.values()):
        return {
            "ratio": math.inf, "ci95_low": math.inf, "ci95_high": math.inf,
            "simultaneous95_low": math.inf, "simultaneous95_high": math.inf,
            "iterations": iterations,
            "simultaneous_method": "undefined: zero baseline with positive M4 cost",
        }
    samples = _stratified_samples(ratios, labels, iterations=iterations, seed=seed)
    alpha = 0.05 / 2
    return {
        "ratio": math.fsum(ratios.values()) / len(ratios),
        "ci95_low": _percentile(samples, 0.025),
        "ci95_high": _percentile(samples, 0.975),
        "simultaneous95_low": _percentile(samples, alpha / 2),
        "simultaneous95_high": _percentile(samples, 1 - alpha / 2),
        "iterations": iterations,
        "simultaneous_method": (
            "Bonferroni 95% family-wise interval over 2 move metrics "
            "(97.5% per metric)"),
    }


def _safe_cost_ratio(current: float, baseline: float) -> float:
    if current < 0 or baseline < 0:
        raise ValueError("move costs cannot be negative")
    if baseline == 0:
        return 1.0 if current == 0 else math.inf
    return current / baseline


def _move_comparison(current: Mapping[str, float], baseline: Mapping[str, float],
                     labels: Mapping[str, str], *, iterations: int,
                     seed: int) -> Mapping[str, object]:
    ratios = {circuit: _safe_cost_ratio(current[circuit], baseline[circuit])
              for circuit in current}
    summary = _bootstrap_cost_ratio(ratios, labels, iterations=iterations, seed=seed)
    improvements = [baseline[circuit] - current[circuit] for circuit in current]
    return {**summary, **paired_wilcoxon(improvements), "n": len(ratios)}


def _main_report(runs: Sequence[RunManifest], circuits: Sequence[str], dataset: str,
                 strata: Mapping[str, str], *, bootstrap_iterations: int,
                 bootstrap_seed: int) -> Mapping[str, object]:
    if not runs:
        return {"available": False, "valid": 0, "N": len(circuits),
                "status": "not_run", "gate": {"passed": False}}
    groups: MutableMapping[Tuple[str, str], List[RunManifest]] = defaultdict(list)
    for run in runs:
        groups[(run.circuit, run.method)].append(run)
    protocol: Dict[str, Dict[str, str]] = {method: {} for method in METHODS}
    method_valid = {method: 0 for method in METHODS}
    values: Dict[Tuple[str, str, str], float] = {}
    for circuit in circuits:
        for method in METHODS:
            records = groups[(circuit, method)]
            valid, reason = _quality_group_valid(method, records)
            protocol[method][circuit] = reason
            if not valid:
                continue
            method_valid[method] += 1
            for metric in ("log_fidelity", "move_batches", "move_time_us",
                           "compiler_time_ns"):
                values[(circuit, method, metric)] = _median_metric(records, metric)
    paired = [circuit for circuit in circuits
              if all(protocol[method][circuit] == "valid" for method in METHODS)]
    per_circuit_quality: List[Mapping[str, object]] = []
    for circuit in circuits:
        paired_row = circuit in paired
        for method in METHODS:
            valid = protocol[method][circuit] == "valid"
            row: Dict[str, object] = {
                "circuit": circuit,
                "method": method,
                "paired": paired_row,
                "protocol": protocol[method][circuit],
            }
            if valid:
                log_fidelity = values[(circuit, method, "log_fidelity")]
                row.update({
                    "log_fidelity": log_fidelity,
                    "fidelity": math.exp(log_fidelity),
                    "move_batches": values[(circuit, method, "move_batches")],
                    "move_time_us": values[(circuit, method, "move_time_us")],
                    # Quality-run timing is retained for audit only.  The
                    # publication runtime column is joined from the separate
                    # separate formal timing cohort by the exporter.
                    "quality_compiler_seconds": (
                        values[(circuit, method, "compiler_time_ns")] / 1e9),
                })
            per_circuit_quality.append(row)
    labels = {
        circuit: (strata[circuit] if circuit in strata else
                  _default_stratum(dataset, groups[(circuit, "M4")][0]))
        for circuit in paired
    }
    status_by_method: Dict[str, object] = {}
    for method in METHODS:
        method_runs = [run for run in runs if run.method == method]
        expected = len(circuits) * (
            1 if method in ("M1", "M2") else len(FORMAL_QUALITY_SEEDS))
        status_by_method[method] = {
            "valid": method_valid[method], "N": len(circuits),
            "status": "complete" if method_valid[method] == len(circuits) else "incomplete",
            "status_counts": _status_counts(
                method_runs, max(0, expected - len(method_runs)),
                max(0, len(method_runs) - expected)),
            "invalid_reasons": {circuit: reason
                                for circuit, reason in protocol[method].items()
                                if reason != "valid"},
        }
    if not paired:
        return {
            "available": True, "valid": 0, "N": len(circuits), "status": "fail",
            "paired_cohort": [], "methods": status_by_method,
            "per_circuit_quality": per_circuit_quality,
            "fidelity": {"comparisons": {}}, "move": {},
            "gate": {"passed": False, "reason": "empty strict paired cohort"},
        }

    paired_summary: Dict[str, Mapping[str, float | int]] = {}
    stratum_summary: Dict[str, Dict[str, Mapping[str, float | int]]] = {}
    for method in METHODS:
        log_values = [values[(circuit, method, "log_fidelity")]
                      for circuit in paired]
        paired_summary[method] = {
            "valid": len(paired),
            "N": len(circuits),
            "fidelity_geometric_mean": geometric_mean_from_logs(log_values),
            "move_batches_median": statistics.median(
                values[(circuit, method, "move_batches")] for circuit in paired),
            "move_time_us_median": statistics.median(
                values[(circuit, method, "move_time_us")] for circuit in paired),
            "quality_compiler_seconds_median": statistics.median(
                values[(circuit, method, "compiler_time_ns")] / 1e9
                for circuit in paired),
        }
    for label in sorted(set(labels.values())):
        members = [circuit for circuit in paired if labels[circuit] == label]
        stratum_summary[label] = {}
        for method in METHODS:
            stratum_summary[label][method] = {
                "n": len(members),
                "fidelity_geometric_mean": geometric_mean_from_logs([
                    values[(circuit, method, "log_fidelity")]
                    for circuit in members]),
                "move_batches_median": statistics.median(
                    values[(circuit, method, "move_batches")]
                    for circuit in members),
                "move_time_us_median": statistics.median(
                    values[(circuit, method, "move_time_us")]
                    for circuit in members),
                "quality_compiler_seconds_median": statistics.median(
                    values[(circuit, method, "compiler_time_ns")] / 1e9
                    for circuit in members),
            }
    for method in METHODS:
        status_by_method[method]["paired_summary"] = paired_summary[method]

    bstar_method: Dict[str, str] = {}
    for circuit in paired:
        m1 = values[(circuit, "M1", "log_fidelity")]
        m2 = values[(circuit, "M2", "log_fidelity")]
        bstar_method[circuit] = "M1" if m1 >= m2 else "M2"
    fidelity_differences: Dict[str, Dict[str, float]] = {
        "M4_vs_M3": {}, "M4_vs_Bstar": {}}
    for circuit in paired:
        m4 = values[(circuit, "M4", "log_fidelity")]
        fidelity_differences["M4_vs_M3"][circuit] = (
            m4 - values[(circuit, "M3", "log_fidelity")])
        fidelity_differences["M4_vs_Bstar"][circuit] = (
            m4 - values[(circuit, bstar_method[circuit], "log_fidelity")])

    comparisons: Dict[str, object] = {}
    raw_p: Dict[str, float] = {}
    for offset, name in enumerate(("M4_vs_M3", "M4_vs_Bstar")):
        differences = fidelity_differences[name]
        bootstrap = stratified_bootstrap_log_ratio(
            differences, labels, iterations=bootstrap_iterations,
            seed=bootstrap_seed + offset, simultaneous_family_size=2)
        test = paired_wilcoxon(list(differences.values()))
        raw_p[name] = float(test["p_value"])
        comparisons[name] = {
            **bootstrap, **test, "n": len(differences),
            "strata": _per_stratum_ratio(differences, labels),
        }
    fidelity_gate_parts: Dict[str, bool] = {}
    for name, adjusted_p in holm_adjust(raw_p).items():
        comparison = comparisons[name]
        comparison["holm_p_value"] = adjusted_p
        comparison["point_gain_at_least_2pct"] = float(comparison["ratio"]) >= 1.02
        comparison["simultaneous_lower_above_1"] = (
            float(comparison["simultaneous95_low"]) > 1.0)
        comparison["holm_significant_0.05"] = adjusted_p <= 0.05
        comparison["stratum_consistent"] = all(
            float(item["ratio"]) > 1.0 for item in comparison["strata"].values())
        fidelity_gate_parts[name] = bool(
            comparison["point_gain_at_least_2pct"] and
            comparison["simultaneous_lower_above_1"] and
            comparison["holm_significant_0.05"] and
            comparison["stratum_consistent"])

    move_report: Dict[str, object] = {}
    metric_best_methods: Dict[str, Dict[str, str]] = {}
    for offset, metric in enumerate(("move_batches", "move_time_us")):
        current = {circuit: values[(circuit, "M4", metric)] for circuit in paired}
        m3 = {circuit: values[(circuit, "M3", metric)] for circuit in paired}
        fidelity_bstar = {
            circuit: values[(circuit, bstar_method[circuit], metric)]
            for circuit in paired}
        best_methods: Dict[str, str] = {}
        metric_best: Dict[str, float] = {}
        for circuit in paired:
            m1 = values[(circuit, "M1", metric)]
            m2 = values[(circuit, "M2", metric)]
            best_methods[circuit] = "M1" if m1 <= m2 else "M2"
            metric_best[circuit] = min(m1, m2)
        metric_best_methods[metric] = best_methods
        move_report[metric] = {
            "lower_is_better": True,
            "vs_metric_best_baseline": _move_comparison(
                current, metric_best, labels, iterations=bootstrap_iterations,
                seed=bootstrap_seed + 10 + offset),
            "vs_fidelity_Bstar": _move_comparison(
                current, fidelity_bstar, labels, iterations=bootstrap_iterations,
                seed=bootstrap_seed + 20 + offset),
            "vs_M3": _move_comparison(
                current, m3, labels, iterations=bootstrap_iterations,
                seed=bootstrap_seed + 30 + offset),
        }
    batch_upper = float(move_report["move_batches"]["vs_fidelity_Bstar"]
                        ["simultaneous95_high"])
    time_upper = float(move_report["move_time_us"]["vs_fidelity_Bstar"]
                       ["simultaneous95_high"])
    improved_one = batch_upper < 1.0 or time_upper < 1.0
    both_noninferior = batch_upper <= 1.02 and time_upper <= 1.02
    move_gate = {
        "passed": improved_one or both_noninferior,
        "criterion": (
            "relative to the per-circuit fidelity B*: at least one Bonferroni "
            "simultaneous upper bound <1, or both upper bounds <=1.02"),
        "baseline": "per-circuit fidelity B*",
        "improved_at_least_one": improved_one,
        "both_within_2pct_noninferiority": both_noninferior,
    }
    paired_rate = len(paired) / len(circuits) if circuits else 0.0
    threshold = _coverage_threshold(dataset)
    quality_coverage_pass = paired_rate >= threshold
    fidelity_pass = all(fidelity_gate_parts.values())
    passed = quality_coverage_pass and fidelity_pass and bool(move_gate["passed"])
    return {
        "available": True, "valid": len(paired), "N": len(circuits),
        "status": "pass" if passed else "fail", "paired_cohort": paired,
        "per_circuit_quality": per_circuit_quality,
        "methods": status_by_method,
        "paired_method_summary": paired_summary,
        "stratum_method_summary": stratum_summary,
        "stratum_by_circuit": labels,
        "fidelity": {
            "Bstar_definition": "per-circuit maximum log fidelity among M1 and M2",
            "Bstar_method_by_circuit": bstar_method,
            "comparisons": comparisons,
            "multiple_testing": (
                "Holm-adjusted one-sided p-values for two primary tests; "
                "Bonferroni simultaneous 95% family-wise percentile CIs"),
            "gate": {"passed": fidelity_pass, "comparisons": fidelity_gate_parts},
        },
        "move": {
            "metric_best_definition": (
                "per circuit and per move metric, minimum cost among M1 and M2"),
            "fidelity_Bstar_definition": (
                "move value from the same baseline selected by maximum fidelity"),
            "metric_best_method_by_circuit": metric_best_methods,
            "metrics": move_report, "gate": move_gate,
        },
        "gate": {
            "passed": passed, "paired_rate": paired_rate,
            "paired_rate_threshold": threshold,
            "quality_coverage_passed": quality_coverage_pass,
            "fidelity_passed": fidelity_pass,
            "move_passed": bool(move_gate["passed"]),
        },
    }


def par2_seconds(manifests: Sequence[RunManifest], timeout_seconds: float = 600.0
                 ) -> float:
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    values = []
    for manifest in manifests:
        if manifest.status == RunStatus.SUCCESS.value and manifest.compiler_time_ns is not None:
            values.append(manifest.compiler_time_ns / 1e9)
        else:
            values.append(2.0 * timeout_seconds)
    return statistics.fmean(values) if values else math.nan


def _timing_report(runs: Sequence[RunManifest], circuits: Sequence[str],
                   timeout_seconds: float, *, dataset: str,
                   strata: Mapping[str, str], bootstrap_iterations: int,
                   bootstrap_seed: int) -> Mapping[str, object]:
    if not runs:
        return {"available": False, "valid": 0, "N": len(circuits),
                "status": "not_run", "gate": {"passed": False}}
    groups: MutableMapping[Tuple[str, str], List[RunManifest]] = defaultdict(list)
    for run in runs:
        groups[(run.circuit, run.method)].append(run)
    methods: Dict[str, object] = {}
    all_protocol_complete = True
    all_runtime_observed = True
    circuit_method_medians: Dict[Tuple[str, str], float] = {}
    circuit_canonical_ledgers: Dict[str, set[str]] = defaultdict(set)
    circuit_canonical_counts: Dict[str, set[int]] = defaultdict(set)
    circuit_observed_ledgers: Dict[str, set[str]] = defaultdict(set)
    circuit_observed_counts: Dict[str, set[int]] = defaultdict(set)
    circuit_ledger_proven: Dict[str, bool] = defaultdict(lambda: True)
    accepted_ledger_sources = {
        "M1": {"compiler.gate_scheduling"},
        "M2": {"normalized_qmap_placement_trace",
               "qmap.stats.two_qubit_gate_layers"},
        "M3": {"compiler.gate_scheduling"},
        "M4": {"compiler.gate_scheduling"},
    }

    def quartiles(values: Sequence[float]) -> Tuple[float, float]:
        if len(values) == 1:
            return values[0], values[0]
        cuts = statistics.quantiles(values, n=4, method="inclusive")
        return float(cuts[0]), float(cuts[2])

    for method in METHODS:
        circuit_rows: Dict[str, object] = {}
        penalties: List[float] = []
        complete = 0
        method_runs = [run for run in runs if run.method == method]
        for circuit in circuits:
            records = groups[(circuit, method)]
            by_rep: MutableMapping[int, List[RunManifest]] = defaultdict(list)
            for record in records:
                by_rep[record.repetition].append(record)
            protocol_ok = (
                set(by_rep) == TIMING_REPETITIONS
                and all(len(by_rep[index]) == 1
                        for index in TIMING_REPETITIONS)
                # Quality seeds belong to the stochastic M3/M4 comparison.
                # Timed repeats are deliberately fixed at seed 0 so runtime
                # variation is not confounded with a different search path.
                and all(by_rep[index][0].seed == 0
                        for index in TIMING_REPETITIONS)
            )
            successful: List[float] = []
            successful_full: List[float] = []
            circuit_penalties: List[float] = []
            for repetition in range(FORMAL_TIMING_REPETITIONS):
                candidates = sorted(by_rep.get(repetition, []), key=lambda run: run.run_id)
                if not candidates:
                    value = 2.0 * timeout_seconds
                else:
                    record = candidates[0]
                    stage_complete = (
                        record.status == RunStatus.SUCCESS.value
                        and record.transition_decision_ns is not None
                        and record.initial_placement_ns is not None
                        and record.routing_ns is not None
                        and record.full_compile_ns is not None
                    )
                    if stage_complete:
                        value = float(record.transition_decision_ns) / 1e9
                        successful.append(value)
                        successful_full.append(
                            float(record.full_compile_ns) / 1e9)
                        ledger_proven = (
                            len(record.canonical_input_layer_ledger_sha256) == 64
                            and record.canonical_input_transition_count is not None
                            and len(
                                record.observed_transition_layer_ledger_sha256) == 64
                            and record.observed_transition_count is not None
                            and record.observed_transition_layer_ledger_source
                            in accepted_ledger_sources[method]
                        )
                        circuit_ledger_proven[circuit] &= ledger_proven
                        if ledger_proven:
                            circuit_canonical_ledgers[circuit].add(
                                record.canonical_input_layer_ledger_sha256)
                            circuit_canonical_counts[circuit].add(
                                int(record.canonical_input_transition_count))
                            circuit_observed_ledgers[circuit].add(
                                record.observed_transition_layer_ledger_sha256)
                            circuit_observed_counts[circuit].add(
                                int(record.observed_transition_count))
                    else:
                        value = 2.0 * timeout_seconds
                penalties.append(value)
                circuit_penalties.append(value)
            runtime_observed = bool(successful)
            if protocol_ok and runtime_observed:
                complete += 1
            if not protocol_ok:
                all_protocol_complete = False
            if not runtime_observed:
                all_runtime_observed = False
            row: Dict[str, object] = {
                "protocol_complete": protocol_ok, "attempts": len(records),
                "successful": len(successful),
                "runtime_observed": runtime_observed,
                "median_success_seconds": (
                    statistics.median(successful) if successful else None),
                "transition_decision_seconds_median": (
                    statistics.median(successful) if successful else None),
                "full_compile_seconds_median": (
                    statistics.median(successful_full)
                    if successful_full else None),
                "PAR2_seconds": statistics.fmean(circuit_penalties),
            }
            if successful:
                q1, q3 = quartiles(successful)
                row.update(
                    transition_decision_seconds_q1=q1,
                    transition_decision_seconds_q3=q3,
                    transition_decision_seconds_iqr=q3 - q1,
                )
                circuit_method_medians[(circuit, method)] = float(
                    statistics.median(successful))
            circuit_rows[circuit] = row
        positive_medians = [
            value for (circuit, row_method), value in circuit_method_medians.items()
            if row_method == method and value > 0
        ]
        methods[method] = {
            "valid": complete, "N": len(circuits),
            "status": "complete" if complete == len(circuits) else "incomplete",
            "PAR2_seconds": statistics.fmean(penalties) if penalties else math.nan,
            "transition_decision_seconds_geometric_mean": (
                geometric_mean_from_logs([math.log(value)
                                          for value in positive_medians])
                if positive_medians else None),
            "status_counts": _status_counts(
                method_runs,
                max(0, len(circuits) * FORMAL_TIMING_REPETITIONS
                    - len(method_runs)),
                max(0, len(method_runs)
                    - len(circuits) * FORMAL_TIMING_REPETITIONS)),
            "circuits": circuit_rows,
        }

    per_circuit: List[Mapping[str, object]] = []
    strict_circuits: List[str] = []
    for circuit in circuits:
        complete_methods = all(
            (circuit, method) in circuit_method_medians
            and methods[method]["circuits"][circuit]["protocol_complete"]
            for method in METHODS)
        strict = (
            complete_methods
            and circuit_ledger_proven[circuit]
            and len(circuit_canonical_ledgers[circuit]) == 1
            and len(circuit_canonical_counts[circuit]) == 1
            and len(circuit_observed_ledgers[circuit]) == 1
            and len(circuit_observed_counts[circuit]) == 1
        )
        record: Dict[str, object] = {
            "circuit": circuit,
            "strict_stage_aligned": strict,
            "canonical_input_layer_ledger_sha256": (
                next(iter(circuit_canonical_ledgers[circuit]))
                if len(circuit_canonical_ledgers[circuit]) == 1 else None),
            "observed_transition_layer_ledger_sha256": (
                next(iter(circuit_observed_ledgers[circuit]))
                if len(circuit_observed_ledgers[circuit]) == 1 else None),
            "layer_ledger_proven": circuit_ledger_proven[circuit],
            "methods": {
                method: methods[method]["circuits"][circuit]
                for method in METHODS
            },
        }
        if strict:
            strict_circuits.append(circuit)
            m2 = circuit_method_medians[(circuit, "M2")]
            for method in ("M3", "M4"):
                ours = circuit_method_medians[(circuit, method)]
                record[f"{method}_speedup_vs_M2"] = (
                    m2 / ours if ours > 0 else None)
        per_circuit.append(record)

    labels: Dict[str, str] = {}
    for circuit in strict_circuits:
        if circuit in strata:
            labels[circuit] = strata[circuit]
            continue
        sample = next((run for run in runs if run.circuit == circuit), None)
        if sample is None:
            raise AssertionError(f"missing timing manifest for {circuit}")
        labels[circuit] = _default_stratum(dataset, sample)
    comparisons: Dict[str, Mapping[str, object]] = {}
    for offset, method in enumerate(("M3", "M4")):
        differences = {
            circuit: (
                math.log(circuit_method_medians[(circuit, "M2")])
                - math.log(circuit_method_medians[(circuit, method)]))
            for circuit in strict_circuits
            if circuit_method_medians[(circuit, "M2")] > 0
            and circuit_method_medians[(circuit, method)] > 0
        }
        if differences:
            comparisons[f"{method}_vs_M2"] = {
                **stratified_bootstrap_log_ratio(
                    differences,
                    {key: labels[key] for key in differences},
                    iterations=bootstrap_iterations,
                    seed=bootstrap_seed + 100 + offset,
                ),
                "n": len(differences),
                "ratio_definition": "M2 transition time / ours transition time",
            }
        else:
            comparisons[f"{method}_vs_M2"] = {
                "n": 0, "ratio": None, "ci95_low": None,
                "ci95_high": None,
                "ratio_definition": "M2 transition time / ours transition time",
            }
    all_stage_aligned = len(strict_circuits) == len(circuits)
    return {
        "available": True,
        "valid": min(int(methods[m]["valid"]) for m in METHODS),
        "N": len(circuits),
        "status": "pass" if (
            all_protocol_complete and all_runtime_observed and all_stage_aligned
        ) else "fail",
        "timeout_seconds": timeout_seconds,
        "PAR2_timeout_penalty_seconds": 2.0 * timeout_seconds,
        "methods": methods,
        "strict_stage_aligned_circuits": strict_circuits,
        "per_circuit": per_circuit,
        "comparisons": comparisons,
        "gate": {
            "passed": (all_protocol_complete and all_runtime_observed
                       and all_stage_aligned),
            "all_protocol_complete": all_protocol_complete,
            "all_circuit_methods_have_successful_runtime": all_runtime_observed,
            "all_circuits_stage_aligned": all_stage_aligned,
        },
    }


def aggregate_experiment(
        manifest_paths: Iterable[str | Path], *, dataset: str,
        strata: Optional[Mapping[str, str]] = None,
        bootstrap_iterations: int = 10_000, bootstrap_seed: int = 0,
        frozen_circuits: Optional[Sequence[str]] = None,
        frozen_suite_sha256: Optional[str] = None,
        experiment_id: Optional[str] = None,
        run_kind: Optional[str] = None,
        timeout_seconds: float = 600.0) -> Mapping[str, object]:
    """Aggregate one dataset with strict experiment and run-kind isolation.

    The legacy CLI call remains accepted.  If it omits ``frozen_circuits``, the
    denominator is reported from the manifest union for diagnostics, but the
    publication claim gate stays closed.
    """
    manifests = [load_run_manifest(path, require_success_metrics=True)
                 for path in manifest_paths]
    if not manifests:
        raise ValueError("no Schema-2 manifests to aggregate")
    wrong = sorted({item.dataset for item in manifests if item.dataset != dataset})
    if wrong:
        raise ValueError(
            f"dataset mixing is forbidden; requested {dataset}, found {wrong}")
    ids = {item.experiment_id for item in manifests if item.experiment_id}
    if experiment_id is None:
        if len(ids) != 1 or any(not item.experiment_id for item in manifests):
            raise ValueError(
                f"exactly one non-empty experiment_id is required, found {sorted(ids)}")
        selected_experiment = next(iter(ids))
    else:
        selected_experiment = experiment_id
        manifests = [item for item in manifests
                     if item.experiment_id == selected_experiment]
        if not manifests:
            raise ValueError(f"no manifests for experiment_id={experiment_id}")
    if run_kind is not None:
        manifests = [item for item in manifests if item.run_kind == run_kind]
        if not manifests:
            raise ValueError(f"no manifests for run_kind={run_kind}")
    if any(item.method not in METHODS for item in manifests):
        raise ValueError("formal aggregate accepts only M1/M2/M3/M4")

    suite_explicit = frozen_circuits is not None
    if frozen_circuits is None:
        circuits = sorted({item.circuit for item in manifests})
        suite_source = "inferred manifest union (diagnostic only)"
    else:
        circuits = list(frozen_circuits)
        if not circuits or len(circuits) != len(set(circuits)):
            raise ValueError("frozen circuit list must be non-empty and unique")
        suite_source = "caller-supplied frozen suite"
    actual_suite_hash = _suite_hash(circuits)
    if frozen_suite_sha256 is not None and frozen_suite_sha256 != actual_suite_hash:
        raise ValueError("frozen suite SHA256 mismatch")
    outside = sorted({item.circuit for item in manifests} - set(circuits))
    if outside:
        raise ValueError(f"manifests outside frozen suite: {outside}")

    kinds = sorted({item.run_kind for item in manifests})
    if "" in kinds:
        raise ValueError("formal aggregate requires non-empty run_kind")
    integrity_errors = _provenance_errors(manifests, circuits)
    by_kind = {kind: [item for item in manifests if item.run_kind == kind]
               for kind in kinds}
    coverage = _coverage_report(by_kind.get("coverage", []), circuits, dataset)
    main = _main_report(
        by_kind.get("main", []), circuits, dataset, dict(strata or {}),
        bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed)
    timing = _timing_report(
        by_kind.get("timing", []), circuits, timeout_seconds,
        dataset=dataset, strata=dict(strata or {}),
        bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed)

    required_sections = bool(
        coverage["available"] and main["available"] and timing["available"])
    ready = bool(
        suite_explicit and not integrity_errors and required_sections and
        coverage["gate"]["passed"] and main["gate"]["passed"] and
        timing["gate"]["passed"])
    return {
        "experiment_schema": 2, "experiment_id": selected_experiment,
        "dataset": dataset, "run_kinds": kinds,
        "method_protocols": {
            method: {
                "trace_protocol": trace_protocol_for_method(method),
                "ghost_policy": ghost_policy_for_method(method),
                "physicalization_policy":
                    physicalization_policy_for_method(method),
                "scoring": "unified_zac_physical_model",
            }
            for method in METHODS
        },
        "frozen_suite": {
            "source": suite_source, "explicit": suite_explicit,
            "sha256": actual_suite_hash, "circuits": circuits, "N": len(circuits),
        },
        "integrity": {"passed": not integrity_errors, "errors": integrity_errors},
        "coverage": coverage, "main": main, "timing": timing,
        "attempt_index": [
            {
                "run_id": item.run_id,
                "run_kind": item.run_kind,
                "circuit": item.circuit,
                "method": item.method,
                "seed": item.seed,
                "repetition": item.repetition,
                "status": item.status,
                "git_commit": item.git_commit,
                "input_sha256": item.input_sha256,
                "config_sha256": item.config_sha256,
                "fidelity_ood": item.fidelity_ood,
                "log_fidelity": item.log_fidelity,
                "fidelity": item.fidelity,
                "move_batches": item.move_batches,
                "move_time_us": item.move_time_us,
                "compiler_time_seconds": (
                    item.compiler_time_ns / 1e9
                    if item.compiler_time_ns is not None else None),
                "transition_decision_seconds": (
                    item.transition_decision_ns / 1e9
                    if item.transition_decision_ns is not None else None),
                "search_kernel_seconds": (
                    item.search_kernel_ns / 1e9
                    if item.search_kernel_ns is not None else None),
                "marshal_seconds": (
                    item.marshal_ns / 1e9
                    if item.marshal_ns is not None else None),
                "fitness_seconds": (
                    item.fitness_ns / 1e9
                    if item.fitness_ns is not None else None),
                "native_parse_seconds": (
                    item.native_parse_ns / 1e9
                    if item.native_parse_ns is not None else None),
                "native_serialize_seconds": (
                    item.native_serialize_ns / 1e9
                    if item.native_serialize_ns is not None else None),
                "initial_placement_seconds": (
                    item.initial_placement_ns / 1e9
                    if item.initial_placement_ns is not None else None),
                "routing_seconds": (
                    item.routing_ns / 1e9
                    if item.routing_ns is not None else None),
                "full_compile_seconds": (
                    item.full_compile_ns / 1e9
                    if item.full_compile_ns is not None else None),
                "layer_ledger_sha256": item.layer_ledger_sha256,
                "transition_count": item.transition_count,
                "canonical_input_layer_ledger_sha256":
                    item.canonical_input_layer_ledger_sha256,
                "canonical_input_transition_count":
                    item.canonical_input_transition_count,
                "observed_transition_layer_ledger_sha256":
                    item.observed_transition_layer_ledger_sha256,
                "observed_transition_count": item.observed_transition_count,
                "observed_transition_layer_ledger_source":
                    item.observed_transition_layer_ledger_source,
                "algorithm_revision": item.algorithm_revision,
                "backend": item.backend,
                "native_abi_version": item.native_abi_version,
                "native_wheel_sha256": item.native_wheel_sha256,
                "tuning_protocol_id": item.tuning_protocol_id,
                "rng_version": item.rng_version,
                "selected_horizon_counts": item.selected_horizon_counts,
                "forecast_summary": item.forecast_summary,
                "compiler_process_wall_seconds": (
                    item.compiler_process_wall_ns / 1e9
                    if item.compiler_process_wall_ns is not None else None),
                "end_to_end_time_seconds": (
                    item.end_to_end_time_ns / 1e9
                    if item.end_to_end_time_ns is not None else None),
                "cpu_time_seconds": (
                    item.cpu_time_ns / 1e9
                    if item.cpu_time_ns is not None else None),
                "peak_rss_bytes": item.peak_rss_bytes,
                "log_one_qubit_gate": item.fidelity_components.get(
                    "log_one_qubit_gate"),
                "log_two_qubit_gate": item.fidelity_components.get(
                    "log_two_qubit_gate"),
                "log_idle_excitation": item.fidelity_components.get(
                    "log_idle_excitation"),
                "log_atom_transfer": item.fidelity_components.get(
                    "log_atom_transfer"),
                "log_coherence_linear": item.fidelity_components.get(
                    "log_coherence_linear"),
                "exponential_sensitivity_log_fidelity":
                    item.exponential_sensitivity_log_fidelity,
                "exponential_sensitivity_fidelity":
                    item.exponential_sensitivity_fidelity,
                "duration_us": item.duration_us,
                "idle_exposures": item.idle_exposures,
                "stay_count": item.stay_count,
                "return_count": item.return_count,
                "reseat_count": item.reseat_count,
                "ghost_repairs": item.ghost_repairs,
                "ghost_splits": item.ghost_splits,
                "ghost_hits": item.ghost_hits,
                "trace_protocol": item.trace_protocol,
                "ghost_policy": item.ghost_policy,
                "physicalization_policy": item.physicalization_policy,
                "verifier_ok": item.verifier_ok,
                "artifact_dir": item.artifact_dir,
                "error": item.error,
            }
            for item in sorted(
                manifests,
                key=lambda row: (
                    row.run_kind, row.circuit, row.method,
                    row.seed, row.repetition, row.run_id),
            )
        ],
        # Compatibility aliases for the old report reader.
        "circuits_total": len(circuits),
        "paired_cohort": main.get("paired_cohort", []),
        "paired_n": main.get("valid", 0),
        "comparisons": main.get("fidelity", {}).get("comparisons", {}),
        "claim_gate": {
            "passed": ready, "status": "pass" if ready else "fail",
            "requires": [
                "explicit frozen suite", "clean consistent provenance",
                "coverage gate", "main fidelity and move gates",
                "three-repeat timing protocol with at least one successful runtime "
                "per circuit and method"],
            "missing_sections": [
                name for name, section in (("coverage", coverage),
                                           ("main", main), ("timing", timing))
                if not section["available"]],
        },
    }


__all__ = [
    "aggregate_experiment", "geometric_mean_from_logs", "holm_adjust",
    "paired_wilcoxon", "par2_seconds", "stratified_bootstrap_log_ratio",
    "wilson_interval",
]
