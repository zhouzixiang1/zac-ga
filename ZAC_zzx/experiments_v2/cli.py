"""Schema-2 command line orchestration for the four-method experiment.

No command discovers legacy score files.  Formal compiler attempts are created
from a frozen plan and are promoted to ``success`` only after independent trace
normalisation, unified physical scoring, and exact canonical gate-ledger checks.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from evaluation import (FidelityResult, normalize_na, normalize_zair, score_trace,
                        validate_trace_physics)

from .ablation import (
    ABLATION_VARIANTS, ablation_experiment_id, build_ablation_config,
    registered_variant, select_ablation_cohort,
)
from .ablation_statistics import aggregate_ablation
from .ablation_export import export_ablation_report
from .canonicalize import canonicalize_suite
from .contracts import (
    CanonicalCircuitManifest,
    RunManifest,
    RunStatus,
    load_run_manifest,
    repository_snapshot,
    sha256_file,
    stable_sha256,
)
from .iccad_reproduction import reproduce_iccad
from .zac_reproduction import reproduce_zac
from .export import export_experiment_report
from .plan import METHODS, DatasetSpec, ExperimentPlan, load_experiment_plan
from .provenance import (ENVIRONMENT_LOCKS, build_reproduction_provenance,
                         validate_frozen_environments,
                         validate_reproduction_provenance)
from .protocol import (enforces_ghost_safety, ghost_policy_for_method,
                       physicalization_policy_for_method,
                       trace_protocol_for_method)
from .reproduction import reproduce_baselines
from .runner import AttemptSpec, run_attempt
from .statistics import aggregate_experiment
from streaming.large_contract import LargeExperimentContract


_CANONICAL_1Q = re.compile(
    r"^\s*(?:u1|u2|u3)\s*\([^;]*\)\s+q\[(\d+)\]\s*;\s*$")
_CANONICAL_CZ = re.compile(
    r"^\s*cz\s+q\[(\d+)\]\s*,\s*q\[(\d+)\]\s*;\s*$")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _relative_plan_path(plan: ExperimentPlan, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = plan.path.parent / path
    return path.absolute()


def _report_path(plan: ExperimentPlan, name: str) -> Path:
    return plan.output_root / "reports" / f"{name}.json"


def _canonical_identity(manifest: CanonicalCircuitManifest) -> tuple[str, Path]:
    path = Path(manifest.canonical_path).resolve()
    return path.stem, path


def _canonical_gate_ledger(manifest: CanonicalCircuitManifest
                           ) -> tuple[Counter[int], dict[int, tuple[str, ...]]]:
    """Read the frozen logical ledger without invoking another transpilation."""
    one_qubit: Counter[int] = Counter()
    operation_sequence: dict[int, list[str]] = {}
    path = Path(manifest.canonical_path)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line:
            continue
        match = _CANONICAL_1Q.match(line)
        if match:
            qubit = int(match.group(1))
            one_qubit[qubit] += 1
            operation_sequence.setdefault(qubit, []).append("1q")
            continue
        match = _CANONICAL_CZ.match(line)
        if match:
            q0, q1 = int(match.group(1)), int(match.group(2))
            operation_sequence.setdefault(q0, []).append(f"cz:{q1}")
            operation_sequence.setdefault(q1, []).append(f"cz:{q0}")
    observed_2q = sum(
        marker.startswith("cz:") for values in operation_sequence.values()
        for marker in values) // 2
    if (sum(one_qubit.values()), observed_2q) != (
            manifest.gates_1q, manifest.gates_2q):
        raise ValueError(
            "canonical gate ledger disagrees with its manifest: "
            f"parsed={(sum(one_qubit.values()), observed_2q)}, "
            f"manifest={(manifest.gates_1q, manifest.gates_2q)}"
        )
    return one_qubit, {
        q: tuple(value) for q, value in sorted(operation_sequence.items())}


def _ledger_sha256(ledger: tuple[Counter[int], dict[int, tuple[str, ...]]]) -> str:
    one_qubit, operation_sequence = ledger
    return stable_sha256({
        "one_qubit_per_atom": [[q, one_qubit[q]] for q in sorted(one_qubit)],
        "per_atom_logical_operation_sequence": [
            [q, list(operation_sequence[q])] for q in sorted(operation_sequence)],
    })


class UnifiedEvaluationGate:
    """One streaming normalisation/score pass shared by verifier and scorer."""

    def __init__(self, plan: ExperimentPlan, canonical: CanonicalCircuitManifest,
                 method: str, *, write_artifacts: bool = True):
        self.plan = plan
        self.canonical = canonical
        self.method = method
        self.write_artifacts = write_artifacts
        self._artifact: Path | None = None
        self._result: FidelityResult | None = None
        self._metrics: Mapping[str, Any] | None = None
        self._observed_ledger: tuple[Counter[int], dict[int, tuple[str, ...]]] | None = None
        self._physical_validation: Mapping[str, Any] | None = None

    @property
    def trace_name(self) -> str:
        return "trace.na" if self.method == "M2" else "trace.zair.json"

    @property
    def enforce_ghost_safety(self) -> bool:
        return enforces_ghost_safety(self.method)

    @staticmethod
    def _artifact_file(artifact: Path, name: str) -> Path:
        """Resolve one raw or gzip-archived artifact, never ambiguously."""
        raw = artifact / name
        archived = artifact / f"{name}.gz"
        matches = [path for path in (raw, archived) if path.is_file()]
        if len(matches) > 1:
            raise ValueError(
                f"attempt contains both raw and archived {name}: {artifact}")
        return matches[0] if matches else raw

    def _normalizer(self, trace_path: Path):
        if self.method == "M2":
            return normalize_na(
                trace_path,
                architecture=self.plan.architecture_path,
                model=self.plan.model,
            )
        return normalize_zair(
            trace_path,
            architecture=self.plan.architecture_path,
            model=self.plan.model,
        )

    def _score(self, artifact: Path) -> FidelityResult:
        trace_path = self._artifact_file(artifact, self.trace_name)
        if not trace_path.is_file():
            raise FileNotFoundError(f"compiler trace is missing: {trace_path}")
        events = self._normalizer(trace_path)
        one_qubit: Counter[int] = Counter()
        operation_sequence: dict[int, list[str]] = {}

        def audited_events():
            for event in events:
                if event.kind == "one_qubit_gate":
                    one_qubit.update(event.atoms)
                    for qubit in event.atoms:
                        operation_sequence.setdefault(qubit, []).append("1q")
                elif event.kind == "two_qubit_gate":
                    for q0, q1 in event.gate_pairs:
                        operation_sequence.setdefault(q0, []).append(f"cz:{q1}")
                        operation_sequence.setdefault(q1, []).append(f"cz:{q0}")
                yield event

        if not self.write_artifacts:
            result = score_trace(
                audited_events(), self.plan.model, n_qubits=self.canonical.qubits)
            self._observed_ledger = (
                one_qubit,
                {q: tuple(value) for q, value in sorted(operation_sequence.items())},
            )
            return result

        output = artifact / "canonical_trace.jsonl.gz"
        temporary = output.with_name(output.name + ".tmp")
        try:
            with gzip.open(temporary, "wt", encoding="utf-8") as handle:
                def recording_events():
                    for event in audited_events():
                        handle.write(json.dumps(event.to_dict(), sort_keys=True,
                                                separators=(",", ":")))
                        handle.write("\n")
                        yield event
                result = score_trace(
                    recording_events(), self.plan.model,
                    n_qubits=self.canonical.qubits,
                )
            os.replace(temporary, output)
            self._observed_ledger = (
                one_qubit,
                {q: tuple(value) for q, value in sorted(operation_sequence.items())},
            )
            return result
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _validate_result(self, result: FidelityResult) -> None:
        expected = (self.canonical.gates_1q, self.canonical.gates_2q)
        actual = (result.one_qubit_gates, result.two_qubit_gates)
        if actual != expected:
            raise ValueError(
                "canonical/native gate ledger mismatch: "
                f"expected 1Q/2Q={expected}, observed={actual}"
            )
        canonical_ledger = _canonical_gate_ledger(self.canonical)
        if self._observed_ledger != canonical_ledger:
            raise ValueError(
                "canonical/native logical gate ledger mismatch: "
                f"expected={canonical_ledger}, observed={self._observed_ledger}"
            )
        if result.log_fidelity is not None:
            component_sum = math.fsum(
                value for value in result.component_log_fidelity.values()
                if value is not None
            )
            if not math.isclose(component_sum, result.log_fidelity,
                                rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(
                    f"log-fidelity decomposition mismatch: {component_sum} != "
                    f"{result.log_fidelity}"
                )

    @staticmethod
    def _compiler_counters(artifact: Path) -> Mapping[str, int]:
        path = UnifiedEvaluationGate._artifact_file(
            artifact, "compiler_stats.json")
        if not path.is_file():
            return {}
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    payload = json.load(handle)
            else:
                payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        decision_log = payload.get("decision_log", [])
        if not isinstance(decision_log, list):
            decision_log = []
        counts = Counter()
        repairs: list[int] = []
        for row in decision_log:
            if not isinstance(row, Mapping):
                continue
            for source, destination in (("stay", "stay_count"),
                                        ("return", "return_count"),
                                        ("reseat", "reseat_count")):
                value = row.get(source, 0)
                if isinstance(value, (int, float)):
                    counts[destination] += int(value)
            repair = row.get("ghost_fix")
            if isinstance(repair, (int, float)):
                repairs.append(int(repair))
        counts["ghost_repairs"] = sum(repairs)
        splits = payload.get("ghost_splits", 0)
        if isinstance(splits, (int, float)):
            counts["ghost_splits"] = int(splits)
        return dict(counts)

    def _build_metrics(self, artifact: Path, result: FidelityResult) -> Mapping[str, Any]:
        components: dict[str, float | None] = {
            "qubits": float(self.canonical.qubits),
            "gates_1q": float(result.one_qubit_gates),
            "gates_2q": float(result.two_qubit_gates),
            "idle_excitations": float(result.idle_excitations),
            "transfers": float(result.transfers),
            "duration_us": float(result.duration_us),
            "exponential_sensitivity_log_fidelity": float(
                result.exponential_sensitivity_log_fidelity),
        }
        for name, value in result.component_fidelity.items():
            components[name] = None if value is None else float(value)
        for name, value in result.component_log_fidelity.items():
            components[f"log_{name}"] = (
                None if value is None else float(value))
        ghost_count = int((self._physical_validation or {}).get("ghost_hits", -1))
        warnings = list(result.warnings)
        if not self.enforce_ghost_safety and ghost_count > 0:
            warnings.append(
                f"paper-native {self.method} trace contains {ghost_count} "
                "stationary-ghost hits; recorded without baseline repair")
        metrics: dict[str, Any] = {
            "log_fidelity": result.log_fidelity,
            "fidelity": result.fidelity,
            "fidelity_components": components,
            "move_batches": result.move_batches,
            "move_time_us": result.move_time_us,
            "idle_exposures": result.idle_excitations,
            "fidelity_ood": result.ood,
            "exponential_sensitivity_fidelity": result.exponential_sensitivity_fidelity,
            "exponential_sensitivity_log_fidelity": result.exponential_sensitivity_log_fidelity,
            "duration_us": result.duration_us,
            "qubits": self.canonical.qubits,
            "expected_gates_1q": self.canonical.gates_1q,
            "expected_gates_2q": self.canonical.gates_2q,
            "observed_gates_1q": result.one_qubit_gates,
            "observed_gates_2q": result.two_qubit_gates,
            "expected_gate_ledger_sha256": _ledger_sha256(
                _canonical_gate_ledger(self.canonical)),
            "observed_gate_ledger_sha256": _ledger_sha256(self._observed_ledger),
            "ghost_hits": ghost_count,
            "trace_protocol": trace_protocol_for_method(self.method),
            "ghost_policy": ghost_policy_for_method(self.method),
            "physicalization_policy": physicalization_policy_for_method(self.method),
            "warnings": warnings,
        }
        metrics.update(self._compiler_counters(artifact))
        return metrics

    def evaluate(self, artifact: Path) -> tuple[FidelityResult, Mapping[str, Any]]:
        artifact = artifact.resolve()
        if self._artifact == artifact and self._result is not None and self._metrics is not None:
            return self._result, self._metrics
        result = self._score(artifact)
        self._validate_result(result)
        # Re-normalize for a second, independent physical replay.  This keeps the
        # fidelity accumulator and correctness verifier from sharing mutable state.
        self._physical_validation = validate_trace_physics(
            self._normalizer(self._artifact_file(artifact, self.trace_name)),
            n_qubits=self.canonical.qubits,
            enforce_ghost_safety=self.enforce_ghost_safety,
        )
        if self.method != "M2":
            from verify_batches import verify
            raw_validation = verify(
                self._artifact_file(artifact, self.trace_name),
                Path(self.canonical.canonical_path),
            )
            errors = {
                name: values
                for name, values in raw_validation["errors"].items()
                if values and not (self.method == "M1" and name == "ghost")
            }
            if errors:
                raise ValueError(f"method-policy ZAIR replay failed: {errors}")
            self._physical_validation = {
                **self._physical_validation,
                "ghost_hits": int(raw_validation["stats"]["ghost_hits"]),
                "zair_stats": raw_validation["stats"],
            }
        metrics = self._build_metrics(artifact, result)
        if self.write_artifacts:
            _atomic_json(artifact / "fidelity.json", {
                "experiment_schema": 2,
                "trace_protocol": trace_protocol_for_method(self.method),
                "ghost_policy": ghost_policy_for_method(self.method),
                "physicalization_policy": physicalization_policy_for_method(
                    self.method),
                "model": self.plan.model.to_dict(),
                "canonical": self.canonical.to_dict(),
                "result": result.to_dict(),
            })
        self._artifact, self._result, self._metrics = artifact, result, metrics
        return result, metrics

    def verifier(self, artifact: Path) -> Mapping[str, Any]:
        result, _ = self.evaluate(artifact)
        return {"ok": True, "warnings": list(result.warnings)}

    def scorer(self, artifact: Path) -> Mapping[str, Any]:
        _, metrics = self.evaluate(artifact)
        return metrics


def _pythonpath(plan: ExperimentPlan) -> str:
    existing = os.environ.get("PYTHONPATH", "")
    values = [str(plan.package_root)]
    if existing:
        values.append(existing)
    return os.pathsep.join(values)


def _attempt_spec(plan: ExperimentPlan, dataset: DatasetSpec,
                  canonical: CanonicalCircuitManifest, method: str,
                  seed: int, repetition: int, phase: str, *,
                  config_path: Path | None = None,
                  ablation_variant: str = "",
                  experiment_id: str | None = None) -> AttemptSpec:
    if phase == "large":
        raise RuntimeError(
            "generic method_driver attempts are forbidden for Large; "
            "a streaming compiler adapter is required"
        )
    circuit, input_path = _canonical_identity(canonical)
    config = config_path or plan.resolved_config(method, seed)
    expected_ledger_sha256 = _ledger_sha256(_canonical_gate_ledger(canonical))
    command = [
        plan.methods[method].python,
        "-m", "experiments_v2.method_driver",
        "--method", method,
        "--input", str(input_path),
        "--config", str(config),
        "--architecture", str(plan.architecture_path),
        "--run-kind", phase,
    ]
    if ablation_variant:
        command.extend(("--ablation-variant", ablation_variant))
    is_large = phase == "large"
    lock_name = ENVIRONMENT_LOCKS[1 if method == "M2" else 0]
    lock_path = plan.path.parent / lock_name
    runtime_packages = {
        **plan.package_versions,
        "compiler_python_path": plan.methods[method].python,
        "compiler_environment": "iccad_qmap320" if method == "M2" else
                                "zac_qiskit124",
    }
    if lock_path.is_file():
        runtime_packages["environment_lock_sha256"] = sha256_file(lock_path)
    return AttemptSpec(
        dataset=dataset.name,
        circuit=circuit,
        method=method,
        seed=seed,
        repetition=repetition,
        command=command,
        output_root=(plan.output_root / "runs" / phase / dataset.name /
                     ablation_variant if ablation_variant else
                     plan.output_root / "runs" / phase / dataset.name),
        repo_root=plan.repo_root,
        input_path=input_path,
        config_path=config,
        architecture_path=plan.architecture_path,
        model_path=plan.model_path,
        run_kind=phase,
        ablation_variant=ablation_variant,
        experiment_id=experiment_id or plan.experiment_id(dataset),
        qubits=canonical.qubits,
        expected_gates_1q=canonical.gates_1q,
        expected_gates_2q=canonical.gates_2q,
        expected_gate_ledger_sha256=expected_ledger_sha256,
        require_clean_git=True,
        timeout_seconds=(plan.large_timeout_seconds if is_large
                         else plan.timeout_seconds),
        rss_limit_bytes=(plan.large_rss_limit_bytes if is_large
                         else plan.rss_limit_bytes),
        minimum_free_bytes=plan.minimum_free_bytes,
        environment={"PYTHONPATH": _pythonpath(plan)},
        cwd=plan.repo_root,
        package_versions=runtime_packages,
    )


def _manifest_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    for path in sorted(root.rglob("manifest.json"), key=str):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        # Loading here is intentional: a Schema-1 or malformed manifest in a
        # formal results directory is an error, never silently ignored.
        load_run_manifest(path)
        paths.append(path)
    return paths


def _resume_key(manifest: RunManifest) -> tuple[Any, ...]:
    return (
        manifest.experiment_id,
        manifest.dataset, manifest.circuit, manifest.method,
        manifest.ablation_variant, manifest.seed, manifest.repetition,
        manifest.input_sha256,
        manifest.config_sha256, manifest.architecture_sha256,
        manifest.model_sha256,
    )


def _resume_manifest_is_current(
        manifest: RunManifest, repository: Mapping[str, Any]) -> bool:
    """Only a clean attempt from the live commit may suppress a formal rerun."""
    return bool(
        repository.get("commit") not in (None, "", "unknown")
        and repository.get("dirty") is False
        and manifest.git_dirty is False
        and manifest.git_commit == repository.get("commit")
    )


def _spec_key(spec: AttemptSpec) -> tuple[Any, ...]:
    return (
        spec.experiment_id,
        spec.dataset, spec.circuit, spec.method, spec.ablation_variant,
        spec.seed, spec.repetition,
        sha256_file(spec.input_path), sha256_file(spec.config_path),
        sha256_file(spec.architecture_path), sha256_file(spec.model_path),
    )


def _reproduction_report(plan: ExperimentPlan) -> Path:
    value = plan.reproduction.get("output", _report_path(plan, "baseline_reproduction"))
    return _relative_plan_path(plan, value)


def _assert_reproduction_gate(plan: ExperimentPlan) -> None:
    path = _reproduction_report(plan)
    if not path.is_file():
        raise RuntimeError(
            "baseline reproduction gate has not been run; execute "
            "reproduce-baselines first"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("experiment_schema") != 2:
        raise ValueError(f"refusing non-Schema-2 reproduction report: {path}")
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError(f"reproduction report lacks formal provenance: {path}")
    validate_reproduction_provenance(plan, provenance)
    validate_frozen_environments(plan)
    assessment = {key: value for key, value in payload.items()
                  if key not in {"provenance", "assessment_payload_sha256"}}
    if (not isinstance(payload.get("assessment_payload_sha256"), str) or
            stable_sha256(assessment) != payload["assessment_payload_sha256"]):
        raise ValueError(f"reproduction assessment payload changed: {path}")

    try:
        zac_evidence = assessment["zac"]["evidence"]
        iccad_evidence = assessment["iccad"]["evidence"]
        zac_run = zac_evidence["fresh_rerun_evidence"]
        iccad_run = iccad_evidence["run_manifest"]
        refreshed = reproduce_baselines(
            zac_evidence["paper_truth_path"],
            Path(zac_run["result_path"]).parent,
            iccad_evidence["paper_truth_path"],
            iccad_run["result_path"],
        )
    except (KeyError, TypeError) as error:
        raise ValueError(f"incomplete reproduction evidence in {path}") from error
    if refreshed != assessment:
        raise RuntimeError(
            f"baseline reproduction assessment is stale or evidence changed: {path}")
    if payload.get("eligible_for_main_experiment") is not True:
        raise RuntimeError(f"baseline reproduction gate did not pass: {path}")


def _run_matrix(plan: ExperimentPlan, datasets: Sequence[DatasetSpec],
                jobs: Sequence[tuple[str, int, int]], *, phase: str,
                resume: bool, dry_run: bool) -> Mapping[str, Any]:
    if phase == "large":
        raise RuntimeError(
            "generic experiment matrix is forbidden for Large; "
            "use the fail-closed streaming run-large path"
        )
    seeds = sorted({seed for method, seed, _ in jobs if method in ("M3", "M4")})
    for seed in seeds:
        plan.validate_resolved_pair(seed)
    if not dry_run:
        _assert_reproduction_gate(plan)

    existing: set[tuple[Any, ...]] = set()
    ignored_stale_existing: list[str] = []
    current_repository = (
        repository_snapshot(plan.repo_root)
        if resume and not dry_run else None)
    if resume:
        for dataset in datasets:
            root = plan.output_root / "runs" / phase / dataset.name
            for path in _manifest_paths(root):
                manifest = load_run_manifest(path)
                if (current_repository is not None and
                        not _resume_manifest_is_current(
                            manifest, current_repository)):
                    ignored_stale_existing.append(str(path))
                    continue
                existing.add(_resume_key(manifest))

    attempted: list[Mapping[str, Any]] = []
    skipped: list[Mapping[str, Any]] = []
    commands: list[Mapping[str, Any]] = []
    for dataset in datasets:
        for canonical in plan.load_suite(dataset):
            for method, seed, repetition in jobs:
                spec = _attempt_spec(
                    plan, dataset, canonical, method, seed, repetition, phase)
                command_row = {
                    "dataset": dataset.name,
                    "circuit": spec.circuit,
                    "method": method,
                    "seed": seed,
                    "repetition": repetition,
                    "command": list(spec.command),
                    "config": str(spec.config_path),
                }
                if resume and _spec_key(spec) in existing:
                    skipped.append(command_row)
                    continue
                if dry_run:
                    commands.append(command_row)
                    continue
                gate = UnifiedEvaluationGate(plan, canonical, method)
                manifest = run_attempt(spec, verifier=gate.verifier, scorer=gate.scorer)
                attempted.append({
                    "dataset": manifest.dataset,
                    "circuit": manifest.circuit,
                    "method": manifest.method,
                    "seed": manifest.seed,
                    "repetition": manifest.repetition,
                    "status": manifest.status,
                    "manifest": str(Path(manifest.artifact_dir) / "manifest.json"),
                })
    statuses = Counter(row["status"] for row in attempted)
    return {
        "experiment_schema": 2,
        "phase": phase,
        "dry_run": dry_run,
        "attempted": attempted,
        "status_counts": dict(sorted(statuses.items())),
        "skipped_existing": skipped,
        "ignored_stale_existing": ignored_stale_existing,
        "commands": commands,
    }


def _parse_ints(values: Sequence[str]) -> list[int]:
    pieces = [piece.strip() for value in values for piece in value.split(",")
              if piece.strip()]
    try:
        result = sorted({int(piece) for piece in pieces})
    except ValueError as error:
        raise ValueError(f"invalid integer list: {values}") from error
    if not result or any(value < 0 for value in result):
        raise ValueError("seed list must contain non-negative integers")
    return result


def _parse_methods(values: Sequence[str]) -> list[str]:
    pieces = [piece.strip() for value in values for piece in value.split(",")
              if piece.strip()]
    if pieces == ["all"]:
        return list(METHODS)
    if "all" in pieces:
        raise ValueError("'all' cannot be combined with method ids")
    unknown = sorted(set(pieces) - set(METHODS))
    if unknown or not pieces:
        raise ValueError(f"invalid methods: {unknown or pieces}")
    return [method for method in METHODS if method in set(pieces)]


def _parse_ablation_variants(values: Sequence[str] | None) -> list[str]:
    if not values:
        return list(ABLATION_VARIANTS)
    pieces = [piece.strip() for value in values for piece in value.split(",")
              if piece.strip()]
    if pieces == ["all"]:
        return list(ABLATION_VARIANTS)
    if "all" in pieces:
        raise ValueError("'all' cannot be combined with ablation variants")
    unknown = sorted(set(pieces) - set(ABLATION_VARIANTS))
    if unknown or not pieces:
        raise ValueError(f"invalid ablation variants: {unknown or pieces}")
    return [name for name in ABLATION_VARIANTS if name in set(pieces)]


def _resolved_ablation_config(plan: ExperimentPlan, variant_name: str,
                              seed: int) -> Path:
    variant = registered_variant(variant_name)
    base_path = plan.resolved_config(variant.base_method, seed)
    base_payload = json.loads(base_path.read_text(encoding="utf-8"))
    payload = build_ablation_config(variant_name, base_payload)
    destination = (plan.output_root / "_resolved_ablation_configs" /
                   variant_name / f"seed-{int(seed)}.json")
    _atomic_json(destination, payload)
    return destination


def command_reproduce_baselines(plan: ExperimentPlan) -> Mapping[str, Any]:
    config = plan.reproduction
    allowed = {
        "zac_truth", "zac_results", "iccad_truth", "iccad_results",
        "zac_execute", "iccad_execute", "output",
    }
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValueError(f"unknown reproduction fields: {unknown}")
    required = ("zac_truth", "iccad_truth")
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"reproduction plan is missing fields: {missing}")

    # Fail before launching either expensive compiler when the formal code or
    # frozen environment is not a clean, versioned state.
    environment_evidence = validate_frozen_environments(plan)
    preflight = build_reproduction_provenance(
        plan, {"environment_locks": environment_evidence})

    zac_results = config.get("zac_results")
    if zac_results is None:
        execute = config.get("zac_execute")
        if not isinstance(execute, Mapping):
            raise ValueError("reproduction requires zac_results or zac_execute")
        unknown_execute = sorted(
            set(execute) - {"hpca_directory", "architecture", "compiler_root",
                            "output_directory"})
        if unknown_execute:
            raise ValueError(f"unknown zac_execute fields: {unknown_execute}")
        missing_execute = [key for key in
                           ("hpca_directory", "architecture", "compiler_root",
                            "output_directory")
                           if key not in execute]
        if missing_execute:
            raise ValueError(f"zac_execute is missing fields: {missing_execute}")
        zac_results = reproduce_zac(
            _relative_plan_path(plan, execute["hpca_directory"]),
            _relative_plan_path(plan, execute["architecture"]),
            plan.python,
            _relative_plan_path(plan, execute["compiler_root"]),
            _relative_plan_path(plan, execute["output_directory"]),
        )

    iccad_results = config.get("iccad_results")
    if iccad_results is None:
        execute = config.get("iccad_execute")
        if not isinstance(execute, Mapping):
            raise ValueError("reproduction requires iccad_results or iccad_execute")
        unknown_execute = sorted(
            set(execute) - {"hpca_directory", "qmap_python", "output_directory"})
        if unknown_execute:
            raise ValueError(f"unknown iccad_execute fields: {unknown_execute}")
        missing_execute = [key for key in
                           ("hpca_directory", "qmap_python", "output_directory")
                           if key not in execute]
        if missing_execute:
            raise ValueError(f"iccad_execute is missing fields: {missing_execute}")
        iccad_results = reproduce_iccad(
            _relative_plan_path(plan, execute["hpca_directory"]),
            plan.architecture_path,
            _relative_plan_path(plan, execute["qmap_python"]),
            _relative_plan_path(plan, execute["output_directory"]),
        )
    report = reproduce_baselines(
        _relative_plan_path(plan, config["zac_truth"]),
        _relative_plan_path(plan, zac_results),
        _relative_plan_path(plan, config["iccad_truth"]),
        _relative_plan_path(plan, iccad_results),
    )
    package_evidence = {
        "zac": report["zac"]["evidence"]["fresh_rerun_evidence"][
            "package_versions"],
        "iccad": report["iccad"]["evidence"]["run_manifest"][
            "package_versions"],
        "environment_locks": environment_evidence,
    }
    provenance = build_reproduction_provenance(plan, package_evidence)
    if (provenance["repository"]["commit"] !=
            preflight["repository"]["commit"]):
        raise RuntimeError("Git commit changed during baseline reproduction")
    report = {
        **report,
        "assessment_payload_sha256": stable_sha256(report),
        "provenance": provenance,
    }
    output = _reproduction_report(plan)
    _atomic_json(output, report)
    return {**report, "report_path": str(output)}


def command_canonicalize(plan: ExperimentPlan,
                         dataset_names: Sequence[str] | None) -> Mapping[str, Any]:
    if dataset_names:
        datasets = [plan.datasets[name] for name in dataset_names]
    else:
        datasets = [plan.datasets[name] for name in sorted(plan.datasets)
                    if plan.datasets[name].sources]
    if not datasets:
        raise ValueError("no datasets with sources selected")
    report_rows: list[Mapping[str, Any]] = []
    for dataset in datasets:
        if dataset.suite_manifest.is_file():
            manifests = plan.load_suite(dataset)
            for manifest in manifests:
                source = Path(manifest.source_path)
                if not source.is_file() or sha256_file(source) != manifest.source_sha256:
                    raise ValueError(
                        f"immutable source changed after canonicalisation: {source}")
            reused = True
        else:
            if dataset.canonical_directory.exists() and any(
                    dataset.canonical_directory.iterdir()):
                raise ValueError(
                    "canonical directory is non-empty but has no suite manifest: "
                    f"{dataset.canonical_directory}"
                )
            manifests = canonicalize_suite(
                dataset.expanded_sources(), dataset.canonical_directory,
                optimization_level=0 if dataset.kind == "large" else 3,
                canonical_profile=("large_qasmbench_expand_only"
                                   if dataset.kind == "large"
                                   else "main_qiskit_1_2_4_opt3"),
                upstream_commit=dataset.upstream_commit,
            )
            reused = False
        report_rows.append({
            "dataset": dataset.name,
            "suite_manifest": str(dataset.suite_manifest),
            "circuits": len(manifests),
            "reused_immutable_suite": reused,
        })
    report = {"experiment_schema": 2, "datasets": report_rows}
    _atomic_json(_report_path(plan, "canonicalize-suite"), report)
    return report


def command_run_coverage(plan: ExperimentPlan, dataset_names: Sequence[str] | None,
                         *, dry_run: bool, resume: bool = False
                         ) -> Mapping[str, Any]:
    datasets = plan.select_datasets(dataset_names, kind="main")
    jobs = [(method, 0, 0) for method in METHODS]
    report = _run_matrix(
        plan, datasets, jobs, phase="coverage", resume=resume, dry_run=dry_run)
    if not dry_run:
        _atomic_json(_report_path(plan, "run-coverage"), report)
    return report


def command_run_main(plan: ExperimentPlan, dataset_names: Sequence[str] | None,
                     seeds: Sequence[int], *, dry_run: bool,
                     resume: bool = False) -> Mapping[str, Any]:
    if list(sorted(set(seeds))) != [0, 1, 2, 3, 4]:
        raise ValueError("formal run-main requires paired seeds exactly 0,1,2,3,4")
    datasets = plan.select_datasets(dataset_names, kind="main")
    jobs = [("M1", 0, 0), ("M2", 0, 0)]
    jobs.extend((method, seed, 0) for seed in seeds for method in ("M3", "M4"))
    report = _run_matrix(
        plan, datasets, jobs, phase="main", resume=resume, dry_run=dry_run)
    if not dry_run:
        _atomic_json(_report_path(plan, "run-main"), report)
    return report


def _run_timing_warmups(plan: ExperimentPlan, datasets: Sequence[DatasetSpec],
                        *, dry_run: bool) -> Mapping[str, Any]:
    """Run one excluded, deterministic warm-up per method and dataset."""
    if not dry_run:
        _assert_reproduction_gate(plan)
    rows: list[Mapping[str, Any]] = []
    for dataset in datasets:
        canonical = min(
            plan.load_suite(dataset),
            key=lambda item: (item.gates_1q + item.gates_2q,
                              item.canonical_sha256),
        )
        for method in METHODS:
            spec = _attempt_spec(
                plan, dataset, canonical, method, 0, 0, "smoke")
            row = {
                "dataset": dataset.name,
                "circuit": Path(canonical.canonical_path).stem,
                "method": method,
                "command": list(spec.command),
            }
            if not dry_run:
                gate = UnifiedEvaluationGate(plan, canonical, method)
                manifest = run_attempt(
                    spec, verifier=gate.verifier, scorer=gate.scorer)
                row = {**row, "status": manifest.status,
                       "manifest": str(Path(manifest.artifact_dir) / "manifest.json")}
            rows.append(row)
    return {"excluded_from_statistics": True, "attempts": rows}


def command_run_timing(plan: ExperimentPlan,
                       dataset_names: Sequence[str] | None,
                       *, dry_run: bool, resume: bool = False
                       ) -> Mapping[str, Any]:
    datasets = plan.select_datasets(dataset_names, kind="main")
    warmups = _run_timing_warmups(plan, datasets, dry_run=dry_run)
    jobs = [(method, 0, repetition)
            for method in METHODS for repetition in range(5)]
    random.Random(plan.bootstrap_seed).shuffle(jobs)
    timed = _run_matrix(
        plan, datasets, jobs, phase="timing", resume=resume, dry_run=dry_run)
    report = {
        "experiment_schema": 2,
        "phase": "timing",
        "schedule_seed": plan.bootstrap_seed,
        "serial_execution": True,
        "warmups": warmups,
        "timed": timed,
    }
    if not dry_run:
        _atomic_json(_report_path(plan, "run-timing"), report)
    return report


def command_run_ablation(plan: ExperimentPlan,
                         dataset_names: Sequence[str] | None,
                         variants: Sequence[str], seeds: Sequence[int], *,
                         resume: bool, dry_run: bool) -> Mapping[str, Any]:
    """Run the preregistered HPCA18/QMAP30 mechanism study.

    This path never edits or augments the frozen main M3/M4 payloads.  Each
    attempt consumes a separate, hashed ablation wrapper and records its variant
    in the RunManifest identity.
    """
    if sorted(set(seeds)) != [0, 1, 2, 3, 4]:
        raise ValueError("formal run-ablation requires seeds exactly 0,1,2,3,4")
    selected_variants = _parse_ablation_variants(list(variants))
    datasets = plan.select_datasets(dataset_names, kind="main")
    for seed in seeds:
        plan.validate_resolved_pair(seed)
    if not dry_run:
        _assert_reproduction_gate(plan)

    attempted: list[Mapping[str, Any]] = []
    skipped: list[Mapping[str, Any]] = []
    commands: list[Mapping[str, Any]] = []
    ignored_stale_existing: list[str] = []
    current_repository = (
        repository_snapshot(plan.repo_root)
        if resume and not dry_run else None)
    selections: dict[str, Any] = {}
    for dataset in datasets:
        cohort, selection = select_ablation_cohort(
            dataset.name, plan.load_suite(dataset))
        experiment_id = ablation_experiment_id(
            plan.experiment_id(dataset), dataset.name, cohort)
        selections[dataset.name] = {
            **selection,
            "experiment_id": experiment_id,
        }
        existing: set[tuple[Any, ...]] = set()
        if resume:
            root = plan.output_root / "runs" / "ablation" / dataset.name
            for path in _manifest_paths(root):
                manifest = load_run_manifest(path)
                if (current_repository is not None and
                        not _resume_manifest_is_current(
                            manifest, current_repository)):
                    ignored_stale_existing.append(str(path))
                    continue
                existing.add(_resume_key(manifest))
        config_paths = {
            (variant_name, seed): _resolved_ablation_config(
                plan, variant_name, seed)
            for variant_name in selected_variants for seed in seeds
        }
        for canonical in cohort:
            for variant_name in selected_variants:
                variant = registered_variant(variant_name)
                for seed in seeds:
                    spec = _attempt_spec(
                        plan, dataset, canonical, variant.base_method, seed, 0,
                        "ablation",
                        config_path=config_paths[(variant_name, seed)],
                        ablation_variant=variant_name,
                        experiment_id=experiment_id,
                    )
                    row = {
                        "dataset": dataset.name,
                        "circuit": spec.circuit,
                        "method": variant.base_method,
                        "ablation_variant": variant_name,
                        "seed": seed,
                        "repetition": 0,
                        "command": list(spec.command),
                        "config": str(spec.config_path),
                    }
                    if resume and _spec_key(spec) in existing:
                        skipped.append(row)
                        continue
                    if dry_run:
                        commands.append(row)
                        continue
                    gate = UnifiedEvaluationGate(
                        plan, canonical, variant.base_method)
                    manifest = run_attempt(
                        spec, verifier=gate.verifier, scorer=gate.scorer)
                    attempted.append({
                        **{key: row[key] for key in (
                            "dataset", "circuit", "method",
                            "ablation_variant", "seed", "repetition")},
                        "status": manifest.status,
                        "manifest": str(
                            Path(manifest.artifact_dir) / "manifest.json"),
                    })
    statuses = Counter(row["status"] for row in attempted)
    report = {
        "experiment_schema": 2,
        "phase": "ablation",
        "dry_run": dry_run,
        "seeds": list(seeds),
        "variants": selected_variants,
        "selection": selections,
        "planned_attempts": sum(
            row["selected"] * len(selected_variants) * len(seeds)
            for row in selections.values()),
        "attempted": attempted,
        "status_counts": dict(sorted(statuses.items())),
        "skipped_existing": skipped,
        "ignored_stale_existing": ignored_stale_existing,
        "commands": commands,
    }
    if not dry_run:
        _atomic_json(_report_path(plan, "run-ablation"), report)
    return report


def command_aggregate_ablation(plan: ExperimentPlan, dataset_name: str,
                               output: Path | None,
                               output_dir: Path | None = None
                               ) -> Mapping[str, Any]:
    _assert_reproduction_gate(plan)
    if dataset_name not in plan.datasets:
        raise ValueError(f"unknown dataset: {dataset_name}")
    dataset = plan.datasets[dataset_name]
    if dataset.kind != "main":
        raise ValueError("ablation aggregation accepts only main datasets")
    cohort, selection = select_ablation_cohort(
        dataset.name, plan.load_suite(dataset))
    experiment_id = ablation_experiment_id(
        plan.experiment_id(dataset), dataset.name, cohort)
    root = plan.output_root / "runs" / "ablation" / dataset.name
    paths = _manifest_paths(root)
    if not paths:
        raise ValueError(f"no Schema-2 ablation manifests found under {root}")
    report = aggregate_ablation(
        paths,
        dataset=dataset.name,
        frozen_circuits=[Path(row.canonical_path).stem for row in cohort],
        experiment_id=experiment_id,
        expected_variants=list(ABLATION_VARIANTS),
    )
    report = {**report, "selection": selection}
    destination = (output.resolve() if output is not None else
                   _report_path(plan, f"aggregate-ablation-{dataset.name}"))
    _atomic_json(destination, report)
    export_directory = (
        output_dir.resolve() if output_dir is not None
        else destination.with_suffix(""))
    delivery = export_ablation_report(report, export_directory)
    return {
        **report, "report_path": str(destination), "delivery": delivery,
    }


def command_run_large(plan: ExperimentPlan, dataset_names: Sequence[str] | None,
                      methods: Sequence[str], *, resume: bool,
                      dry_run: bool) -> Mapping[str, Any]:
    datasets = plan.select_datasets(dataset_names, kind="large")
    contract = LargeExperimentContract()
    contract.validate()
    if plan.large_rss_limit_bytes is None:
        raise ValueError("Large experiments require an explicit RSS limit")
    contract.validate_attempt_limits(
        rss_limit_bytes=plan.large_rss_limit_bytes,
        timeout_seconds=plan.large_timeout_seconds,
    )
    unknown_methods = sorted(set(methods) - set(contract.methods))
    if not methods or unknown_methods:
        raise ValueError(f"invalid Large methods: {unknown_methods or list(methods)}")

    # There are bounded-memory parsing, checkpointing, event-writing and
    # incremental-scoring primitives in ``streaming/``.  No compiler currently
    # drives those primitives end to end, however: method_driver still builds a
    # complete ZAC/QMAP result and UnifiedEvaluationGate normalises it only after
    # compilation.  Never silently fall back to that path for a formal Large run.
    if not dry_run:
        contract.require_streaming_execution()
        raise RuntimeError(
            "formal run-large has no streaming compiler adapter entrypoint"
        )

    if any(method in {"M3", "M4"} for method in methods):
        plan.validate_resolved_pair(0)

    planned_attempts: list[Mapping[str, Any]] = []
    for dataset in datasets:
        suite = plan.load_suite(dataset)
        names = {Path(item.canonical_path).stem for item in suite}
        if names != set(contract.circuits):
            raise ValueError(
                f"Large suite must contain exactly {list(contract.circuits)}; "
                f"missing={sorted(set(contract.circuits) - names)}, "
                f"extra={sorted(names - set(contract.circuits))}")
        for canonical in sorted(suite, key=lambda item: Path(item.canonical_path).stem):
            circuit, input_path = _canonical_identity(canonical)
            for method in methods:
                config_path = plan.resolved_config(method, 0)
                planned_attempts.append({
                    "dataset": dataset.name,
                    "circuit": circuit,
                    "method": method,
                    "seed": 0,
                    "repetition": 0,
                    "input": str(input_path),
                    "input_sha256": canonical.canonical_sha256,
                    "config": str(config_path),
                    "config_sha256": sha256_file(config_path),
                    # Deliberately not an executable method_driver command.
                    "execution_command": None,
                    "blocked": True,
                    "blocked_reason": "streaming_compiler_integrated=false",
                })

    return {
        "experiment_schema": 2,
        "phase": "large",
        "dry_run": True,
        "resume_requested": bool(resume),
        "streaming_compiler_integrated": contract.streaming_compiler_integrated,
        "support_claim_eligible": False,
        "execution_status": "blocked_not_integrated",
        "blocked_reason": (
            "bounded-memory compiler/event/scorer adapter is not implemented; "
            "standalone streaming primitives do not establish BWT end-to-end support"
        ),
        "large_contract_sha256": contract.sha256,
        "large_contract": contract.to_dict(),
        "planned_attempts": planned_attempts,
        # An empty commands list prevents this dry-run artifact from being
        # mistaken for an executable fallback schedule.
        "commands": [],
        "attempted": [],
        "status_counts": {},
        "skipped_existing": [],
    }


def _find_canonical(plan: ExperimentPlan, manifest: RunManifest
                    ) -> CanonicalCircuitManifest:
    if manifest.dataset not in plan.datasets:
        raise ValueError(f"run dataset is absent from plan: {manifest.dataset}")
    matches = [row for row in plan.load_suite(plan.datasets[manifest.dataset])
               if _canonical_identity(row)[0] == manifest.circuit]
    if len(matches) != 1:
        raise ValueError(
            f"expected one canonical record for {manifest.dataset}/{manifest.circuit}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _close_float(label: str, expected: float | int | None,
                 actual: float | int | None) -> None:
    if expected is None or actual is None:
        if expected != actual:
            raise ValueError(f"{label} differs: manifest={expected}, recomputed={actual}")
        return
    if not math.isclose(float(expected), float(actual), rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError(f"{label} differs: manifest={expected}, recomputed={actual}")


def command_verify_run(plan: ExperimentPlan, manifest_path: Path) -> Mapping[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = load_run_manifest(manifest_path, require_success_metrics=True)
    if manifest.status != RunStatus.SUCCESS.value:
        raise ValueError(f"only successful runs can be fully reverified: {manifest.status}")
    if manifest.method not in METHODS:
        raise ValueError(f"run method is not registered: {manifest.method}")
    canonical = _find_canonical(plan, manifest)
    _, canonical_path = _canonical_identity(canonical)
    if manifest.run_kind == "ablation":
        variant = registered_variant(manifest.ablation_variant)
        if manifest.method != variant.base_method:
            raise ValueError("ablation manifest method/variant mismatch")
        cohort, _ = select_ablation_cohort(
            manifest.dataset, plan.load_suite(plan.datasets[manifest.dataset]))
        selected_names = {Path(row.canonical_path).stem for row in cohort}
        if manifest.circuit not in selected_names:
            raise ValueError("ablation run circuit is outside the frozen cohort")
        expected_experiment_id = ablation_experiment_id(
            plan.experiment_id(plan.datasets[manifest.dataset]),
            manifest.dataset, cohort)
        if manifest.experiment_id != expected_experiment_id:
            raise ValueError("ablation experiment_id differs from frozen cohort")
        resolved_config = _resolved_ablation_config(
            plan, manifest.ablation_variant, manifest.seed)
    else:
        resolved_config = plan.resolved_config(manifest.method, manifest.seed)
    expected_hashes = {
        "input_sha256": sha256_file(canonical_path),
        "config_sha256": sha256_file(resolved_config),
        "architecture_sha256": sha256_file(plan.architecture_path),
        "model_sha256": sha256_file(plan.model_path),
    }
    for field, expected in expected_hashes.items():
        if getattr(manifest, field) != expected:
            raise ValueError(
                f"run provenance mismatch for {field}: "
                f"{getattr(manifest, field)} != {expected}"
            )
    gate = UnifiedEvaluationGate(
        plan, canonical, manifest.method, write_artifacts=False)
    result, metrics = gate.evaluate(manifest_path.parent)
    if manifest.fidelity_ood != result.ood:
        raise ValueError(
            "fidelity OOD flag differs: "
            f"manifest={manifest.fidelity_ood}, recomputed={result.ood}")
    _close_float("log_fidelity", manifest.log_fidelity, metrics["log_fidelity"])
    _close_float("fidelity", manifest.fidelity, metrics["fidelity"])
    _close_float(
        "exponential_sensitivity_log_fidelity",
        manifest.exponential_sensitivity_log_fidelity,
        metrics["exponential_sensitivity_log_fidelity"],
    )
    _close_float(
        "exponential_sensitivity_fidelity",
        manifest.exponential_sensitivity_fidelity,
        metrics["exponential_sensitivity_fidelity"],
    )
    if set(manifest.fidelity_components) != set(metrics["fidelity_components"]):
        raise ValueError("fidelity component keys differ from recomputed result")
    for name in sorted(manifest.fidelity_components):
        _close_float(
            f"fidelity_components.{name}",
            manifest.fidelity_components[name],
            metrics["fidelity_components"][name],
        )
    _close_float("move_batches", manifest.move_batches, metrics["move_batches"])
    _close_float("move_time_us", manifest.move_time_us, metrics["move_time_us"])
    _close_float("idle_exposures", manifest.idle_exposures, metrics["idle_exposures"])
    _close_float("duration_us", manifest.duration_us, metrics["duration_us"])
    for field in (
        "qubits", "expected_gates_1q", "expected_gates_2q",
        "observed_gates_1q", "observed_gates_2q", "ghost_hits",
    ):
        if getattr(manifest, field) != metrics[field]:
            raise ValueError(
                f"{field} differs: manifest={getattr(manifest, field)}, "
                f"recomputed={metrics[field]}")
    for field in ("expected_gate_ledger_sha256", "observed_gate_ledger_sha256"):
        if getattr(manifest, field) != metrics[field]:
            raise ValueError(f"{field} differs from recomputed gate ledger")
    return {
        "experiment_schema": 2,
        "manifest": str(manifest_path),
        "run_id": manifest.run_id,
        "verified": True,
        "gate_counts_match": True,
        "score_matches_manifest": True,
        "fidelity_ood": result.ood,
    }


def command_aggregate(plan: ExperimentPlan, dataset: str, phase: str,
                      output: Path | None, output_dir: Path | None = None
                      ) -> Mapping[str, Any]:
    _assert_reproduction_gate(plan)
    if dataset not in plan.datasets:
        raise ValueError(f"unknown dataset: {dataset}")
    phases = (("coverage", "main", "timing") if phase == "all" else (phase,))
    paths = [path for selected_phase in phases
             for path in _manifest_paths(
                 plan.output_root / "runs" / selected_phase / dataset)]
    if not paths:
        raise ValueError(
            f"no Schema-2 manifests found for {dataset} phases={list(phases)}")
    # Validate every successful record against the strict result contract before
    # passing the cohort to the statistical API.
    for path in paths:
        load_run_manifest(path, require_success_metrics=True)
    dataset_spec = plan.datasets[dataset]
    frozen_circuits = [Path(item.canonical_path).stem
                       for item in plan.load_suite(dataset_spec)]
    report = aggregate_experiment(
        paths,
        dataset=dataset,
        bootstrap_iterations=plan.bootstrap_iterations,
        bootstrap_seed=plan.bootstrap_seed,
        frozen_circuits=frozen_circuits,
        experiment_id=plan.experiment_id(dataset_spec),
        run_kind=None if phase == "all" else phase,
        timeout_seconds=(plan.large_timeout_seconds if phase == "large"
                         else plan.timeout_seconds),
    )
    destination = (output.resolve() if output is not None else
                   _report_path(plan, f"aggregate-{phase}-{dataset}"))
    _atomic_json(destination, report)
    export_directory = (
        output_dir.resolve() if output_dir is not None
        else destination.with_suffix("")
    )
    delivery = export_experiment_report(report, export_directory)
    return {
        **report, "report_path": str(destination),
        "delivery": delivery,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m experiments_v2",
        description="Schema-2 ZAC/ICCAD/NL/LK experiment orchestration",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    reproduce = subparsers.add_parser("reproduce-baselines")
    reproduce.add_argument("--plan", required=True, type=Path)

    canonicalize = subparsers.add_parser("canonicalize-suite")
    canonicalize.add_argument("--plan", required=True, type=Path)
    canonicalize.add_argument("--datasets", nargs="+")

    coverage = subparsers.add_parser("run-coverage")
    coverage.add_argument("--plan", required=True, type=Path)
    coverage.add_argument("--datasets", nargs="+")
    coverage.add_argument("--resume", action="store_true")
    coverage.add_argument("--dry-run", action="store_true")

    main_run = subparsers.add_parser("run-main")
    main_run.add_argument("--plan", required=True, type=Path)
    main_run.add_argument("--datasets", nargs="+")
    main_run.add_argument("--seeds", nargs="+", required=True)
    main_run.add_argument("--resume", action="store_true")
    main_run.add_argument("--dry-run", action="store_true")

    timing = subparsers.add_parser("run-timing")
    timing.add_argument("--plan", required=True, type=Path)
    timing.add_argument("--datasets", nargs="+")
    timing.add_argument("--resume", action="store_true")
    timing.add_argument("--dry-run", action="store_true")

    ablation = subparsers.add_parser("run-ablation")
    ablation.add_argument("--plan", required=True, type=Path)
    ablation.add_argument("--datasets", nargs="+")
    ablation.add_argument("--variants", nargs="+", default=["all"])
    ablation.add_argument("--seeds", nargs="+", default=["0,1,2,3,4"])
    ablation.add_argument("--resume", action="store_true")
    ablation.add_argument("--dry-run", action="store_true")

    aggregate_ablation_parser = subparsers.add_parser("aggregate-ablation")
    aggregate_ablation_parser.add_argument("--plan", required=True, type=Path)
    aggregate_ablation_parser.add_argument("--dataset", required=True)
    aggregate_ablation_parser.add_argument("--output", type=Path)
    aggregate_ablation_parser.add_argument(
        "--output-dir", type=Path,
        help=("ablation delivery directory; writes CSV/Markdown/LaTeX, "
              "verified XLSX, and PDF/SVG/PNG figures"))

    large = subparsers.add_parser("run-large")
    large.add_argument("--plan", required=True, type=Path)
    large.add_argument("--datasets", nargs="+")
    large.add_argument("--methods", nargs="+", required=True)
    large.add_argument("--resume", action="store_true")
    large.add_argument("--dry-run", action="store_true")

    verify = subparsers.add_parser("verify-run")
    verify.add_argument("--plan", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)

    aggregate = subparsers.add_parser("aggregate")
    aggregate.add_argument("--plan", required=True, type=Path)
    aggregate.add_argument("--dataset", required=True)
    aggregate.add_argument(
        "--phase", choices=("all", "coverage", "main", "timing", "large"),
        default="all")
    aggregate.add_argument(
        "--output", type=Path,
        help="optional legacy path for the aggregate JSON")
    aggregate.add_argument(
        "--output-dir", type=Path,
        help=("delivery directory; defaults to the JSON path without its suffix; "
              "writes CSV/Markdown/LaTeX, verified XLSX, and PDF/SVG/PNG figures"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        plan = load_experiment_plan(args.plan)
        if (args.command == "canonicalize-suite" and
                os.path.abspath(sys.executable) != os.path.abspath(plan.python) and
                os.environ.get("ZAC_CANONICAL_PYTHON_REEXEC") != "1"):
            # Canonicalization imports Qiskit in-process.  Relaunch only this
            # command in the frozen Qiskit-1.2.4 interpreter declared by the
            # plan instead of relying on the caller's shell environment.
            forwarded = list(sys.argv[1:] if argv is None else argv)
            environment = os.environ.copy()
            environment["ZAC_CANONICAL_PYTHON_REEXEC"] = "1"
            package_root = str(plan.package_root)
            existing = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                package_root if not existing else package_root + os.pathsep + existing)
            return subprocess.run(
                [plan.python, "-m", "experiments_v2", *forwarded],
                cwd=Path.cwd(), env=environment,
                check=False,
            ).returncode
        if args.command == "reproduce-baselines":
            result = command_reproduce_baselines(plan)
        elif args.command == "canonicalize-suite":
            result = command_canonicalize(plan, args.datasets)
        elif args.command == "run-coverage":
            result = command_run_coverage(
                plan, args.datasets, resume=args.resume, dry_run=args.dry_run)
        elif args.command == "run-main":
            result = command_run_main(
                plan, args.datasets, _parse_ints(args.seeds),
                resume=args.resume, dry_run=args.dry_run)
        elif args.command == "run-timing":
            result = command_run_timing(
                plan, args.datasets, resume=args.resume, dry_run=args.dry_run)
        elif args.command == "run-ablation":
            result = command_run_ablation(
                plan, args.datasets,
                _parse_ablation_variants(args.variants),
                _parse_ints(args.seeds),
                resume=args.resume, dry_run=args.dry_run)
        elif args.command == "aggregate-ablation":
            result = command_aggregate_ablation(
                plan, args.dataset, args.output, args.output_dir)
        elif args.command == "run-large":
            result = command_run_large(
                plan, args.datasets, _parse_methods(args.methods),
                resume=args.resume, dry_run=args.dry_run)
        elif args.command == "verify-run":
            result = command_verify_run(plan, args.manifest)
        elif args.command == "aggregate":
            result = command_aggregate(
                plan, args.dataset, args.phase, args.output,
                args.output_dir)
        else:  # pragma: no cover - argparse exhaustiveness guard
            parser.error(f"unknown command: {args.command}")
            return 2
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    _print_json(result)
    return 0


__all__ = [
    "UnifiedEvaluationGate", "build_parser", "command_aggregate",
    "command_aggregate_ablation",
    "command_canonicalize", "command_reproduce_baselines",
    "command_run_ablation", "command_run_coverage", "command_run_large",
    "command_run_main", "command_run_timing",
    "command_verify_run", "main",
]
