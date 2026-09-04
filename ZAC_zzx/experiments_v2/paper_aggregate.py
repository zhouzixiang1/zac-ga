"""Paper-specific aggregation for the Chinese ZAC18/QMAP154 submission.

The accepted native-GA workbook predates the three-seed paper experiment, so
the generic final-results exporter is deliberately not reused here.  This
module has a narrower contract:

* M1/M2 are one actually executed seed-0 manifest per circuit;
* M3/M4 are the median of seeds 0, 1, and 2 per circuit;
* the primary fidelity comparison requires only ZAC, ICCAD/QMAP A*, and
  GA-LK; GA-NL is retained as an internal configuration and never gates the
  primary cohort;
* QMAP inference uses the mean within each ``canonical_sha256`` cluster as
  one independent observation, while compiler coverage remains file based;
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
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from .contracts import RunManifest, RunStatus, load_run_manifest, sha256_file
from .statistics import holm_adjust, paired_wilcoxon, wilson_interval


METHODS = ("M1", "M2", "M3", "M4")
PRIMARY_METHODS = ("M1", "M2", "M4")
INTERNAL_CONFIGURATION_METHODS = ("M3",)
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
ANALYSIS_FIELDS = (
    "log_fidelity", "transfers", "idle_exposures", "move_batches",
    "move_time_us", "algorithm_time_s", *FIDELITY_COMPONENTS,
)
FIG6_RAW_EXPORTS = (
    "fig6_fidelity_gain.csv",
    "fig6_mechanism.csv",
    "fig6_ablation.csv",
    "fig6_m4_stage_time.csv",
)
FIG6_DERIVED_OUTPUTS = (
    "fig6_zac_fidelity.dat",
    "fig6_qmap_fidelity.dat",
    "fig6_mechanism.dat",
    "fig6_ablation.dat",
    "fig6_stage_time.dat",
    "fig6_selected_cases.dat",
    "fig6_selected_cases.tex",
    "fig6_meta.tex",
)

ManifestInput = RunManifest | str | Path


def _is_sha256(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64 and
            all(character in "0123456789abcdef" for character in value))


def _load_manifests(inputs: Iterable[ManifestInput]) -> list[RunManifest]:
    rows: list[RunManifest] = []
    for item in inputs:
        if isinstance(item, RunManifest):
            rows.append(item)
        else:
            rows.append(load_run_manifest(item, require_success_metrics=True))
    return rows


def _exact_matrix(
        label: str, manifests: Sequence[RunManifest],
        expected: set[tuple[Any, ...]], *,
        key: Any) -> None:
    """Fail closed unless one terminal manifest exists for every planned cell."""
    observed = [tuple(key(run)) for run in manifests]
    counts = Counter(observed)
    duplicates = sorted(identity for identity, count in counts.items() if count != 1)
    observed_set = set(observed)
    missing = sorted(expected - observed_set)
    extra = sorted(observed_set - expected)
    if duplicates or missing or extra or len(observed) != len(expected):
        raise ValueError(
            f"paper {label} identity matrix drift: expected={len(expected)}, "
            f"observed={len(observed)}, missing={missing[:5]}, "
            f"extra={extra[:5]}, duplicates={duplicates[:5]}")


def _validate_frozen_evidence(
        label: str, manifests: Sequence[RunManifest], *,
        frozen_inputs: Mapping[str, Mapping[str, str]],
        expected_native_abi: int | None,
        expected_native_wheel_sha256: str | None = None,
        legacy_unspecified_fallback: set[tuple[str, str, str, int, int]] = frozenset(),
        ) -> dict[str, Any]:
    """Validate canonical/ledger/native evidence before any paper statistic.

    ``python_fallback=None`` is accepted only for the explicitly named frozen
    seed-0 rows that passed the parity gate.  New runs and all ABI9 runs must
    state ``False``; ``True`` is never accepted.
    """
    ledgers: MutableMapping[tuple[str, str], set[str]] = defaultdict(set)
    legacy_used: list[tuple[str, str, str, int, int]] = []
    native_wheels: set[str] = set()
    for run in manifests:
        identity = (run.dataset, run.circuit, run.method, run.seed, run.repetition)
        try:
            expected_input = frozen_inputs[run.dataset][run.circuit]
        except KeyError as error:
            raise ValueError(
                f"paper {label} manifest is outside the frozen canonical suite: "
                f"{identity}") from error
        if run.input_sha256 != expected_input:
            raise ValueError(
                f"paper {label} input SHA drift for {identity}: "
                f"{run.input_sha256} != {expected_input}")
        if not run.expected_gate_ledger_sha256:
            raise ValueError(f"paper {label} lacks expected gate ledger: {identity}")
        ledgers[(run.dataset, run.circuit)].add(run.expected_gate_ledger_sha256)
        if (run.status == RunStatus.SUCCESS.value and
                run.observed_gate_ledger_sha256 !=
                run.expected_gate_ledger_sha256):
            raise ValueError(
                f"paper {label} observed gate-ledger mismatch: {identity}")
        if run.status != RunStatus.SUCCESS.value or run.method not in {"M3", "M4"}:
            continue
        if (run.backend != "native" or
                run.native_abi_version != expected_native_abi or
                run.verifier_ok is not True or run.ghost_hits != 0):
            raise ValueError(
                f"paper {label} native success evidence is invalid: {identity}")
        if (not _is_sha256(run.native_wheel_sha256) or
                (expected_native_wheel_sha256 is not None and
                 run.native_wheel_sha256 != expected_native_wheel_sha256)):
            raise ValueError(
                f"paper {label} native wheel drift for {identity}")
        native_wheels.add(run.native_wheel_sha256)
        if run.python_fallback is True:
            raise ValueError(f"paper {label} used Python fallback: {identity}")
        if run.python_fallback is not False:
            if identity not in legacy_unspecified_fallback:
                raise ValueError(
                    f"paper {label} lacks explicit no-fallback evidence: {identity}")
            legacy_used.append(identity)
    ledger_drift = {
        f"{dataset}/{circuit}": sorted(values)
        for (dataset, circuit), values in ledgers.items() if len(values) != 1
    }
    if ledger_drift:
        raise ValueError(
            f"paper {label} cross-method gate-ledger drift: {ledger_drift}")
    return {
        "manifest_N": len(manifests),
        "canonical_inputs_verified": True,
        "cross_method_gate_ledgers_verified": True,
        "successful_observed_gate_ledgers_verified": True,
        "expected_native_abi": expected_native_abi,
        "native_wheel_sha256": sorted(native_wheels),
        "native_successes_fail_closed": True,
        "python_fallback_true_rejected": True,
        "legacy_python_fallback_unspecified": [list(row) for row in sorted(legacy_used)],
        "legacy_python_fallback_exception_N": len(legacy_used),
    }


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


def _has_complete_linear_fidelity(row: Mapping[str, Any], method: str) -> bool:
    cell = row[method]
    return bool(
        cell["log_fidelity"] is not None and
        cell["valid"] == cell["N"] and
        cell["fidelity_valid"] == cell["fidelity_N"])


def _analysis_unit_name(dataset: str) -> str:
    return ("canonical_sha256_cluster_mean" if dataset == "qmap154" else
            "circuit_file")


def _analysis_units(
        rows: Sequence[Mapping[str, Any]], *, dataset: str,
        required_methods: Sequence[str]) -> list[dict[str, Any]]:
    """Return independent units for one fidelity comparison.

    QMAP contains aliases with identical canonical QASM.  Those aliases stay
    in file-level coverage, but their numeric outcomes are averaged within a
    canonical hash before any geometric mean, bootstrap, or signed-rank test.
    ZAC18 has no such aliasing contract, so each frozen file remains one unit.
    """
    grouped: MutableMapping[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if not all(_has_complete_linear_fidelity(row, method)
                   for method in required_methods):
            continue
        circuit = str(row["circuit"])
        digest = row.get("canonical_sha256")
        if dataset == "qmap154":
            if not _is_sha256(digest):
                raise ValueError(
                    "QMAP cluster inference requires canonical_sha256 for "
                    f"every eligible file: {circuit}")
            unit_id = str(digest)
        else:
            unit_id = f"{dataset}/{circuit}"
        grouped[unit_id].append(row)

    units: list[dict[str, Any]] = []
    analysis_unit = _analysis_unit_name(dataset)
    for unit_id, members in sorted(grouped.items()):
        circuits = sorted(str(row["circuit"]) for row in members)
        unit: dict[str, Any] = {
            "analysis_unit": analysis_unit,
            "unit_id": unit_id,
            "canonical_sha256": (
                unit_id if dataset == "qmap154" else
                str(members[0].get("canonical_sha256", ""))),
            "circuits": circuits,
            "member_N": len(members),
        }
        for method in required_methods:
            cell = {
                field: _mean(row[method].get(field) for row in members)
                for field in ANALYSIS_FIELDS
            }
            log_fidelity = cell["log_fidelity"]
            cell["fidelity"] = (
                math.exp(float(log_fidelity))
                if log_fidelity is not None else None)
            unit[method] = cell
        units.append(unit)
    return units


def _comparison_against_strongest_baseline(
        units: Sequence[Mapping[str, Any]], method: str, *,
        analysis_unit: str, iterations: int, seed: int) -> dict[str, Any]:
    paired: list[dict[str, Any]] = []
    for unit in units:
        m1 = float(unit["M1"]["log_fidelity"])
        m2 = float(unit["M2"]["log_fidelity"])
        target = float(unit[method]["log_fidelity"])
        baseline_method = "M1" if float(m1) >= float(m2) else "M2"
        paired.append({
            "unit_id": str(unit["unit_id"]),
            "circuits": list(unit["circuits"]),
            "member_N": int(unit["member_N"]),
            "target": target,
            "baseline": max(m1, m2),
            "baseline_method": baseline_method,
        })
    differences = [row["target"] - row["baseline"] for row in paired]
    bootstrap = {
        **_bootstrap_log_ratio(differences, iterations=iterations, seed=seed),
        "analysis_unit": analysis_unit,
        "resampling": "independent_units_with_replacement",
    }
    wilcoxon = paired_wilcoxon(differences) if differences else None
    if wilcoxon is not None:
        wilcoxon = {**wilcoxon, "analysis_unit": analysis_unit}
    tolerance = 1e-12
    wins = sum(value > tolerance for value in differences)
    losses = sum(value < -tolerance for value in differences)
    ties = len(differences) - wins - losses
    robust: dict[str, Any] = {}
    ordered = sorted(
        paired, key=lambda item: item["target"] - item["baseline"], reverse=True)
    for count in (1, 5, 10):
        retained = ordered[count:] if len(ordered) > count else []
        retained_diffs = [row["target"] - row["baseline"] for row in retained]
        robust[f"remove_top_{count}"] = {
            "removed_units": [
                {key: item[key] for key in ("unit_id", "circuits", "member_N")}
                for item in ordered[:count]
            ],
            "removed_circuits": [
                ",".join(item["circuits"]) for item in ordered[:count]
            ],
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
        "analysis_role": (
            "primary" if method == "M4" else "internal_configuration"),
        "independent_analysis_unit": analysis_unit,
        "strict_common_linear_N": len(paired),
        "strict_common_linear_file_N": sum(row["member_N"] for row in paired),
        "target_geometric_mean_fidelity": _geometric_mean_logs(
            row["target"] for row in paired),
        "strongest_baseline_geometric_mean_fidelity": _geometric_mean_logs(
            row["baseline"] for row in paired),
        "geometric_mean_ratio": bootstrap["ratio"],
        "bootstrap": bootstrap,
        "median_per_circuit_ratio": (
            math.exp(statistics.median(differences)) if differences else None),
        "median_per_independent_unit_ratio": (
            math.exp(statistics.median(differences)) if differences else None),
        "wins": wins, "ties": ties, "losses": losses,
        "wilcoxon": wilcoxon,
        "strongest_baseline_choice_counts": dict(Counter(
            row["baseline_method"] for row in paired)),
        "robustness": robust,
    }


def _method_summary(rows: Sequence[Mapping[str, Any]], method: str,
                    analysis_units: Sequence[Mapping[str, Any]], *,
                    analysis_unit: str) -> dict[str, Any]:
    cells = [row[method] for row in rows]
    common = [unit[method] for unit in analysis_units]
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
        "independent_analysis_unit": analysis_unit,
        "strict_common_linear_N": len(linear),
        "strict_common_linear_file_N": sum(
            int(unit["member_N"]) for unit in analysis_units),
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


def _mechanism_summary(
        analysis_units: Sequence[Mapping[str, Any]], method: str) -> dict[str, Any]:
    cells = [unit[method] for unit in analysis_units]
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
        analysis_units: Sequence[Mapping[str, Any]], method: str) -> dict[str, Any]:
    paired: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for unit in analysis_units:
        baseline = (unit["M1"] if float(unit["M1"]["log_fidelity"]) >=
                    float(unit["M2"]["log_fidelity"]) else unit["M2"])
        paired.append((unit[method], baseline))

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
    """Summarize file coverage separately from independent fidelity units."""
    datasets: dict[str, Any] = {}
    for dataset_index, dataset in enumerate(DATASETS):
        rows = list(main_rows[dataset])
        primary_units = _analysis_units(
            rows, dataset=dataset, required_methods=PRIMARY_METHODS)
        internal_units = _analysis_units(
            rows, dataset=dataset,
            required_methods=("M1", "M2", *INTERNAL_CONFIGURATION_METHODS))
        analysis_unit = _analysis_unit_name(dataset)
        units_by_method = {
            "M1": primary_units,
            "M2": primary_units,
            "M3": internal_units,
            "M4": primary_units,
        }
        comparisons = {
            "M3_vs_Bstar": _comparison_against_strongest_baseline(
                internal_units, "M3", analysis_unit=analysis_unit,
                iterations=bootstrap_iterations,
                seed=bootstrap_seed + 101 * dataset_index),
            "M4_vs_Bstar": _comparison_against_strongest_baseline(
                primary_units, "M4", analysis_unit=analysis_unit,
                iterations=bootstrap_iterations,
                seed=bootstrap_seed + 101 * dataset_index + 1),
        }
        primary_circuits = sorted(
            circuit for unit in primary_units for circuit in unit["circuits"])
        if dataset == "qmap154":
            invalid_hash_circuits = sorted(
                str(row["circuit"]) for row in rows
                if not _is_sha256(row.get("canonical_sha256")))
            if invalid_hash_circuits:
                raise ValueError(
                    "QMAP file inventory lacks canonical_sha256: "
                    f"{invalid_hash_circuits[:5]}")
            full_cluster_counts = Counter(
                str(row["canonical_sha256"]) for row in rows)
        else:
            full_cluster_counts = Counter(
                f"{dataset}/{row['circuit']}" for row in rows)
        cluster_members = [
            {
                "unit_id": unit["unit_id"],
                "canonical_sha256": unit["canonical_sha256"],
                "circuits": unit["circuits"],
                "member_N": unit["member_N"],
            }
            for unit in primary_units
        ]
        datasets[dataset] = {
            "circuit_N": len(rows),
            "coverage_unit": "frozen_input_file",
            "independent_analysis_unit": analysis_unit,
            "primary_methods": list(PRIMARY_METHODS),
            "internal_configuration_methods": list(
                INTERNAL_CONFIGURATION_METHODS),
            "strict_common_linear_circuits": primary_circuits,
            "strict_common_linear_file_N": len(primary_circuits),
            "strict_common_linear_N": len(primary_units),
            "canonical_file_N": len(rows),
            "canonical_cluster_N": len(full_cluster_counts),
            "canonical_cluster_members": cluster_members,
            "canonical_duplicate_cluster_N": sum(
                count > 1 for count in full_cluster_counts.values()),
            "canonical_duplicate_file_N": sum(
                count for count in full_cluster_counts.values() if count > 1),
            "strict_common_duplicate_cluster_N": sum(
                int(unit["member_N"]) > 1 for unit in primary_units),
            "methods": {method: _method_summary(
                rows, method, units_by_method[method],
                analysis_unit=analysis_unit)
                        for method in METHODS},
            "comparisons": comparisons,
            "comparison_roles": {
                "M4_vs_Bstar": "primary",
                "M3_vs_Bstar": "internal_configuration",
            },
            "mechanism": {method: _mechanism_summary(
                units_by_method[method], method)
                          for method in METHODS},
            "mechanism_delta_vs_strongest_baseline": {
                "M3": _mechanism_delta_vs_strongest(internal_units, "M3"),
                "M4": _mechanism_delta_vs_strongest(primary_units, "M4"),
            },
        }
    return {
        "protocol": "paper-zh-v2-main-summary-v1",
        "bootstrap_iterations": bootstrap_iterations,
        "bootstrap_seed": bootstrap_seed,
        "primary_methods": list(PRIMARY_METHODS),
        "internal_configuration_methods": list(INTERNAL_CONFIGURATION_METHODS),
        "fidelity_cohort_semantics": (
            "M1/M2/M4 strict common linear-model cohort; QMAP uses the mean "
            "within each canonical_sha256 cluster as one independent unit"),
        "inference_semantics": (
            "bootstrap and Wilcoxon operate on independent units; GA-NL/M3 "
            "is an internal configuration and does not gate the primary cohort"),
        "coverage_semantics": (
            "compiler success remains reported over every frozen input file"),
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
        explicit = None
        for source in (statistics_payload,
                       statistics_payload.get("decision_summary", {})):
            if not isinstance(source, Mapping):
                continue
            for key in keys:
                value = source.get(key)
                if value is not None:
                    explicit = int(value)
                    break
            if explicit is not None:
                break
        if explicit is not None:
            derived.append(explicit)
            continue
        decisions = statistics_payload.get("decision_log", [])
        if not isinstance(decisions, list):
            continue
        derived.append(sum(
            isinstance(decision, Mapping) and
            (str(decision.get("search_mode", "")).startswith("ga") or
             str(decision.get("search_mode", "")).startswith("greedy-only"))
            for decision in decisions))
    if derived:
        return max(derived), "artifact.compiler_stats.ga_applicable_boundaries"
    return None, "unavailable"


def _paired_variant_fidelity(
        current: Mapping[str, Mapping[str, Any]],
        reference: Mapping[str, Mapping[str, Any]], *,
        iterations: int, seed: int,
        eligible: set[str] | None = None) -> dict[str, Any]:
    circuits = sorted(set(current) & set(reference))
    if eligible is not None:
        circuits = [circuit for circuit in circuits if circuit in eligible]
    circuits = [
        circuit for circuit in circuits
        if all(
            cell["log_fidelity"] is not None and
            cell["valid"] == cell["N"] and
            cell["fidelity_valid"] == cell["fidelity_N"]
            for cell in (current[circuit], reference[circuit]))
    ]
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
                "valid": cell["valid"] if cell else 0,
                "N": cell["N"] if cell else 0,
                "fidelity_valid": cell["fidelity_valid"] if cell else 0,
                "fidelity_N": cell["fidelity_N"] if cell else 0,
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
    setting_cells: dict[str, dict[tuple[str, str], RunManifest]] = {}
    for label in labels:
        cells: dict[tuple[str, str], RunManifest] = {}
        for run in grouped.get(label, ()):
            identity = (run.dataset, run.circuit)
            if identity in cells:
                raise ValueError(
                    "duplicate paper sensitivity identity for "
                    f"{label}: {run.dataset}/{run.circuit}")
            cells[identity] = run
        setting_cells[label] = cells

    default_labels = sorted(
        label for label in labels
        if label == "default" or label.endswith("_default"))
    if len(default_labels) > 1:
        raise ValueError(
            f"multiple paper sensitivity default settings: {default_labels}")
    default_label = default_labels[0] if default_labels else None
    default_cells = setting_cells.get(default_label, {}) if default_label else {}

    def paired_vs_default(
            cells: Mapping[tuple[str, str], RunManifest], *,
            dataset: str | None = None) -> dict[str, Any]:
        identities = sorted(set(default_cells) & set(cells))
        if dataset is not None:
            identities = [identity for identity in identities
                          if identity[0] == dataset]
        fidelity_deltas: list[float] = []
        time_ratios: list[float] = []
        wins = ties = losses = 0
        for identity in identities:
            reference, current = default_cells[identity], cells[identity]
            if (reference.status == RunStatus.SUCCESS.value and
                    current.status == RunStatus.SUCCESS.value and
                    not reference.fidelity_ood and not current.fidelity_ood and
                    reference.log_fidelity is not None and
                    current.log_fidelity is not None):
                delta = float(current.log_fidelity) - float(reference.log_fidelity)
                fidelity_deltas.append(delta)
                if abs(delta) <= 1e-12:
                    ties += 1
                elif delta > 0.0:
                    wins += 1
                else:
                    losses += 1
            reference_ns, current_ns = _runtime_ns(reference), _runtime_ns(current)
            if (reference.status == RunStatus.SUCCESS.value and
                    current.status == RunStatus.SUCCESS.value and
                    reference_ns is not None and current_ns is not None and
                    reference_ns > 0 and current_ns > 0):
                time_ratios.append(float(current_ns) / float(reference_ns))
        fidelity_ratio = (
            math.exp(statistics.fmean(fidelity_deltas))
            if fidelity_deltas else None)
        time_ratio = (
            math.exp(statistics.fmean(math.log(value)
                                      for value in time_ratios))
            if time_ratios else None)
        return {
            "default_setting": default_label,
            "paired_identity_N": len(identities),
            "fidelity_N": len(fidelity_deltas),
            "fidelity_geometric_mean_ratio": fidelity_ratio,
            "fidelity_percent_change": (
                (fidelity_ratio - 1.0) * 100.0
                if fidelity_ratio is not None else None),
            "fidelity_wins": wins,
            "fidelity_ties": ties,
            "fidelity_losses": losses,
            "algorithm_time_N": len(time_ratios),
            "algorithm_time_geometric_mean_ratio": time_ratio,
            "algorithm_time_percent_change": (
                (time_ratio - 1.0) * 100.0 if time_ratio is not None else None),
        }

    datasets = sorted({run.dataset for run in manifests} |
                      {dataset for dataset, _circuit in expected_identities or ()})
    for label in sorted(labels):
        setting_rows = [row for row in rows if row["setting"] == label]
        successful = [row for row in setting_rows
                      if row["status"] == RunStatus.SUCCESS.value]
        linear_logs = [
            float(run.log_fidelity) for run in grouped.get(label, ())
            if run.status == RunStatus.SUCCESS.value and
            not run.fidelity_ood and run.log_fidelity is not None
        ]
        summaries[label] = {
            "profile_id": label.removeprefix("paper_sensitivity_"),
            "valid": len(successful), "N": len(setting_rows),
            "fidelity_geometric_mean": _geometric_mean_logs(
                linear_logs),
            "move_batches_mean": _mean(row["move_batches"] for row in successful),
            "move_time_us_mean": _mean(row["move_time_us"] for row in successful),
            "algorithm_time_s_mean": _mean(
                row["algorithm_time_s"] for row in successful),
            "paired_vs_default": paired_vs_default(setting_cells[label]),
            "paired_vs_default_by_dataset": {
                dataset: paired_vs_default(
                    setting_cells[label], dataset=dataset)
                for dataset in datasets
            },
        }

    for row in rows:
        identity = (row["dataset"], row["circuit"])
        current = setting_cells.get(row["setting"], {}).get(identity)
        reference = default_cells.get(identity)
        row["paired_fidelity_ratio_vs_default"] = None
        row["paired_algorithm_time_ratio_vs_default"] = None
        if current is None or reference is None:
            continue
        if (current.status == RunStatus.SUCCESS.value and
                reference.status == RunStatus.SUCCESS.value and
                not current.fidelity_ood and not reference.fidelity_ood and
                current.log_fidelity is not None and
                reference.log_fidelity is not None):
            row["paired_fidelity_ratio_vs_default"] = math.exp(
                float(current.log_fidelity) - float(reference.log_fidelity))
        current_ns, reference_ns = _runtime_ns(current), _runtime_ns(reference)
        if (current.status == RunStatus.SUCCESS.value and
                reference.status == RunStatus.SUCCESS.value and
                current_ns is not None and reference_ns is not None and
                current_ns > 0 and reference_ns > 0):
            row["paired_algorithm_time_ratio_vs_default"] = (
                float(current_ns) / float(reference_ns))

    cohort = set(default_cells)
    return ({
        "available": bool(manifests),
        "default_setting": default_label,
        "cohort_N": len(cohort),
        "settings": summaries,
        "narrative_thresholds_percent": {
            "fidelity_near_equal": 0.2,
            "algorithm_time_near_equal": 1.0,
        },
    }, rows)


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
        duplicate_repetitions = sum(
            max(0, len(attempts) - 1) for attempts in by_repetition.values())
        unexpected_repetitions = sum(
            run.repetition not in expected_repetitions for run in runs)
        strict_complete = (
            len(successful) == len(expected_repetitions) and
            duplicate_repetitions == 0 and unexpected_repetitions == 0)
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
            "strict_complete": strict_complete,
            "status_counts": json.dumps(dict(Counter(run.status for run in runs
                                                      if run.repetition in expected_repetitions)),
                                        sort_keys=True, separators=(",", ":")),
            "duplicate_repetitions": duplicate_repetitions,
            "unexpected_repetitions": unexpected_repetitions,
        })
    identities = sorted({(str(row["dataset"]), str(row["circuit"]))
                         for row in rows})
    summary: dict[str, Any] = {
        "available": bool(manifests),
        "cohort_N": len(identities),
        "methods": {},
    }
    for method in METHODS:
        method_rows = [row for row in rows if row["method"] == method]
        method_times = [float(row["algorithm_time_median_s"])
                        for row in method_rows
                        if row["strict_complete"] and
                        row["algorithm_time_median_s"] is not None]
        within_circuit_iqrs = [float(row["algorithm_time_iqr_s"])
                               for row in method_rows
                               if row["strict_complete"] and
                               row["algorithm_time_iqr_s"] is not None]
        incomplete_rows = [row for row in method_rows
                           if not row["strict_complete"]]
        incomplete_status_counts: Counter[str] = Counter()
        verifier_fail_circuits: list[str] = []
        for row in incomplete_rows:
            status_counts = json.loads(str(row["status_counts"]))
            incomplete_status_counts.update(status_counts)
            if status_counts.get(RunStatus.VERIFIER_FAIL.value, 0):
                verifier_fail_circuits.append(
                    f"{row['dataset']}/{row['circuit']}")
        summary["methods"][method] = {
            "circuit_N": len(method_times),
            "cohort_N": len(identities),
            "incomplete_circuit_N": len(incomplete_rows),
            "incomplete_status_counts": dict(sorted(
                incomplete_status_counts.items())),
            "verifier_fail_circuit_N": len(verifier_fail_circuits),
            "verifier_fail_circuits": verifier_fail_circuits,
            "median_of_circuit_medians_s": (
                statistics.median(method_times) if method_times else None),
            "median_of_circuit_iqrs_s": (
                statistics.median(within_circuit_iqrs)
                if within_circuit_iqrs else None),
            "iqr_of_circuit_medians_s": _iqr(method_times),
            "arithmetic_mean_of_circuit_medians_s": _mean(method_times),
        }
    by_identity = {
        (str(row["dataset"]), str(row["circuit"]), str(row["method"])): row
        for row in rows
    }
    paired_ratios: dict[str, Any] = {}
    for method in ("M3", "M4"):
        log_ratios: list[float] = []
        paired_circuits: list[str] = []
        for dataset, circuit in identities:
            current = by_identity.get((dataset, circuit, method), {})
            reference = by_identity.get((dataset, circuit, "M2"), {})
            current_time = current.get("algorithm_time_median_s")
            reference_time = reference.get("algorithm_time_median_s")
            if (not current.get("strict_complete") or
                    not reference.get("strict_complete") or
                    current_time is None or reference_time is None or
                    float(current_time) <= 0.0 or float(reference_time) <= 0.0):
                continue
            log_ratios.append(math.log(float(current_time) / float(reference_time)))
            paired_circuits.append(f"{dataset}/{circuit}")
        paired_ratios[f"{method}_vs_M2"] = {
            "N": len(log_ratios),
            "geometric_mean_time_ratio": (
                math.exp(statistics.fmean(log_ratios)) if log_ratios else None),
            "circuits": paired_circuits,
            "semantics": "method full-compile median divided by M2 full-compile median",
        }
    summary["paired_ratios"] = paired_ratios
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
        writer = csv.DictWriter(
            handle,
            fieldnames=keys,
            extrasaction="ignore",
            lineterminator="\n",
        )
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


SENSITIVITY_PAPER_PROFILES = (
    ("budget_192", "低预算192", "BudgetLow"),
    ("budget_1152", "高预算1152", "BudgetHigh"),
    ("return_4_2", "RETURN 4/2", "ReturnSmall"),
    ("return_10_8", "RETURN 10/8", "ReturnLarge"),
    ("horizon_2", r"$H_{\max}=2$", "HorizonTwo"),
    ("horizon_4", r"$H_{\max}=4$", "HorizonFour"),
    ("decay_0p2_0p5", "衰减$(0.2,0.5)$", "DecayWeak"),
    ("decay_0p35_0p6", "衰减$(0.35,0.6)$", "DecayMiddle"),
)


def _sensitivity_profiles(
        settings: Mapping[str, Mapping[str, Any]],
        ) -> dict[str, Mapping[str, Any]]:
    return {
        str(value.get("profile_id") or
            str(label).removeprefix("paper_sensitivity_")): value
        for label, value in settings.items()
    }


def _sensitivity_fidelity_phrase(change: float | None, *,
                                 near_equal: float) -> str:
    if change is None:
        return "Fidelity配对不可用"
    if abs(change) <= near_equal:
        return f"Fidelity近似不变（{change:+.2f}\\%）"
    if change > 0.0:
        verb = "略优" if change <= 1.0 else "提高"
        return f"Fidelity{verb}{change:.2f}\\%"
    return f"Fidelity下降{-change:.2f}\\%"


def _sensitivity_time_phrase(change: float | None, *,
                             near_equal: float) -> str:
    if change is None:
        return "完整编译时间配对不可用"
    if abs(change) <= near_equal:
        return f"完整编译时间近似不变（{change:+.2f}\\%）"
    if change < 0.0:
        return f"更快（完整编译时间降低{-change:.2f}\\%）"
    return f"更慢（完整编译时间增加{change:.2f}\\%）"


def _sensitivity_statement(sensitivity: Mapping[str, Any]) -> str:
    settings = sensitivity.get("settings", {})
    profiles = _sensitivity_profiles(settings)
    thresholds = sensitivity.get("narrative_thresholds_percent", {})
    fidelity_threshold = float(thresholds.get("fidelity_near_equal", 0.2))
    time_threshold = float(thresholds.get("algorithm_time_near_equal", 1.0))
    def change(profile: str, field: str) -> float | None:
        value = profiles.get(profile, {}).get(
            "paired_vs_default", {}).get(field)
        return float(value) if value is not None else None

    fidelity = {
        profile: change(profile, "fidelity_percent_change")
        for profile, _label, _macro_suffix in SENSITIVITY_PAPER_PROFILES
    }
    runtime = {
        profile: change(profile, "algorithm_time_percent_change")
        for profile, _label, _macro_suffix in SENSITIVITY_PAPER_PROFILES
    }
    if not any(value is not None for value in (*fidelity.values(),
                                                *runtime.values())):
        return r"\textemdash{}"
    default = profiles.get("default", {}).get("paired_vs_default", {})
    fidelity_n = default.get("fidelity_N")
    time_n = default.get("algorithm_time_N")
    cohort_n = sensitivity.get("cohort_N")
    evidence = (
        f"共{cohort_n}个电路、{len(settings)}组单因素设置相对默认值同电路配对"
        f"（Fidelity有效$N={fidelity_n}$，时间$N={time_n}$）")
    # The registered one-factor design is always represented by structured
    # ratios above.  Keep the prose macro compact by grouping settings only
    # when their data-derived qualitative relations actually agree.
    low_profiles = ("budget_192", "return_4_2")
    slow_profiles = ("budget_1152", "return_10_8")
    degraded_profiles = (
        "horizon_2", "decay_0p2_0p5", "decay_0p35_0p6")
    expected_pattern = (
        all(fidelity[p] is not None and
            abs(fidelity[p]) <= fidelity_threshold and
            runtime[p] is not None and runtime[p] < -time_threshold
            for p in low_profiles) and
        all(fidelity[p] is not None and
            abs(fidelity[p]) <= fidelity_threshold and
            runtime[p] is not None and runtime[p] > time_threshold
            for p in slow_profiles) and
        all(fidelity[p] is not None and fidelity[p] < -fidelity_threshold
            for p in degraded_profiles) and
        fidelity["horizon_4"] is not None and
        abs(fidelity["horizon_4"]) <= fidelity_threshold and
        runtime["horizon_4"] is not None)
    if expected_pattern:
        parts = [
            ("低预算192/RETURN 4/2的Fidelity近似持平且更快"
             f"（Fidelity {fidelity['budget_192']:+.2f}\\%/"
             f"{fidelity['return_4_2']:+.2f}\\%，完整编译时间"
             f"{runtime['budget_192']:+.2f}\\%/"
             f"{runtime['return_4_2']:+.2f}\\%）"),
            ("高预算1152/RETURN 10/8质量近似但更慢"
             f"（完整编译时间{runtime['budget_1152']:+.2f}\\%/"
             f"{runtime['return_10_8']:+.2f}\\%）"),
            (r"$H_{\max}=2$及衰减$(0.2,0.5)/(0.35,0.6)$的Fidelity分别"
             f"下降{-fidelity['horizon_2']:.2f}\\%/"
             f"{-fidelity['decay_0p2_0p5']:.2f}\\%/"
             f"{-fidelity['decay_0p35_0p6']:.2f}\\%"),
            (r"$H_{\max}=4$近似不变"
             f"（Fidelity {fidelity['horizon_4']:+.2f}\\%，完整编译时间"
             f"{runtime['horizon_4']:+.2f}\\%）"),
        ]
    else:
        parts = []
        for profile, label, _macro_suffix in SENSITIVITY_PAPER_PROFILES:
            if fidelity[profile] is None and runtime[profile] is None:
                continue
            parts.append(
                f"{label}："
                f"{_sensitivity_fidelity_phrase(fidelity[profile], near_equal=fidelity_threshold)}，"
                f"{_sensitivity_time_phrase(runtime[profile], near_equal=time_threshold)}")
    return evidence + "；" + "；".join(parts)


def build_paper_values(
        main_summary: Mapping[str, Any],
        ablation_summary: Mapping[str, Any],
        main_rows: Mapping[str, Sequence[Mapping[str, Any]]],
        sensitivity_summary: Mapping[str, Any] | None = None,
        runtime_summary: Mapping[str, Any] | None = None) -> dict[str, Any]:
    macros: dict[str, str] = {}
    for dataset, prefix in (("zac18", "ZAC"), ("qmap154", "QMAP")):
        dataset_summary = main_summary["datasets"][dataset]
        macros[f"{prefix}StrictN"] = str(
            dataset_summary["strict_common_linear_N"])
        macros[f"{prefix}StrictFileN"] = str(
            dataset_summary["strict_common_linear_file_N"])
        macros[f"{prefix}AnalysisUnit"] = (
            "canonical哈希簇" if dataset == "qmap154" else "电路文件")
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
            coverage_count = (
                coverage["complete_seed_circuits"]
                if method in {"M3", "M4"}
                else coverage["success_circuits"])
            macros[f"{prefix}{suffix}V"] = (
                f"{coverage_count}/{coverage['N']}")
        macros[f"{prefix}CoverageStatement"] = "，".join(
            f"{method} 成功"
            f"{dataset_summary['methods'][method]['coverage']['success_circuits']}"
            f"/{dataset_summary['methods'][method]['coverage']['N']}"
            f"（完整种子"
            f"{dataset_summary['methods'][method]['coverage']['complete_seed_circuits']}"
            f"/{dataset_summary['methods'][method]['coverage']['N']}）"
            for method in METHODS)

        for method in ("M3", "M4"):
            suffix = _macro(method)
            comparison = dataset_summary["comparisons"][f"{method}_vs_Bstar"]
            bootstrap = comparison["bootstrap"]
            wilcoxon = comparison.get("wilcoxon") or {}
            macros[f"{prefix}{suffix}Ratio"] = _format_number(
                comparison.get("geometric_mean_ratio"), digits=4)
            low = bootstrap.get("ci95_low")
            high = bootstrap.get("ci95_high")
            macros[f"{prefix}{suffix}CI"] = (
                f"[{float(low):.4f}, {float(high):.4f}]"
                if low is not None and high is not None else r"\textemdash{}")
            macros[f"{prefix}{suffix}Median"] = _format_number(
                comparison.get("median_per_circuit_ratio"), digits=4)
            macros[f"{prefix}{suffix}WTL"] = (
                f"{comparison['wins']}/{comparison['ties']}/{comparison['losses']}")
            # M4 and H8 are members of the frozen primary Holm family.  M3 is
            # an exploratory diagnostic and therefore retains its raw p-value.
            p_value = (wilcoxon.get("paper_holm_adjusted_p_value")
                       if method == "M4" else wilcoxon.get("p_value"))
            macros[f"{prefix}{suffix}P"] = _format_number(p_value, digits=4)
            macros[f"{prefix}{suffix}Effect"] = _format_number(
                wilcoxon.get("rank_biserial"), digits=3)
            for count, name in ((1, "One"), (5, "Five"), (10, "Ten")):
                robust = comparison["robustness"][f"remove_top_{count}"]
                macros[f"{prefix}{suffix}Robust{name}"] = _format_number(
                    robust.get("geometric_mean_ratio"), digits=4)

        mechanism_delta = dataset_summary[
            "mechanism_delta_vs_strongest_baseline"]["M4"]
        macros[f"{prefix}MFourDeltaTransfer"] = _format_number(
            mechanism_delta.get("transfers_mean_delta"), digits=2)
        macros[f"{prefix}MFourDeltaIdle"] = _format_number(
            mechanism_delta.get("idle_exposures_mean_delta"), digits=2)
        macros[f"{prefix}MFourDeltaCoherence"] = _format_number(
            mechanism_delta.get("log_coherence_linear_mean_delta"), digits=4)
        macros[f"{prefix}MFourDeltaBatch"] = _format_number(
            mechanism_delta.get("move_batches_mean_delta"), digits=2)
        move_delta = mechanism_delta.get("move_time_us_mean_delta")
        macros[f"{prefix}MFourDeltaMoveTime"] = _format_number(
            float(move_delta) / 1000.0 if move_delta is not None else None,
            digits=3)

    statements: list[str] = []
    for dataset, label in (("zac18", "ZAC18"), ("qmap154", "QMAP154")):
        comparison = main_summary["datasets"][dataset]["comparisons"]["M4_vs_Bstar"]
        ratio = comparison["geometric_mean_ratio"]
        if ratio is not None:
            change = float(ratio) - 1.0
            if abs(change) < 5e-5:
                direction = "Fidelity几何均值点估计基本持平"
            elif change > 0:
                direction = f"Fidelity几何均值点估计提高{change * 100.0:.2f}\\%"
            else:
                direction = f"Fidelity几何均值点估计降低{-change * 100.0:.2f}\\%"
            bootstrap = comparison.get("bootstrap", {})
            low = bootstrap.get("ci95_low")
            high = bootstrap.get("ci95_high")
            median = comparison.get("median_per_circuit_ratio")
            robust_ten = comparison.get("robustness", {}).get(
                "remove_top_10", {}).get("geometric_mean_ratio")
            evidence = [
                (f"严格共同集合$N={comparison['strict_common_linear_N']}$个"
                 f"{'canonical哈希簇' if dataset == 'qmap154' else '电路文件'}"
                 + (f"（对应{comparison['strict_common_linear_file_N']}个文件）"
                    if dataset == "qmap154" else "")),
                (f"95\\% CI [{float(low):.4f}, {float(high):.4f}]"
                 if low is not None and high is not None else "95\\% CI不可用"),
                (f"中位比{float(median):.4f}"
                 if median is not None else "中位比不可用"),
                f"{comparison['wins']}/{comparison['ties']}/{comparison['losses']} 胜/平/负",
                (f"去除最大10项收益后比值{float(robust_ten):.4f}"
                 if robust_ten is not None else "去长尾比值不可用"),
            ]
            limits: list[str] = []
            if (low is not None and high is not None and
                    float(low) <= 1.0 <= float(high)):
                limits.append("置信区间跨1")
            if (median is not None and
                    (float(median) <= 1.0 or
                     comparison["wins"] <= comparison["losses"])):
                limits.append("中位比和胜负分布不支持多数电路改善")
            if robust_ten is not None and float(robust_ten) <= 1.0:
                limits.append("去长尾后总体增益不再保持")
            statement = (
                f"在{label}上，完整方法相对逐电路最强基线的{direction}"
                f"（{'，'.join(evidence)}）")
            if limits:
                statement += f"；{'，'.join(limits)}"
            statements.append(statement)
    macros["ResultStatement"] = (
        "；".join(statements) if statements else r"\textemdash{}")

    if ablation_summary.get("available"):
        lookahead = ablation_summary["lookahead_H8_vs_H0"]
        ga = ablation_summary["GA_vs_greedy"]
        h_wilcoxon = lookahead.get("wilcoxon") or {}
        ga_wilcoxon = ga.get("wilcoxon") or {}
        macros["AblationHZero"] = "M4-H0"
        macros["AblationHMulti"] = "M4-H8"
        macros["AblationHN"] = str(lookahead.get("N", 0))
        macros["AblationHRatio"] = _format_number(
            lookahead.get("geometric_mean_ratio"), digits=4)
        h_bootstrap = lookahead.get("bootstrap", {})
        macros["AblationHCI"] = (
            f"[{float(h_bootstrap['ci95_low']):.4f}, "
            f"{float(h_bootstrap['ci95_high']):.4f}]"
            if h_bootstrap.get("ci95_low") is not None and
            h_bootstrap.get("ci95_high") is not None else r"\textemdash{}")
        macros["AblationHWTL"] = (
            f"{lookahead['wins']}/{lookahead['ties']}/{lookahead['losses']}")
        macros["AblationHP"] = _format_number(
            h_wilcoxon.get("paper_holm_adjusted_p_value"), digits=4)
        deltas = lookahead.get("mechanism_deltas", {})
        macros["AblationHPredictionHit"] = _format_number(
            (float(deltas["H8_prediction_hit_rate"]) * 100.0
             if deltas.get("H8_prediction_hit_rate") is not None else None),
            digits=2)
        for macro_name, field in (
                ("AblationHTransferDelta", "transfers"),
                ("AblationHIdleDelta", "idle_exposures"),
                ("AblationHCoherenceDelta", "log_coherence_linear")):
            macros[macro_name] = _format_number(
                deltas.get(field, {}).get("mean_H8_minus_H0"), digits=3)
        h_move_delta = deltas.get("move_time_us", {}).get("mean_H8_minus_H0")
        macros["AblationHMoveTimeDelta"] = _format_number(
            float(h_move_delta) / 1000.0 if h_move_delta is not None else None,
            digits=3)
        macros["AblationGreedy"] = "M4-greedy"
        macros["AblationGA"] = "M4-GA"
        macros["AblationGAN"] = str(ga.get("N", 0))
        applicable = ablation_summary.get("ga_applicable_circuit_N")
        macros["AblationGAApplicableN"] = (
            str(applicable) if applicable is not None else r"\textemdash{}")
        macros["AblationGARatio"] = _format_number(
            ga.get("geometric_mean_ratio"), digits=4)
        ga_bootstrap = ga.get("bootstrap", {})
        macros["AblationGACI"] = (
            f"[{float(ga_bootstrap['ci95_low']):.4f}, "
            f"{float(ga_bootstrap['ci95_high']):.4f}]"
            if ga_bootstrap.get("ci95_low") is not None and
            ga_bootstrap.get("ci95_high") is not None else r"\textemdash{}")
        macros["AblationGAWTL"] = (
            f"{ga['wins']}/{ga['ties']}/{ga['losses']}")
        macros["AblationGAP"] = _format_number(
            ga_wilcoxon.get("p_value"), digits=4)
    else:
        for name in (
                "AblationHZero", "AblationHMulti", "AblationHN",
                "AblationHRatio", "AblationHCI", "AblationHWTL",
                "AblationHP", "AblationHPredictionHit",
                "AblationHTransferDelta", "AblationHIdleDelta",
                "AblationHCoherenceDelta", "AblationHMoveTimeDelta",
                "AblationGreedy", "AblationGA", "AblationGAN",
                "AblationGAApplicableN", "AblationGARatio",
                "AblationGACI", "AblationGAWTL", "AblationGAP"):
            macros[name] = r"\textemdash{}"

    m4_cells = [
        row["M4"] for dataset in DATASETS for row in main_rows[dataset]
        if row["M4"]["valid"] == row["M4"]["N"]
    ]
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

    runtime = runtime_summary or {}
    if runtime.get("available"):
        strict_parts: list[str] = []
        cohort_n = int(runtime.get("cohort_N", 0))
        for method in METHODS:
            suffix = _macro(method)
            method_runtime = runtime.get("methods", {}).get(method, {})
            circuit_n = int(method_runtime.get("circuit_N", 0))
            macros[f"Strict{suffix}V"] = (
                f"{circuit_n}/{cohort_n}"
                if cohort_n else r"\textemdash{}")
            macros[f"Strict{suffix}Time"] = _format_number(
                method_runtime.get("median_of_circuit_medians_s"), digits=3)
            macros[f"Strict{suffix}IQR"] = _format_number(
                method_runtime.get("median_of_circuit_iqrs_s"), digits=3)
            median_time = method_runtime.get("median_of_circuit_medians_s")
            time_text = (f"{float(median_time):.3f}s"
                         if median_time is not None else "时间不可用")
            coverage_parts = [
                f"有效{circuit_n}/{cohort_n}"
                if cohort_n else "有效电路数不可用"]
            verifier_fail_n = int(
                method_runtime.get("verifier_fail_circuit_N", 0))
            if verifier_fail_n:
                coverage_parts.append(f"{verifier_fail_n}个电路验证失败")
            strict_parts.append(
                f"{method} {time_text}（{'，'.join(coverage_parts)}）")
        ratios = runtime.get("paired_ratios", {})
        macros["StrictMThreeVsMTwo"] = _format_number(
            ratios.get("M3_vs_M2", {}).get("geometric_mean_time_ratio"),
            digits=3)
        macros["StrictMFourVsMTwo"] = _format_number(
            ratios.get("M4_vs_M2", {}).get("geometric_mean_time_ratio"),
            digits=3)
        macros["StrictRuntimeStatement"] = "，".join(strict_parts)
    else:
        for method in METHODS:
            suffix = _macro(method)
            macros[f"Strict{suffix}V"] = r"\textemdash{}"
            macros[f"Strict{suffix}Time"] = r"\textemdash{}"
            macros[f"Strict{suffix}IQR"] = r"\textemdash{}"
        macros["StrictMThreeVsMTwo"] = r"\textemdash{}"
        macros["StrictMFourVsMTwo"] = r"\textemdash{}"
        macros["StrictRuntimeStatement"] = r"\textemdash{}"

    sensitivity = sensitivity_summary or {}
    sensitivity_settings = sensitivity.get("settings", {})
    if sensitivity.get("available") and sensitivity_settings:
        profiles = _sensitivity_profiles(sensitivity_settings)
        default_comparison = profiles.get("default", {}).get(
            "paired_vs_default", {})
        macros["SensitivityCircuitN"] = str(sensitivity.get("cohort_N", 0))
        macros["SensitivityFidelityN"] = str(
            default_comparison.get("fidelity_N", 0))
        macros["SensitivityTimeN"] = str(
            default_comparison.get("algorithm_time_N", 0))
        for profile, _label, macro_suffix in SENSITIVITY_PAPER_PROFILES:
            comparison = profiles.get(profile, {}).get(
                "paired_vs_default", {})
            macros[f"Sensitivity{macro_suffix}FidelityRatio"] = _format_number(
                comparison.get("fidelity_geometric_mean_ratio"), digits=4)
            macros[f"Sensitivity{macro_suffix}TimeRatio"] = _format_number(
                comparison.get("algorithm_time_geometric_mean_ratio"), digits=4)
        macros["SensitivityStatement"] = _sensitivity_statement(sensitivity)
    else:
        macros["SensitivityCircuitN"] = "0"
        macros["SensitivityFidelityN"] = "0"
        macros["SensitivityTimeN"] = "0"
        for _profile, _label, macro_suffix in SENSITIVITY_PAPER_PROFILES:
            macros[f"Sensitivity{macro_suffix}FidelityRatio"] = r"\textemdash{}"
            macros[f"Sensitivity{macro_suffix}TimeRatio"] = r"\textemdash{}"
        macros["SensitivityStatement"] = r"\textemdash{}"

    for method in METHODS:
        method_mechanisms = [
            (main_summary["datasets"][dataset]["mechanism"][method],
             int(main_summary["datasets"][dataset]["methods"][method][
                 "strict_common_linear_N"]))
            for dataset in DATASETS
        ]

        def weighted(field: str) -> float | None:
            values = [
                (float(summary[field]), count)
                for summary, count in method_mechanisms
                if count > 0 and summary.get(field) is not None
            ]
            total = sum(count for _value, count in values)
            return (sum(value * count for value, count in values) / total
                    if total else None)

        suffix = _macro(method)
        macros[f"Mechanism{suffix}Transfer"] = _format_number(
            weighted("transfers_mean"), digits=1)
        macros[f"Mechanism{suffix}Idle"] = _format_number(
            weighted("idle_exposures_mean"), digits=1)
        coherence = weighted("log_coherence_linear_mean")
        macros[f"Mechanism{suffix}Coherence"] = _format_number(
            -float(coherence) if coherence is not None else None, digits=3)
        macros[f"Mechanism{suffix}Batch"] = _format_number(
            weighted("move_batches_mean"), digits=1)
        move_time = weighted("move_time_us_mean")
        macros[f"Mechanism{suffix}MoveTime"] = _format_number(
            float(move_time) / 1000.0 if move_time is not None else None, digits=3)
    return {
        "protocol": "paper-zh-v2-values-v1",
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


def _primary_analysis_unit_rows(
        main_rows: Mapping[str, Sequence[Mapping[str, Any]]],
        ) -> list[dict[str, Any]]:
    """Export the exact independent observations used by the primary tests."""
    result: list[dict[str, Any]] = []
    for dataset in DATASETS:
        units = _analysis_units(
            main_rows[dataset], dataset=dataset,
            required_methods=PRIMARY_METHODS)
        for unit in units:
            m1_log = float(unit["M1"]["log_fidelity"])
            m2_log = float(unit["M2"]["log_fidelity"])
            baseline_method = "M1" if m1_log >= m2_log else "M2"
            baseline_log = max(m1_log, m2_log)
            m4_log = float(unit["M4"]["log_fidelity"])
            row: dict[str, Any] = {
                "dataset": dataset,
                "analysis_unit": unit["analysis_unit"],
                "unit_id": unit["unit_id"],
                "canonical_sha256": unit["canonical_sha256"],
                "member_N": unit["member_N"],
                "circuits": _csv_value(unit["circuits"]),
                "baseline_method": baseline_method,
                "Bstar__log_fidelity": baseline_log,
                "Bstar__fidelity": math.exp(baseline_log),
                "M4_minus_Bstar_delta_logF": m4_log - baseline_log,
                "M4_over_Bstar_ratio": math.exp(m4_log - baseline_log),
            }
            for method in PRIMARY_METHODS:
                for field, value in unit[method].items():
                    row[f"{method}__{field}"] = value
            result.append(row)
    return result


def _figure_main_rows(
        main_rows: Mapping[str, Sequence[Mapping[str, Any]]],
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                   list[dict[str, Any]]]:
    """Build plot-ready per-circuit main-result tables for manuscript figures.

    The strongest baseline is selected by fidelity on the same circuit.  Rows
    outside the strict three-method primary linear-model cohort are retained with an
    explicit ``strict_paired=False`` marker and blank deltas, so a plotting
    script cannot silently change the denominator by dropping failed runs.
    """
    fidelity_rows: list[dict[str, Any]] = []
    mechanism_rows: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    mechanism_fields = (
        "transfers", "idle_exposures", "move_batches", "move_time_us",
        "log_atom_transfer", "log_idle_excitation", "log_coherence_linear",
    )
    for dataset in DATASETS:
        canonical_counts = Counter(
            str(row.get("canonical_sha256", ""))
            for row in main_rows[dataset])
        for row in main_rows[dataset]:
            strict = all(
                _has_complete_linear_fidelity(row, method)
                for method in PRIMARY_METHODS)
            m1_log = row["M1"]["log_fidelity"]
            m2_log = row["M2"]["log_fidelity"]
            baseline_method = None
            if m1_log is not None and m2_log is not None:
                baseline_method = (
                    "M1" if float(m1_log) >= float(m2_log) else "M2")
            baseline = row[baseline_method] if baseline_method else None
            m4 = row["M4"]
            delta_log = (
                float(m4["log_fidelity"]) - float(baseline["log_fidelity"])
                if strict and baseline is not None else None)
            fidelity_rows.append({
                "dataset": dataset,
                "circuit": row["circuit"],
                "canonical_sha256": row.get("canonical_sha256", ""),
                "independent_analysis_unit": _analysis_unit_name(dataset),
                "canonical_cluster_file_N": canonical_counts[
                    str(row.get("canonical_sha256", ""))],
                "strict_paired": strict,
                "baseline_method": baseline_method,
                "baseline_log_fidelity": (
                    baseline["log_fidelity"] if baseline else None),
                "baseline_fidelity": baseline["fidelity"] if baseline else None,
                "M4_log_fidelity": m4["log_fidelity"],
                "M4_fidelity": m4["fidelity"],
                "M4_minus_Bstar_delta_logF": delta_log,
                "M4_over_Bstar_ratio": (
                    math.exp(delta_log) if delta_log is not None else None),
                "M4_valid_over_N": m4["valid_over_N"],
            })

            mechanism: dict[str, Any] = {
                "dataset": dataset,
                "circuit": row["circuit"],
                "canonical_sha256": row.get("canonical_sha256", ""),
                "independent_analysis_unit": _analysis_unit_name(dataset),
                "canonical_cluster_file_N": canonical_counts[
                    str(row.get("canonical_sha256", ""))],
                "strict_paired": strict,
                "baseline_method": baseline_method,
                "count_delta_semantics": (
                    "M4 minus strongest-fidelity baseline; negative favorable"),
                "log_component_delta_semantics": (
                    "M4 minus strongest-fidelity baseline; positive favorable"),
            }
            for field in mechanism_fields:
                reference = baseline.get(field) if baseline else None
                current = m4.get(field)
                mechanism[f"Bstar_{field}"] = reference
                mechanism[f"M4_{field}"] = current
                mechanism[f"M4_minus_Bstar_{field}"] = (
                    float(current) - float(reference)
                    if strict and current is not None and reference is not None
                    else None)
            mechanism_rows.append(mechanism)

            stage_rows.append({
                "dataset": dataset,
                "circuit": row["circuit"],
                "status": m4["status"],
                "valid": m4["valid"],
                "N": m4["N"],
                "valid_over_N": m4["valid_over_N"],
                "full_compile_s": m4["algorithm_time_s"],
                "initial_placement_s": m4["initial_placement_s"],
                "problem_preparation_s": m4["problem_preparation_s"],
                "search_kernel_s": m4["search_kernel_s"],
                "result_commit_s": m4["result_commit_s"],
                "routing_s": m4["routing_s"],
                "transition_decision_s": m4["transition_decision_s"],
                "return_match_s": m4["return_match_s"],
                "forecast_s": m4["forecast_s"],
                "return_match_and_forecast_nested_in_search_kernel": True,
            })
    return fidelity_rows, mechanism_rows, stage_rows


def _figure_ablation_rows(
        rows: Sequence[Mapping[str, Any]], *, h0_variant: str,
        h8_variant: str, greedy_variant: str) -> list[dict[str, Any]]:
    """Return one plot-ready row per circuit and controlled comparison."""
    grouped = {(str(row["dataset"]), str(row["circuit"]), str(row["variant"])):
               row for row in rows}
    identities = sorted({(key[0], key[1]) for key in grouped})
    result: list[dict[str, Any]] = []
    for dataset, circuit in identities:
        h0 = grouped.get((dataset, circuit, h0_variant), {})
        h8 = grouped.get((dataset, circuit, h8_variant), {})
        greedy = grouped.get((dataset, circuit, greedy_variant), {})
        comparisons = (
            ("H8_vs_H0", h8, h0, True),
            ("GA_vs_greedy", h8, greedy,
             bool((h8.get("ga_applicable_boundaries") or 0) > 0)),
        )
        for comparison, current, reference, in_scope in comparisons:
            current_fidelity = current.get("fidelity")
            reference_fidelity = reference.get("fidelity")
            current_complete = bool(
                current.get("valid") == current.get("N") and
                current.get("fidelity_valid") == current.get("fidelity_N") and
                current.get("N") in {1, 3})
            reference_complete = bool(
                reference.get("valid") == reference.get("N") and
                reference.get("fidelity_valid") == reference.get("fidelity_N") and
                reference.get("N") in {1, 3})
            paired = bool(
                in_scope and current_complete and reference_complete and
                current_fidelity is not None and
                reference_fidelity is not None and
                float(current_fidelity) > 0 and float(reference_fidelity) > 0)
            delta_log = (
                math.log(float(current_fidelity)) -
                math.log(float(reference_fidelity)) if paired else None)
            output: dict[str, Any] = {
                "dataset": dataset,
                "circuit": circuit,
                "comparison": comparison,
                "in_scope": in_scope,
                "paired_valid": paired,
                "current_variant": current.get("variant"),
                "reference_variant": reference.get("variant"),
                "current_fidelity": current_fidelity,
                "reference_fidelity": reference_fidelity,
                "delta_logF": delta_log,
                "fidelity_ratio": (
                    math.exp(delta_log) if delta_log is not None else None),
                "current_valid_over_N": current.get("valid_over_N"),
                "reference_valid_over_N": reference.get("valid_over_N"),
                "ga_applicable_boundaries": h8.get(
                    "ga_applicable_boundaries"),
            }
            for field in ("transfers", "idle_exposures",
                          "log_coherence_linear", "move_batches",
                          "move_time_us", "algorithm_time_s"):
                left, right = current.get(field), reference.get(field)
                output[f"current_{field}"] = left
                output[f"reference_{field}"] = right
                output[f"delta_{field}"] = (
                    float(left) - float(right)
                    if paired and left is not None and right is not None
                    else None)
            result.append(output)
    return result


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
    paper_values = build_paper_values(
        main_summary, ablation_summary, main_rows,
        sensitivity_summary=sensitivity_summary,
        runtime_summary=runtime_summary)
    report: dict[str, Any] = {
        "protocol": "paper-zh-v2-aggregate-v1",
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
        figure_fidelity, figure_mechanism, figure_stages = _figure_main_rows(
            main_rows)
        figure_ablation = _figure_ablation_rows(
            ablation_rows, h0_variant=h0_variant, h8_variant=h8_variant,
            greedy_variant=greedy_variant)
        _write_csv(destination / "zac18.csv",
                   [_flatten_main_row(row) for row in main_rows["zac18"]])
        _write_csv(destination / "qmap154.csv",
                   [_flatten_main_row(row) for row in main_rows["qmap154"]])
        _write_csv(destination / "ablation.csv", ablation_rows)
        _write_csv(destination / "sensitivity.csv", sensitivity_rows)
        _write_csv(destination / "runtime.csv", runtime_rows)
        _write_csv(destination / "main_primary_analysis_units.csv",
                   _primary_analysis_unit_rows(main_rows))
        _write_csv(destination / "fig6_fidelity_gain.csv", figure_fidelity)
        _write_csv(destination / "fig6_mechanism.csv", figure_mechanism)
        _write_csv(destination / "fig6_ablation.csv", figure_ablation)
        _write_csv(destination / "fig6_m4_stage_time.csv", figure_stages)
        _json_dump(destination / "main_summary.json", main_summary)
        _json_dump(destination / "paper_values.json", paper_values)
        (destination / "results_values_zh.tex").write_text(
            render_results_values_tex(paper_values), encoding="utf-8")
        files = [path for path in destination.iterdir()
                 if path.is_file() and path.name != "final_manifest.json"]
        final_manifest = {
            "protocol": "paper-zh-v2-final-manifest-v1",
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
    manifests: list[RunManifest] = []
    expected: set[tuple[str, str, str, int, int]] = set()
    for method in ("M1", "M2"):
        for identity, row in sorted(payload["baselines"][method].items()):
            dataset, circuit = identity.split("/", 1)
            path = _verified_manifest_path(
                row, label=f"quality {method}/{identity}")
            run = load_run_manifest(path, require_success_metrics=True)
            key = (dataset, circuit, method, 0, 0)
            if ((run.dataset, run.circuit, run.method, run.seed, run.repetition)
                    != key or run.status != row.get("status")):
                raise ValueError(f"quality source identity/status drift: {key}")
            paths.append(path)
            manifests.append(run)
            expected.add(key)
    for method in ("M3", "M4"):
        for identity, seeds in sorted(payload["ours"][method].items()):
            if set(seeds) != {"0", "1", "2"}:
                raise ValueError(
                    f"quality {method}/{identity} does not contain seeds 0,1,2")
            for seed, row in sorted(seeds.items(), key=lambda item: int(item[0])):
                dataset, circuit = identity.split("/", 1)
                path = _verified_manifest_path(
                    row, label=f"quality {method}/{identity}/seed{seed}")
                run = load_run_manifest(path, require_success_metrics=True)
                key = (dataset, circuit, method, int(seed), 0)
                if ((run.dataset, run.circuit, run.method, run.seed,
                     run.repetition) != key or run.status != row.get("status")):
                    raise ValueError(f"quality source identity/status drift: {key}")
                paths.append(path)
                manifests.append(run)
                expected.add(key)
    _exact_matrix(
        "quality", manifests, expected,
        key=lambda run: (run.dataset, run.circuit, run.method,
                         run.seed, run.repetition))
    return paths


def _legacy_seed0_fallback_exception(
        quality_payload: Mapping[str, Any], freeze: Mapping[str, Any],
        ) -> tuple[set[tuple[str, str, str, int, int]], Mapping[str, Any]]:
    """Authorize the one historical missing-field exception after parity only."""
    if quality_payload.get("accepted_old_seed0") is not True:
        return set(), {
            "enabled": False, "reason": "paper main reran seed0",
            "scope": "none",
        }
    parity_path = Path(str(quality_payload.get("parity_report", ""))).resolve()
    if not parity_path.is_file():
        raise FileNotFoundError(
            "accepted old seed0 requires the retained parity report")
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    frozen_source_path = Path(
        str(freeze["seed0_source_manifest"]["path"])).resolve()
    frozen_source_sha = freeze["seed0_source_manifest"].get("sha256")
    if (not frozen_source_path.is_file() or
            (_is_sha256(frozen_source_sha) and
             sha256_file(frozen_source_path) != frozen_source_sha)):
        raise ValueError("frozen seed0 source manifest hash drift")
    frozen_source = json.loads(frozen_source_path.read_text(encoding="utf-8"))
    expected_compared = len(freeze["parity_timing_cohort"]["identities"]) * 2
    comparisons_payload = parity.get("comparisons", ())
    comparisons = (comparisons_payload
                   if isinstance(comparisons_payload, list) else [])
    expected_parity = {
        (str(dataset), str(circuit), method)
        for dataset, circuit in freeze["parity_timing_cohort"]["identities"]
        for method in ("M3", "M4")
    }
    observed_parity = [
        (str(row.get("dataset")), str(row.get("circuit")),
         str(row.get("method")))
        for row in comparisons if isinstance(row, Mapping)
    ]
    parity_counts = Counter(observed_parity)
    if (parity.get("freeze_id") != freeze.get("freeze_id") or
            parity.get("passed") is not True or parity.get("status") != "passed" or
            parity.get("compared") != expected_compared or
            len(comparisons) != expected_compared or
            set(observed_parity) != expected_parity or
            any(count != 1 for count in parity_counts.values()) or
            any(not isinstance(row, Mapping) or row.get("passed") is not True
                for row in comparisons)):
        raise ValueError("old seed0 fallback exception lacks a passed parity gate")
    for row in comparisons:
        identity = f"{row['dataset']}/{row['circuit']}"
        expected_old = Path(
            str(frozen_source[row["method"]][identity]["path"])).resolve()
        if Path(str(row.get("old_manifest", ""))).resolve() != expected_old:
            raise ValueError(
                f"parity old-manifest identity drift: {row['method']}/{identity}")
    allowed: set[tuple[str, str, str, int, int]] = set()
    for method in ("M3", "M4"):
        for identity, seeds in quality_payload["ours"][method].items():
            if seeds["0"] != frozen_source[method][identity]:
                raise ValueError(
                    f"accepted old seed0 is not the frozen source: {method}/{identity}")
            dataset, circuit = identity.split("/", 1)
            allowed.add((dataset, circuit, method, 0, 0))
    return allowed, {
        "enabled": True,
        "scope": "only frozen parity-passed M3/M4 seed0 manifests",
        "reason": (
            "historical ABI8 manifests predate the python_fallback field; "
            "native ABI8, wheel, verifier, ledger and ghost evidence remain required"),
        "parity_report": {"path": str(parity_path),
                          "sha256": sha256_file(parity_path),
                          "compared": expected_compared},
        "allowed_identity_N": len(allowed),
    }


def _track_manifests(root: Path, track: str) -> list[Path]:
    track_root = root / "runs" / track
    if not track_root.exists():
        return []
    result = []
    for path in track_root.rglob("manifest.json"):
        relative = path.relative_to(track_root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        result.append(path)
    return sorted(result, key=str)


def _require_exact_manifest_count(
        label: str, paths: Sequence[Path], expected: int) -> None:
    if len(paths) != expected:
        raise ValueError(
            f"paper {label} manifest count is incomplete or duplicated: "
            f"expected={expected}, found={len(paths)}")


def _validate_timing_warmup_matrix(
        manifests: Sequence[RunManifest], *,
        allowed_identities: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Verify the eight non-statistical timing warmups exactly once."""
    terminal_statuses = {item.value for item in RunStatus}
    invalid_statuses = sorted({run.status for run in manifests
                               if run.status not in terminal_statuses})
    if invalid_statuses:
        raise ValueError(
            f"paper timing warmup has non-terminal statuses: {invalid_statuses}")
    status_counts = dict(sorted(Counter(
        run.status for run in manifests).items()))
    if status_counts != {RunStatus.SUCCESS.value: len(manifests)}:
        raise ValueError(
            "paper timing warmup requires eight successful compilations: "
            f"status_counts={status_counts}")
    allowed = set(allowed_identities)
    warmup_circuits: dict[str, str] = {}
    for dataset in DATASETS:
        circuits = {run.circuit for run in manifests
                    if run.dataset == dataset}
        if len(circuits) != 1:
            raise ValueError(
                "paper timing warmup must use one circuit per dataset: "
                f"{dataset}={sorted(circuits)}")
        circuit = next(iter(circuits))
        if (dataset, circuit) not in allowed:
            raise ValueError(
                "paper timing warmup circuit is outside the frozen cohort: "
                f"{dataset}/{circuit}")
        warmup_circuits[dataset] = circuit
    expected = {
        (dataset, circuit, method, 0, 0, "timing", "")
        for dataset, circuit in warmup_circuits.items()
        for method in METHODS
    }
    _exact_matrix(
        "timing warmup", manifests, expected,
        key=lambda run: (run.dataset, run.circuit, run.method, run.seed,
                         run.repetition, run.run_kind,
                         run.ablation_variant))
    return {
        "manifest_count": len(manifests),
        "status_counts": status_counts,
        "circuits": warmup_circuits,
        "excluded_from_runtime_statistics": True,
    }


