"""Fail-closed tables for the final two-sheet results workbook.

This module deliberately stops at a renderer-independent workbook contract.
It never creates an XLSX file: the final workbook is authored and visually
checked by the artifact-tool workflow after these rows have been frozen.

The accepted cohorts are intentionally narrow:

* quality: ``run_kind=main``; one M1/M2 attempt and three paired M3/M4 seeds;
* timing: ``run_kind=timing``; three repetitions for every method;
* datasets: exactly the frozen ``zac18`` and ``qmap154`` suites.

Every expected attempt must have a terminal manifest, including failures.  A
missing manifest is not silently converted into a failed run, while a failed
manifest remains visible through ``valid/N`` and blank metric cells.
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import RunManifest, RunStatus, SCHEMA_VERSION, load_run_manifest
from .protocol import FORMAL_QUALITY_SEEDS, FORMAL_TIMING_REPETITIONS
from zzx.algorithm_v2 import (
    FORMAL_NATIVE_ABI_VERSION,
    FORMAL_NATIVE_ALGORITHM_REVISION,
    FORMAL_NATIVE_RNG_VERSION,
    FORMAL_NATIVE_TUNING_PROTOCOL_ID,
)


FINAL_RESULTS_CONTRACT_ID = "native-ga-v1-two-sheet-results-v1"
METHODS = ("M1", "M2", "M3", "M4")
DATASET_SHEETS = {"zac18": "ZAC18", "qmap154": "QMAP154"}
SHEET_NAMES = ("ZAC18", "QMAP154")
QUALITY_NUMERATOR = {
    "M1": 1, "M2": 1,
    "M3": len(FORMAL_QUALITY_SEEDS), "M4": len(FORMAL_QUALITY_SEEDS),
}
TIMING_REPETITIONS = FORMAL_TIMING_REPETITIONS
OVERALL_LABEL = "整体汇总"

# M3/M4 expose both mutually exclusive wall-clock stages and nested kernel
# diagnostics.  Keeping the mapping in one place makes the CSV/XLSX contract
# explicit and prevents a new native counter from being silently dropped.
# ``problem_preparation/search_kernel/result_commit/transition_residual`` are
# the additive transition-decision decomposition.  The remaining counters are
# nested inside those wall-clock stages and therefore must not be summed with
# them.
OURS_TIMING_METRICS = (
    ("initial_placement_s", "initial_placement_ns", "初始布局 (s)"),
    ("problem_preparation_s", "problem_preparation_ns", "问题构造 (s)"),
    ("search_kernel_s", "search_kernel_ns", "搜索阶段墙钟 (s)"),
    ("result_commit_s", "result_commit_ns", "结果提交 (s)"),
    ("transition_residual_s", None, "逐层决策其余开销 (s)"),
    ("python_marshal_s", "python_marshal_ns", "Python/C++转换 (s)"),
    ("native_call_wall_s", "native_call_wall_ns", "C++调用墙钟 (s)"),
    ("native_parse_s", "native_parse_ns", "C++解析 (s)"),
    ("native_search_wall_s", "native_search_wall_ns", "C++搜索墙钟 (s)"),
    ("native_serialize_s", "native_serialize_ns", "C++序列化 (s)"),
    ("fitness_s", "fitness_ns", "Fitness累计 (s)"),
    ("normalize_s", "normalize_ns", "染色体规范化 (s)"),
    ("decode_s", "decode_ns", "染色体解码 (s)"),
    ("return_match_s", "return_match_ns", "RETURN匹配 (s)"),
    ("forecast_s", "forecast_ns", "前瞻计算 (s)"),
    ("selection_s", "selection_ns", "选择排序 (s)"),
    ("horizon_selection_s", "horizon_selection_ns", "前瞻深度选择 (s)"),
    ("routing_s", "routing_ns", "最终路由 (s)"),
    ("full_compile_s", "full_compile_ns", "完整编译 (s)"),
)


ManifestInput = RunManifest | str | os.PathLike[str]


def _is_sha256(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _load_explicit(inputs: Iterable[ManifestInput], *, label: str) -> list[RunManifest]:
    manifests: list[RunManifest] = []
    for item in inputs:
        if isinstance(item, RunManifest):
            manifest = item
            if manifest.experiment_schema != SCHEMA_VERSION:
                raise ValueError(f"refusing non-Schema-2 {label} manifest")
            manifest.validate(require_success_metrics=True)
        else:
            path = Path(item)
            if path.is_dir():
                raise ValueError(
                    f"{label} inputs must be explicit manifest files, not a directory: {path}")
            manifest = load_run_manifest(path, require_success_metrics=True)
        manifests.append(manifest)
    if not manifests:
        raise ValueError(f"{label} manifest cohort is empty")
    return manifests


def _validate_frozen_inputs(
        expected_experiment_ids: Mapping[str, str],
        frozen_suites: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    expected = set(DATASET_SHEETS)
    if set(expected_experiment_ids) != expected:
        raise ValueError(
            "expected_experiment_ids must contain exactly zac18 and qmap154")
    if set(frozen_suites) != expected:
        raise ValueError("frozen_suites must contain exactly zac18 and qmap154")
    suites: dict[str, tuple[str, ...]] = {}
    for dataset in DATASET_SHEETS:
        experiment_id = expected_experiment_ids[dataset]
        if not _is_sha256(experiment_id):
            raise ValueError(f"{dataset} experiment_id is not a SHA256")
        circuits = tuple(str(value) for value in frozen_suites[dataset])
        if not circuits or any(not value for value in circuits):
            raise ValueError(f"{dataset} frozen suite is empty or contains a blank circuit")
        if len(set(circuits)) != len(circuits):
            raise ValueError(f"{dataset} frozen suite contains duplicate circuits")
        suites[dataset] = circuits
    return suites


def _quality_identity(run: RunManifest) -> tuple[str, str, str, int, int]:
    return run.dataset, run.circuit, run.method, run.seed, run.repetition


def _timing_identity(run: RunManifest) -> tuple[str, str, str, int, int]:
    return run.dataset, run.circuit, run.method, run.seed, run.repetition


def _expected_quality_identities(
        suites: Mapping[str, Sequence[str]]) -> set[tuple[str, str, str, int, int]]:
    identities: set[tuple[str, str, str, int, int]] = set()
    for dataset, circuits in suites.items():
        for circuit in circuits:
            for method in ("M1", "M2"):
                identities.add((dataset, circuit, method, 0, 0))
            for method in ("M3", "M4"):
                for seed in FORMAL_QUALITY_SEEDS:
                    identities.add((dataset, circuit, method, seed, 0))
    return identities


def _expected_timing_identities(
        suites: Mapping[str, Sequence[str]]) -> set[tuple[str, str, str, int, int]]:
    return {
        (dataset, circuit, method, 0, repetition)
        for dataset, circuits in suites.items()
        for circuit in circuits
        for method in METHODS
        for repetition in range(TIMING_REPETITIONS)
    }


def _format_identity(identity: tuple[str, str, str, int, int]) -> str:
    dataset, circuit, method, seed, repetition = identity
    return f"{dataset}/{circuit}/{method}/seed{seed}/rep{repetition}"


def _validate_cohort(
        manifests: Sequence[RunManifest], *, run_kind: str,
        suites: Mapping[str, Sequence[str]],
        expected_experiment_ids: Mapping[str, str]) -> None:
    if run_kind not in {"main", "timing"}:
        raise ValueError(f"unsupported final-results run kind: {run_kind}")
    seen_run_ids: set[str] = set()
    seen_identities: set[tuple[str, str, str, int, int]] = set()
    allowed_circuits = {
        dataset: set(circuits) for dataset, circuits in suites.items()
    }
    identity_function = (
        _quality_identity if run_kind == "main" else _timing_identity)
    for run in manifests:
        if run.run_kind != run_kind:
            raise ValueError(
                f"{run_kind} cohort contains run_kind={run.run_kind}: {run.run_id}")
        if run.dataset not in DATASET_SHEETS:
            raise ValueError(f"unknown or mixed dataset in final results: {run.dataset}")
        if run.circuit not in allowed_circuits[run.dataset]:
            raise ValueError(
                f"{run.dataset} cohort contains a non-frozen circuit: {run.circuit}")
        if run.method not in METHODS:
            raise ValueError(f"unknown method in final results: {run.method}")
        if run.experiment_id != expected_experiment_ids[run.dataset]:
            raise ValueError(
                f"experiment_id mismatch for {run.dataset}/{run.run_id}: "
                f"{run.experiment_id!r}")
        if run.run_id in seen_run_ids:
            raise ValueError(f"duplicate run_id in {run_kind} cohort: {run.run_id}")
        seen_run_ids.add(run.run_id)
        identity = identity_function(run)
        if identity in seen_identities:
            raise ValueError(
                f"duplicate {run_kind} attempt: {_format_identity(identity)}")
        seen_identities.add(identity)

    expected = (_expected_quality_identities(suites) if run_kind == "main"
                else _expected_timing_identities(suites))
    missing, extra = expected - seen_identities, seen_identities - expected
    if missing or extra:
        details = []
        if missing:
            preview = ", ".join(_format_identity(value)
                                for value in sorted(missing)[:5])
            details.append(f"missing={len(missing)} [{preview}]")
        if extra:
            preview = ", ".join(_format_identity(value)
                                for value in sorted(extra)[:5])
            details.append(f"extra={len(extra)} [{preview}]")
        raise ValueError(f"incomplete or mixed {run_kind} cohort: {'; '.join(details)}")


def _validate_cross_cohort_provenance(
        manifests: Sequence[RunManifest],
        suites: Mapping[str, Sequence[str]]) -> None:
    run_ids: set[str] = set()
    for run in manifests:
        if run.run_id in run_ids:
            raise ValueError(f"duplicate run_id across quality/timing cohorts: {run.run_id}")
        run_ids.add(run.run_id)
        for field in ("input_sha256", "config_sha256", "architecture_sha256",
                      "model_sha256"):
            if not _is_sha256(getattr(run, field)):
                raise ValueError(f"{run.run_id} has invalid {field}")

    dirty = [run.run_id for run in manifests if run.git_dirty]
    commits = {run.git_commit for run in manifests}
    if dirty:
        raise ValueError(f"final cohorts contain dirty runs: {dirty[:5]}")
    if len(commits) != 1 or "unknown" in commits:
        raise ValueError(
            f"quality/timing cohorts mix Git commits: {sorted(commits)}")

    architecture = {run.architecture_sha256 for run in manifests}
    model = {run.model_sha256 for run in manifests}
    if len(architecture) != 1 or len(model) != 1:
        raise ValueError("quality/timing cohorts mix architecture or fidelity model hashes")

    for dataset, circuits in suites.items():
        for circuit in circuits:
            values = {
                run.input_sha256 for run in manifests
                if run.dataset == dataset and run.circuit == circuit
            }
            if len(values) != 1:
                raise ValueError(
                    f"quality/timing cohorts mix canonical inputs for {dataset}/{circuit}")

    # Repetitions of one frozen method/seed must not silently mix settings.
    # A method/seed is one frozen algorithm configuration across both datasets
    # and across its quality/timing executions.  Grouping by dataset or run kind
    # would let the implementation change between the two headline tables or
    # let a separately tuned timing-only method enter the speed comparison.
    grouped: dict[tuple[str, int], set[str]] = defaultdict(set)
    for run in manifests:
        grouped[(run.method, run.seed)].add(
            run.config_sha256)
    drift = {key: values for key, values in grouped.items() if len(values) != 1}
    if drift:
        first_key = sorted(drift)[0]
        raise ValueError(f"mixed config hashes for {first_key}: {sorted(drift[first_key])}")

    successful_ours = [
        run for run in manifests
        if run.method in {"M3", "M4"} and
        run.status == RunStatus.SUCCESS.value
    ]
    if not successful_ours:
        raise ValueError("final cohorts contain no successful native M3/M4 run")
    expected_native = {
        "algorithm_revision": FORMAL_NATIVE_ALGORITHM_REVISION,
        "backend": "native",
        "native_abi_version": FORMAL_NATIVE_ABI_VERSION,
        "tuning_protocol_id": FORMAL_NATIVE_TUNING_PROTOCOL_ID,
        "rng_version": FORMAL_NATIVE_RNG_VERSION,
    }
    for run in successful_ours:
        provenance_drift = {
            field: (getattr(run, field), expected)
            for field, expected in expected_native.items()
            if getattr(run, field) != expected
        }
        if provenance_drift:
            raise ValueError(
                f"native provenance mismatch for {run.run_id}: "
                f"{provenance_drift}")
    wheel_hashes = {run.native_wheel_sha256 for run in successful_ours}
    if (len(wheel_hashes) != 1 or not _is_sha256(next(iter(wheel_hashes), ""))):
        raise ValueError(
            f"quality/timing cohorts mix native wheels: {sorted(wheel_hashes)}")


def _successes(runs: Sequence[RunManifest]) -> list[RunManifest]:
    return [run for run in runs if run.status == RunStatus.SUCCESS.value]


def _median(values: Iterable[float | int | None]) -> float | int | None:
    materialized = [value for value in values if value is not None]
    if not materialized:
        return None
    return statistics.median(materialized)


def _complete_geometric_mean(
        values: Iterable[float | int | None]) -> float | None:
    """Return a geometric mean only for a complete non-negative cohort.

    The overall row must never make a failed circuit disappear.  A missing
    circuit-level value therefore makes the aggregate blank; ``valid/N`` next
    to it still exposes the exact coverage.  Move metrics may legitimately be
    zero, in which case their geometric mean is exactly zero.
    """
    materialized = list(values)
    if not materialized or any(value is None for value in materialized):
        return None
    numeric = [float(value) for value in materialized]
    if any(not math.isfinite(value) or value < 0.0 for value in numeric):
        raise ValueError("overall geometric mean requires finite non-negative values")
    if any(value == 0.0 for value in numeric):
        return 0.0
    return math.exp(math.fsum(math.log(value) for value in numeric) /
                    len(numeric))


def _complete_median(
        values: Iterable[float | int | None]) -> float | None:
    materialized = list(values)
    if not materialized or any(value is None for value in materialized):
        return None
    numeric = [float(value) for value in materialized]
    if any(not math.isfinite(value) or value < 0.0 for value in numeric):
        raise ValueError("overall median requires finite non-negative values")
    return float(statistics.median(numeric))


def _overall_quality_row(
        circuit_rows: Sequence[Mapping[str, Any]], *,
        quality_groups: Mapping[tuple[str, str, str], Sequence[RunManifest]],
        dataset: str, circuits: Sequence[str]) -> dict[str, Any]:
    row: dict[str, Any] = {"circuit": OVERALL_LABEL}
    for method in METHODS:
        geometric_suffixes = ["fidelity", "transition_decision_s"]
        if method in {"M3", "M4"}:
            geometric_suffixes.extend(
                suffix for suffix, _field, _label in OURS_TIMING_METRICS)
        for suffix in geometric_suffixes:
            row[f"{method}__{suffix}"] = _complete_geometric_mean(
                item.get(f"{method}__{suffix}") for item in circuit_rows)
        for suffix in ("move_batches", "move_time_us"):
            row[f"{method}__{suffix}"] = _complete_median(
                item.get(f"{method}__{suffix}") for item in circuit_rows)
        valid = sum(
            len(_successes(quality_groups[(dataset, circuit, method)]))
            for circuit in circuits)
        required = len(circuits) * QUALITY_NUMERATOR[method]
        row[f"{method}__valid_over_N"] = f"{valid}/{required}"
    return row


def _overall_runtime_row(
        circuit_rows: Sequence[Mapping[str, Any]], *, dataset: str,
        circuit_count: int) -> dict[str, Any]:
    comparable = bool(circuit_rows) and all(
        bool(item["layer_ledger_comparable"]) for item in circuit_rows)
    row: dict[str, Any] = {
        "dataset": dataset,
        "circuit": OVERALL_LABEL,
        "layer_ledger_comparable": comparable,
        "layer_ledger_reason": (
            "all_circuits_comparable" if comparable else
            "one_or_more_circuits_not_comparable"),
    }
    for method in METHODS:
        method_complete = all(
            item[f"{method}__valid_over_N"] ==
            f"{TIMING_REPETITIONS}/{TIMING_REPETITIONS}"
            for item in circuit_rows)
        for suffix in (
                "transition_decision_s_median", "full_compile_s_median"):
            row[f"{method}__{suffix}"] = (
                _complete_geometric_mean(
                    item.get(f"{method}__{suffix}") for item in circuit_rows)
                if method_complete else None)
        # Per-circuit Q1/Q3/IQR describe the formal repetitions of one circuit.  A
        # geometric mean of those endpoints is not a dataset-level IQR, so the
        # overall row deliberately leaves them blank.
        for suffix in ("transition_decision_s_q1",
                       "transition_decision_s_q3",
                       "transition_decision_s_iqr"):
            row[f"{method}__{suffix}"] = None
        valid = sum(
            int(str(item[f"{method}__valid_over_N"]).split("/", 1)[0])
            for item in circuit_rows)
        row[f"{method}__valid_over_N"] = (
            f"{valid}/{circuit_count * TIMING_REPETITIONS}")
    for method in ("M3", "M4"):
        row[f"{method}__speedup_vs_M2"] = (
            _complete_geometric_mean(
                item.get(f"{method}__speedup_vs_M2") for item in circuit_rows)
            if comparable else None)
    return row


def _fidelity_median(runs: Sequence[RunManifest]) -> float | None:
    # A formal three-seed quality cohort is one indivisible physical-model
    # result.  Silently dropping an OOD seed would make an incomplete cohort
    # look complete.  Preserve compile coverage
    # but leave linear-model fidelity blank whenever any required seed is OOD.
    if (not runs or any(run.fidelity_ood or run.log_fidelity is None
                        for run in runs)):
        return None
    logs = [float(run.log_fidelity) for run in runs]
    return math.exp(float(statistics.median(logs)))


def _seconds(value: float | int | None) -> float | None:
    return None if value is None else float(value) / 1_000_000_000.0


def _quartiles(values: Sequence[int]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    if len(values) == 1:
        value = float(values[0])
        return value, value
    cuts = statistics.quantiles(values, n=4, method="inclusive")
    return float(cuts[0]), float(cuts[2])


def _timing_summary(runs: Sequence[RunManifest]) -> dict[str, Any]:
    successful = _successes(runs)
    for run in successful:
        required = {
            "transition_decision_ns": run.transition_decision_ns,
            "full_compile_ns": run.full_compile_ns,
            "initial_placement_ns": run.initial_placement_ns,
            "routing_ns": run.routing_ns,
        }
        missing = [name for name, value in required.items()
                   if value is None or value < 0]
        if missing or not run.canonical_input_layer_ledger_sha256:
            raise ValueError(
                f"successful timing run {run.run_id} lacks stage evidence: "
                f"{missing or ['canonical_input_layer_ledger_sha256']}")
    stage = [int(run.transition_decision_ns) for run in successful
             if run.transition_decision_ns is not None]
    full = [int(run.full_compile_ns) for run in successful
            if run.full_compile_ns is not None]
    q1, q3 = _quartiles(stage)
    median_stage = _median(stage)
    result = {
        "valid": len(successful),
        "N": TIMING_REPETITIONS,
        "valid_over_N": f"{len(successful)}/{TIMING_REPETITIONS}",
        "transition_decision_s_median": _seconds(median_stage),
        "transition_decision_s_q1": _seconds(q1),
        "transition_decision_s_q3": _seconds(q3),
        "transition_decision_s_iqr": (
            _seconds(q3 - q1) if q1 is not None and q3 is not None else None),
        "full_compile_s_median": _seconds(_median(full)),
    }
    for suffix, manifest_field, _label in OURS_TIMING_METRICS:
        if manifest_field is None:
            values = []
            for run in successful:
                components = (
                    run.problem_preparation_ns,
                    run.search_kernel_ns,
                    run.result_commit_ns,
                )
                if run.transition_decision_ns is None or any(
                        value is None for value in components):
                    values.append(None)
                    continue
                residual = int(run.transition_decision_ns) - sum(
                    int(value) for value in components if value is not None)
                # A negative residual means one of the supposedly exclusive
                # stage counters overlaps another.  Do not conceal that timing
                # contract violation by clamping it to zero.
                values.append(residual if residual >= 0 else None)
        else:
            values = [getattr(run, manifest_field) for run in successful]
        result[f"{suffix}_median"] = (
            _seconds(_median(values))
            if len(values) == len(successful)
            and all(value is not None and int(value) >= 0 for value in values)
            else None
        )
    return result


def _ledger_comparability(
        by_method: Mapping[str, Sequence[RunManifest]]) -> tuple[bool, str]:
    successful = {method: _successes(by_method[method]) for method in METHODS}
    incomplete = [method for method in METHODS
                  if len(successful[method]) != TIMING_REPETITIONS]
    if incomplete:
        return False, "incomplete_success_repetitions:" + ",".join(incomplete)
    canonical_hashes = {
        run.canonical_input_layer_ledger_sha256
        for runs in successful.values() for run in runs
    }
    if "" in canonical_hashes:
        return False, "missing_canonical_input_layer_ledger_sha256"
    if len(canonical_hashes) != 1:
        return False, "canonical_input_layer_ledger_sha256_mismatch"
    canonical_counts = {
        run.canonical_input_transition_count
        for runs in successful.values() for run in runs
    }
    if None in canonical_counts:
        return False, "missing_canonical_input_transition_count"
    if len(canonical_counts) != 1:
        return False, "canonical_input_transition_count_mismatch"

    observed_hashes = {
        run.observed_transition_layer_ledger_sha256
        for runs in successful.values() for run in runs
    }
    if "" in observed_hashes:
        unproven = sorted(
            method for method in METHODS
            if any(not run.observed_transition_layer_ledger_sha256
                   for run in successful[method]))
        return False, "unproven_observed_transition_layer_ledger:" + ",".join(
            unproven)
    if len(observed_hashes) != 1:
        return False, "observed_transition_layer_ledger_sha256_mismatch"
    observed_counts = {
        run.observed_transition_count
        for runs in successful.values() for run in runs
    }
    if None in observed_counts:
        return False, "missing_observed_transition_count"
    if len(observed_counts) != 1:
        return False, "observed_transition_count_mismatch"
    missing_sources = sorted(
        method for method in METHODS
        if any(not run.observed_transition_layer_ledger_source
               for run in successful[method]))
    if missing_sources:
        return False, "missing_observed_transition_layer_ledger_source:" + ",".join(
            missing_sources)
    accepted_sources = {
        "M1": {"compiler.gate_scheduling"},
        "M2": {"normalized_qmap_placement_trace",
               "qmap.stats.two_qubit_gate_layers"},
        "M3": {"compiler.gate_scheduling"},
        "M4": {"compiler.gate_scheduling"},
    }
    source_drift = sorted(
        method for method in METHODS
        if any(run.observed_transition_layer_ledger_source
               not in accepted_sources[method]
               for run in successful[method]))
    if source_drift:
        return False, "untrusted_observed_transition_layer_ledger_source:" + ",".join(
            source_drift)
    return True, "comparable"


def _quality_columns() -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = [
        {"key": "circuit", "label": "电路", "group": None, "type": "text"}
    ]
    metric_specs = (
        ("fidelity", "Fidelity", "0.000000"),
        ("move_batches", "Move批次", "0.00"),
        ("move_time_us", "Move时间 (us)", "0.000"),
        ("transition_decision_s", "逐层放置时间 (s)", "0.000000"),
        ("valid_over_N", "valid/N", "text"),
    )
    for method in METHODS:
        for suffix, label, number_format in metric_specs:
            columns.append({
                "key": f"{method}__{suffix}", "label": label,
                "group": method,
                "type": "text" if suffix == "valid_over_N" else "number",
                "number_format": number_format,
            })
        if method in {"M3", "M4"}:
            for suffix, _manifest_field, label in OURS_TIMING_METRICS:
                columns.append({
                    "key": f"{method}__{suffix}",
                    "label": label,
                    "group": method,
                    "type": "number",
                    "number_format": "0.000000",
                })
    return columns


def _runtime_columns() -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = [
        {"key": "dataset", "label": "数据集", "group": None, "type": "text"},
        {"key": "circuit", "label": "电路", "group": None, "type": "text"},
    ]
    metric_specs = (
        ("transition_decision_s_median", "逐层放置时间中位数 (s)"),
        ("transition_decision_s_q1", "Q1 (s)"),
        ("transition_decision_s_q3", "Q3 (s)"),
        ("transition_decision_s_iqr", "IQR (s)"),
        ("full_compile_s_median", "完整编译时间中位数 (s)"),
        ("valid_over_N", "valid/N"),
    )
    for method in METHODS:
        for suffix, label in metric_specs:
            columns.append({
                "key": f"{method}__{suffix}", "label": label,
                "group": method,
                "type": "text" if suffix == "valid_over_N" else "number",
                "number_format": "text" if suffix == "valid_over_N" else "0.000000",
            })
    columns.extend((
        {"key": "M3__speedup_vs_M2", "label": "M3相对M2速度比",
         "group": "同阶段比较", "type": "number", "number_format": "0.000"},
        {"key": "M4__speedup_vs_M2", "label": "M4相对M2速度比",
         "group": "同阶段比较", "type": "number", "number_format": "0.000"},
        {"key": "layer_ledger_comparable", "label": "Layer ledger严格可比",
         "group": "同阶段比较", "type": "boolean"},
        {"key": "layer_ledger_reason", "label": "可比性说明",
         "group": "同阶段比较", "type": "text"},
    ))
    return columns


def aggregate_final_results(
        quality_manifests: Iterable[ManifestInput],
        timing_manifests: Iterable[ManifestInput], *,
        expected_experiment_ids: Mapping[str, str],
        frozen_suites: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """Validate exact formal cohorts and produce the two dataset row sets."""
    suites = _validate_frozen_inputs(expected_experiment_ids, frozen_suites)
    quality = _load_explicit(quality_manifests, label="quality")
    timing = _load_explicit(timing_manifests, label="timing")
    _validate_cohort(
        quality, run_kind="main", suites=suites,
        expected_experiment_ids=expected_experiment_ids)
    _validate_cohort(
        timing, run_kind="timing", suites=suites,
        expected_experiment_ids=expected_experiment_ids)
    _validate_cross_cohort_provenance([*quality, *timing], suites)

    quality_groups: dict[tuple[str, str, str], list[RunManifest]] = defaultdict(list)
    timing_groups: dict[tuple[str, str, str], list[RunManifest]] = defaultdict(list)
    for run in quality:
        quality_groups[(run.dataset, run.circuit, run.method)].append(run)
    for run in timing:
        timing_groups[(run.dataset, run.circuit, run.method)].append(run)

    rows: dict[str, list[dict[str, Any]]] = {
        name: [] for name in SHEET_NAMES
    }
    for dataset, sheet in DATASET_SHEETS.items():
        dataset_quality_rows: list[dict[str, Any]] = []
        for circuit in suites[dataset]:
            row: dict[str, Any] = {"circuit": circuit}
            method_timing_runs = {
                method: timing_groups[(dataset, circuit, method)]
                for method in METHODS
            }
            timing_comparable, _timing_reason = _ledger_comparability(
                method_timing_runs)
            for method in METHODS:
                quality_runs = quality_groups[(dataset, circuit, method)]
                successful_quality = _successes(quality_runs)
                timing_summary = _timing_summary(
                    timing_groups[(dataset, circuit, method)])
                quality_complete = (
                    len(successful_quality) == QUALITY_NUMERATOR[method])
                strict_quality = successful_quality if quality_complete else []
                timing_complete = (
                    timing_summary["valid"] == TIMING_REPETITIONS)
                row.update({
                    f"{method}__fidelity": _fidelity_median(strict_quality),
                    f"{method}__move_batches": _median(
                        run.move_batches for run in strict_quality),
                    f"{method}__move_time_us": _median(
                        run.move_time_us for run in strict_quality),
                    f"{method}__transition_decision_s":
                        (timing_summary["transition_decision_s_median"]
                         if timing_complete and timing_comparable else None),
                    f"{method}__valid_over_N":
                        f"{len(successful_quality)}/{QUALITY_NUMERATOR[method]}",
                })
                if method in {"M3", "M4"}:
                    for suffix, _manifest_field, _label in OURS_TIMING_METRICS:
                        row[f"{method}__{suffix}"] = (
                            timing_summary[f"{suffix}_median"]
                            if timing_complete else None
                        )
            rows[sheet].append(row)
            dataset_quality_rows.append(row)

        rows[sheet].append(_overall_quality_row(
            dataset_quality_rows, quality_groups=quality_groups,
            dataset=dataset, circuits=suites[dataset]))

    quality_columns = _quality_columns()
    workbook_contract = {
        "experiment_schema": SCHEMA_VERSION,
        "contract_id": FINAL_RESULTS_CONTRACT_ID,
        "exact_sheet_count": 2,
        "sheet_names": list(SHEET_NAMES),
        "charts": False,
        "notes": {
            "quality_seed_rule": "M1/M2 one run; M3/M4 median of seeds 0-2",
            "quality_valid_N": "successful verified quality attempts / required attempts",
            "fidelity_ood_rule": (
                "if any required successful seed is outside the linear coherence "
                "model, Fidelity is blank; valid/N remains compiler coverage"),
            "quality_stage_time": "median of the three separate timing repetitions",
            "ours_timing_breakdown": (
                "problem preparation + search stage wall + result commit + "
                "transition residual is the additive transition-decision split; "
                "Python/C++ conversion, native call/parse/search/serialize, fitness, "
                "normalize/decode/RETURN/forecast/selection are nested diagnostics "
                "and must not be added to the wall-clock split"),
            "runtime_primary_metric": "transition_decision_ns",
            "speedup_definition": "M2 median / ours median; values above 1 are faster",
            "overall_row": (
                "strict complete cohort only: fidelity and runtime use circuit-level "
                "geometric means; Move batches/time use circuit-level medians; blank "
                "if any circuit value is missing, with summed valid/N shown separately"),
            "xlsx_generation": "artifact-tool only; render and inspect every sheet",
        },
        "sheets": [
            {
                "name": "ZAC18", "source_csv": "zac18.csv",
                "header_rows": 2,
                "frozen_row_order": [*suites["zac18"], OVERALL_LABEL],
                "columns": quality_columns,
            },
            {
                "name": "QMAP154", "source_csv": "qmap154.csv",
                "header_rows": 2,
                "frozen_row_order": [*suites["qmap154"], OVERALL_LABEL],
                "columns": quality_columns,
            },
        ],
    }
    return {
        "experiment_schema": SCHEMA_VERSION,
        "contract_id": FINAL_RESULTS_CONTRACT_ID,
        "experiment_ids": dict(expected_experiment_ids),
        "rows": rows,
        "workbook_contract": workbook_contract,
    }


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]],
               columns: Sequence[Mapping[str, Any]]) -> None:
    keys = [str(column["key"]) for column in columns]
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_final_results(result: Mapping[str, Any],
                        output_directory: str | os.PathLike[str]) -> dict[str, Path]:
    """Write deterministic CSV inputs and the renderer workbook contract."""
    if result.get("experiment_schema") != SCHEMA_VERSION:
        raise ValueError("refusing to write non-Schema-2 final results")
    if result.get("contract_id") != FINAL_RESULTS_CONTRACT_ID:
        raise ValueError("unknown final-results contract")
    contract = result.get("workbook_contract")
    rows = result.get("rows")
    if not isinstance(contract, Mapping) or not isinstance(rows, Mapping):
        raise ValueError("final results lack rows or workbook contract")
    if tuple(contract.get("sheet_names", ())) != SHEET_NAMES:
        raise ValueError("workbook must contain exactly ZAC18 and QMAP154")
    sheets = contract.get("sheets")
    if not isinstance(sheets, list) or len(sheets) != 2:
        raise ValueError("workbook contract must describe exactly two sheets")

    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for sheet in sheets:
        name = sheet["name"]
        source = destination / sheet["source_csv"]
        _write_csv(source, rows[name], sheet["columns"])
        paths[name] = source
    contract_path = destination / "workbook_contract.json"
    temporary = contract_path.with_name(contract_path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(contract, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, contract_path)
    paths["workbook_contract"] = contract_path
    return paths


__all__ = [
    "DATASET_SHEETS", "FINAL_RESULTS_CONTRACT_ID", "METHODS", "OVERALL_LABEL",
    "SHEET_NAMES",
    "TIMING_REPETITIONS", "aggregate_final_results", "write_final_results",
]
