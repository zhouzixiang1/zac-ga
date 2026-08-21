"""Strict diagnostic aggregation for the preregistered ablation track.

This module intentionally has no publication claim gate.  It summarizes
mechanisms only, after taking the median of the five paired seeds within each
circuit.  Missing, duplicate, failed, OOD, or protocol-mismatched observations
remain explicit and are never imputed.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .ablation import ABLATION_VARIANTS, registered_variant
from .contracts import RunManifest, RunStatus, load_run_manifest, stable_sha256


EXPECTED_SEEDS = (0, 1, 2, 3, 4)


def _summary(values: Sequence[float]) -> Mapping[str, float | int] | None:
    if not values:
        return None
    finite = [float(value) for value in values]
    if any(not math.isfinite(value) for value in finite):
        raise ValueError("ablation summary received a non-finite value")
    return {
        "n": len(finite),
        "mean": statistics.fmean(finite),
        "median": statistics.median(finite),
        "min": min(finite),
        "max": max(finite),
    }


def _machine_id(run: RunManifest) -> str:
    return stable_sha256(run.machine)


def _integrity_errors(manifests: Sequence[RunManifest],
                      circuits: Sequence[str]) -> list[str]:
    errors: list[str] = []
    commits = {row.git_commit for row in manifests}
    if len(commits) != 1 or "unknown" in commits:
        errors.append(f"expected one known git commit, found {sorted(commits)}")
    if any(row.git_dirty for row in manifests):
        errors.append("formal ablation contains a dirty Git attempt")
    architectures = {row.architecture_sha256 for row in manifests}
    models = {row.model_sha256 for row in manifests}
    machines = {_machine_id(row) for row in manifests}
    if len(architectures) != 1 or "" in architectures:
        errors.append("architecture hash is missing or mixed")
    if len(models) != 1 or "" in models:
        errors.append("fidelity-model hash is missing or mixed")
    if len(machines) != 1:
        errors.append("ablation attempts were run on multiple machine snapshots")
    for circuit in circuits:
        hashes = {row.input_sha256 for row in manifests if row.circuit == circuit}
        if len(hashes) > 1 or "" in hashes:
            errors.append(f"{circuit}: canonical input hash is missing or mixed")
    by_variant_seed: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in manifests:
        by_variant_seed[(row.ablation_variant, row.seed)].add(row.config_sha256)
    for key, hashes in sorted(by_variant_seed.items()):
        if len(hashes) != 1 or "" in hashes:
            errors.append(
                f"variant/seed {key}: resolved config hash is missing or mixed")
    return errors


def _cell(circuit: str, variant_name: str,
          runs: Sequence[RunManifest]) -> Mapping[str, Any]:
    variant = registered_variant(variant_name)
    by_seed: dict[int, list[RunManifest]] = defaultdict(list)
    unexpected_seeds: list[int] = []
    for run in runs:
        if run.seed not in EXPECTED_SEEDS:
            unexpected_seeds.append(run.seed)
        else:
            by_seed[run.seed].append(run)

    reasons: list[str] = []
    seed_rows: list[Mapping[str, Any]] = []
    valid_runs: list[RunManifest] = []
    if unexpected_seeds:
        reasons.append(f"unexpected_seeds={sorted(unexpected_seeds)}")
    for seed in EXPECTED_SEEDS:
        observed = by_seed.get(seed, [])
        if not observed:
            reasons.append(f"seed_{seed}:missing")
            seed_rows.append({"seed": seed, "status": "missing", "run_ids": []})
            continue
        if len(observed) != 1:
            reasons.append(f"seed_{seed}:duplicate_attempts={len(observed)}")
        run = sorted(observed, key=lambda row: row.run_id)[0]
        row_reasons = []
        if run.repetition != 0:
            row_reasons.append(f"unexpected_repetition={run.repetition}")
        if run.method != variant.base_method:
            row_reasons.append(
                f"method={run.method},expected={variant.base_method}")
        if run.status != RunStatus.SUCCESS.value:
            row_reasons.append(f"status={run.status}")
        if len(observed) != 1:
            row_reasons.append("duplicate_seed")
        reasons.extend(f"seed_{seed}:{reason}" for reason in row_reasons)
        seed_rows.append({
            "seed": seed,
            "status": run.status,
            "run_ids": [item.run_id for item in sorted(
                observed, key=lambda item: item.run_id)],
            "repetition": run.repetition,
            "method": run.method,
            "fidelity_ood": run.fidelity_ood,
            "log_fidelity": run.log_fidelity,
            "move_batches": run.move_batches,
            "move_time_us": run.move_time_us,
            "compiler_time_seconds": (
                run.compiler_time_ns / 1e9
                if run.compiler_time_ns is not None else None),
            "error": run.error,
        })
        if not row_reasons:
            valid_runs.append(run)

    protocol_valid = not reasons and len(valid_runs) == len(EXPECTED_SEEDS)
    metrics: dict[str, Any] = {
        "log_fidelity": None,
        "fidelity": None,
        "move_batches": None,
        "move_time_us": None,
        "compiler_time_seconds": None,
    }
    fidelity_valid = False
    if protocol_valid:
        metrics["move_batches"] = statistics.median(
            float(row.move_batches) for row in valid_runs)
        metrics["move_time_us"] = statistics.median(
            float(row.move_time_us) for row in valid_runs)
        metrics["compiler_time_seconds"] = statistics.median(
            float(row.compiler_time_ns) / 1e9 for row in valid_runs)
        fidelity_valid = all(
            not row.fidelity_ood and row.log_fidelity is not None
            for row in valid_runs)
        if fidelity_valid:
            log_fidelity = statistics.median(
                float(row.log_fidelity) for row in valid_runs)
            metrics["log_fidelity"] = log_fidelity
            metrics["fidelity"] = math.exp(log_fidelity)

    return {
        "circuit": circuit,
        "ablation_variant": variant_name,
        "base_method": variant.base_method,
        "success_valid": protocol_valid,
        "fidelity_valid": fidelity_valid,
        "invalid_reasons": reasons,
        "seed_runs": seed_rows,
        "seed_median": metrics,
    }


def aggregate_ablation(
        manifest_paths: Iterable[str | Path], *, dataset: str,
        frozen_circuits: Sequence[str], experiment_id: str,
        expected_variants: Sequence[str] | None = None) -> Mapping[str, Any]:
    """Aggregate one frozen ablation cohort without creating paper claims."""
    paths = list(manifest_paths)
    if not paths:
        raise ValueError("no Schema-2 ablation manifests to aggregate")
    manifests = [load_run_manifest(path, require_success_metrics=True)
                 for path in paths]
    if any(row.run_kind != "ablation" for row in manifests):
        raise ValueError("aggregate_ablation accepts only run_kind=ablation")
    wrong_datasets = sorted({row.dataset for row in manifests
                             if row.dataset != dataset})
    if wrong_datasets:
        raise ValueError(
            f"ablation dataset mixing is forbidden: {wrong_datasets}")
    ids = {row.experiment_id for row in manifests}
    if ids != {experiment_id}:
        raise ValueError(
            f"ablation experiment_id mismatch: expected {experiment_id}, "
            f"found {sorted(ids)}")
    circuits = list(frozen_circuits)
    if not circuits or len(circuits) != len(set(circuits)):
        raise ValueError("frozen ablation cohort must be non-empty and unique")
    outside = sorted({row.circuit for row in manifests} - set(circuits))
    if outside:
        raise ValueError(f"ablation manifests outside frozen cohort: {outside}")
    variants = list(expected_variants or ABLATION_VARIANTS)
    if not variants or len(variants) != len(set(variants)):
        raise ValueError("expected ablation variants must be non-empty and unique")
    for name in variants:
        registered_variant(name)
    outside_variants = sorted(
        {row.ablation_variant for row in manifests} - set(variants))
    if outside_variants:
        raise ValueError(
            f"unexpected ablation variants: {outside_variants}")

    grouped: dict[tuple[str, str], list[RunManifest]] = defaultdict(list)
    for row in manifests:
        grouped[(row.circuit, row.ablation_variant)].append(row)
    variant_reports: dict[str, Any] = {}
    all_cells: dict[tuple[str, str], Mapping[str, Any]] = {}
    for variant_name in variants:
        circuit_rows = []
        for circuit in circuits:
            cell = _cell(
                circuit, variant_name,
                grouped.get((circuit, variant_name), []))
            circuit_rows.append(cell)
            all_cells[(circuit, variant_name)] = cell
        successful = [row for row in circuit_rows if row["success_valid"]]
        fidelity_rows = [row for row in successful if row["fidelity_valid"]]
        logs = [row["seed_median"]["log_fidelity"] for row in fidelity_rows]
        batches = [row["seed_median"]["move_batches"] for row in successful]
        move_times = [row["seed_median"]["move_time_us"] for row in successful]
        runtimes = [row["seed_median"]["compiler_time_seconds"]
                    for row in successful]
        log_summary = _summary(logs)
        variant_runs = [row for row in manifests
                        if row.ablation_variant == variant_name]
        observed_statuses = Counter(row.status for row in variant_runs)
        expected_attempts = len(circuits) * len(EXPECTED_SEEDS)
        variant_reports[variant_name] = {
            "base_method": registered_variant(variant_name).base_method,
            "controls": registered_variant(variant_name).controls(),
            "success": {
                "valid": len(successful),
                "N": len(circuits),
                "rate": len(successful) / len(circuits),
            },
            "fidelity": {
                "valid": len(fidelity_rows),
                "N": len(circuits),
                "log_fidelity": log_summary,
                "geometric_mean_fidelity": (
                    math.exp(float(log_summary["mean"]))
                    if log_summary is not None else None),
            },
            "move_batches": {
                "valid": len(batches), "N": len(circuits),
                "summary": _summary(batches),
            },
            "move_time_us": {
                "valid": len(move_times), "N": len(circuits),
                "summary": _summary(move_times),
            },
            "compiler_time_seconds": {
                "valid": len(runtimes), "N": len(circuits),
                "summary": _summary(runtimes),
            },
            "attempts": {
                "observed": len(variant_runs),
                "expected": expected_attempts,
                "missing_slots": max(0, expected_attempts - len(variant_runs)),
                "extra_records": max(0, len(variant_runs) - expected_attempts),
                "status_counts": dict(sorted(observed_statuses.items())),
            },
            "circuits": circuit_rows,
        }

    fully_paired_success = [
        circuit for circuit in circuits
        if all(all_cells[(circuit, variant)]["success_valid"]
               for variant in variants)
    ]
    fully_paired_fidelity = [
        circuit for circuit in fully_paired_success
        if all(all_cells[(circuit, variant)]["fidelity_valid"]
               for variant in variants)
    ]
    integrity_errors = _integrity_errors(manifests, circuits)
    return {
        "experiment_schema": 2,
        "run_kind": "ablation",
        "experiment_id": experiment_id,
        "dataset": dataset,
        "diagnostic_only": True,
        "eligible_for_main_claim_gate": False,
        "aggregation_order": (
            "median over seeds 0..4 within circuit; then summarize circuits"),
        "frozen_cohort": {
            "circuits": circuits,
            "N": len(circuits),
            "sha256": stable_sha256(circuits),
        },
        "expected_variants": variants,
        "expected_seeds": list(EXPECTED_SEEDS),
        "integrity": {
            "passed": not integrity_errors,
            "errors": integrity_errors,
        },
        "fully_paired_success": {
            "valid": len(fully_paired_success), "N": len(circuits),
            "circuits": fully_paired_success,
        },
        "fully_paired_fidelity": {
            "valid": len(fully_paired_fidelity), "N": len(circuits),
            "circuits": fully_paired_fidelity,
        },
        "variants": variant_reports,
        "attempt_index": [
            {
                "run_id": row.run_id,
                "circuit": row.circuit,
                "ablation_variant": row.ablation_variant,
                "method": row.method,
                "seed": row.seed,
                "repetition": row.repetition,
                "status": row.status,
                "fidelity_ood": row.fidelity_ood,
                "log_fidelity": row.log_fidelity,
                "move_batches": row.move_batches,
                "move_time_us": row.move_time_us,
                "compiler_time_seconds": (
                    row.compiler_time_ns / 1e9
                    if row.compiler_time_ns is not None else None),
                "artifact_dir": row.artifact_dir,
                "error": row.error,
            }
            for row in sorted(manifests, key=lambda item: (
                item.circuit, item.ablation_variant, item.seed,
                item.repetition, item.run_id))
        ],
    }


__all__ = ["EXPECTED_SEEDS", "aggregate_ablation"]