def _publish_paper_fig6_data(delivery: Path, paper: Path) -> dict[str, Any]:
    """Generate, validate, atomically publish, and hash Fig. 6 inputs.

    The manuscript owns the renderer-specific converter while this repository
    owns its aggregate CSV inputs.  Running the checked-in converter here keeps
    both sides synchronized and makes ``aggregate-paper`` fail closed instead
    of silently leaving stale plot data in the manuscript tree.
    """
    converter = paper / "figures" / "prepare_experimental_summary.py"
    if not converter.is_file():
        raise FileNotFoundError(
            f"paper Fig. 6 converter is missing: {converter}")
    raw_inputs = {name: delivery / name for name in FIG6_RAW_EXPORTS}
    missing_raw = sorted(name for name, path in raw_inputs.items()
                         if not path.is_file() or path.stat().st_size == 0)
    if missing_raw:
        raise FileNotFoundError(
            f"paper Fig. 6 raw aggregate inputs are missing/empty: {missing_raw}")

    figures_root = paper / "figures"
    figures_root.mkdir(parents=True, exist_ok=True)
    published_root = figures_root / "data"
    published_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix=".fig6-derived-", dir=figures_root) as temporary:
        staging = Path(temporary)
        command = [
            sys.executable, str(converter),
            "--input-root", str(delivery),
            "--output-root", str(staging),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=60,
                check=False)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "paper Fig. 6 converter exceeded the 60-second limit") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-4000:]
            raise RuntimeError(
                f"paper Fig. 6 converter failed with exit code "
                f"{completed.returncode}: {detail}")

        observed = {path.name for path in staging.iterdir() if path.is_file()}
        expected = set(FIG6_DERIVED_OUTPUTS)
        if observed != expected:
            raise ValueError(
                "paper Fig. 6 derived output set drift: "
                f"missing={sorted(expected - observed)}, "
                f"extra={sorted(observed - expected)}")
        empty = sorted(name for name in expected
                       if (staging / name).stat().st_size == 0)
        if empty:
            raise ValueError(
                f"paper Fig. 6 derived outputs are empty: {empty}")
        for name in FIG6_DERIVED_OUTPUTS:
            (staging / name).replace(published_root / name)

    return {
        "protocol": "paper-fig6-derived-v2",
        "converter": {
            "path": str(converter.resolve()),
            "sha256": sha256_file(converter),
        },
        "raw_inputs": {
            name: {"path": str(path.resolve()),
                   "sha256": sha256_file(path),
                   "bytes": path.stat().st_size}
            for name, path in sorted(raw_inputs.items())
        },
        "output_root": str(published_root.resolve()),
        "files": {
            name: {"path": str((published_root / name).resolve()),
                   "sha256": sha256_file(published_root / name),
                   "bytes": (published_root / name).stat().st_size}
            for name in FIG6_DERIVED_OUTPUTS
        },
    }


