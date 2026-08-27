"""Fail-closed QASMBench Small/Medium/Large run orchestration.

The official directory is the experiment identity.  Every compiler attempt
owns a UUID directory, terminal failures are first-class results, and no
attempt reads an output produced by a prior timeout.  M1/M2 use their original
batch method drivers at every scale.  Only Large M3/M4 use the exact SQLite
placement stream; the development streaming proxy is never imported.
"""

from __future__ import annotations

from collections import Counter
import concurrent.futures
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import time
from typing import Any, Mapping, Sequence
import uuid

from .contracts import (
    CanonicalCircuitManifest,
    FORBIDDEN_QASMBENCH_IMPLEMENTATION,
    QASMBENCH_CANONICAL_PROFILE,
    RunManifest,
    RunStatus,
    load_run_manifest,
    repository_snapshot,
    sha256_file,
    stable_sha256,
)
from .plan import ExperimentPlan
from .protocol import (
    ghost_policy_for_method,
    physicalization_policy_for_method,
    trace_protocol_for_method,
)
from .runner import (
    AttemptSpec,
    _process_group_rss,
    _terminate_group,
    run_attempt,
)
from streaming.large_contract import QASMBENCH_COMMIT


METHODS = ("M1", "M2", "M3", "M4")
SCALES = ("small", "medium", "large")
CONCURRENCY_BY_SCALE = {"small": 4, "medium": 2, "large": 1}
RSS_LIMIT_BYTES = 22 * (1 << 30)
MINIMUM_FREE_BYTES = 4 * (1 << 30)
RETRY_TIMEOUT_SECONDS = 6 * 60 * 60
EVENT_HASH_PROTOCOL = "sha256-uncompressed-canonical-jsonl-v1"
TERMINAL_STATUSES = frozenset(item.value for item in RunStatus)
QASMBENCH_RUN_PROTOCOL = "qasmbench-four-method-runner-v1"


@dataclass(frozen=True)
class QASMBenchSource:
    benchmark_scale: str
    benchmark_directory: str
    upstream_path: str
    upstream_git_blob: str
    source_sha256: str
    selection_reason: str
    canonical_profile: str
    status: str
    canonical_path: str = ""
    canonical_sha256: str = ""
    qubits: int | None = None
    gates_1q: int | None = None
    gates_2q: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "QASMBenchSource":
        allowed = {item.name for item in fields(cls)}
        row = {key: value[key] for key in allowed if key in value}
        source = cls(**row)
        if source.benchmark_scale not in SCALES:
            raise ValueError(
                f"invalid QASMBench scale: {source.benchmark_scale!r}")
        if not source.benchmark_directory:
            raise ValueError("QASMBench source lacks benchmark_directory")
        if source.status == "success":
            if source.canonical_profile != QASMBENCH_CANONICAL_PROFILE:
                raise ValueError("QASMBench canonical profile drift")
            path = Path(source.canonical_path).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"canonical QASM is missing: {path}")
            if sha256_file(path) != source.canonical_sha256:
                raise ValueError(f"canonical QASM hash mismatch: {path}")
            if any(value is None for value in (
                    source.qubits, source.gates_1q, source.gates_2q)):
                raise ValueError("successful QASMBench source lacks gate counts")
        elif source.status not in {"canonical_error", "no_qasm_source"}:
            raise ValueError(f"unknown QASMBench source status: {source.status}")
        return source

    def canonical_manifest(self) -> CanonicalCircuitManifest:
        if self.status != "success":
            raise ValueError("only successful sources have a canonical manifest")
        manifest = CanonicalCircuitManifest(
            experiment_schema=2,
            source_path=self.upstream_path,
            canonical_path=str(Path(self.canonical_path).resolve()),
            source_sha256=self.source_sha256,
            canonical_sha256=self.canonical_sha256,
            qiskit_version="not-used",
            basis_gates=["cz", "u1", "u2", "u3"],
            optimization_level=0,
            seed_transpiler=0,
            qubits=int(self.qubits),
            gates_1q=int(self.gates_1q),
            gates_2q=int(self.gates_2q),
            depth=0,
            canonical_profile=QASMBENCH_CANONICAL_PROFILE,
            upstream_commit=QASMBENCH_COMMIT,
            canonicalizer_version="qasmbench-standard-expander-v1",
        )
        manifest.validate()
        return manifest


