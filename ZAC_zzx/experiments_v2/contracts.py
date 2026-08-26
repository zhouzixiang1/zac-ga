"""Versioned, JSON-serialisable contracts for canonical inputs and runs."""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import math
import os
import platform
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .protocol import (BASELINE_METHODS, OURS_METHODS,
                       ghost_policy_for_method,
                       physicalization_policy_for_method,
                       trace_protocol_for_method)


SCHEMA_VERSION = 2


class RunStatus(str, enum.Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    OOM = "oom"
    COMPILER_ERROR = "compiler_error"
    VERIFIER_FAIL = "verifier_fail"
    SCORER_ERROR = "scorer_error"


def sha256_file(path: os.PathLike[str] | str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def stable_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _git_info(root: Path) -> tuple[str, bool]:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True,
            stderr=subprocess.DEVNULL).strip())
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True


def repository_snapshot(root: Path) -> Dict[str, Any]:
    """Return the immutable Git identity used by formal evidence gates.

    A missing repository is represented as ``commit=unknown, dirty=true`` so
    callers fail closed instead of silently treating an unversioned directory
    as a reproducible environment.
    """
    requested = Path(root).resolve()
    commit, dirty = _git_info(requested)
    branch = "unknown"
    actual_root = str(requested)
    if commit != "unknown":
        try:
            actual_root = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"], cwd=requested,
                text=True, stderr=subprocess.DEVNULL).strip()
            branch = subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=requested,
                text=True, stderr=subprocess.DEVNULL).strip() or "detached"
        except (OSError, subprocess.CalledProcessError):
            commit, dirty, branch = "unknown", True, "unknown"
    return {
        "root": actual_root,
        "commit": commit,
        "branch": branch,
        "dirty": dirty,
    }


def machine_snapshot() -> Dict[str, Any]:
    memory_bytes: Optional[int] = None
    try:
        memory_bytes = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        pass
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "memory_bytes": memory_bytes,
        "python": sys.version.split()[0],
    }