def command_aggregate_paper(*, plan: Any, freeze_path: str | Path,
                            artifact_root: str | Path,
                            output_root: str | Path,
                            paper_directory: str | Path | None = None,
                            publish_to_paper: bool = True) -> Mapping[str, Any]:
    """Integration entry used by ``paper_cli aggregate-paper``.

    The function verifies the frozen quality-source hashes, enumerates only the
    five registered paper run roots (never QASMBench/Large).  Manuscript
    publication is an explicit final step and can be disabled for a read-only
    re-aggregation of frozen results.
    """
    from .paper_protocol import (PAPER_ABLATION_VARIANTS,
                                 SENSITIVITY_PROFILE_IDS,
                                 load_paper_freeze)

    freeze_file = Path(freeze_path).resolve()
    freeze = load_paper_freeze(freeze_file)
    artifacts = Path(artifact_root).resolve()
    destination = Path(output_root).resolve()
    paper = (Path(paper_directory).resolve()
             if paper_directory is not None else None)
    if publish_to_paper and paper is None:
        raise ValueError("paper_directory is required when publishing")
    quality_source = artifacts / "quality_source_manifest.json"
    if not quality_source.is_file():
        raise FileNotFoundError(
            "paper main experiment has not produced quality_source_manifest.json")
    quality_payload = json.loads(quality_source.read_text(encoding="utf-8"))
    if (quality_payload.get("freeze_id") != freeze.get("freeze_id") or
            quality_payload.get("protocol_id") != freeze.get("protocol_id")):
        raise ValueError(
            "paper quality_source_manifest does not belong to the loaded freeze")
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
    timing_warmup_paths = _track_manifests(artifacts, "timing-warmup")
    sensitivity_variants = [f"paper_sensitivity_{profile}"
                            for profile in SENSITIVITY_PROFILE_IDS]
    circuit_count = sum(len(rows) for rows in frozen_suites.values())
    _require_exact_manifest_count("quality", quality_paths, circuit_count * 8)
    _require_exact_manifest_count(
        "ablation", ablation_paths, len(ablation_identities) * 7)
    _require_exact_manifest_count(
        "sensitivity", sensitivity_paths,
        len(twelve_identities) * len(sensitivity_variants))
    _require_exact_manifest_count(
        "timing", timing_paths, len(twelve_identities) * 4 * 3)
    _require_exact_manifest_count(
        "timing warmup", timing_warmup_paths, len(DATASETS) * len(METHODS))
    quality_runs = _load_manifests(quality_paths)
    ablation_runs = _load_manifests(ablation_paths)
    sensitivity_runs = _load_manifests(sensitivity_paths)
    timing_runs = _load_manifests(timing_paths)
    timing_warmup_runs = _load_manifests(timing_warmup_paths)
    quality_expected = {
        (dataset, circuit, method, seed, 0)
        for dataset, circuits in frozen_suites.items() for circuit in circuits
        for method in METHODS for seed in EXPECTED_SEEDS[method]
    }
    _exact_matrix(
        "quality", quality_runs, quality_expected,
        key=lambda run: (run.dataset, run.circuit, run.method,
                         run.seed, run.repetition))
    ablation_expected = {
        (dataset, circuit, variant, method, seed, 0, "ablation")
        for dataset, circuit in ablation_identities
        for variant, method, seeds in (
            (PAPER_ABLATION_VARIANTS["h0"], "M4", (0, 1, 2)),
            (PAPER_ABLATION_VARIANTS["h8"], "M4", (0, 1, 2)),
            (PAPER_ABLATION_VARIANTS["greedy"], "M4", (0,)))
        for seed in seeds
    }
    _exact_matrix(
        "ablation", ablation_runs, ablation_expected,
        key=lambda run: (run.dataset, run.circuit, run.ablation_variant,
                         run.method, run.seed, run.repetition, run.run_kind))
    sensitivity_expected = {
        (dataset, circuit, variant, "M4", 0, 0, "ablation")
        for dataset, circuit in twelve_identities
        for variant in sensitivity_variants
    }
    _exact_matrix(
        "sensitivity", sensitivity_runs, sensitivity_expected,
        key=lambda run: (run.dataset, run.circuit, run.ablation_variant,
                         run.method, run.seed, run.repetition, run.run_kind))
    timing_expected = {
        (dataset, circuit, method, 0, repetition, "timing")
        for dataset, circuit in twelve_identities for method in METHODS
        for repetition in range(3)
    }
    _exact_matrix(
        "timing", timing_runs, timing_expected,
        key=lambda run: (run.dataset, run.circuit, run.method, run.seed,
                         run.repetition, run.run_kind))
    timing_warmup = _validate_timing_warmup_matrix(
        timing_warmup_runs, allowed_identities=twelve_identities)

    frozen_inputs = {
        dataset: {str(circuit): str(digest) for circuit, digest in
                  freeze["canonical_suites"][dataset]["canonical_inputs"].items()}
        for dataset in DATASETS
    }
    legacy_allowed, legacy_policy = _legacy_seed0_fallback_exception(
        quality_payload, freeze)
    abi8_wheel_sha = str(freeze["abi8_wheel"]["sha256"])
    evidence = {
        "quality": _validate_frozen_evidence(
            "quality", quality_runs, frozen_inputs=frozen_inputs,
            expected_native_abi=8,
            expected_native_wheel_sha256=abi8_wheel_sha,
            legacy_unspecified_fallback=legacy_allowed),
        "ablation": _validate_frozen_evidence(
            "ablation", ablation_runs, frozen_inputs=frozen_inputs,
            expected_native_abi=9),
        "sensitivity": _validate_frozen_evidence(
            "sensitivity", sensitivity_runs, frozen_inputs=frozen_inputs,
            expected_native_abi=9),
        "timing": _validate_frozen_evidence(
            "timing", timing_runs, frozen_inputs=frozen_inputs,
            expected_native_abi=8,
            expected_native_wheel_sha256=abi8_wheel_sha),
        "timing_warmup": _validate_frozen_evidence(
            "timing warmup", timing_warmup_runs,
            frozen_inputs=frozen_inputs, expected_native_abi=8,
            expected_native_wheel_sha256=abi8_wheel_sha),
    }
    abi9_wheels = (set(evidence["ablation"]["native_wheel_sha256"]) |
                   set(evidence["sensitivity"]["native_wheel_sha256"]))
    if len(abi9_wheels) != 1:
        raise ValueError(
            f"paper ABI9 tracks do not share one frozen wheel: {sorted(abi9_wheels)}")
    report = aggregate_paper(
        quality_runs,
        frozen_suites=frozen_suites,
        output_dir=destination,
        ablation_manifest_inputs=ablation_runs,
        sensitivity_manifest_inputs=sensitivity_runs,
        timing_manifest_inputs=timing_runs,
        ablation_identities=ablation_identities,
        sensitivity_identities=twelve_identities,
        sensitivity_settings=sensitivity_variants,
        timing_identities=twelve_identities,
        h0_variant=PAPER_ABLATION_VARIANTS["h0"],
        h8_variant=PAPER_ABLATION_VARIANTS["h8"],
        greedy_variant=PAPER_ABLATION_VARIANTS["greedy"],
    )
    macro_source = destination / "results_values_zh.tex"
    macro_target: Path | None = None
    if publish_to_paper:
        assert paper is not None
        paper.mkdir(parents=True, exist_ok=True)
        macro_target = paper / "results_values_zh.tex"
        temporary = macro_target.with_name(f".{macro_target.name}.tmp")
        temporary.write_bytes(macro_source.read_bytes())
        temporary.replace(macro_target)

    from .paper_workbook import export_paper_workbook
    workbook_path = destination / "four_methods_results.xlsx"
    workbook_result = export_paper_workbook(
        destination, workbook_path,
        qa_directory=destination / "paper_workbook_qa")
    fig6_data = (_publish_paper_fig6_data(destination, paper)
                 if publish_to_paper and paper is not None else None)

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
            "timing_warmup": len(timing_warmup_paths),
        },
        "git_commit_sets": {
            "main": sorted({str(run.git_commit) for run in quality_runs}),
            "ablation": sorted({str(run.git_commit)
                                for run in ablation_runs}),
            "sensitivity": sorted({str(run.git_commit)
                                   for run in sensitivity_runs}),
            "timing": sorted({str(run.git_commit) for run in timing_runs}),
            "timing_warmup": sorted({str(run.git_commit)
                                     for run in timing_warmup_runs}),
        },
        "timing_warmup": timing_warmup,
        "paper_publication": {
            "performed": publish_to_paper,
            "paper_directory": str(paper) if paper is not None else None,
        },
        "paper_run_reports": reports,
        "evidence_validation": evidence,
        "legacy_python_fallback_policy": legacy_policy,
        "abi9_native_wheel_sha256": next(iter(abi9_wheels)),
        "xlsx_generated_here": True,
        "paper_workbook": {
            "path": str(workbook_path.resolve()),
            "sha256": sha256_file(workbook_path),
            "bytes": workbook_path.stat().st_size,
            "sheet_names": workbook_result["sheet_names"],
            "row_counts": workbook_result["row_counts"],
            "column_count": workbook_result["column_count"],
            "freeze_panes": workbook_result["freeze_panes"],
            "qa_path": workbook_result["qa_path"],
            "qa_sha256": sha256_file(workbook_result["qa_path"]),
            "qa_artifact_sha256": {
                Path(path).name: sha256_file(path)
                for path in workbook_result.get(
                    "qa_artifact_paths", [workbook_result["qa_path"]])
            },
            "preview_sha256": {
                Path(path).name: sha256_file(path)
                for path in workbook_result["preview_paths"]
            },
        },
        "plan_path": str(getattr(plan, "path", "")),
    })
    if macro_target is not None:
        final_payload["paper_macro_target"] = {
            "path": str(macro_target.resolve()),
            "sha256": sha256_file(macro_target),
        }
    if fig6_data is not None:
        final_payload["paper_fig6_data"] = fig6_data
    final_payload["files"][workbook_path.name] = {
        "sha256": sha256_file(workbook_path),
        "bytes": workbook_path.stat().st_size,
    }
    _json_dump(final_path, final_payload)
    return {
        "protocol": report["protocol"],
        "output_root": str(destination),
        "paper_values": str((destination / "paper_values.json").resolve()),
        "results_values_zh_tex": str(macro_source.resolve()),
        "paper_results_values_zh_tex": (
            str(macro_target.resolve()) if macro_target is not None else None),
        "final_manifest": str(final_path.resolve()),
        "four_methods_results_xlsx": str(workbook_path.resolve()),
        "paper_fig6_data": fig6_data,
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
    "DATASETS", "EXPECTED_SEEDS", "INTERNAL_CONFIGURATION_METHODS", "METHODS",
    "NESTED_TIME_FIELDS", "PRIMARY_METHODS",
    "aggregate_paper", "command_aggregate_paper", "build_main_rows", "build_paper_values",
    "render_results_values_tex", "summarize_ablation", "summarize_main",
    "summarize_runtime", "summarize_sensitivity",
]
