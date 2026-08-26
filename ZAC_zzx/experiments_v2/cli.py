"""Schema-2 command line orchestration for the four-method experiment.

No command discovers legacy score files.  Formal compiler attempts are created
from a frozen plan and are promoted to ``success`` only after independent trace
normalisation, unified physical scoring, and exact canonical gate-ledger checks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import replace
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
                       FORMAL_QUALITY_SEEDS, FORMAL_TIMING_REPETITIONS,
                       physicalization_policy_for_method,
                       trace_protocol_for_method)
from .reproduction import reproduce_baselines
from .runner import AttemptSpec, run_attempt
from .runtime_benchmark import (build_balanced_schedule,
                                ordered_two_qubit_layer_ledger,
                                two_qubit_layer_ledger,
                                validate_balanced_schedule)
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


def _canonical_layer_ledger(manifest: CanonicalCircuitManifest) -> Mapping[str, Any]:
    pairs: list[tuple[int, int]] = []
    for line in Path(manifest.canonical_path).read_text(
            encoding="utf-8").splitlines():
        match = _CANONICAL_CZ.match(line)
        if match:
            pairs.append((int(match.group(1)), int(match.group(2))))
    if len(pairs) != manifest.gates_2q:
        raise ValueError(
            f"canonical CZ ledger mismatch: manifest={manifest.gates_2q}, "
            f"parsed={len(pairs)}")
    return two_qubit_layer_ledger(manifest.qubits, pairs)


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
                 method: str, *, write_artifacts: bool = True,
                 raw_trace_name: str | None = None):
        self.plan = plan
        self.canonical = canonical
        self.method = method
        self.write_artifacts = write_artifacts
        allowed_override = "trace.na.raw" if method == "M2" else None
        if raw_trace_name is not None and raw_trace_name != allowed_override:
            raise ValueError(
                f"{method} cannot be evaluated from raw trace "
                f"{raw_trace_name!r}; expected {allowed_override!r}"
            )
        self._raw_trace_name = raw_trace_name
        self._artifact: Path | None = None
        self._result: FidelityResult | None = None
        self._metrics: Mapping[str, Any] | None = None
        self._observed_ledger: tuple[Counter[int], dict[int, tuple[str, ...]]] | None = None
        self._observed_transition_layers: list[list[tuple[int, int]]] | None = None
        self._physical_validation: Mapping[str, Any] | None = None

    @property
    def trace_name(self) -> str:
        if self._raw_trace_name is not None:
            return self._raw_trace_name
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
        transition_layers: list[list[tuple[int, int]]] = []

        def audited_events():
            for event in events:
                if event.kind == "one_qubit_gate":
                    one_qubit.update(event.atoms)
                    for qubit in event.atoms:
                        operation_sequence.setdefault(qubit, []).append("1q")
                elif event.kind == "two_qubit_gate":
                    transition_layers.append([
                        (int(q0), int(q1)) for q0, q1 in event.gate_pairs
                    ])
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
            self._observed_transition_layers = transition_layers
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
            self._observed_transition_layers = transition_layers
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
        compact_summary = payload.get("decision_summary", {})
        if (payload.get("decision_log_compacted") is True
                and isinstance(compact_summary, Mapping)):
            for key in ("stay_count", "return_count", "reseat_count",
                        "ghost_repairs"):
                value = compact_summary.get(key, 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    counts[key] = int(value)
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
        if not payload.get("decision_log_compacted"):
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
        if self.method == "M2":
            if self._observed_transition_layers is None:
                raise RuntimeError(
                    "QMAP placement trace was not consumed before ledger scoring")
            # Preserve the native CZ pulse boundaries instead of rebuilding an
            # ASAP schedule from the flattened gate order.
            observed = ordered_two_qubit_layer_ledger(
                self.canonical.qubits, self._observed_transition_layers)
            metrics.update({
                "observed_transition_layer_ledger_sha256":
                    observed["layer_ledger_sha256"],
                "observed_transition_count": observed["transitions"],
                "observed_transition_layer_ledger_source":
                    "normalized_qmap_placement_trace",
            })
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


def _compiler_python(plan: ExperimentPlan, method: str, phase: str) -> str:
    if method == "M2" and phase == "timing":
        # The timing-only QMAP wheel lives under the existing artifact root and
        # is trace-hash-equivalent to the official 3.2.0 package.  Quality runs
        # continue to use plan.methods[M2].python unchanged.
        return str(
            Path(plan.repo_root).resolve().parent / "artifacts" /
            "native-ga-v1" / "build" /
            "qmap-timing-venv" / "bin" / "python")
    return plan.methods[method].python


def _qmap_timing_package_evidence(plan: ExperimentPlan) -> Mapping[str, str]:
    """Bind a timing AttemptSpec to the frozen wheel/parity/lock artifacts.

    The compiler-side validator additionally compares the *installed* native
    binaries byte-for-byte with the wheel.  This parent-side check records the
    expected identities before the subprocess starts, preventing the official
    QMAP quality lock from being mislabeled as the timing environment.
    """
    freeze_path = (plan.package_root / "third_party" / "qmap32_streaming" /
                   "timing_freeze_manifest.json")
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    if (freeze.get("status") != "frozen"
            or freeze.get("parity", {}).get(
                "all_native_trace_hashes_equal") is not True):
        raise RuntimeError("QMAP timing provenance is not frozen and parity-valid")
    base = freeze_path.parent
    declared = {
        "qmap_timing_wheel_sha256": freeze["build"]["wheel_sha256"],
        "qmap_timing_environment_lock_sha256":
            freeze["build"]["environment_lock_sha256"],
        "qmap_timing_parity_report_sha256":
            freeze["parity"]["report_sha256"],
        "qmap_timing_patch_sha256":
            freeze["instrumentation"]["patch_sha256"],
    }
    paths = {
        "qmap_timing_wheel_sha256":
            (base / freeze["build"]["wheel_path"]).resolve(),
        "qmap_timing_environment_lock_sha256":
            (base / freeze["build"]["environment_lock"]).resolve(),
        "qmap_timing_parity_report_sha256":
            (base / freeze["parity"]["report"]).resolve(),
        "qmap_timing_patch_sha256":
            (base / freeze["instrumentation"]["patch"]).resolve(),
    }
    for key, path in paths.items():
        if not path.is_file() or sha256_file(path) != declared[key]:
            raise RuntimeError(f"QMAP timing provenance drift: {key} at {path}")
    return {
        "qmap_timing_protocol": str(freeze["protocol"]),
        **{key: str(value) for key, value in declared.items()},
    }


def _qmap_timing_environment_lock(plan: ExperimentPlan) -> Path:
    freeze_path = (plan.package_root / "third_party" / "qmap32_streaming" /
                   "timing_freeze_manifest.json")
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    return (freeze_path.parent /
            freeze["build"]["environment_lock"]).resolve()


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
    layer_ledger = _canonical_layer_ledger(canonical)
    compiler_python = _compiler_python(plan, method, phase)
    command = [
        compiler_python,
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
    timing_m2 = method == "M2" and phase == "timing"
    lock_name = ENVIRONMENT_LOCKS[1 if method == "M2" else 0]
    lock_path = plan.path.parent / lock_name
    timing_evidence: Mapping[str, str] = {}
    if timing_m2:
        timing_evidence = _qmap_timing_package_evidence(plan)
        lock_path = _qmap_timing_environment_lock(plan)
    runtime_packages = {
        **plan.package_versions,
        **timing_evidence,
        "compiler_python_path": compiler_python,
        "compiler_environment": (
            "iccad_qmap320_timing_instrumented" if timing_m2 else
            "iccad_qmap320" if method == "M2" else "zac_qiskit124"),
    }
    if lock_path.is_file():
        runtime_packages["environment_lock_sha256"] = sha256_file(lock_path)
    if timing_m2 and runtime_packages.get("environment_lock_sha256") != \
            runtime_packages["qmap_timing_environment_lock_sha256"]:
        raise RuntimeError(
            "QMAP timing AttemptSpec environment lock differs from freeze")
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
        layer_ledger_sha256=str(layer_ledger["layer_ledger_sha256"]),
        transition_count=int(layer_ledger["transitions"]),
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


def _manifest_path(manifest: RunManifest) -> Path:
    """Return and verify the one canonical path owned by ``manifest``."""
    path = (Path(manifest.artifact_dir) / "manifest.json").resolve()
    if not path.is_file():
        raise FileNotFoundError(
            f"run manifest disappeared from its artifact directory: {path}")
    loaded = load_run_manifest(path)
    if loaded.run_id != manifest.run_id or loaded.to_dict() != manifest.to_dict():
        raise ValueError(f"returned run manifest differs from disk: {path}")
    return path


def _cohort_manifest_row(manifest: RunManifest, path: Path, *,
                         schedule_order: int | None = None,
                         schedule_sha256: str | None = None
                         ) -> Mapping[str, Any]:
    """Build the exact, hash-bound registry row consumed by final sealing."""
    resolved = path.resolve()
    expected = (Path(manifest.artifact_dir) / "manifest.json").resolve()
    if resolved != expected:
        raise ValueError(
            f"manifest path is outside its declared artifact directory: {resolved}")
    row: dict[str, Any] = {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "run_id": manifest.run_id,
        "status": manifest.status,
        "identity": [
            manifest.dataset, manifest.circuit, manifest.method,
            manifest.seed, manifest.repetition,
        ],
    }
    if schedule_order is not None:
        row["schedule_order"] = int(schedule_order)
    if schedule_sha256 is not None:
        row["schedule_sha256"] = str(schedule_sha256)
    return row


def _sorted_complete_cohort(
        registry: Mapping[tuple[Any, ...], tuple[RunManifest, Path]],
        expected_keys: Sequence[tuple[Any, ...]], *,
        timing_orders: Mapping[tuple[Any, ...], int] | None = None,
        schedule_sha256: str | None = None) -> list[Mapping[str, Any]]:
    """Close a completed formal cohort, rejecting gaps and ambiguity."""
    if len(expected_keys) != len(set(expected_keys)):
        raise ValueError("formal run plan contains duplicate attempt identities")
    expected = set(expected_keys)
    actual = set(registry)
    if actual != expected or len(registry) != len(expected_keys):
        raise RuntimeError(
            "formal cohort is incomplete or contains unexpected attempts: "
            f"expected={len(expected)}, actual={len(actual)}, "
            f"missing={len(expected - actual)}, extra={len(actual - expected)}")
    paths = [path.resolve() for _, path in registry.values()]
    if len(paths) != len(set(paths)):
        raise ValueError("formal cohort reuses one manifest path for multiple jobs")
    rows: list[Mapping[str, Any]] = []
    ordered = sorted(
        registry.items(),
        key=lambda item: (
            item[1][0].dataset, item[1][0].circuit,
            item[1][0].method, item[1][0].seed,
            item[1][0].repetition),
    )
    for key, (manifest, path) in ordered:
        rows.append(_cohort_manifest_row(
            manifest, path,
            schedule_order=(None if timing_orders is None
                            else timing_orders[key]),
            schedule_sha256=schedule_sha256))
    return rows


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


def _assert_formal_selection_gates(plan: ExperimentPlan) -> Mapping[str, Any]:
    """Require the audited initializer and tuning selections before a run.

    Imports are deliberately local: the selection runners use this module's
    attempt construction helpers, so importing them at module initialization
    would create a cycle.
    """
    from .initial_placement_runner import validate_initial_selection_for_plan
    from .quality_racing_runner import validate_quality_selection_for_plan

    initial_path = (
        plan.output_root / "initial-placement" / "selected_engine.json")
    tuning_path = (
        plan.output_root / "tuning-quality-v1" /
        "selected_config_manifest.json")
    initial = validate_initial_selection_for_plan(
        plan, initial_path, enforce_pre_tuning_core=False)
    tuning = validate_quality_selection_for_plan(plan, tuning_path)
    if (tuning.get("initial_selection_record_sha256") !=
            initial.get("record_sha256")):
        raise ValueError(
            "tuning selection was produced from a different initial-placement gate")
    return {
        "initial_selection": str(initial_path),
        "initial_selection_record_sha256": initial["record_sha256"],
        "initial_placement_engine": initial["selected_engine"],
        "tuning_selection": str(tuning_path),
        "tuning_selection_manifest_sha256": tuning["manifest_sha256"],
        "shared_candidate_id": tuning["shared_candidate_id"],
    }


def _run_matrix(plan: ExperimentPlan, datasets: Sequence[DatasetSpec],
                jobs: Sequence[tuple[str, int, int]], *, phase: str,
                resume: bool, dry_run: bool, workers: int = 1
                ) -> Mapping[str, Any]:
    if phase == "large":
        raise RuntimeError(
            "generic experiment matrix is forbidden for Large; "
            "use the fail-closed streaming run-large path"
        )
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if workers > 1 and phase != "main":
        raise ValueError("parallel workers are registered only for run-main")
    seeds = sorted({seed for method, seed, _ in jobs if method in ("M3", "M4")})
    for seed in seeds:
        plan.validate_resolved_pair(seed)
    if not dry_run:
        _assert_reproduction_gate(plan)

    existing: dict[tuple[Any, ...], tuple[RunManifest, Path]] = {}
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
                if manifest.run_kind != phase or manifest.ablation_variant:
                    raise ValueError(
                        f"current manifest is in the wrong formal run tree: {path}")
                key = _resume_key(manifest)
                if key in existing:
                    raise ValueError(
                        "multiple current manifests match one resume identity: "
                        f"{existing[key][1]} and {path}")
                existing[key] = (manifest, path.resolve())

    attempted: list[Mapping[str, Any]] = []
    skipped: list[Mapping[str, Any]] = []
    commands: list[Mapping[str, Any]] = []
    pending: list[tuple[AttemptSpec, CanonicalCircuitManifest,
                        tuple[Any, ...]]] = []
    registry: dict[tuple[Any, ...], tuple[RunManifest, Path]] = {}
    expected_keys: list[tuple[Any, ...]] = []
    for dataset in datasets:
        for canonical in plan.load_suite(dataset):
            for method, seed, repetition in jobs:
                spec = _attempt_spec(
                    plan, dataset, canonical, method, seed, repetition, phase)
                spec_key = _spec_key(spec)
                expected_keys.append(spec_key)
                command_row = {
                    "dataset": dataset.name,
                    "circuit": spec.circuit,
                    "method": method,
                    "seed": seed,
                    "repetition": repetition,
                    "command": list(spec.command),
                    "config": str(spec.config_path),
                }
                if resume and spec_key in existing:
                    manifest, manifest_path = existing[spec_key]
                    registry[spec_key] = (manifest, manifest_path)
                    skipped.append({
                        **command_row,
                        "status": manifest.status,
                        "manifest": str(manifest_path),
                        "manifest_sha256": sha256_file(manifest_path),
                    })
                    continue
                if dry_run:
                    commands.append(command_row)
                    continue
                pending.append((spec, canonical, spec_key))

    def execute(item: tuple[AttemptSpec, CanonicalCircuitManifest,
                            tuple[Any, ...]]
                ) -> tuple[tuple[Any, ...], RunManifest, Path]:
        spec, canonical, spec_key = item
        gate = UnifiedEvaluationGate(plan, canonical, spec.method)
        manifest = run_attempt(
            spec, verifier=gate.verifier, scorer=gate.scorer)
        manifest_path = _manifest_path(manifest)
        if _resume_key(manifest) != spec_key:
            raise ValueError(
                f"completed attempt identity differs from plan: {manifest_path}")
        return spec_key, manifest, manifest_path

    if workers == 1:
        completed = [execute(item) for item in pending]
    else:
        # Threads only orchestrate run_attempt.  Every compiler remains an
        # isolated child process group with its own UUID temporary directory
        # and atomic final rename.  executor.map preserves plan order even
        # when attempts finish out of order, keeping reports deterministic.
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="formal-run-main") as executor:
            completed = list(executor.map(execute, pending))

    for spec_key, manifest, manifest_path in completed:
        registry[spec_key] = (manifest, manifest_path)
        attempted.append({
            "dataset": manifest.dataset,
            "circuit": manifest.circuit,
            "method": manifest.method,
            "seed": manifest.seed,
            "repetition": manifest.repetition,
            "status": manifest.status,
            "manifest": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
        })
    statuses = Counter(row["status"] for row in attempted)
    cohort = ([] if dry_run else
              _sorted_complete_cohort(registry, expected_keys))
    cohort_statuses = Counter(row["status"] for row in cohort)
    return {
        "experiment_schema": 2,
        "phase": phase,
        "dry_run": dry_run,
        "workers": workers,
        "parallel_execution": workers > 1 and len(pending) > 1,
        "planned_jobs": len(expected_keys),
        "attempted": attempted,
        "status_counts": dict(sorted(statuses.items())),
        "cohort_status_counts": dict(sorted(cohort_statuses.items())),
        "cohort_manifests": cohort,
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
    selection = None if dry_run else _assert_formal_selection_gates(plan)
    datasets = plan.select_datasets(dataset_names, kind="main")
    jobs = [(method, 0, 0) for method in METHODS]
    report = _run_matrix(
        plan, datasets, jobs, phase="coverage", resume=resume, dry_run=dry_run)
    report = {**report, "formal_selection": selection}
    if not dry_run:
        _atomic_json(_report_path(plan, "run-coverage"), report)
    return report


def command_run_main(plan: ExperimentPlan, dataset_names: Sequence[str] | None,
                     seeds: Sequence[int], *, dry_run: bool,
                     resume: bool = False, workers: int = 1
                     ) -> Mapping[str, Any]:
    if tuple(sorted(seeds)) != FORMAL_QUALITY_SEEDS:
        rendered = ",".join(str(seed) for seed in FORMAL_QUALITY_SEEDS)
        raise ValueError(
            f"formal run-main requires paired seeds exactly {rendered}")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    selection = None if dry_run else _assert_formal_selection_gates(plan)
    datasets = plan.select_datasets(dataset_names, kind="main")
    jobs = [("M1", 0, 0), ("M2", 0, 0)]
    jobs.extend((method, seed, 0) for seed in seeds for method in ("M3", "M4"))
    report = _run_matrix(
        plan, datasets, jobs, phase="main", resume=resume, dry_run=dry_run,
        workers=workers)
    report = {**report, "formal_selection": selection}
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
                plan, dataset, canonical, method, 0, 0, "timing")
            spec = replace(
                spec,
                output_root=(plan.output_root / "runs" / "timing-warmup" /
                             dataset.name),
                run_kind="smoke",
            )
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


def _timing_schedule_path(plan: ExperimentPlan) -> Path:
    return plan.output_root / "timing" / "randomized_schedule.json"


def _timing_circuit_catalog(
        plan: ExperimentPlan, datasets: Sequence[DatasetSpec]
        ) -> dict[str, tuple[DatasetSpec, CanonicalCircuitManifest]]:
    catalog: dict[str, tuple[DatasetSpec, CanonicalCircuitManifest]] = {}
    for dataset in datasets:
        for canonical in plan.load_suite(dataset):
            circuit, _ = _canonical_identity(canonical)
            key = f"{dataset.name}/{circuit}"
            if key in catalog:
                raise ValueError(f"duplicate timing circuit identity: {key}")
            catalog[key] = (dataset, canonical)
    if not catalog:
        raise ValueError("timing schedule requires at least one circuit")
    return catalog


def _load_or_create_timing_schedule(
        plan: ExperimentPlan, circuit_keys: Sequence[str], *, persist: bool
        ) -> tuple[Mapping[str, Any], Path, bool]:
    """Load the one immutable schedule, or exclusively create it once."""
    path = _timing_schedule_path(plan)
    expected = build_balanced_schedule(
        circuit_keys, repetitions=FORMAL_TIMING_REPETITIONS,
        seed=plan.bootstrap_seed,
        methods=METHODS)
    if path.is_file():
        observed = json.loads(path.read_text(encoding="utf-8"))
        validate_balanced_schedule(
            observed, circuit_keys, repetitions=FORMAL_TIMING_REPETITIONS,
            seed=plan.bootstrap_seed,
            methods=METHODS)
        return observed, path, False
    if path.exists():
        raise ValueError(f"timing schedule path is not a file: {path}")
    if not persist:
        return expected, path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(expected, indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        observed = json.loads(path.read_text(encoding="utf-8"))
        validate_balanced_schedule(
            observed, circuit_keys, repetitions=FORMAL_TIMING_REPETITIONS,
            seed=plan.bootstrap_seed,
            methods=METHODS)
        return observed, path, False
    return expected, path, True


def _run_timing_schedule(
        plan: ExperimentPlan,
        datasets: Sequence[DatasetSpec],
        catalog: Mapping[str, tuple[DatasetSpec, CanonicalCircuitManifest]],
        schedule: Mapping[str, Any], *, resume: bool, dry_run: bool
        ) -> Mapping[str, Any]:
    """Consume the frozen schedule in order, skipping only sealed attempts."""
    plan.validate_resolved_pair(0)
    if not dry_run:
        _assert_reproduction_gate(plan)

    schedule_sha256 = str(schedule["sha256"])
    expected_orders = {
        (str(row["circuit"]), str(row["method"]), int(row["repetition"])):
            int(row["order"])
        for row in schedule["jobs"]
    }
    existing: dict[tuple[Any, ...], tuple[RunManifest, Path]] = {}
    ignored_stale_existing: list[str] = []
    ignored_schedule_mismatch_existing: list[str] = []
    current_repository = (
        repository_snapshot(plan.repo_root)
        if resume and not dry_run else None)
    if resume:
        for dataset in datasets:
            root = plan.output_root / "runs" / "timing" / dataset.name
            for path in _manifest_paths(root):
                manifest = load_run_manifest(path)
                if (current_repository is not None and
                        not _resume_manifest_is_current(
                            manifest, current_repository)):
                    ignored_stale_existing.append(str(path))
                    continue
                if manifest.run_kind != "timing" or manifest.ablation_variant:
                    raise ValueError(
                        f"current manifest is in the wrong timing run tree: {path}")
                circuit_key = f"{manifest.dataset}/{manifest.circuit}"
                expected_order = expected_orders.get((
                    circuit_key, manifest.method, manifest.repetition))
                recorded_sha = manifest.package_versions.get(
                    "timing_schedule_sha256")
                recorded_order = manifest.package_versions.get(
                    "timing_schedule_order")
                if (recorded_sha != schedule_sha256
                        or expected_order is None
                        or recorded_order != str(expected_order)):
                    ignored_schedule_mismatch_existing.append(str(path))
                    continue
                key = _resume_key(manifest)
                if key in existing:
                    raise ValueError(
                        "multiple current timing manifests match one resume "
                        f"identity: {existing[key][1]} and {path}")
                existing[key] = (manifest, path.resolve())

    attempted: list[Mapping[str, Any]] = []
    skipped: list[Mapping[str, Any]] = []
    commands: list[Mapping[str, Any]] = []
    registry: dict[tuple[Any, ...], tuple[RunManifest, Path]] = {}
    expected_keys: list[tuple[Any, ...]] = []
    timing_orders: dict[tuple[Any, ...], int] = {}
    for row in schedule["jobs"]:
        order = int(row["order"])
        circuit_key = str(row["circuit"])
        method = str(row["method"])
        repetition = int(row["repetition"])
        if circuit_key not in catalog:
            raise ValueError(
                f"timing schedule references an unknown circuit: {circuit_key}")
        dataset, canonical = catalog[circuit_key]
        spec = _attempt_spec(
            plan, dataset, canonical, method, 0, repetition, "timing")
        spec = replace(spec, package_versions={
            **spec.package_versions,
            "timing_schedule_protocol": str(schedule["protocol_id"]),
            "timing_schedule_sha256": schedule_sha256,
            "timing_schedule_order": str(order),
        })
        spec_key = _spec_key(spec)
        expected_keys.append(spec_key)
        if spec_key in timing_orders:
            raise ValueError(f"timing schedule repeats one attempt identity: {row}")
        timing_orders[spec_key] = order
        command_row = {
            "schedule_order": order,
            "schedule_sha256": schedule_sha256,
            "dataset": dataset.name,
            "circuit": spec.circuit,
            "method": method,
            "seed": 0,
            "repetition": repetition,
            "command": list(spec.command),
            "config": str(spec.config_path),
        }
        if resume and spec_key in existing:
            manifest, manifest_path = existing[spec_key]
            registry[spec_key] = (manifest, manifest_path)
            skipped.append({
                **command_row,
                "status": manifest.status,
                "manifest": str(manifest_path),
                "manifest_sha256": sha256_file(manifest_path),
            })
            continue
        if dry_run:
            commands.append(command_row)
            continue
        gate = UnifiedEvaluationGate(plan, canonical, method)
        manifest = run_attempt(
            spec, verifier=gate.verifier, scorer=gate.scorer)
        manifest_path = _manifest_path(manifest)
        if _resume_key(manifest) != spec_key:
            raise ValueError(
                f"completed timing identity differs from schedule: {manifest_path}")
        registry[spec_key] = (manifest, manifest_path)
        attempted.append({
            "schedule_order": order,
            "schedule_sha256": schedule_sha256,
            "dataset": manifest.dataset,
            "circuit": manifest.circuit,
            "method": manifest.method,
            "seed": manifest.seed,
            "repetition": manifest.repetition,
            "status": manifest.status,
            "manifest": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
        })
    statuses = Counter(row["status"] for row in attempted)
    cohort = ([] if dry_run else _sorted_complete_cohort(
        registry, expected_keys, timing_orders=timing_orders,
        schedule_sha256=schedule_sha256))
    cohort_statuses = Counter(row["status"] for row in cohort)
    return {
        "experiment_schema": 2,
        "phase": "timing",
        "dry_run": dry_run,
        "schedule_sha256": schedule_sha256,
        "scheduled_jobs": len(schedule["jobs"]),
        "planned_jobs": len(expected_keys),
        "attempted": attempted,
        "status_counts": dict(sorted(statuses.items())),
        "cohort_status_counts": dict(sorted(cohort_statuses.items())),
        "cohort_manifests": cohort,
        "skipped_existing": skipped,
        "ignored_stale_existing": ignored_stale_existing,
        "ignored_schedule_mismatch_existing":
            ignored_schedule_mismatch_existing,
        "commands": commands,
    }


def command_run_timing(plan: ExperimentPlan,
                       dataset_names: Sequence[str] | None,
                       *, dry_run: bool, resume: bool = False
                       ) -> Mapping[str, Any]:
    selection = None if dry_run else _assert_formal_selection_gates(plan)
    datasets = plan.select_datasets(dataset_names, kind="main")
    catalog = _timing_circuit_catalog(plan, datasets)
    schedule, schedule_path, schedule_created = \
        _load_or_create_timing_schedule(
            plan, sorted(catalog), persist=not dry_run)
    warmups = _run_timing_warmups(plan, datasets, dry_run=dry_run)
    timed = _run_timing_schedule(
        plan, datasets, catalog, schedule, resume=resume, dry_run=dry_run)
    report = {
        "experiment_schema": 2,
        "phase": "timing",
        "dry_run": dry_run,
        "schedule_seed": plan.bootstrap_seed,
        "schedule_path": str(schedule_path),
        "schedule_sha256": schedule["sha256"],
        "schedule_created": schedule_created,
        "schedule_jobs": len(schedule["jobs"]),
        "schedule_circuits": len(catalog),
        "serial_execution": True,
        "formal_selection": selection,
        "warmups": warmups,
        "timed": timed,
        # Duplicate the final timed cohort at the report root so the sealing
        # command has one uniform, explicit registry contract for main/timing.
        "planned_jobs": timed["planned_jobs"],
        "cohort_manifests": timed["cohort_manifests"],
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
    raw_trace_name = None
    if manifest.package_versions.get("baseline_replay_protocol") == \
            "paper-native-baseline-raw-replay-v1":
        if manifest.method == "M2":
            raw_trace_name = "trace.na.raw"
        elif manifest.method != "M1":
            raise ValueError(
                "baseline raw replay provenance is forbidden for M3/M4")
    gate = UnifiedEvaluationGate(
        plan, canonical, manifest.method, write_artifacts=False,
        raw_trace_name=raw_trace_name)
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


def command_import_baseline_raw(
        plan: ExperimentPlan, source_roots: Sequence[Path],
        dataset_names: Sequence[str] | None, methods: Sequence[str], *,
        audit_only: bool, output_root: Path | None = None,
        m1_lineage_manifest: Path | None = None
        ) -> Mapping[str, Any]:
    """Audit or replay uniquely matched M1/M2 compiler-authored raw traces."""
    from .baseline_raw_replay import (audit_baseline_raw_replay,
                                      import_baseline_raw_replays)

    selected_methods = (
        ("M1", "M2") if list(methods) == ["all"] else tuple(methods))
    if "M1" in selected_methods and m1_lineage_manifest is None:
        from .baseline_raw_replay import default_m1_lineage_manifest
        m1_lineage_manifest = default_m1_lineage_manifest(plan)
    if audit_only:
        return audit_baseline_raw_replay(
            plan, source_roots, dataset_names=dataset_names,
            methods=selected_methods,
            m1_lineage_manifest=m1_lineage_manifest)
    return import_baseline_raw_replays(
        plan, source_roots, dataset_names=dataset_names,
        methods=selected_methods, output_root=output_root,
        m1_lineage_manifest=m1_lineage_manifest)


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
    main_run.add_argument("--workers", type=int, default=1)
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

    replay = subparsers.add_parser("import-baseline-raw")
    replay.add_argument("--plan", required=True, type=Path)
    replay.add_argument("--source-roots", nargs="+", required=True, type=Path)
    replay.add_argument("--datasets", nargs="+")
    replay.add_argument(
        "--methods", nargs="+", default=["all"],
        choices=("all", "M1", "M2"))
    replay.add_argument("--audit-only", action="store_true")
    replay.add_argument("--output-root", type=Path)
    replay.add_argument(
        "--m1-lineage-manifest", type=Path,
        help=("single-root M1 source-lineage manifest; defaults to the "
              "registered tuning-quality-v1-51583a5 workspace manifest"))

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
                resume=args.resume, dry_run=args.dry_run,
                workers=args.workers)
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
        elif args.command == "import-baseline-raw":
            if "all" in args.methods and args.methods != ["all"]:
                raise ValueError("--methods all cannot be combined with M1/M2")
            result = command_import_baseline_raw(
                plan, args.source_roots, args.datasets, args.methods,
                audit_only=args.audit_only, output_root=args.output_root,
                m1_lineage_manifest=args.m1_lineage_manifest)
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
    "command_canonicalize", "command_import_baseline_raw",
    "command_reproduce_baselines",
    "command_run_ablation", "command_run_coverage", "command_run_large",
    "command_run_main", "command_run_timing",
    "command_verify_run", "main",
]