@dataclass(frozen=True)
class CanonicalCircuitManifest:
    experiment_schema: int
    source_path: str
    canonical_path: str
    source_sha256: str
    canonical_sha256: str
    qiskit_version: str
    basis_gates: List[str]
    optimization_level: int
    seed_transpiler: int
    qubits: int
    gates_1q: int
    gates_2q: int
    depth: int
    removed_operations: Mapping[str, int] = field(default_factory=dict)
    canonical_profile: str = "main_qiskit_1_2_4_opt3"
    upstream_commit: str = ""
    canonicalizer_version: str = "qiskit-transpile-v1"

    def validate(self) -> None:
        if self.experiment_schema != SCHEMA_VERSION:
            raise ValueError(f"canonical schema must be {SCHEMA_VERSION}")
        if self.gates_1q < 0 or self.gates_2q < 0 or self.qubits <= 0:
            raise ValueError("invalid canonical circuit counts")
        if tuple(self.basis_gates) != ("cz", "u1", "u2", "u3"):
            raise ValueError("canonical basis must be cz,u1,u2,u3")
        if self.canonical_profile == "main_qiskit_1_2_4_opt3":
            if (self.qiskit_version != "1.2.4" or
                    self.canonicalizer_version != "qiskit-transpile-v1"):
                raise ValueError("canonical main input requires Qiskit 1.2.4")
            if self.optimization_level != 3 or self.seed_transpiler != 0:
                raise ValueError(
                    "canonical main input requires optimization_level=3, seed=0")
            if self.upstream_commit:
                raise ValueError("main canonical input may not claim a Large upstream commit")
        elif self.canonical_profile == "large_qasmbench_expand_only":
            if (self.qiskit_version != "not-used" or
                    self.canonicalizer_version != "qasm2-stream-expander-v1"):
                raise ValueError("Large input requires the frozen streaming expander")
            if self.optimization_level != 0 or self.seed_transpiler != 0:
                raise ValueError(
                    "Large canonical input requires expansion-only level=0, seed=0")
            if self.upstream_commit != "357b942396d5c2b7cbc1c229c585a6ef5ccaebac":
                raise ValueError("Large canonical input has the wrong QASMBench commit")
        else:
            raise ValueError(f"unknown canonical profile: {self.canonical_profile}")
        for label, value in (("source", self.source_sha256),
                             ("canonical", self.canonical_sha256)):
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"invalid {label} SHA256")

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class RunManifest:
    experiment_schema: int = SCHEMA_VERSION
    run_id: str = ""
    dataset: str = ""
    circuit: str = ""
    method: str = ""
    seed: int = 0
    repetition: int = 0
    run_kind: str = ""
    ablation_variant: str = ""
    experiment_id: str = ""
    status: str = RunStatus.COMPILER_ERROR.value
    git_commit: str = "unknown"
    git_dirty: bool = True
    package_versions: Dict[str, str] = field(default_factory=dict)
    machine: Dict[str, Any] = field(default_factory=machine_snapshot)
    algorithm_revision: str = ""
    backend: str = ""
    native_abi_version: Optional[int] = None
    native_wheel_sha256: str = ""
    compiler_and_flags: Dict[str, Any] = field(default_factory=dict)
    tuning_protocol_id: str = ""
    rng_version: str = ""
    input_sha256: str = ""
    config_sha256: str = ""
    architecture_sha256: str = ""
    model_sha256: str = ""
    command: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    timeout_seconds: Optional[float] = None
    started_at_utc: str = ""
    ended_at_utc: str = ""
    compiler_time_ns: Optional[int] = None
    compiler_process_wall_ns: Optional[int] = None
    end_to_end_time_ns: Optional[int] = None
    cpu_time_ns: Optional[int] = None
    peak_rss_bytes: Optional[int] = None
    transition_decision_ns: Optional[int] = None
    search_kernel_ns: Optional[int] = None
    problem_preparation_ns: Optional[int] = None
    result_commit_ns: Optional[int] = None
    marshal_ns: Optional[int] = None
    python_marshal_ns: Optional[int] = None
    native_call_wall_ns: Optional[int] = None
    native_search_wall_ns: Optional[int] = None
    fitness_ns: Optional[int] = None
    normalize_ns: Optional[int] = None
    decode_ns: Optional[int] = None
    return_match_ns: Optional[int] = None
    forecast_ns: Optional[int] = None
    selection_ns: Optional[int] = None
    native_parse_ns: Optional[int] = None
    native_serialize_ns: Optional[int] = None
    horizon_selection_ns: Optional[int] = None
    initial_placement_ns: Optional[int] = None
    routing_ns: Optional[int] = None
    full_compile_ns: Optional[int] = None
    # ``layer_ledger_sha256``/``transition_count`` are retained as the
    # canonical-input aliases used by legacy Schema-2 diagnostics.  Formal
    # timing evidence uses the explicitly separated fields below.
    layer_ledger_sha256: str = ""
    transition_count: Optional[int] = None
    canonical_input_layer_ledger_sha256: str = ""
    canonical_input_transition_count: Optional[int] = None
    observed_transition_layer_ledger_sha256: str = ""
    observed_transition_count: Optional[int] = None
    observed_transition_layer_ledger_source: str = ""
    # Legacy discrete H=0/1/2 regression evidence only.  Formal decay runs use
    # forecast_summary so configured/effective/visible depth cannot be conflated.
    selected_horizon_counts: Dict[str, int] = field(default_factory=dict)
    forecast_summary: Dict[str, Any] = field(default_factory=dict)
    log_fidelity: Optional[float] = None
    fidelity: Optional[float] = None
    fidelity_components: Dict[str, Optional[float]] = field(default_factory=dict)
    fidelity_ood: bool = False
    exponential_sensitivity_fidelity: Optional[float] = None
    exponential_sensitivity_log_fidelity: Optional[float] = None
    duration_us: Optional[float] = None
    qubits: Optional[int] = None
    expected_gates_1q: Optional[int] = None
    expected_gates_2q: Optional[int] = None
    observed_gates_1q: Optional[int] = None
    observed_gates_2q: Optional[int] = None
    expected_gate_ledger_sha256: str = ""
    observed_gate_ledger_sha256: str = ""
    move_batches: Optional[int] = None
    move_time_us: Optional[float] = None
    stay_count: Optional[int] = None
    return_count: Optional[int] = None
    reseat_count: Optional[int] = None
    idle_exposures: Optional[int] = None
    ghost_repairs: Optional[int] = None
    ghost_splits: Optional[int] = None
    ghost_hits: Optional[int] = None
    trace_protocol: str = ""
    ghost_policy: str = ""
    physicalization_policy: str = ""
    verifier_ok: Optional[bool] = None
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    stdout_path: str = "stdout.log"
    stderr_path: str = "stderr.log"
    artifact_dir: str = ""

    @classmethod
    def with_provenance(cls, repo_root: Path, **kwargs: Any) -> "RunManifest":
        commit, dirty = _git_info(repo_root)
        return cls(git_commit=commit, git_dirty=dirty, **kwargs)

    def validate(self, *, require_success_metrics: bool = False) -> None:
        if self.experiment_schema != SCHEMA_VERSION:
            raise ValueError(f"run schema must be {SCHEMA_VERSION}")
        allowed = {item.value for item in RunStatus}
        if self.status not in allowed:
            raise ValueError(f"unknown run status: {self.status}")
        if not self.run_id or not self.dataset or not self.circuit or not self.method:
            raise ValueError("run identity fields may not be empty")
        if self.run_kind and self.run_kind not in {"coverage", "main", "timing", "ablation", "large", "smoke"}:
            raise ValueError(f"unknown run_kind: {self.run_kind}")
        if self.run_kind in {"coverage", "main", "timing", "ablation", "large"}:
            if not self.experiment_id:
                raise ValueError("formal run is missing its frozen experiment_id")
        if self.run_kind == "ablation":
            if not self.ablation_variant:
                raise ValueError("ablation run is missing ablation_variant")
        elif self.ablation_variant:
            raise ValueError("ablation_variant is forbidden outside ablation runs")
        if self.method in BASELINE_METHODS | OURS_METHODS and (
                self.run_kind in {"coverage", "main", "timing", "ablation", "large"}
                or self.trace_protocol or self.ghost_policy
                or self.physicalization_policy):
            expected_trace = trace_protocol_for_method(self.method)
            expected_ghost = ghost_policy_for_method(self.method)
            expected_physicalization = physicalization_policy_for_method(self.method)
            if self.trace_protocol != expected_trace:
                raise ValueError(
                    f"{self.method} requires trace_protocol={expected_trace}")
            if self.ghost_policy != expected_ghost:
                raise ValueError(
                    f"{self.method} requires ghost_policy={expected_ghost}")
            if self.physicalization_policy != expected_physicalization:
                raise ValueError(
                    f"{self.method} requires physicalization_policy="
                    f"{expected_physicalization}")
        if self.backend == "native":
            if (not isinstance(self.native_abi_version, int)
                    or isinstance(self.native_abi_version, bool)
                    or self.native_abi_version <= 0):
                raise ValueError("native backend requires a positive native_abi_version")
            if (len(self.native_wheel_sha256) != 64 or any(
                    ch not in "0123456789abcdef"
                    for ch in self.native_wheel_sha256)):
                raise ValueError("native backend requires native_wheel_sha256")
            if not self.compiler_and_flags:
                raise ValueError("native backend requires compiler_and_flags")
            if not self.rng_version:
                raise ValueError("native backend requires rng_version")
            required_flags = {
                "cxx_standard": 17,
                "openmp": False,
                "fast_math": False,
            }
            flag_drift = {
                key: (self.compiler_and_flags.get(key), expected)
                for key, expected in required_flags.items()
                if self.compiler_and_flags.get(key) != expected
            }
            if flag_drift:
                raise ValueError(
                    f"native backend compiler flags are not frozen: {flag_drift}")
        for label, value in (
                ("layer_ledger_sha256", self.layer_ledger_sha256),
                ("canonical_input_layer_ledger_sha256",
                 self.canonical_input_layer_ledger_sha256),
                ("observed_transition_layer_ledger_sha256",
                 self.observed_transition_layer_ledger_sha256)):
            if value and (len(value) != 64 or any(
                    ch not in "0123456789abcdef" for ch in value)):
                raise ValueError(f"{label} must be a lowercase SHA256")
        for label, value in (
                ("transition_count", self.transition_count),
                ("canonical_input_transition_count",
                 self.canonical_input_transition_count),
                ("observed_transition_count", self.observed_transition_count)):
            if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int)
                    or value < 0):
                raise ValueError(f"{label} must be a non-negative integer")
        if (self.layer_ledger_sha256 and
                self.canonical_input_layer_ledger_sha256 and
                self.layer_ledger_sha256 !=
                self.canonical_input_layer_ledger_sha256):
            raise ValueError(
                "legacy/canonical input layer-ledger aliases disagree")
        if (self.transition_count is not None and
                self.canonical_input_transition_count is not None and
                self.transition_count != self.canonical_input_transition_count):
            raise ValueError(
                "legacy/canonical input transition-count aliases disagree")
        canonical_evidence = (
            bool(self.canonical_input_layer_ledger_sha256),
            self.canonical_input_transition_count is not None,
        )
        if any(canonical_evidence) and not all(canonical_evidence):
            raise ValueError(
                "canonical input transition-layer ledger evidence is partial")
        observed_evidence = (
            bool(self.observed_transition_layer_ledger_sha256),
            self.observed_transition_count is not None,
            bool(self.observed_transition_layer_ledger_source),
        )
        if any(observed_evidence) and not all(observed_evidence):
            raise ValueError(
                "observed transition-layer ledger evidence is partial")
        for label, value in self.selected_horizon_counts.items():
            try:
                if int(label) < 0 or str(int(label)) != str(label):
                    raise ValueError
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"invalid selected horizon bucket: {label!r}") from error
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid selected horizon count: {label}={value!r}")
        if self.forecast_summary:
            required_forecast = {
                "mode", "policy", "decay", "configured_depth",
                "effective_depth_counts", "visible_depth_counts", "rho",
                "epsilon", "alpha_lookahead", "offset_weights",
                "weighted_negative_log_fidelity_total", "transition_count",
            }
            missing = required_forecast - set(self.forecast_summary)
            if missing:
                raise ValueError(
                    f"forecast_summary missing fields: {sorted(missing)}")
            if self.forecast_summary["mode"] != "decay":
                raise ValueError("forecast_summary mode must be decay")
            for key in ("configured_depth", "transition_count"):
                value = self.forecast_summary[key]
                if (isinstance(value, bool) or not isinstance(value, int)
                        or value < 0):
                    raise ValueError(f"invalid forecast_summary {key}")
            for key in ("effective_depth_counts", "visible_depth_counts"):
                counts = self.forecast_summary[key]
                if not isinstance(counts, Mapping):
                    raise ValueError(f"forecast_summary {key} must be an object")
                for depth, count in counts.items():
                    if (not str(depth).isdigit() or isinstance(count, bool)
                            or not isinstance(count, int) or count < 0):
                        raise ValueError(f"invalid forecast_summary {key}")
        if require_success_metrics and self.status == RunStatus.SUCCESS.value:
            required = (self.move_batches, self.move_time_us,
                        self.compiler_time_ns, self.duration_us, self.qubits,
                        self.expected_gates_1q, self.expected_gates_2q,
                        self.observed_gates_1q, self.observed_gates_2q,
                        self.ghost_repairs, self.ghost_splits, self.ghost_hits)
            if any(item is None for item in required):
                raise ValueError("successful run is missing a primary metric")
            if self.verifier_ok is not True:
                raise ValueError("successful run must pass method-policy verification")
            if ((self.expected_gates_1q, self.expected_gates_2q) !=
                    (self.observed_gates_1q, self.observed_gates_2q)):
                raise ValueError("successful run has a gate-count mismatch")
            if (not self.expected_gate_ledger_sha256 or
                    self.expected_gate_ledger_sha256 != self.observed_gate_ledger_sha256):
                raise ValueError("successful run has a logical gate-ledger mismatch")
            if self.method in OURS_METHODS and self.ghost_hits != 0:
                raise ValueError("successful M3/M4 run must have ghost_hits=0")
            if self.method in BASELINE_METHODS and any(
                    value not in (None, 0)
                    for value in (self.ghost_repairs, self.ghost_splits)):
                raise ValueError(
                    "successful M1/M2 run may not contain paper-external ghost repair")
            required_components = {
                "log_one_qubit_gate", "log_two_qubit_gate",
                "log_idle_excitation", "log_atom_transfer",
                "log_coherence_linear",
            }
            if not required_components.issubset(self.fidelity_components):
                raise ValueError("successful run is missing fidelity decomposition")
            if self.fidelity_ood:
                if self.log_fidelity is not None or self.fidelity is not None:
                    raise ValueError("OOD result must not invent a linear fidelity")
                if self.exponential_sensitivity_log_fidelity is None:
                    raise ValueError("OOD result is missing exponential sensitivity")
                if self.fidelity_components["log_coherence_linear"] is not None:
                    raise ValueError(
                        "OOD result must record undefined linear coherence as null")
            else:
                if self.log_fidelity is None or self.fidelity is None:
                    raise ValueError("in-domain success is missing fidelity")
                if self.fidelity_components["log_coherence_linear"] is None:
                    raise ValueError("in-domain success has null linear coherence")
            numeric_values = {
                "move_time_us": self.move_time_us,
                "duration_us": self.duration_us,
                "compiler_time_ns": self.compiler_time_ns,
                "cpu_time_ns": self.cpu_time_ns,
                "log_fidelity": self.log_fidelity,
                "fidelity": self.fidelity,
                "exponential_sensitivity_log_fidelity":
                    self.exponential_sensitivity_log_fidelity,
                "exponential_sensitivity_fidelity":
                    self.exponential_sensitivity_fidelity,
            }
            numeric_values.update({
                "transition_decision_ns": self.transition_decision_ns,
                "search_kernel_ns": self.search_kernel_ns,
                "problem_preparation_ns": self.problem_preparation_ns,
                "result_commit_ns": self.result_commit_ns,
                "marshal_ns": self.marshal_ns,
                "python_marshal_ns": self.python_marshal_ns,
                "native_call_wall_ns": self.native_call_wall_ns,
                "native_search_wall_ns": self.native_search_wall_ns,
                "fitness_ns": self.fitness_ns,
                "normalize_ns": self.normalize_ns,
                "decode_ns": self.decode_ns,
                "return_match_ns": self.return_match_ns,
                "forecast_ns": self.forecast_ns,
                "selection_ns": self.selection_ns,
                "native_parse_ns": self.native_parse_ns,
                "native_serialize_ns": self.native_serialize_ns,
                "horizon_selection_ns": self.horizon_selection_ns,
                "initial_placement_ns": self.initial_placement_ns,
                "routing_ns": self.routing_ns,
                "full_compile_ns": self.full_compile_ns,
            })
            numeric_values.update({
                f"fidelity_components.{key}": value
                for key, value in self.fidelity_components.items()
            })
            for label, value in numeric_values.items():
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"successful run has non-numeric {label}")
                if not math.isfinite(float(value)):
                    raise ValueError(f"successful run has non-finite {label}")
            integer_counts = {
                "move_batches": self.move_batches,
                "qubits": self.qubits,
                "expected_gates_1q": self.expected_gates_1q,
                "expected_gates_2q": self.expected_gates_2q,
                "observed_gates_1q": self.observed_gates_1q,
                "observed_gates_2q": self.observed_gates_2q,
                "ghost_repairs": self.ghost_repairs,
                "ghost_splits": self.ghost_splits,
                "ghost_hits": self.ghost_hits,
            }
            for label, value in integer_counts.items():
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"successful run has invalid {label}")
            if any(float(value) < 0 for value in (
                    self.move_time_us, self.duration_us, self.compiler_time_ns)):
                raise ValueError("successful run has a negative time metric")
            if not self.fidelity_ood:
                assert self.log_fidelity is not None and self.fidelity is not None
                if self.log_fidelity > 0 or not (0 <= self.fidelity <= 1):
                    raise ValueError("successful run has an invalid fidelity range")
                component_sum = math.fsum(
                    float(self.fidelity_components[key]) for key in (
                        "log_one_qubit_gate", "log_two_qubit_gate",
                        "log_idle_excitation", "log_atom_transfer",
                        "log_coherence_linear",
                    )
                )
                if not math.isclose(component_sum, self.log_fidelity,
                                    rel_tol=0.0, abs_tol=1e-12):
                    raise ValueError("log fidelity decomposition does not sum to log_fidelity")
                expected_fidelity = math.exp(self.log_fidelity)
                if not math.isclose(expected_fidelity, self.fidelity,
                                    rel_tol=1e-12, abs_tol=1e-300):
                    raise ValueError("fidelity does not match exp(log_fidelity)")

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def write(self, path: os.PathLike[str] | str) -> None:
        self.validate()
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)


def load_run_manifest(path: os.PathLike[str] | str,
                      *, require_success_metrics: bool = False) -> RunManifest:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("experiment_schema") != SCHEMA_VERSION:
        raise ValueError(f"refusing non-Schema-2 manifest: {path}")
    known = {field.name for field in dataclasses.fields(RunManifest)}
    unknown = set(payload) - known
    if unknown:
        raise ValueError(f"unknown RunManifest fields in {path}: {sorted(unknown)}")
    manifest = RunManifest(**payload)
    manifest.validate(require_success_metrics=require_success_metrics)
    return manifest


__all__ = [
    "CanonicalCircuitManifest", "RunManifest", "RunStatus", "SCHEMA_VERSION",
    "load_run_manifest", "machine_snapshot", "repository_snapshot",
    "sha256_file", "stable_sha256",
]