@dataclass(frozen=True)
class QASMBenchJob:
    source: QASMBenchSource
    method: str
    seed: int
    repetition: int
    timeout_seconds: float
    experiment_id: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_qasmbench_sources(path: str | Path) -> list[QASMBenchSource]:
    manifest_path = Path(path).resolve()
    from .qasmbench_inputs import load_qasmbench_source_manifest

    inventory = load_qasmbench_source_manifest(manifest_path)
    rows = []
    for entry in inventory.entries:
        row = entry.to_dict()
        canonical_path = row.get("canonical_path")
        if canonical_path and not Path(str(canonical_path)).is_absolute():
            row["canonical_path"] = str(
                (manifest_path.parent / str(canonical_path)).resolve())
        rows.append(row)
    sources = [QASMBenchSource.from_mapping(row) for row in rows]
    identities = [
        (source.benchmark_scale, source.benchmark_directory)
        for source in sources
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("QASMBench source manifest repeats an official directory")
    counts = Counter(source.benchmark_scale for source in sources)
    expected = {"small": 42, "medium": 25, "large": 70}
    if counts != Counter(expected):
        raise ValueError(
            f"QASMBench inventory count mismatch: {dict(counts)} != {expected}")
    selected = sum(source.status != "no_qasm_source" for source in sources)
    no_source = sum(source.status == "no_qasm_source" for source in sources)
    if (selected, no_source) != (131, 6):
        raise ValueError(
            "QASMBench selected-source/terminal count mismatch: "
            f"{selected}/{no_source} != 131/6")
    return sorted(
        sources,
        key=lambda source: (
            SCALES.index(source.benchmark_scale), source.benchmark_directory),
    )


def timeout_seconds_for_two_qubit_gates(gates_2q: int) -> int:
    gates = int(gates_2q)
    if gates < 0:
        raise ValueError("canonical 2Q gate count cannot be negative")
    if gates <= 10_000:
        return 600
    if gates <= 250_000:
        return 1_800
    return 3_600


def _pythonpath(plan: ExperimentPlan) -> str:
    values = [str(plan.package_root)]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        values.append(existing)
    return os.pathsep.join(values)


def _canonical_evidence(canonical: CanonicalCircuitManifest) -> tuple[str, str, int]:
    # Import locally to avoid a module cycle when experiments_v2.cli exposes
    # the public QASMBench subcommands.
    from .cli import (
        _canonical_gate_ledger,
        _canonical_layer_ledger,
        _ledger_sha256,
    )

    logical = _ledger_sha256(_canonical_gate_ledger(canonical))
    layers = _canonical_layer_ledger(canonical)
    return (
        logical,
        str(layers["layer_ledger_sha256"]),
        int(layers["transitions"]),
    )


def qasmbench_experiment_id(
        plan: ExperimentPlan, source_manifest: Path) -> str:
    repository = repository_snapshot(plan.repo_root)
    implementation_paths = (
        Path(__file__),
        Path(__file__).with_name("qasmbench_large_driver.py"),
        Path(__file__).with_name("method_driver.py"),
        Path(__file__).with_name("cli.py"),
        Path(__file__).with_name("runner.py"),
        Path(__file__).parents[1] / "streaming" / "formal_large_zac_compiler.py",
        Path(__file__).parents[1] / "streaming" / "formal_zac_placement.py",
    )
    return stable_sha256({
        "protocol": QASMBENCH_RUN_PROTOCOL,
        "git_commit": repository["commit"],
        "source_manifest_sha256": sha256_file(source_manifest),
        "architecture_sha256": sha256_file(plan.architecture_path),
        "model_sha256": sha256_file(plan.model_path),
        "method_configs": {
            method: sha256_file(plan.methods[method].config_path)
            for method in METHODS
        },
        "implementation_sha256": {
            path.name: sha256_file(path) for path in implementation_paths
        },
        "resource_contract": {
            "rss_limit_bytes": RSS_LIMIT_BYTES,
            "minimum_free_bytes": MINIMUM_FREE_BYTES,
            "concurrency_by_scale": CONCURRENCY_BY_SCALE,
        },
    })


def _attempt_spec(
        plan: ExperimentPlan, output_root: Path, job: QASMBenchJob) -> AttemptSpec:
    source = job.source
    canonical = source.canonical_manifest()
    config = plan.resolved_config(job.method, job.seed)
    logical, layers, transitions = _canonical_evidence(canonical)
    compiler_python = plan.methods[job.method].python
    command = [
        compiler_python,
        "-m", "experiments_v2.method_driver",
        "--method", job.method,
        "--input", canonical.canonical_path,
        "--config", str(config),
        "--architecture", str(plan.architecture_path),
        "--run-kind", "qasmbench",
    ]
    return AttemptSpec(
        dataset=f"qasmbench_{source.benchmark_scale}",
        circuit=source.benchmark_directory,
        method=job.method,
        seed=job.seed,
        repetition=job.repetition,
        command=command,
        output_root=(output_root / "runs" / "qasmbench" /
                     source.benchmark_scale),
        repo_root=plan.repo_root,
        input_path=Path(canonical.canonical_path),
        config_path=config,
        architecture_path=plan.architecture_path,
        model_path=plan.model_path,
        run_kind="qasmbench",
        experiment_id=job.experiment_id,
        benchmark_scale=source.benchmark_scale,
        benchmark_directory=source.benchmark_directory,
        upstream_git_blob=source.upstream_git_blob,
        input_selection_reason=source.selection_reason,
        canonical_profile=source.canonical_profile,
        trace_retained=False,
        concurrency_limit=CONCURRENCY_BY_SCALE[source.benchmark_scale],
        implementation_status="exact_method_driver_v1",
        qubits=canonical.qubits,
        expected_gates_1q=canonical.gates_1q,
        expected_gates_2q=canonical.gates_2q,
        expected_gate_ledger_sha256=logical,
        layer_ledger_sha256=layers,
        transition_count=transitions,
        require_clean_git=True,
        timeout_seconds=job.timeout_seconds,
        rss_limit_bytes=RSS_LIMIT_BYTES,
        minimum_free_bytes=MINIMUM_FREE_BYTES,
        environment={"PYTHONPATH": _pythonpath(plan)},
        cwd=plan.repo_root,
        package_versions={
            **plan.package_versions,
            "qasmbench_protocol": QASMBENCH_RUN_PROTOCOL,
            "compiler_python_path": compiler_python,
        },
    )


def _standard_attempt(
        plan: ExperimentPlan, output_root: Path,
        job: QASMBenchJob) -> RunManifest:
    from .cli import UnifiedEvaluationGate

    spec = _attempt_spec(plan, output_root, job)
    gate = UnifiedEvaluationGate(plan, job.source.canonical_manifest(), job.method)
    return run_attempt(spec, verifier=gate.verifier, scorer=gate.scorer)


def _sha256_uncompressed_gzip(path: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _integral_count(value: Any, label: str) -> int:
    if (isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not float(value).is_integer()
            or int(value) < 0):
        raise ValueError(f"invalid formal Large {label}: {value!r}")
    return int(value)


def _fidelity_metrics(value: Mapping[str, Any], qubits: int) -> dict[str, Any]:
    counts = value.get("counts")
    components = value.get("components")
    if not isinstance(counts, Mapping) or not isinstance(components, Mapping):
        raise ValueError("formal Large fidelity result is incomplete")
    component_values: dict[str, float | None] = {
        "qubits": float(qubits),
        "gates_1q": float(counts["one_qubit_gates"]),
        "gates_2q": float(counts["two_qubit_gates"]),
        "idle_excitations": float(counts["idle_excitations"]),
        "transfers": float(_integral_count(counts["transfers"], "transfers")),
        "duration_us": float(value["duration_us"]),
        "exponential_sensitivity_log_fidelity": float(
            value["exponential_sensitivity_log_fidelity"]),
    }
    for name, row in components.items():
        if not isinstance(row, Mapping):
            raise ValueError("formal Large fidelity component is not an object")
        component_values[str(name)] = row.get("fidelity")
        component_values[f"log_{name}"] = row.get("log_fidelity")
    return {
        "log_fidelity": value.get("log_fidelity"),
        "fidelity": value.get("fidelity"),
        "fidelity_components": component_values,
        "fidelity_ood": bool(value.get("ood")),
        "exponential_sensitivity_fidelity": value.get(
            "exponential_sensitivity_fidelity"),
        "exponential_sensitivity_log_fidelity": value.get(
            "exponential_sensitivity_log_fidelity"),
        "duration_us": float(value["duration_us"]),
        "move_batches": _integral_count(value["move_batches"], "move_batches"),
        "move_time_us": float(value["move_time_us"]),
        "transfers": _integral_count(counts["transfers"], "transfers"),
        "idle_exposures": _integral_count(
            counts["idle_excitations"], "idle_excitations"),
        "warnings": [str(item) for item in value.get("warnings", [])],
    }


def _set_large_success(
        manifest: RunManifest, payload: Mapping[str, Any],
        canonical: CanonicalCircuitManifest, logical_ledger: str) -> None:
    if FORBIDDEN_QASMBENCH_IMPLEMENTATION in json.dumps(
            payload, sort_keys=True, default=str):
        raise ValueError("development streaming proxy evidence is forbidden")
    if payload.get("implementation_status") != "formal_exact_zac_sqlite_v1":
        raise ValueError("Large ZAC attempt lacks exact implementation evidence")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Large ZAC driver result is missing")
    if (result.get("status") != "success"
            or result.get("full_circuit") is not True
            or result.get("support_claim_eligible") is not True):
        raise ValueError("Large ZAC attempt is not full-circuit claim eligible")
    validation = result.get("validation")
    fidelity = result.get("fidelity")
    if not isinstance(validation, Mapping) or not isinstance(fidelity, Mapping):
        raise ValueError("Large ZAC result lacks validation/fidelity evidence")
    if (validation.get("ok") is not True
            or int(validation.get("ghost_hits", -1)) != 0
            or int(validation.get("one_qubit_gates", -1)) != canonical.gates_1q
            or int(validation.get("two_qubit_gates", -1)) != canonical.gates_2q):
        raise ValueError("Large ZAC strict validation disagrees with canonical input")
    metrics = _fidelity_metrics(fidelity, canonical.qubits)
    for key, value in metrics.items():
        if key == "warnings":
            manifest.warnings.extend(value)
        else:
            setattr(manifest, key, value)
    manifest.verifier_ok = True
    manifest.qubits = canonical.qubits
    manifest.expected_gates_1q = canonical.gates_1q
    manifest.expected_gates_2q = canonical.gates_2q
    manifest.observed_gates_1q = int(validation["one_qubit_gates"])
    manifest.observed_gates_2q = int(validation["two_qubit_gates"])
    manifest.expected_gate_ledger_sha256 = logical_ledger
    # The inner exact validator independently proves the same per-atom logical
    # sequence using its rolling SQLite ledger.  Publish the common manifest's
    # canonical ledger hash only after that independent proof succeeds.
    manifest.observed_gate_ledger_sha256 = logical_ledger
    manifest.ghost_hits = int(validation["ghost_hits"])
    manifest.ghost_repairs = 0
    manifest.ghost_splits = 0
    manifest.compiler_time_ns = int(result["compiler_core_ns"])
    manifest.full_compile_ns = manifest.compiler_time_ns
    manifest.end_to_end_time_ns = int(result["end_to_end_ns"])
    manifest.peak_rss_bytes = max(
        int(manifest.peak_rss_bytes or 0), int(result["peak_rss_bytes"] or 0))
    manifest.implementation_status = str(payload["implementation_status"])
    timing = result.get("timing_breakdown_ns", {})
    if isinstance(timing, Mapping):
        aliases = {
            "initial_placement_ns": "initial_placement_ns",
            "problem_preparation_ns": "problem_preparation_ns",
            "search_kernel_ns": "search_kernel_ns",
            "native_search_wall_ns": "native_search_wall_ns",
            "return_match_ns": "return_match_ns",
            "forecast_ns": "forecast_ns",
            "result_commit_ns": "result_commit_ns",
            "routing_ns": "routing_ns",
        }
        for source, destination in aliases.items():
            if timing.get(source) is not None:
                setattr(manifest, destination, int(timing[source]))
    native = payload.get("native_identity")
    if not isinstance(native, Mapping):
        raise ValueError("Large M3/M4 lacks native identity")
    for key in (
            "algorithm_revision", "backend", "native_abi_version",
            "native_wheel_sha256", "tuning_protocol_id", "rng_version",
            "compiler_and_flags", "python_fallback"):
        setattr(manifest, key, native[key])


def _discard_large_payload(
        payload_root: Path, manifest: RunManifest) -> None:
    if payload_root.name != "payload" or not payload_root.is_dir():
        raise ValueError(f"refusing unsafe Large payload cleanup: {payload_root}")
    trace = payload_root / "formal" / "trace.jsonl.gz"
    hashes: dict[str, str] = {}
    if manifest.status == RunStatus.SUCCESS.value:
        if not trace.is_file():
            raise FileNotFoundError("successful Large attempt has no canonical trace")
        manifest.event_stream_sha256 = _sha256_uncompressed_gzip(trace)
        manifest.event_stream_hash_protocol = EVENT_HASH_PROTOCOL
    for path in (
        payload_root / "driver_result.json",
        payload_root / "formal" / "manifest.json",
        payload_root / "formal" / "native.jsonl.gz",
        payload_root / "formal" / "trace.jsonl.gz",
        payload_root / "formal" / "decisions.jsonl.gz",
        payload_root / "formal" / "route.jsonl.gz",
        payload_root / "formal" / "checkpoint.json",
    ):
        if path.is_file():
            hashes[str(path.relative_to(payload_root))] = sha256_file(path)
    manifest.transient_artifact_sha256 = dict(sorted(hashes.items()))
    manifest.trace_retained = False
    shutil.rmtree(payload_root)


def _large_zac_attempt(
        plan: ExperimentPlan, output_root: Path,
        job: QASMBenchJob) -> RunManifest:
    source = job.source
    canonical = source.canonical_manifest()
    config = plan.resolved_config(job.method, job.seed)
    logical, layers, transitions = _canonical_evidence(canonical)
    attempt_root = output_root / "runs" / "qasmbench" / source.benchmark_scale
    attempt_root.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(attempt_root).free
    if free_bytes < MINIMUM_FREE_BYTES:
        raise RuntimeError(
            f"runner paused: only {free_bytes / (1 << 30):.2f} GiB free; "
            "requires 4.00 GiB")
    run_id = (
        f"qasmbench_{source.benchmark_scale}-{source.benchmark_directory}-"
        f"{job.method}-s{job.seed}-r{job.repetition}-{uuid.uuid4().hex[:12]}")
    temporary = attempt_root / f".{run_id}.tmp"
    final = attempt_root / run_id
    temporary.mkdir()
    payload_root = temporary / "payload"
    command = [
        plan.python, "-m", "experiments_v2.qasmbench_large_driver",
        "--method", job.method,
        "--input", canonical.canonical_path,
        "--config", str(config),
        "--architecture", str(plan.architecture_path),
        "--output-root", str(payload_root),
    ]
    manifest = RunManifest.with_provenance(
        plan.repo_root,
        run_id=run_id,
        dataset="qasmbench_large",
        circuit=source.benchmark_directory,
        method=job.method,
        seed=job.seed,
        repetition=job.repetition,
        run_kind="qasmbench",
        experiment_id=job.experiment_id,
        benchmark_scale="large",
        benchmark_directory=source.benchmark_directory,
        upstream_git_blob=source.upstream_git_blob,
        input_selection_reason=source.selection_reason,
        canonical_profile=source.canonical_profile,
        status=RunStatus.COMPILER_ERROR.value,
        input_sha256=canonical.canonical_sha256,
        config_sha256=sha256_file(config),
        architecture_sha256=sha256_file(plan.architecture_path),
        model_sha256=sha256_file(plan.model_path),
        command=command,
        timeout_seconds=job.timeout_seconds,
        rss_limit_bytes=RSS_LIMIT_BYTES,
        minimum_free_bytes=MINIMUM_FREE_BYTES,
        concurrency_limit=1,
        qubits=canonical.qubits,
        expected_gates_1q=canonical.gates_1q,
        expected_gates_2q=canonical.gates_2q,
        expected_gate_ledger_sha256=logical,
        layer_ledger_sha256=layers,
        transition_count=transitions,
        canonical_input_layer_ledger_sha256=layers,
        canonical_input_transition_count=transitions,
        trace_protocol=trace_protocol_for_method(job.method),
        ghost_policy=ghost_policy_for_method(job.method),
        physicalization_policy=physicalization_policy_for_method(job.method),
        trace_retained=False,
        implementation_status="formal_exact_zac_sqlite_v1",
        artifact_dir=str(final),
        package_versions={
            **plan.package_versions,
            "qasmbench_protocol": QASMBENCH_RUN_PROTOCOL,
            "compiler_python_path": plan.python,
        },
    )
    manifest.started_at_utc = _utc_now()
    manifest.write(temporary / "manifest.json")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = _pythonpath(plan)
    stdout_path = temporary / manifest.stdout_path
    stderr_path = temporary / manifest.stderr_path
    started_ns = time.perf_counter_ns()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    forced_status: RunStatus | None = None
    peak_rss = 0
    process: subprocess.Popen[bytes] | None = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                command,
                cwd=plan.repo_root,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            deadline = time.monotonic() + job.timeout_seconds
            while process.poll() is None:
                rss = _process_group_rss(process.pid)
                peak_rss = max(peak_rss, rss)
                if rss > RSS_LIMIT_BYTES:
                    forced_status = RunStatus.OOM
                    _terminate_group(process)
                    break
                if time.monotonic() >= deadline:
                    forced_status = RunStatus.TIMEOUT
                    _terminate_group(process)
                    break
                time.sleep(0.1)
            manifest.exit_code = process.returncode
    except BaseException as error:
        if process is not None:
            _terminate_group(process)
        manifest.error = f"runner exception: {type(error).__name__}: {error}"
        forced_status = RunStatus.COMPILER_ERROR
    manifest.compiler_process_wall_ns = time.perf_counter_ns() - started_ns
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    manifest.cpu_time_ns = round(1e9 * (
        (child_after.ru_utime - child_before.ru_utime)
        + (child_after.ru_stime - child_before.ru_stime)))
    manifest.peak_rss_bytes = peak_rss
    manifest.status = (
        forced_status.value if forced_status is not None else
        (RunStatus.SUCCESS.value if manifest.exit_code == 0
         else RunStatus.COMPILER_ERROR.value))
    result_path = payload_root / "driver_result.json"
    payload: Mapping[str, Any] | None = None
    if result_path.is_file():
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                payload = loaded
        except (OSError, json.JSONDecodeError) as error:
            manifest.warnings.append(f"invalid Large driver result: {error}")
    if manifest.status == RunStatus.SUCCESS.value:
        try:
            if payload is None or payload.get("status") != "success":
                raise ValueError("Large driver exited without success evidence")
            _set_large_success(manifest, payload, canonical, logical)
        except BaseException as error:
            manifest.status = RunStatus.SCORER_ERROR.value
            manifest.verifier_ok = False
            manifest.error = f"Large evidence error: {type(error).__name__}: {error}"
    elif payload is not None and payload.get("error"):
        manifest.error = str(payload["error"])
    try:
        if payload_root.exists():
            _discard_large_payload(payload_root, manifest)
    except BaseException as error:
        if manifest.status == RunStatus.SUCCESS.value:
            manifest.status = RunStatus.SCORER_ERROR.value
            manifest.error = (
                "Large transient finalization failed: "
                f"{type(error).__name__}: {error}")
        else:
            manifest.warnings.append(
                f"Large transient finalization failed: {type(error).__name__}: {error}")
    if manifest.status == RunStatus.SUCCESS.value:
        try:
            manifest.validate(require_success_metrics=True)
        except ValueError as error:
            manifest.status = RunStatus.SCORER_ERROR.value
            manifest.error = f"incomplete Large success contract: {error}"
    manifest.end_to_end_time_ns = time.perf_counter_ns() - started_ns
    manifest.ended_at_utc = _utc_now()
    manifest.write(temporary / "manifest.json")
    os.replace(temporary, final)
    manifest.artifact_dir = str(final)
    manifest.write(final / "manifest.json")
    return manifest


def _run_job(
        plan: ExperimentPlan, output_root: Path,
        job: QASMBenchJob) -> RunManifest:
    if job.source.benchmark_scale == "large" and job.method in {"M3", "M4"}:
        return _large_zac_attempt(plan, output_root, job)
    # Large M1/M2 intentionally remain the paper-original batch drivers.  If
    # either in-memory compiler cannot finish under the resource tier,
    # run_attempt records timeout/OOM instead of substituting a repair wrapper.
    return _standard_attempt(plan, output_root, job)


def _is_runner_pause(error: BaseException) -> bool:
    """Preserve the existing fail-closed disk-space pause contract."""
    return (
        isinstance(error, RuntimeError)
        and str(error).startswith("runner paused:")
    )


def _seal_orchestration_error(
        plan: ExperimentPlan, output_root: Path, job: QASMBenchJob,
        error: Exception) -> RunManifest:
    """Seal an unexpected pre-attempt worker failure as a terminal result.

    Disk-space pauses are deliberately filtered by the caller and never enter
    this path.  The manifest contains no fabricated compiler output or metrics;
    it only makes the attempted experiment identity and exception auditable.
    """
    source = job.source
    attempt_root = output_root / "runs" / "qasmbench" / source.benchmark_scale
    attempt_root.mkdir(parents=True, exist_ok=True)
    run_id = (
        f"qasmbench_{source.benchmark_scale}-{source.benchmark_directory}-"
        f"{job.method}-s{job.seed}-r{job.repetition}-{uuid.uuid4().hex[:12]}"
    )
    temporary = attempt_root / f".{run_id}.tmp"
    final = attempt_root / run_id
    temporary.mkdir()
    now = _utc_now()
    manifest = RunManifest.with_provenance(
        plan.repo_root,
        run_id=run_id,
        dataset=f"qasmbench_{source.benchmark_scale}",
        circuit=source.benchmark_directory,
        method=job.method,
        seed=job.seed,
        repetition=job.repetition,
        run_kind="qasmbench",
        experiment_id=job.experiment_id,
        benchmark_scale=source.benchmark_scale,
        benchmark_directory=source.benchmark_directory,
        upstream_git_blob=source.upstream_git_blob,
        input_selection_reason=source.selection_reason,
        canonical_profile=source.canonical_profile,
        status=RunStatus.COMPILER_ERROR.value,
        input_sha256=source.canonical_sha256,
        timeout_seconds=job.timeout_seconds,
        rss_limit_bytes=RSS_LIMIT_BYTES,
        minimum_free_bytes=MINIMUM_FREE_BYTES,
        concurrency_limit=CONCURRENCY_BY_SCALE[source.benchmark_scale],
        qubits=source.qubits,
        expected_gates_1q=source.gates_1q,
        expected_gates_2q=source.gates_2q,
        trace_protocol=trace_protocol_for_method(job.method),
        ghost_policy=ghost_policy_for_method(job.method),
        physicalization_policy=physicalization_policy_for_method(job.method),
        trace_retained=False,
        implementation_status=(
            "formal_exact_zac_sqlite_v1"
            if source.benchmark_scale == "large" and job.method in {"M3", "M4"}
            else "exact_method_driver_v1"
        ),
        started_at_utc=now,
        ended_at_utc=now,
        error=f"orchestration exception: {type(error).__name__}: {error}",
        artifact_dir=str(final),
    )
    manifest.write(temporary / "manifest.json")
    os.replace(temporary, final)
    return manifest


def _manifest_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    paths = []
    for path in sorted(root.rglob("manifest.json"), key=str):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        paths.append(path)
    return paths


def _identity(
        *, experiment_id: str, scale: str, directory: str, method: str,
        seed: int, repetition: int) -> tuple[Any, ...]:
    return (experiment_id, scale, directory, method, int(seed), int(repetition))


def _load_existing(
        output_root: Path, experiment_id: str) -> dict[tuple[Any, ...], tuple[RunManifest, Path]]:
    selected: dict[tuple[Any, ...], tuple[RunManifest, Path]] = {}
    root = output_root / "runs" / "qasmbench"
    for path in _manifest_paths(root):
        manifest = load_run_manifest(path)
        if manifest.run_kind != "qasmbench" or manifest.experiment_id != experiment_id:
            continue
        if manifest.status not in TERMINAL_STATUSES:
            raise ValueError(f"non-terminal QASMBench manifest: {path}")
        key = _identity(
            experiment_id=manifest.experiment_id,
            scale=manifest.benchmark_scale,
            directory=manifest.benchmark_directory,
            method=manifest.method,
            seed=manifest.seed,
            repetition=manifest.repetition,
        )
        if key in selected:
            raise ValueError(
                f"duplicate current QASMBench terminal identity: "
                f"{selected[key][1]} and {path}")
        selected[key] = (manifest, path.resolve())
    return selected


def _best_seed0_statuses(
        existing: Mapping[tuple[Any, ...], tuple[RunManifest, Path]],
        experiment_id: str, source: QASMBenchSource) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for method in METHODS:
        for repetition in (1, 0):
            key = _identity(
                experiment_id=experiment_id,
                scale=source.benchmark_scale,
                directory=source.benchmark_directory,
                method=method,
                seed=0,
                repetition=repetition,
            )
            if key in existing:
                status = existing[key][0].status
                if status == RunStatus.SUCCESS.value or method not in statuses:
                    statuses[method] = status
                if status == RunStatus.SUCCESS.value:
                    break
    return statuses


def _jobs_for_stage(
        sources: Sequence[QASMBenchSource], *, stage: str,
        experiment_id: str,
        existing: Mapping[tuple[Any, ...], tuple[RunManifest, Path]],
        methods: Sequence[str]) -> list[QASMBenchJob]:
    jobs: list[QASMBenchJob] = []
    selected_methods = tuple(method for method in METHODS if method in methods)
    for source in sources:
        if source.status != "success":
            continue
        tier = timeout_seconds_for_two_qubit_gates(int(source.gates_2q))
        if stage == "seed0":
            jobs.extend(QASMBenchJob(
                source, method, 0, 0, tier, experiment_id)
                for method in selected_methods)
        elif stage == "retry":
            statuses = _best_seed0_statuses(existing, experiment_id, source)
            if sum(value == RunStatus.SUCCESS.value
                   for value in statuses.values()) < 2:
                continue
            jobs.extend(QASMBenchJob(
                source, method, 0, 1, RETRY_TIMEOUT_SECONDS, experiment_id)
                for method in selected_methods
                if statuses.get(method) != RunStatus.SUCCESS.value)
        elif stage == "followup":
            statuses = _best_seed0_statuses(existing, experiment_id, source)
            if any(statuses.get(method) != RunStatus.SUCCESS.value
                   for method in METHODS):
                continue
            jobs.extend(QASMBenchJob(
                source, method, seed, 0, tier, experiment_id)
                for seed in (1, 2)
                for method in ("M3", "M4")
                if method in selected_methods)
        else:
            raise ValueError(f"unknown QASMBench run stage: {stage}")
    return jobs


def _execute_stage(
        plan: ExperimentPlan, output_root: Path,
        jobs: Sequence[QASMBenchJob], *, resume: bool, dry_run: bool,
        existing: dict[tuple[Any, ...], tuple[RunManifest, Path]],
) -> Mapping[str, Any]:
    attempted: list[Mapping[str, Any]] = []
    skipped: list[Mapping[str, Any]] = []
    commands: list[Mapping[str, Any]] = []
    pending_by_scale: dict[str, list[QASMBenchJob]] = {scale: [] for scale in SCALES}
    for job in jobs:
        key = _identity(
            experiment_id=job.experiment_id,
            scale=job.source.benchmark_scale,
            directory=job.source.benchmark_directory,
            method=job.method,
            seed=job.seed,
            repetition=job.repetition,
        )
        row = {
            "benchmark_scale": job.source.benchmark_scale,
            "benchmark_directory": job.source.benchmark_directory,
            "method": job.method,
            "seed": job.seed,
            "repetition": job.repetition,
            "timeout_seconds": job.timeout_seconds,
            "rss_limit_bytes": RSS_LIMIT_BYTES,
            "concurrency_limit": CONCURRENCY_BY_SCALE[job.source.benchmark_scale],
        }
        if resume and key in existing:
            manifest, path = existing[key]
            skipped.append({
                **row,
                "status": manifest.status,
                "manifest": str(path),
                "manifest_sha256": sha256_file(path),
            })
        elif key in existing:
            raise ValueError(
                "QASMBench terminal identity already exists; rerun with "
                f"--resume: {existing[key][1]}")
        elif dry_run:
            commands.append(row)
        else:
            pending_by_scale[job.source.benchmark_scale].append(job)

    def completed(job: QASMBenchJob, manifest: RunManifest) -> None:
        path = Path(manifest.artifact_dir) / "manifest.json"
        key = _identity(
            experiment_id=manifest.experiment_id,
            scale=manifest.benchmark_scale,
            directory=manifest.benchmark_directory,
            method=manifest.method,
            seed=manifest.seed,
            repetition=manifest.repetition,
        )
        existing[key] = (manifest, path.resolve())
        attempted.append({
            "benchmark_scale": manifest.benchmark_scale,
            "benchmark_directory": manifest.benchmark_directory,
            "method": manifest.method,
            "seed": manifest.seed,
            "repetition": manifest.repetition,
            "status": manifest.status,
            "manifest": str(path.resolve()),
            "manifest_sha256": sha256_file(path),
        })

    for scale in SCALES:
        pending = pending_by_scale[scale]
        workers = CONCURRENCY_BY_SCALE[scale]
        if not pending:
            continue
        if workers == 1:
            for job in pending:
                try:
                    manifest = _run_job(plan, output_root, job)
                except Exception as error:
                    if _is_runner_pause(error):
                        raise
                    manifest = _seal_orchestration_error(
                        plan, output_root, job, error)
                completed(job, manifest)
            continue
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_job, plan, output_root, job): job
                for job in pending
            }
            for future in concurrent.futures.as_completed(futures):
                job = futures[future]
                try:
                    manifest = future.result()
                except Exception as error:
                    if _is_runner_pause(error):
                        for pending_future in futures:
                            pending_future.cancel()
                        raise
                    manifest = _seal_orchestration_error(
                        plan, output_root, job, error)
                completed(job, manifest)
    attempted.sort(key=lambda row: (
        SCALES.index(str(row["benchmark_scale"])),
        str(row["benchmark_directory"]), str(row["method"]),
        int(row["seed"]), int(row["repetition"])))
    return {
        "planned_jobs": len(jobs),
        "attempted": attempted,
        "skipped_existing": skipped,
        "commands": commands,
        "status_counts": dict(sorted(Counter(
            row["status"] for row in attempted).items())),
    }


