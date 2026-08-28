"""Paper-specific aggregation for the Chinese ZAC18/QMAP154 submission.

The accepted native-GA workbook predates the three-seed paper experiment, so
the generic final-results exporter is deliberately not reused here.  This
module has a narrower contract:

* M1/M2 are one actually executed seed-0 manifest per circuit;
* M3/M4 are the median of seeds 0, 1, and 2 per circuit;
* fidelity comparisons use the strict common *linear-model* cohort, while
  compiler coverage is reported independently;
* ``return_match_ns`` and ``forecast_ns`` are nested diagnostics inside
  ``search_kernel_ns`` and are never added to the wall-clock decomposition.

The module writes renderer-neutral CSV/JSON/TeX inputs.  XLSX authoring is
intentionally kept in the separately verified spreadsheet workflow.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from .contracts import RunManifest, RunStatus, load_run_manifest, sha256_file
from .statistics import holm_adjust, paired_wilcoxon, wilson_interval


METHODS = ("M1", "M2", "M3", "M4")
DATASETS = ("zac18", "qmap154")
EXPECTED_SEEDS = {
    "M1": (0,), "M2": (0,), "M3": (0, 1, 2), "M4": (0, 1, 2),
}
TIME_FIELDS = (
    "initial_placement_ns",
    "transition_decision_ns",
    "problem_preparation_ns",
    "search_kernel_ns",
    "result_commit_ns",
    "routing_ns",
    # The following two counters are diagnostics nested in search_kernel_ns.
    "return_match_ns",
    "forecast_ns",
)
NESTED_TIME_FIELDS = frozenset(("return_match_ns", "forecast_ns"))
FIDELITY_COMPONENTS = (
    "log_atom_transfer", "log_idle_excitation", "log_coherence_linear",
)

ManifestInput = RunManifest | str | Path


def _load_manifests(inputs: Iterable[ManifestInput]) -> list[RunManifest]:
    rows: list[RunManifest] = []
    for item in inputs:
        if isinstance(item, RunManifest):
            rows.append(item)
        else:
            rows.append(load_run_manifest(item, require_success_metrics=True))
    return rows


def _median(values: Iterable[float | int | None]) -> float | None:
    finite = [float(value) for value in values if value is not None]
    if not finite:
        return None
    if any(not math.isfinite(value) for value in finite):
        raise ValueError("non-finite value in paper aggregate")
    return float(statistics.median(finite))


def _mean(values: Iterable[float | int | None]) -> float | None:
    finite = [float(value) for value in values if value is not None]
    return statistics.fmean(finite) if finite else None


def _geometric_mean_logs(values: Iterable[float | int | None]) -> float | None:
    finite = [float(value) for value in values if value is not None]
    if not finite:
        return None
    return math.exp(statistics.fmean(finite))


def _runtime_ns(run: RunManifest) -> int | None:
    """Return the implementation-level quality-run wall time.

    Native M3/M4 explicitly use ``full_compile_ns``.  Existing M1/M2 manifests
    may predate that field, so their actually measured compiler wall time is the
    documented fallback.
    """
    if run.method in {"M3", "M4"} and run.full_compile_ns is not None:
        return run.full_compile_ns
    return run.full_compile_ns or run.compiler_time_ns


def _component(run: RunManifest, key: str) -> float | None:
    value = run.fidelity_components.get(key)
    return float(value) if value is not None else None


def _transfers(run: RunManifest) -> float | None:
    if run.transfers is not None:
        return float(run.transfers)
    value = run.fidelity_components.get("transfers")
    return float(value) if value is not None else None


def _seed_cell(runs: Sequence[RunManifest], method: str, *,
               expected_seeds: Sequence[int] | None = None) -> dict[str, Any]:
    expected = tuple(expected_seeds or EXPECTED_SEEDS[method])
    by_seed: MutableMapping[int, list[RunManifest]] = defaultdict(list)
    unexpected: list[str] = []
    for run in runs:
        if run.repetition != 0:
            unexpected.append(f"seed{run.seed}:repetition{run.repetition}")
            continue
        if run.seed not in expected:
            unexpected.append(f"unexpected_seed{run.seed}")
            continue
        by_seed[run.seed].append(run)

    selected: list[RunManifest] = []
    seed_status: list[dict[str, Any]] = []
    for seed in expected:
        candidates = sorted(by_seed.get(seed, []), key=lambda row: row.run_id)
        if len(candidates) > 1:
            unexpected.append(f"seed{seed}:duplicate={len(candidates)}")
        run = candidates[0] if len(candidates) == 1 else None
        seed_status.append({
            "seed": seed,
            "status": run.status if run is not None else
            ("duplicate" if candidates else "missing"),
            "run_id": run.run_id if run is not None else None,
        })
        if run is not None and run.status == RunStatus.SUCCESS.value:
            selected.append(run)

    valid = len(selected)
    fidelity_runs = [run for run in selected
                     if not run.fidelity_ood and run.log_fidelity is not None]
    log_fidelity = _median(run.log_fidelity for run in fidelity_runs)
    status = ("success" if valid == len(expected) and not unexpected else
              "partial" if valid else
              next((item["status"] for item in seed_status
                    if item["status"] not in {"missing", "duplicate"}),
                   "missing"))
    cell: dict[str, Any] = {
        "status": status,
        "valid": valid,
        "N": len(expected),
        "valid_over_N": f"{valid}/{len(expected)}",
        "fidelity_valid": len(fidelity_runs),
        "fidelity_N": len(expected),
        "log_fidelity": log_fidelity,
        "fidelity": math.exp(log_fidelity) if log_fidelity is not None else None,
        "fidelity_ood": bool(selected) and not fidelity_runs,
        "transfers": _median(_transfers(run) for run in selected),
        "idle_exposures": _median(run.idle_exposures for run in selected),
        "move_batches": _median(run.move_batches for run in selected),
        "move_time_us": _median(run.move_time_us for run in selected),
        "algorithm_time_s": _median(
            _runtime_ns(run) / 1e9 if _runtime_ns(run) is not None else None
            for run in selected),
        "seed_status": seed_status,
        "protocol_warnings": unexpected,
        "input_sha256": next((run.input_sha256 for run in runs
                              if run.input_sha256), ""),
        "gate_ledger_sha256": next((run.expected_gate_ledger_sha256 for run in runs
                                    if run.expected_gate_ledger_sha256), ""),
    }
    for component in FIDELITY_COMPONENTS:
        cell[component] = _median(_component(run, component) for run in fidelity_runs)
    for key in ("weighted_negative_log_fidelity_total", "prediction_hits",
                "prediction_opportunities", "predicted_reentries",
                "realized_reentries"):
        cell[f"forecast_{key}"] = _median(
            run.forecast_summary.get(key) for run in selected)
    for field in TIME_FIELDS:
        cell[field.removesuffix("_ns") + "_s"] = _median(
            getattr(run, field) / 1e9 if getattr(run, field) is not None else None
            for run in selected)
    cell["timing_semantics"] = {
        "additive_wall_clock_stages": [
            "initial_placement_s", "problem_preparation_s", "search_kernel_s",
            "result_commit_s", "routing_s"],
        "transition_decision_s": "reported_stage_not_added_to_full_compile",
        "nested_in_search_kernel": ["return_match_s", "forecast_s"],
        "nested_fields_must_not_be_summed": True,
    }
    return cell


def build_main_rows(
        manifest_inputs: Iterable[ManifestInput], *,
        frozen_suites: Mapping[str, Sequence[str]]) -> dict[str, list[dict[str, Any]]]:
    """Build one renderer-neutral row for every frozen circuit."""
    if set(frozen_suites) != set(DATASETS):
        raise ValueError("frozen_suites must contain exactly zac18 and qmap154")
    manifests = _load_manifests(manifest_inputs)
    allowed = {(dataset, circuit) for dataset, circuits in frozen_suites.items()
               for circuit in circuits}
    outside = sorted({(run.dataset, run.circuit) for run in manifests} - allowed)
    if outside:
        raise ValueError(f"main manifests outside frozen suites: {outside[:5]}")
    invalid_methods = sorted({run.method for run in manifests} - set(METHODS))
    if invalid_methods:
        raise ValueError(f"unsupported paper methods: {invalid_methods}")
    grouped: MutableMapping[tuple[str, str, str], list[RunManifest]] = defaultdict(list)
    for run in manifests:
        grouped[(run.dataset, run.circuit, run.method)].append(run)

    result: dict[str, list[dict[str, Any]]] = {}
    for dataset in DATASETS:
        rows: list[dict[str, Any]] = []
        for circuit in frozen_suites[dataset]:
            circuit_runs = [run for run in manifests
                            if run.dataset == dataset and run.circuit == circuit]
            row: dict[str, Any] = {
                "dataset": dataset,
                "circuit": circuit,
                "canonical_sha256": next((run.input_sha256 for run in circuit_runs
                                           if run.input_sha256), ""),
                "qubits": next((run.qubits for run in circuit_runs
                                if run.qubits is not None), None),
                "gates_1q": next((run.expected_gates_1q for run in circuit_runs
                                  if run.expected_gates_1q is not None), None),
                "gates_2q": next((run.expected_gates_2q for run in circuit_runs
                                  if run.expected_gates_2q is not None), None),
            }
            for method in METHODS:
                cell = _seed_cell(grouped[(dataset, circuit, method)], method)
                row[method] = cell
                for key, value in cell.items():
                    if key not in {"seed_status", "protocol_warnings", "timing_semantics"}:
                        row[f"{method}__{key}"] = value
            rows.append(row)
        result[dataset] = rows
    return result


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile of empty sample")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _bootstrap_log_ratio(
        differences: Sequence[float], *, iterations: int, seed: int) -> dict[str, Any]:
    if not differences:
        return {"ratio": None, "ci95_low": None, "ci95_high": None,
                "iterations": iterations}
    rng = random.Random(seed)
    n = len(differences)
    samples = [
        math.exp(statistics.fmean(differences[rng.randrange(n)] for _ in range(n)))
        for _ in range(iterations)
    ]
    return {
        "ratio": math.exp(statistics.fmean(differences)),
        "ci95_low": _percentile(samples, 0.025),
        "ci95_high": _percentile(samples, 0.975),
        "iterations": iterations,
    }


def _comparison_against_strongest_baseline(
        rows: Sequence[Mapping[str, Any]], method: str, *,
        iterations: int, seed: int) -> dict[str, Any]:
    paired: list[tuple[str, float, float, str]] = []
    for row in rows:
        m1 = row["M1"]["log_fidelity"]
        m2 = row["M2"]["log_fidelity"]
        target = row[method]["log_fidelity"]
        # The paper's headline ratio uses the same all-four strict cohort as
        # the table, not a method-specific pair that changes its denominator.
        if (m1 is None or m2 is None or target is None or
                any(row[item]["log_fidelity"] is None or
                    row[item]["valid"] != row[item]["N"] or
                    row[item]["fidelity_valid"] != row[item]["fidelity_N"]
                    for item in METHODS)):
            continue
        baseline_method = "M1" if float(m1) >= float(m2) else "M2"
        paired.append((str(row["circuit"]), float(target), max(float(m1), float(m2)),
                       baseline_method))
    differences = [target - baseline for _circuit, target, baseline, _choice in paired]
    bootstrap = _bootstrap_log_ratio(differences, iterations=iterations, seed=seed)
    wilcoxon = paired_wilcoxon(differences) if differences else None
    tolerance = 1e-12
    wins = sum(value > tolerance for value in differences)
    losses = sum(value < -tolerance for value in differences)
    ties = len(differences) - wins - losses
    robust: dict[str, Any] = {}
    ordered = sorted(paired, key=lambda item: item[1] - item[2], reverse=True)
    for count in (1, 5, 10):
        retained = ordered[count:] if len(ordered) > count else []
        retained_diffs = [target - baseline
                          for _circuit, target, baseline, _choice in retained]
        robust[f"remove_top_{count}"] = {
            "removed_circuits": [item[0] for item in ordered[:count]],
            "n": len(retained),
            "geometric_mean_ratio": (
                math.exp(statistics.fmean(retained_diffs))
                if retained_diffs else None),
            "median_ratio": (
                math.exp(statistics.median(retained_diffs))
                if retained_diffs else None),
            "wins": sum(value > tolerance for value in retained_diffs),
            "ties": sum(abs(value) <= tolerance for value in retained_diffs),
            "losses": sum(value < -tolerance for value in retained_diffs),
        }
    return {
        "method": method,
        "strict_common_linear_N": len(paired),
        "target_geometric_mean_fidelity": _geometric_mean_logs(
            target for _circuit, target, _baseline, _choice in paired),
        "strongest_baseline_geometric_mean_fidelity": _geometric_mean_logs(
            baseline for _circuit, _target, baseline, _choice in paired),
        "geometric_mean_ratio": bootstrap["ratio"],
        "bootstrap": bootstrap,
        "median_per_circuit_ratio": (
            math.exp(statistics.median(differences)) if differences else None),
        "wins": wins, "ties": ties, "losses": losses,
        "wilcoxon": wilcoxon,
        "strongest_baseline_choice_counts": dict(Counter(
            choice for _circuit, _target, _baseline, choice in paired)),
        "robustness": robust,
    }


def _method_summary(rows: Sequence[Mapping[str, Any]], method: str,
                    common_circuits: set[str]) -> dict[str, Any]:
    cells = [row[method] for row in rows]
    common = [row[method] for row in rows if row["circuit"] in common_circuits]
    successes = sum(cell["valid"] > 0 for cell in cells)
    complete = sum(cell["valid"] == cell["N"] for cell in cells)
    linear = [cell for cell in common if cell["log_fidelity"] is not None]
    interval = wilson_interval(successes, len(rows)) if rows else (0.0, 0.0)
    return {
        "coverage": {
            "success_circuits": successes,
            "complete_seed_circuits": complete,
            "N": len(rows),
            "rate": successes / len(rows) if rows else 0.0,
            "wilson95": list(interval),
            "status_counts": dict(Counter(cell["status"] for cell in cells)),
        },
        "strict_common_linear_N": len(linear),
        "fidelity_geometric_mean": _geometric_mean_logs(
            cell["log_fidelity"] for cell in linear),
        "transfers_arithmetic_mean": _mean(cell["transfers"] for cell in common),
        "idle_exposures_arithmetic_mean": _mean(
            cell["idle_exposures"] for cell in common),
        "move_batches_arithmetic_mean": _mean(
            cell["move_batches"] for cell in common),
        "move_time_us_arithmetic_mean": _mean(
            cell["move_time_us"] for cell in common),
        "algorithm_time_s_arithmetic_mean": _mean(
            cell["algorithm_time_s"] for cell in common),
    }


def _mechanism_summary(rows: Sequence[Mapping[str, Any]], method: str,
                       common_circuits: set[str]) -> dict[str, Any]:
    cells = [row[method] for row in rows if row["circuit"] in common_circuits]
    result = {
        "N": len(cells),
        "transfers_mean": _mean(cell["transfers"] for cell in cells),
        "idle_exposures_mean": _mean(cell["idle_exposures"] for cell in cells),
        "move_batches_mean": _mean(cell["move_batches"] for cell in cells),
        "move_time_us_mean": _mean(cell["move_time_us"] for cell in cells),
        "log_atom_transfer_mean": _mean(
            cell["log_atom_transfer"] for cell in cells),
        "log_idle_excitation_mean": _mean(
            cell["log_idle_excitation"] for cell in cells),
        "log_coherence_linear_mean": _mean(
            cell["log_coherence_linear"] for cell in cells),
    }
    return result


def _mechanism_delta_vs_strongest(
        rows: Sequence[Mapping[str, Any]], method: str,
        common_circuits: set[str]) -> dict[str, Any]:
    paired: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for row in rows:
        if row["circuit"] not in common_circuits:
            continue
        baseline = (row["M1"] if float(row["M1"]["log_fidelity"]) >=
                    float(row["M2"]["log_fidelity"]) else row["M2"])
        paired.append((row[method], baseline))

    def delta(field: str) -> float | None:
        values = [float(current[field]) - float(reference[field])
                  for current, reference in paired
                  if current[field] is not None and reference[field] is not None]
        return statistics.fmean(values) if values else None

    return {
        "N": len(paired),
        "count_delta_semantics": "method minus strongest-fidelity baseline; negative is favorable",
        "log_component_delta_semantics": (
            "method minus strongest-fidelity baseline; positive is favorable"),
        "transfers_mean_delta": delta("transfers"),
        "idle_exposures_mean_delta": delta("idle_exposures"),
        "move_batches_mean_delta": delta("move_batches"),
        "move_time_us_mean_delta": delta("move_time_us"),
        "log_atom_transfer_mean_delta": delta("log_atom_transfer"),
        "log_idle_excitation_mean_delta": delta("log_idle_excitation"),
        "log_coherence_linear_mean_delta": delta("log_coherence_linear"),
    }


def summarize_main(
        main_rows: Mapping[str, Sequence[Mapping[str, Any]]], *,
        bootstrap_iterations: int = 10_000,
        bootstrap_seed: int = 0) -> dict[str, Any]:
    """Summarize datasets separately; never mix QMAP coverage with fidelity."""
    datasets: dict[str, Any] = {}
    for dataset_index, dataset in enumerate(DATASETS):
        rows = list(main_rows[dataset])
        common = {
            str(row["circuit"]) for row in rows
            if all(row[method]["log_fidelity"] is not None and
                   row[method]["valid"] == row[method]["N"] and
                   row[method]["fidelity_valid"] == row[method]["fidelity_N"]
                   for method in METHODS)
        }
        comparisons = {}
        for method_index, method in enumerate(("M3", "M4")):
            comparison = _comparison_against_strongest_baseline(
                rows, method, iterations=bootstrap_iterations,
                seed=bootstrap_seed + 101 * dataset_index + method_index)
            comparisons[f"{method}_vs_Bstar"] = comparison
        datasets[dataset] = {
            "circuit_N": len(rows),
            "strict_common_linear_circuits": sorted(common),
            "strict_common_linear_N": len(common),
            "methods": {method: _method_summary(rows, method, common)
                        for method in METHODS},
            "comparisons": comparisons,
            "mechanism": {method: _mechanism_summary(rows, method, common)
                          for method in METHODS},
            "mechanism_delta_vs_strongest_baseline": {
                method: _mechanism_delta_vs_strongest(rows, method, common)
                for method in ("M3", "M4")},
        }
    return {
        "protocol": "paper-zh-v1-main-summary-v1",
        "bootstrap_iterations": bootstrap_iterations,
        "bootstrap_seed": bootstrap_seed,
        "fidelity_cohort_semantics": (
            "strict common linear-model cohort; separate for zac18 and qmap154"),
        "coverage_semantics": "compiler success is reported independently of fidelity",
        "datasets": datasets,
    }


def _variant_cells(manifests: Sequence[RunManifest], variant: str,
                   expected_seeds: Sequence[int]) -> dict[str, dict[str, Any]]:
    grouped: MutableMapping[str, list[RunManifest]] = defaultdict(list)
    for run in manifests:
        if run.ablation_variant == variant:
            grouped[f"{run.dataset}/{run.circuit}"].append(run)
    return {circuit: _seed_cell(runs, "M4", expected_seeds=expected_seeds)
            for circuit, runs in grouped.items()}


def _ga_applicable_count(runs: Sequence[RunManifest]) -> tuple[int | None, str]:
    keys = ("ga_applicable_boundaries", "ga_boundary_count",
            "large_search_boundaries")
    observed: list[int] = []
    for run in runs:
        for key in keys:
            value = run.forecast_summary.get(key)
            if value is not None:
                observed.append(int(value))
                break
    if observed:
        return max(observed), "manifest.forecast_summary"

    derived: list[int] = []
    for run in runs:
        if not run.artifact_dir:
            continue
        stats_path = Path(run.artifact_dir) / "compiler_stats.json.gz"
        if not stats_path.is_file():
            continue
        try:
            with gzip.open(stats_path, "rt", encoding="utf-8") as handle:
                statistics_payload = json.load(handle)
        except (OSError, ValueError, TypeError):
            continue
        decisions = statistics_payload.get("decision_log", [])
        if not isinstance(decisions, list):
            continue
        derived.append(sum(
            isinstance(decision, Mapping) and
            str(decision.get("search_mode", "")).startswith("ga")
            for decision in decisions))
    if derived:
        return max(derived), "artifact.compiler_stats.decision_log.search_mode"
    return None, "unavailable"


def _paired_variant_fidelity(
        current: Mapping[str, Mapping[str, Any]],
        reference: Mapping[str, Mapping[str, Any]], *,
        iterations: int, seed: int,
        eligible: set[str] | None = None) -> dict[str, Any]:
    circuits = sorted(set(current) & set(reference))
    if eligible is not None:
        circuits = [circuit for circuit in circuits if circuit in eligible]
    circuits = [circuit for circuit in circuits
                if current[circuit]["log_fidelity"] is not None and
                reference[circuit]["log_fidelity"] is not None]
    differences = [float(current[circuit]["log_fidelity"]) -
                   float(reference[circuit]["log_fidelity"])
                   for circuit in circuits]
    bootstrap = _bootstrap_log_ratio(differences, iterations=iterations, seed=seed)
    return {
        "circuits": circuits,
        "N": len(circuits),
        "geometric_mean_ratio": bootstrap["ratio"],
        "bootstrap": bootstrap,
        "wilcoxon": paired_wilcoxon(differences) if differences else None,
        "wins": sum(value > 1e-12 for value in differences),
        "ties": sum(abs(value) <= 1e-12 for value in differences),
        "losses": sum(value < -1e-12 for value in differences),
    }


def _paired_variant_deltas(
        current: Mapping[str, Mapping[str, Any]],
        reference: Mapping[str, Mapping[str, Any]],
        circuits: Sequence[str]) -> dict[str, Any]:
    fields = (
        "transfers", "idle_exposures", "log_coherence_linear",
        "move_batches", "move_time_us", "algorithm_time_s",
        "forecast_weighted_negative_log_fidelity_total",
        "forecast_prediction_hits", "forecast_prediction_opportunities",
        "forecast_predicted_reentries", "forecast_realized_reentries",
    )
    result: dict[str, Any] = {}
    for field in fields:
        values = [float(current[circuit][field]) - float(reference[circuit][field])
                  for circuit in circuits
                  if current[circuit].get(field) is not None and
                  reference[circuit].get(field) is not None]
        result[field] = {
            "paired_N": len(values),
            "mean_H8_minus_H0": statistics.fmean(values) if values else None,
            "median_H8_minus_H0": statistics.median(values) if values else None,
        }
    prediction_hits = sum(
        float(current[circuit]["forecast_prediction_hits"])
        for circuit in circuits
        if current[circuit].get("forecast_prediction_hits") is not None)
    prediction_opportunities = sum(
        float(current[circuit]["forecast_prediction_opportunities"])
        for circuit in circuits
        if current[circuit].get("forecast_prediction_opportunities") is not None)
    result["H8_prediction_hit_rate"] = (
        prediction_hits / prediction_opportunities
        if prediction_opportunities > 0 else None)
    return result


def summarize_ablation(
        manifest_inputs: Iterable[ManifestInput], *,
        h0_variant: str = "H0", h8_variant: str = "H8",
        greedy_variant: str = "greedy_only",
        expected_identities: Sequence[tuple[str, str]] | None = None,
        bootstrap_iterations: int = 10_000,
        bootstrap_seed: int = 0) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifests = _load_manifests(manifest_inputs)
    if not manifests:
        return ({"available": False, "reason": "no ablation manifests"}, [])
    variants = {run.ablation_variant for run in manifests}
    required = {h0_variant, h8_variant, greedy_variant}
    missing = sorted(required - variants)
    if missing:
        return ({"available": False, "reason": f"missing variants: {missing}",
                 "observed_variants": sorted(variants)}, [])
    h0 = _variant_cells(manifests, h0_variant, (0, 1, 2))
    h8 = _variant_cells(manifests, h8_variant, (0, 1, 2))
    greedy = _variant_cells(manifests, greedy_variant, (0,))
    expected_keys = ({f"{dataset}/{circuit}" for dataset, circuit
                      in expected_identities}
                     if expected_identities is not None else
                     set(h0) | set(h8) | set(greedy))
    for key in expected_keys:
        h0.setdefault(key, _seed_cell([], "M4", expected_seeds=(0, 1, 2)))
        h8.setdefault(key, _seed_cell([], "M4", expected_seeds=(0, 1, 2)))
        greedy.setdefault(key, _seed_cell([], "M4", expected_seeds=(0,)))
    lookahead = _paired_variant_fidelity(
        h8, h0, iterations=bootstrap_iterations, seed=bootstrap_seed)
    lookahead["mechanism_deltas"] = _paired_variant_deltas(
        h8, h0, lookahead["circuits"])
    h8_runs: MutableMapping[str, list[RunManifest]] = defaultdict(list)
    for run in manifests:
        if run.ablation_variant == h8_variant:
            h8_runs[f"{run.dataset}/{run.circuit}"].append(run)
    applicability_details = {circuit: _ga_applicable_count(runs)
                             for circuit, runs in h8_runs.items()}
    applicability = {circuit: detail[0]
                     for circuit, detail in applicability_details.items()}
    applicability_source = {circuit: detail[1]
                            for circuit, detail in applicability_details.items()}
    marker_available = any(value is not None for value in applicability.values())
    eligible = ({circuit for circuit, value in applicability.items()
                 if value is not None and value > 0}
                if marker_available else None)
    ga = _paired_variant_fidelity(
        h8, greedy, iterations=bootstrap_iterations, seed=bootstrap_seed + 1,
        eligible=eligible)
    rows: list[dict[str, Any]] = []
    all_circuits = sorted(expected_keys)
    for circuit in all_circuits:
        dataset, circuit_name = circuit.split("/", 1)
        for variant, cells in ((h0_variant, h0), (h8_variant, h8),
                               (greedy_variant, greedy)):
            cell = cells.get(circuit)
            rows.append({
                "dataset": dataset, "circuit": circuit_name, "variant": variant,
                "fidelity": cell["fidelity"] if cell else None,
                "transfers": cell["transfers"] if cell else None,
                "idle_exposures": cell["idle_exposures"] if cell else None,
                "log_coherence_linear": (
                    cell["log_coherence_linear"] if cell else None),
                "move_batches": cell["move_batches"] if cell else None,
                "move_time_us": cell["move_time_us"] if cell else None,
                "algorithm_time_s": cell["algorithm_time_s"] if cell else None,
                "valid_over_N": cell["valid_over_N"] if cell else "0/0",
                "status": cell["status"] if cell else "missing",
                "ga_applicable_boundaries": applicability.get(circuit),
                "ga_applicability_source": applicability_source.get(
                    circuit, "unavailable"),
            })
    return ({
        "available": True,
        "lookahead_H8_vs_H0": lookahead,
        "GA_vs_greedy": ga,
        "ga_applicability_marker_available": marker_available,
        "ga_applicable_circuit_N": len(eligible) if eligible is not None else None,
        "ga_applicability_sources": dict(Counter(applicability_source.values())),
        "variant_coverage": {
            variant: {
                "valid": sum(cells[key]["valid"] == cells[key]["N"]
                             for key in expected_keys),
                "N": len(expected_keys),
            }
            for variant, cells in ((h0_variant, h0), (h8_variant, h8),
                                   (greedy_variant, greedy))
        },
        "nested_timing_note": (
            "RETURN matching and forecast are nested inside search_kernel and "
            "must not be added to stage totals"),
    }, rows)


def summarize_sensitivity(
        manifest_inputs: Iterable[ManifestInput], *,
        expected_identities: Sequence[tuple[str, str]] | None = None,
        expected_settings: Sequence[str] | None = None,
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifests = _load_manifests(manifest_inputs)
    rows: list[dict[str, Any]] = []
    grouped: MutableMapping[str, list[RunManifest]] = defaultdict(list)
    for run in manifests:
        label = run.ablation_variant or run.experiment_id or run.config_sha256
        grouped[label].append(run)
        rows.append({
            "setting": label,
            "config_sha256": run.config_sha256,
            "dataset": run.dataset,
            "circuit": run.circuit,
            "seed": run.seed,
            "status": run.status,
            "fidelity": run.fidelity if not run.fidelity_ood else None,
            "transfers": _transfers(run),
            "idle_exposures": run.idle_exposures,
            "move_batches": run.move_batches,
            "move_time_us": run.move_time_us,
            "algorithm_time_s": (
                _runtime_ns(run) / 1e9 if _runtime_ns(run) is not None else None),
            "search_kernel_s": (
                run.search_kernel_ns / 1e9
                if run.search_kernel_ns is not None else None),
            "alpha": run.forecast_summary.get("alpha_lookahead"),
            "rho": run.forecast_summary.get("rho"),
            "max_horizon": run.forecast_summary.get("configured_depth"),
        })
    if expected_identities is not None and expected_settings is not None:
        observed = {(row["dataset"], row["circuit"], row["setting"])
                    for row in rows}
        for dataset, circuit in expected_identities:
            for setting in expected_settings:
                if (dataset, circuit, setting) not in observed:
                    rows.append({
                        "setting": setting, "config_sha256": "",
                        "dataset": dataset, "circuit": circuit, "seed": 0,
                        "status": "missing", "fidelity": None,
                        "transfers": None, "idle_exposures": None,
                        "move_batches": None, "move_time_us": None,
                        "algorithm_time_s": None, "search_kernel_s": None,
                        "alpha": None, "rho": None, "max_horizon": None,
                    })
    summaries: dict[str, Any] = {}
    labels = (set(grouped) | set(expected_settings or ()))
    for label in sorted(labels):
        setting_rows = [row for row in rows if row["setting"] == label]
        successful = [row for row in setting_rows
                      if row["status"] == RunStatus.SUCCESS.value]
        linear_logs = [math.log(float(row["fidelity"])) for row in successful
                       if row["fidelity"] is not None and row["fidelity"] > 0]
        summaries[label] = {
            "valid": len(successful), "N": len(setting_rows),
            "fidelity_geometric_mean": _geometric_mean_logs(
                linear_logs),
            "move_batches_mean": _mean(row["move_batches"] for row in successful),
            "move_time_us_mean": _mean(row["move_time_us"] for row in successful),
            "algorithm_time_s_mean": _mean(
                row["algorithm_time_s"] for row in successful),
        }
    return ({"available": bool(manifests), "settings": summaries}, rows)


def _iqr(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return _percentile(values, 0.75) - _percentile(values, 0.25)


def summarize_runtime(
        manifest_inputs: Iterable[ManifestInput], *,
        expected_identities: Sequence[tuple[str, str]] | None = None,
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifests = _load_manifests(manifest_inputs)
    grouped: MutableMapping[tuple[str, str, str], list[RunManifest]] = defaultdict(list)
    for run in manifests:
        grouped[(run.dataset, run.circuit, run.method)].append(run)
    if expected_identities is not None:
        for dataset, circuit in expected_identities:
            for method in METHODS:
                grouped.setdefault((dataset, circuit, method), [])
    rows: list[dict[str, Any]] = []
    for (dataset, circuit, method), runs in sorted(grouped.items()):
        expected_repetitions = set(range(3))
        by_repetition: MutableMapping[int, list[RunManifest]] = defaultdict(list)
        for run in runs:
            if run.repetition in expected_repetitions:
                by_repetition[run.repetition].append(run)
        successful = [attempts[0] for repetition, attempts in by_repetition.items()
                      if len(attempts) == 1 and
                      attempts[0].status == RunStatus.SUCCESS.value]
        times = [float(_runtime_ns(run)) / 1e9 for run in successful
                 if _runtime_ns(run) is not None]
        transition = [float(run.transition_decision_ns) / 1e9 for run in successful
                      if run.transition_decision_ns is not None]
        rows.append({
            "dataset": dataset, "circuit": circuit, "method": method,
            "algorithm_time_median_s": statistics.median(times) if times else None,
            "algorithm_time_iqr_s": _iqr(times),
            "transition_time_median_s": (
                statistics.median(transition) if transition else None),
            "transition_time_iqr_s": _iqr(transition),
            "valid": len(successful), "N": 3,
            "valid_over_N": f"{len(successful)}/3",
            "status_counts": json.dumps(dict(Counter(run.status for run in runs
                                                      if run.repetition in expected_repetitions)),
                                        sort_keys=True, separators=(",", ":")),
            "duplicate_repetitions": sum(
                max(0, len(attempts) - 1) for attempts in by_repetition.values()),
            "unexpected_repetitions": sum(
                run.repetition not in expected_repetitions for run in runs),
        })
    summary: dict[str, Any] = {"available": bool(manifests), "methods": {}}
    for method in METHODS:
        method_times = [float(row["algorithm_time_median_s"]) for row in rows
                        if row["method"] == method and
                        row["algorithm_time_median_s"] is not None]
        summary["methods"][method] = {
            "circuit_N": len(method_times),
            "median_of_circuit_medians_s": (
                statistics.median(method_times) if method_times else None),
            "arithmetic_mean_of_circuit_medians_s": _mean(method_times),
        }
    return summary, rows


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
    return value


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen and not isinstance(row[key], RunManifest):
                seen.add(key)
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in keys})


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                               indent=2) + "\n", encoding="utf-8")


def _format_number(value: float | int | None, *, digits: int = 3,
                   scientific: bool = False) -> str:
    if value is None:
        return r"\textemdash{}"
    if scientific:
        return f"{float(value):.{digits}e}"
    return f"{float(value):.{digits}f}"


def _macro(method: str) -> str:
    return {"M1": "MOne", "M2": "MTwo", "M3": "MThree", "M4": "MFour"}[method]


def build_paper_values(
        main_summary: Mapping[str, Any],
        ablation_summary: Mapping[str, Any],
        main_rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    macros: dict[str, str] = {}
    for dataset, prefix in (("zac18", "ZAC"), ("qmap154", "QMAP")):
        dataset_summary = main_summary["datasets"][dataset]
        for method in METHODS:
            method_summary = dataset_summary["methods"][method]
            suffix = _macro(method)
            macros[f"{prefix}{suffix}F"] = _format_number(
                method_summary["fidelity_geometric_mean"], digits=6,
                scientific=True)
            macros[f"{prefix}{suffix}B"] = _format_number(
                method_summary["move_batches_arithmetic_mean"], digits=2)
            move_us = method_summary["move_time_us_arithmetic_mean"]
            macros[f"{prefix}{suffix}T"] = _format_number(
                float(move_us) / 1000.0 if move_us is not None else None, digits=3)
            macros[f"{prefix}{suffix}C"] = _format_number(
                method_summary["algorithm_time_s_arithmetic_mean"], digits=3)
            coverage = method_summary["coverage"]
            macros[f"{prefix}{suffix}V"] = (
                f"{coverage['success_circuits']}/{coverage['N']}")

    statements: list[str] = []
    for dataset, label in (("zac18", "ZAC18"), ("qmap154", "QMAP154")):
        comparison = main_summary["datasets"][dataset]["comparisons"]["M4_vs_Bstar"]
        ratio = comparison["geometric_mean_ratio"]
        if ratio is not None:
            statements.append(
                f"在{label}严格共同集合上，完整方法相对逐电路最强基线的"
                f"Fidelity几何均值提高{(float(ratio) - 1.0) * 100.0:.2f}\\%"
                f"（{comparison['wins']}/{comparison['ties']}/{comparison['losses']} 胜/平/负）")
    macros["ResultStatement"] = "；".join(statements) + "。" if statements else r"\textemdash{}"

    if ablation_summary.get("available"):
        lookahead = ablation_summary["lookahead_H8_vs_H0"]
        ga = ablation_summary["GA_vs_greedy"]
        macros["AblationHZero"] = "共享参数对照"
        macros["AblationHMulti"] = (
            f"Fidelity比{lookahead['geometric_mean_ratio']:.4f}"
            if lookahead["geometric_mean_ratio"] is not None else r"\textemdash{}")
        macros["AblationGreedy"] = "同目标确定性贪心"
        macros["AblationGA"] = (
            f"Fidelity比{ga['geometric_mean_ratio']:.4f}"
            if ga["geometric_mean_ratio"] is not None else r"\textemdash{}")
    else:
        for name in ("AblationHZero", "AblationHMulti",
                     "AblationGreedy", "AblationGA"):
            macros[name] = r"\textemdash{}"

    m4_cells = [row["M4"] for dataset in DATASETS for row in main_rows[dataset]
                if row["M4"]["valid"] > 0]
    time_macros = {
        "TimeInitial": "initial_placement_s",
        "TimePrepare": "problem_preparation_s",
        "TimeSearch": "search_kernel_s",
        "TimeReturn": "return_match_s",
        "TimeForecast": "forecast_s",
        "TimeCommit": "result_commit_s",
        "TimeRouting": "routing_s",
    }
    for macro_name, field in time_macros.items():
        macros[macro_name] = _format_number(
            _mean(cell[field] for cell in m4_cells), digits=3)
    macros["TimeFullCompile"] = _format_number(
        _mean(cell["algorithm_time_s"] for cell in m4_cells), digits=3)

    for method in METHODS:
        method_cells = [
            row[method] for dataset in DATASETS for row in main_rows[dataset]
            if all(row[item]["log_fidelity"] is not None and
                   row[item]["valid"] == row[item]["N"] and
                   row[item]["fidelity_valid"] == row[item]["fidelity_N"]
                   for item in METHODS)]
        suffix = _macro(method)
        macros[f"Mechanism{suffix}Transfer"] = _format_number(
            _mean(cell["transfers"] for cell in method_cells), digits=1)
        macros[f"Mechanism{suffix}Idle"] = _format_number(
            _mean(cell["idle_exposures"] for cell in method_cells), digits=1)
        coherence = _mean(cell["log_coherence_linear"] for cell in method_cells)
        macros[f"Mechanism{suffix}Coherence"] = _format_number(
            -float(coherence) if coherence is not None else None, digits=3)
        macros[f"Mechanism{suffix}Batch"] = _format_number(
            _mean(cell["move_batches"] for cell in method_cells), digits=1)
        move_time = _mean(cell["move_time_us"] for cell in method_cells)
        macros[f"Mechanism{suffix}MoveTime"] = _format_number(
            float(move_time) / 1000.0 if move_time is not None else None, digits=3)
    return {
        "protocol": "paper-zh-v1-values-v1",
        "macros": macros,
        "nested_timing_semantics": {
            "return_match": "nested in search_kernel",
            "forecast": "nested in search_kernel",
            "must_not_be_summed": True,
        },
    }


def render_results_values_tex(paper_values: Mapping[str, Any]) -> str:
    lines = [
        "% 由 experiments_v2.paper_aggregate 自动生成；禁止手工重复填数。",
        "% RETURN匹配和前瞻时间嵌套于搜索核，不得与阶段墙钟重复相加。",
    ]
    for name, value in paper_values["macros"].items():
        lines.append(f"\\newcommand{{\\{name}}}{{{value}}}")
    return "\n".join(lines) + "\n"


def _flatten_main_row(row: Mapping[str, Any]) -> dict[str, Any]:
    flat = {key: value for key, value in row.items()
            if not isinstance(value, Mapping)}
    for method in METHODS:
        cell = row[method]
        for key, value in cell.items():
            if key in {"seed_status", "protocol_warnings", "timing_semantics"}:
                flat[f"{method}__{key}"] = _csv_value(value)
            else:
                flat[f"{method}__{key}"] = value
    return flat


def aggregate_paper(
        main_manifest_inputs: Iterable[ManifestInput], *,
        frozen_suites: Mapping[str, Sequence[str]],
        output_dir: str | Path | None = None,
        ablation_manifest_inputs: Iterable[ManifestInput] = (),
        sensitivity_manifest_inputs: Iterable[ManifestInput] = (),
        timing_manifest_inputs: Iterable[ManifestInput] = (),
        ablation_identities: Sequence[tuple[str, str]] | None = None,
        sensitivity_identities: Sequence[tuple[str, str]] | None = None,
        sensitivity_settings: Sequence[str] | None = None,
        timing_identities: Sequence[tuple[str, str]] | None = None,
        bootstrap_iterations: int = 10_000,
        bootstrap_seed: int = 0,
        h0_variant: str = "H0", h8_variant: str = "H8",
        greedy_variant: str = "greedy_only") -> dict[str, Any]:
    """Aggregate every paper experiment and optionally freeze small outputs."""
    main_rows = build_main_rows(main_manifest_inputs, frozen_suites=frozen_suites)
    main_summary = summarize_main(
        main_rows, bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed)
    ablation_summary, ablation_rows = summarize_ablation(
        ablation_manifest_inputs, h0_variant=h0_variant, h8_variant=h8_variant,
        greedy_variant=greedy_variant, expected_identities=ablation_identities,
        bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed)
    sensitivity_summary, sensitivity_rows = summarize_sensitivity(
        sensitivity_manifest_inputs, expected_identities=sensitivity_identities,
        expected_settings=sensitivity_settings)
    runtime_summary, runtime_rows = summarize_runtime(
        timing_manifest_inputs, expected_identities=timing_identities)
    primary_tests: dict[str, float] = {}
    for dataset in DATASETS:
        test = main_summary["datasets"][dataset]["comparisons"][
            "M4_vs_Bstar"]["wilcoxon"]
        if test is not None:
            primary_tests[f"{dataset}:M4_vs_Bstar"] = float(test["p_value"])
    lookahead_test = ablation_summary.get("lookahead_H8_vs_H0", {}).get(
        "wilcoxon") if ablation_summary.get("available") else None
    if lookahead_test is not None:
        primary_tests["ablation:H8_vs_H0"] = float(lookahead_test["p_value"])
    primary_holm = holm_adjust(primary_tests) if primary_tests else {}
    for name, adjusted in primary_holm.items():
        scope, comparison = name.split(":", 1)
        if scope in DATASETS:
            main_summary["datasets"][scope]["comparisons"][comparison][
                "wilcoxon"]["paper_holm_adjusted_p_value"] = adjusted
        else:
            ablation_summary["lookahead_H8_vs_H0"]["wilcoxon"][
                "paper_holm_adjusted_p_value"] = adjusted
    main_summary["primary_holm_family"] = {
        "tests": primary_tests, "adjusted_p_values": primary_holm,
        "definition": (
            "M4 versus strongest baseline in each dataset plus shared-config "
            "H8 versus H0 when available"),
    }
    paper_values = build_paper_values(main_summary, ablation_summary, main_rows)
    report: dict[str, Any] = {
        "protocol": "paper-zh-v1-aggregate-v1",
        "main_summary": main_summary,
        "ablation_summary": ablation_summary,
        "sensitivity_summary": sensitivity_summary,
        "runtime_summary": runtime_summary,
        "paper_values": paper_values,
        "main_rows": main_rows,
    }
    if output_dir is not None:
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        _write_csv(destination / "zac18.csv",
                   [_flatten_main_row(row) for row in main_rows["zac18"]])
        _write_csv(destination / "qmap154.csv",
                   [_flatten_main_row(row) for row in main_rows["qmap154"]])
        _write_csv(destination / "ablation.csv", ablation_rows)
        _write_csv(destination / "sensitivity.csv", sensitivity_rows)
        _write_csv(destination / "runtime.csv", runtime_rows)
        _json_dump(destination / "main_summary.json", main_summary)
        _json_dump(destination / "paper_values.json", paper_values)
        (destination / "results_values_zh.tex").write_text(
            render_results_values_tex(paper_values), encoding="utf-8")
        files = [path for path in destination.iterdir()
                 if path.is_file() and path.name != "final_manifest.json"]
        final_manifest = {
            "protocol": "paper-zh-v1-final-manifest-v1",
            "datasets": {dataset: list(frozen_suites[dataset])
                         for dataset in DATASETS},
            "files": {path.name: {"sha256": sha256_file(path),
                                   "bytes": path.stat().st_size}
                      for path in sorted(files)},
            "nested_timing_semantics": paper_values["nested_timing_semantics"],
            "xlsx_generated_here": False,
        }
        _json_dump(destination / "final_manifest.json", final_manifest)
        report["output_dir"] = str(destination)
        report["final_manifest"] = final_manifest
    return report


def _verified_manifest_path(row: Mapping[str, Any], *, label: str) -> Path:
    path = Path(str(row.get("path", ""))).resolve()
    expected = str(row.get("sha256", ""))
    if not path.is_file():
        raise FileNotFoundError(f"{label} manifest is missing: {path}")
    if len(expected) != 64 or sha256_file(path) != expected:
        raise ValueError(f"{label} manifest hash drift: {path}")
    return path


def _quality_manifest_paths(source_path: Path) -> list[Path]:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if (not isinstance(payload, Mapping) or
            set(payload.get("baselines", {})) != {"M1", "M2"} or
            set(payload.get("ours", {})) != {"M3", "M4"}):
        raise ValueError("paper quality_source_manifest has an invalid method inventory")
    paths: list[Path] = []
    for method in ("M1", "M2"):
        for identity, row in sorted(payload["baselines"][method].items()):
            paths.append(_verified_manifest_path(
                row, label=f"quality {method}/{identity}"))
    for method in ("M3", "M4"):
        for identity, seeds in sorted(payload["ours"][method].items()):
            if set(seeds) != {"0", "1", "2"}:
                raise ValueError(
                    f"quality {method}/{identity} does not contain seeds 0,1,2")
            for seed, row in sorted(seeds.items(), key=lambda item: int(item[0])):
                paths.append(_verified_manifest_path(
                    row, label=f"quality {method}/{identity}/seed{seed}"))
    return paths


def _track_manifests(root: Path, track: str) -> list[Path]:
    track_root = root / "runs" / track
    if not track_root.exists():
        return []
    return sorted(track_root.rglob("manifest.json"), key=str)


def command_aggregate_paper(*, plan: Any, freeze_path: str | Path,
                            artifact_root: str | Path,
                            output_root: str | Path,
                            paper_directory: str | Path) -> Mapping[str, Any]:
    """Integration entry used by ``paper_cli aggregate-paper``.

    The function verifies the frozen quality-source hashes, enumerates only the
    four registered paper run roots (never QASMBench/Large), and copies the
    generated macro file into the Chinese IEEE manuscript directory.
    """
    from .paper_protocol import (PAPER_ABLATION_VARIANTS,
                                 SENSITIVITY_PROFILE_IDS,
                                 load_paper_freeze)

    freeze_file = Path(freeze_path).resolve()
    freeze = load_paper_freeze(freeze_file)
    artifacts = Path(artifact_root).resolve()
    destination = Path(output_root).resolve()
    paper = Path(paper_directory).resolve()
    quality_source = artifacts / "quality_source_manifest.json"
    if not quality_source.is_file():
        raise FileNotFoundError(
            "paper main experiment has not produced quality_source_manifest.json")
    quality_paths = _quality_manifest_paths(quality_source)
    frozen_suites = {
        dataset: sorted(str(circuit) for circuit in
                        freeze["canonical_suites"][dataset]["canonical_inputs"])
        for dataset in DATASETS
    }
    ablation_identities = [
        (str(dataset), str(circuit))
        for dataset, circuit in freeze["ablation_cohort"]["identities"]]
    twelve_identities = [
        (str(dataset), str(circuit))
        for dataset, circuit in freeze["parity_timing_cohort"]["identities"]]
    ablation_paths = _track_manifests(artifacts, "ablation")
    sensitivity_paths = _track_manifests(artifacts, "sensitivity")
    timing_paths = _track_manifests(artifacts, "timing")
    sensitivity_variants = [f"paper_sensitivity_{profile}"
                            for profile in SENSITIVITY_PROFILE_IDS]
    report = aggregate_paper(
        quality_paths,
        frozen_suites=frozen_suites,
        output_dir=destination,
        ablation_manifest_inputs=ablation_paths,
        sensitivity_manifest_inputs=sensitivity_paths,
        timing_manifest_inputs=timing_paths,
        ablation_identities=ablation_identities,
        sensitivity_identities=twelve_identities,
        sensitivity_settings=sensitivity_variants,
        timing_identities=twelve_identities,
        h0_variant=PAPER_ABLATION_VARIANTS["h0"],
        h8_variant=PAPER_ABLATION_VARIANTS["h8"],
        greedy_variant=PAPER_ABLATION_VARIANTS["greedy"],
    )
    paper.mkdir(parents=True, exist_ok=True)
    macro_source = destination / "results_values_zh.tex"
    macro_target = paper / "results_values_zh.tex"
    temporary = macro_target.with_name(f".{macro_target.name}.tmp")
    temporary.write_bytes(macro_source.read_bytes())
    temporary.replace(macro_target)

    final_path = destination / "final_manifest.json"
    final_payload = json.loads(final_path.read_text(encoding="utf-8"))
    reports = {
        path.name: {"path": str(path.resolve()), "sha256": sha256_file(path)}
        for path in sorted((artifacts / "reports").glob("*.json"), key=str)
    } if (artifacts / "reports").exists() else {}
    final_payload.update({
        "paper_freeze": {"path": str(freeze_file),
                         "sha256": sha256_file(freeze_file),
                         "freeze_id": freeze["freeze_id"]},
        "quality_source_manifest": {"path": str(quality_source.resolve()),
                                    "sha256": sha256_file(quality_source)},
        "input_manifest_counts": {
            "main": len(quality_paths), "ablation": len(ablation_paths),
            "sensitivity": len(sensitivity_paths), "timing": len(timing_paths),
        },
        "paper_macro_target": {"path": str(macro_target.resolve()),
                               "sha256": sha256_file(macro_target)},
        "paper_run_reports": reports,
        "plan_path": str(getattr(plan, "path", "")),
    })
    _json_dump(final_path, final_payload)
    return {
        "protocol": report["protocol"],
        "output_root": str(destination),
        "paper_values": str((destination / "paper_values.json").resolve()),
        "results_values_zh_tex": str(macro_target.resolve()),
        "final_manifest": str(final_path.resolve()),
        "input_manifest_counts": final_payload["input_manifest_counts"],
        "strict_common_linear_N": {
            dataset: report["main_summary"]["datasets"][dataset][
                "strict_common_linear_N"] for dataset in DATASETS
        },
        "ablation_available": report["ablation_summary"].get("available", False),
        "sensitivity_available": report["sensitivity_summary"].get(
            "available", False),
        "runtime_available": report["runtime_summary"].get("available", False),
    }


__all__ = [
    "DATASETS", "EXPECTED_SEEDS", "METHODS", "NESTED_TIME_FIELDS",
    "aggregate_paper", "command_aggregate_paper", "build_main_rows", "build_paper_values",
    "render_results_values_tex", "summarize_ablation", "summarize_main",
    "summarize_runtime", "summarize_sensitivity",
]