def command_run_qasmbench(
    plan: ExperimentPlan,
    source_manifest: str | Path,
    *,
    output_root: str | Path | None = None,
    scales: Sequence[str] = SCALES,
    methods: Sequence[str] = METHODS,
    stage: str = "seed0",
    resume: bool = False,
    dry_run: bool = False,
    retry_missing: bool = False,
) -> Mapping[str, Any]:
    source_path = Path(source_manifest).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"QASMBench source manifest is missing: {source_path}")
    unknown_scales = sorted(set(scales) - set(SCALES))
    unknown_methods = sorted(set(methods) - set(METHODS))
    if unknown_scales or not scales:
        raise ValueError(f"invalid QASMBench scales: {unknown_scales or scales}")
    if unknown_methods or not methods:
        raise ValueError(f"invalid QASMBench methods: {unknown_methods or methods}")
    if stage not in {"seed0", "retry", "followup", "all"}:
        raise ValueError(f"invalid QASMBench stage: {stage}")
    destination = (
        Path(output_root).resolve() if output_root is not None
        else plan.output_root.resolve())
    sources = [
        source for source in load_qasmbench_sources(source_path)
        if source.benchmark_scale in set(scales)
    ]
    experiment_id = qasmbench_experiment_id(plan, source_path)
    existing = _load_existing(destination, experiment_id)
    stages = [stage]
    if stage == "all":
        stages = ["seed0"]
        if retry_missing:
            stages.append("retry")
        stages.append("followup")
    stage_reports: dict[str, Any] = {}
    for stage_name in stages:
        # A prior stage in this invocation has populated ``existing``.  This is
        # what makes followup depend on actual four-method seed-0 success.
        jobs = _jobs_for_stage(
            sources,
            stage=stage_name,
            experiment_id=experiment_id,
            existing=existing,
            methods=methods,
        )
        stage_reports[stage_name] = _execute_stage(
            plan, destination, jobs,
            resume=resume,
            dry_run=dry_run,
            existing=existing,
        )
    report = {
        "experiment_schema": 2,
        "protocol": QASMBENCH_RUN_PROTOCOL,
        "experiment_id": experiment_id,
        "source_manifest": str(source_path),
        "source_manifest_sha256": sha256_file(source_path),
        "output_root": str(destination),
        "scales": [scale for scale in SCALES if scale in set(scales)],
        "methods": [method for method in METHODS if method in set(methods)],
        "stage": stage,
        "dry_run": bool(dry_run),
        "resume": bool(resume),
        "resource_contract": {
            "timeout_tiers_by_canonical_2q": {
                "lte_10000": 600,
                "10001_to_250000": 1800,
                "gt_250000": 3600,
                "supplemental_retry": RETRY_TIMEOUT_SECONDS,
            },
            "rss_limit_bytes": RSS_LIMIT_BYTES,
            "minimum_free_bytes": MINIMUM_FREE_BYTES,
            "concurrency_by_scale": CONCURRENCY_BY_SCALE,
        },
        "implementation_contract": {
            "small_medium": "existing_exact_method_driver",
            "large_M1_M2": "paper_original_batch_method_driver",
            "large_M3_M4": "formal_exact_zac_sqlite_v1",
            "forbidden": FORBIDDEN_QASMBENCH_IMPLEMENTATION,
            "trace_retained": False,
        },
        "stages": stage_reports,
    }
    if not dry_run:
        _atomic_json(
            destination / "reports" /
            f"run-qasmbench-{stage}-{experiment_id[:12]}.json",
            report,
        )
    return report


__all__ = [
    "CONCURRENCY_BY_SCALE",
    "EVENT_HASH_PROTOCOL",
    "METHODS",
    "MINIMUM_FREE_BYTES",
    "QASMBenchJob",
    "QASMBenchSource",
    "QASMBENCH_RUN_PROTOCOL",
    "RETRY_TIMEOUT_SECONDS",
    "RSS_LIMIT_BYTES",
    "SCALES",
    "command_run_qasmbench",
    "load_qasmbench_sources",
    "qasmbench_experiment_id",
    "timeout_seconds_for_two_qubit_gates",
]
