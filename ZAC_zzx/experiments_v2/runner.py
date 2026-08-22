"""Failure-safe attempt runner with unique directories and process-group limits."""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
import resource
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .contracts import RunManifest, RunStatus, sha256_file, stable_sha256


Verifier = Callable[[Path], Mapping[str, Any] | bool]
Scorer = Callable[[Path], Mapping[str, Any]]


_ARCHIVE_ARTIFACTS = (
    "trace.zair.json",
    "trace.na",
    "trace.na.raw",
    "compiler_stats.json",
)


def _gzip_artifact(path: Path) -> Path:
    """Atomically archive one evidence file and remove only its raw duplicate."""
    destination = path.with_name(path.name + ".gz")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite archive: {destination}")
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with path.open("rb") as source, temporary.open("wb") as target:
            with gzip.GzipFile(
                    filename="", mode="wb", fileobj=target,
                    compresslevel=6, mtime=0) as archive:
                shutil.copyfileobj(source, archive, length=1 << 20)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
        path.unlink()
        return destination
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _archive_attempt_artifacts(directory: Path) -> list[str]:
    archived = []
    for name in _ARCHIVE_ARTIFACTS:
        path = directory / name
        if path.is_file():
            archived.append(_gzip_artifact(path).name)
    return archived


@dataclass(frozen=True)
class AttemptSpec:
    dataset: str
    circuit: str
    method: str
    seed: int
    repetition: int
    command: Sequence[str]
    output_root: Path
    repo_root: Path
    input_path: Path
    config_path: Path
    architecture_path: Path
    model_path: Path
    run_kind: str = "smoke"
    ablation_variant: str = ""
    experiment_id: str = ""
    qubits: Optional[int] = None
    expected_gates_1q: Optional[int] = None
    expected_gates_2q: Optional[int] = None
    expected_gate_ledger_sha256: str = ""
    require_clean_git: bool = False
    timeout_seconds: float = 600.0
    rss_limit_bytes: Optional[int] = None
    minimum_free_bytes: int = 5 * (1 << 30)
    environment: Mapping[str, str] = field(default_factory=dict)
    cwd: Optional[Path] = None
    package_versions: Mapping[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.command:
            raise ValueError("empty compiler command")
        for label, path in (
            ("input", self.input_path), ("config", self.config_path),
            ("architecture", self.architecture_path), ("model", self.model_path),
        ):
            if not Path(path).is_file():
                raise FileNotFoundError(f"{label} file does not exist: {path}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if self.run_kind not in {"coverage", "main", "timing", "ablation", "large", "smoke"}:
            raise ValueError(f"invalid run_kind: {self.run_kind}")
        if self.run_kind == "ablation":
            if not self.ablation_variant:
                raise ValueError("ablation attempt requires ablation_variant")
        elif self.ablation_variant:
            raise ValueError("ablation_variant is forbidden outside ablation attempts")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _process_group_rss(pgid: int) -> int:
    """Return summed RSS for a process group (bytes), or zero if unavailable."""
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pgid=,rss="], text=True,
            stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return 0
    total_kib = 0
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            row_group, rss_kib = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if row_group == pgid:
            total_kib += rss_kib
    return total_kib * 1024


def _terminate_group(process: subprocess.Popen[bytes], grace_seconds: float = 2.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def _apply_metrics(manifest: RunManifest, metrics: Mapping[str, Any]) -> None:
    aliases = {
        "log_fidelity": "log_fidelity", "fidelity": "fidelity",
        "fidelity_components": "fidelity_components",
        "move_batches": "move_batches", "move_time_us": "move_time_us",
        "compiler_time_ns": "compiler_time_ns", "cpu_time_ns": "cpu_time_ns",
        "stay_count": "stay_count", "return_count": "return_count",
        "reseat_count": "reseat_count", "idle_exposures": "idle_exposures",
        "ghost_repairs": "ghost_repairs", "ghost_splits": "ghost_splits",
        "ghost_hits": "ghost_hits", "fidelity_ood": "fidelity_ood",
        "exponential_sensitivity_fidelity": "exponential_sensitivity_fidelity",
        "exponential_sensitivity_log_fidelity": "exponential_sensitivity_log_fidelity",
        "duration_us": "duration_us", "qubits": "qubits",
        "expected_gates_1q": "expected_gates_1q",
        "expected_gates_2q": "expected_gates_2q",
        "observed_gates_1q": "observed_gates_1q",
        "observed_gates_2q": "observed_gates_2q",
        "expected_gate_ledger_sha256": "expected_gate_ledger_sha256",
        "observed_gate_ledger_sha256": "observed_gate_ledger_sha256",
    }
    for source, destination in aliases.items():
        if source in metrics:
            setattr(manifest, destination, metrics[source])
    warnings = metrics.get("warnings", [])
    if isinstance(warnings, list):
        manifest.warnings.extend(str(item) for item in warnings)


def run_attempt(spec: AttemptSpec, *, verifier: Optional[Verifier] = None,
                scorer: Optional[Scorer] = None) -> RunManifest:
    """Run exactly one compiler attempt and atomically seal all its evidence.

    The child receives ``ZAC_RUN_DIR`` and must write only there.  No file from
    any prior attempt is inspected.  A successful compiler is downgraded to
    ``verifier_fail`` or ``scorer_error`` if either postcondition fails.
    """
    spec.validate()
    if verifier is None or scorer is None:
        raise ValueError("Schema-2 run_attempt requires both verifier and scorer")
    output_root = Path(spec.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(output_root).free
    if free_bytes < spec.minimum_free_bytes:
        raise RuntimeError(
            f"runner paused: only {free_bytes / (1 << 30):.2f} GiB free; "
            f"requires {spec.minimum_free_bytes / (1 << 30):.2f} GiB")

    variant_token = f"-{spec.ablation_variant}" if spec.ablation_variant else ""
    run_id = (f"{spec.dataset}-{spec.circuit}-{spec.method}{variant_token}-s{spec.seed}-"
              f"r{spec.repetition}-{uuid.uuid4().hex[:12]}")
    temporary = output_root / f".{run_id}.tmp"
    final = output_root / run_id
    temporary.mkdir()

    manifest = RunManifest.with_provenance(
        Path(spec.repo_root), run_id=run_id, dataset=spec.dataset,
        circuit=spec.circuit, method=spec.method, seed=spec.seed,
        repetition=spec.repetition, run_kind=spec.run_kind,
        ablation_variant=spec.ablation_variant,
        experiment_id=spec.experiment_id,
        command=[str(item) for item in spec.command],
        timeout_seconds=spec.timeout_seconds,
        input_sha256=sha256_file(spec.input_path),
        config_sha256=sha256_file(spec.config_path),
        architecture_sha256=sha256_file(spec.architecture_path),
        model_sha256=sha256_file(spec.model_path),
        package_versions=dict(spec.package_versions), artifact_dir=str(final),
        qubits=spec.qubits, expected_gates_1q=spec.expected_gates_1q,
        expected_gates_2q=spec.expected_gates_2q,
        expected_gate_ledger_sha256=spec.expected_gate_ledger_sha256,
    )
    if spec.require_clean_git and (manifest.git_dirty or manifest.git_commit == "unknown"):
        shutil.rmtree(temporary)
        raise RuntimeError("formal experiment requires a clean Git commit")
    manifest.started_at_utc = _utc_now()
    manifest.write(temporary / "manifest.json")

    environment = os.environ.copy()
    environment.update({str(key): str(value) for key, value in spec.environment.items()})
    environment["ZAC_RUN_DIR"] = str(temporary)
    environment["ZAC_RUN_ID"] = run_id
    environment["ZAC_RUN_KIND"] = spec.run_kind
    environment["ZAC_ABLATION_VARIANT"] = spec.ablation_variant
    stdout_path = temporary / manifest.stdout_path
    stderr_path = temporary / manifest.stderr_path
    start_wall = time.perf_counter_ns()
    child_usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    peak_rss = 0
    forced_status: Optional[RunStatus] = None
    process: Optional[subprocess.Popen[bytes]] = None

    try:
        with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
            process = subprocess.Popen(
                [str(item) for item in spec.command],
                cwd=str(spec.cwd or spec.repo_root), env=environment,
                stdout=stdout, stderr=stderr, start_new_session=True,
            )
            deadline = time.monotonic() + spec.timeout_seconds
            while process.poll() is None:
                now = time.monotonic()
                rss = _process_group_rss(process.pid)
                peak_rss = max(peak_rss, rss)
                if spec.rss_limit_bytes is not None and rss > spec.rss_limit_bytes:
                    forced_status = RunStatus.OOM
                    _terminate_group(process)
                    break
                if now >= deadline:
                    forced_status = RunStatus.TIMEOUT
                    _terminate_group(process)
                    break
                time.sleep(min(0.1, max(0.01, deadline - now)))
            manifest.exit_code = process.returncode
    except BaseException as error:
        if process is not None:
            _terminate_group(process)
        manifest.error = f"runner exception: {type(error).__name__}: {error}"
        forced_status = RunStatus.COMPILER_ERROR

    manifest.compiler_process_wall_ns = time.perf_counter_ns() - start_wall
    child_usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    manifest.cpu_time_ns = round(1e9 * (
        (child_usage_after.ru_utime - child_usage_before.ru_utime) +
        (child_usage_after.ru_stime - child_usage_before.ru_stime)))
    manifest.peak_rss_bytes = peak_rss
    manifest.status = (forced_status.value if forced_status else
                       (RunStatus.SUCCESS.value if manifest.exit_code == 0
                        else RunStatus.COMPILER_ERROR.value))

    if manifest.status == RunStatus.SUCCESS.value:
        try:
            result = verifier(temporary)
            if isinstance(result, Mapping):
                manifest.verifier_ok = bool(result.get("ok", False))
                manifest.warnings.extend(str(item) for item in result.get("warnings", []))
            else:
                manifest.verifier_ok = bool(result)
            if not manifest.verifier_ok:
                manifest.status = RunStatus.VERIFIER_FAIL.value
        except BaseException as error:
            manifest.verifier_ok = False
            manifest.status = RunStatus.VERIFIER_FAIL.value
            manifest.error = f"verifier exception: {type(error).__name__}: {error}"

    if manifest.status == RunStatus.SUCCESS.value:
        try:
            _apply_metrics(manifest, scorer(temporary))
        except BaseException as error:
            manifest.status = RunStatus.SCORER_ERROR.value
            manifest.error = f"scorer exception: {type(error).__name__}: {error}"

    # If a method-specific wrapper reports core timing, prefer that interval.
    timing_path = temporary / "compiler_timing.json"
    if timing_path.is_file():
        try:
            with open(timing_path, encoding="utf-8") as handle:
                timing = json.load(handle)
            manifest.compiler_time_ns = int(timing["compiler_time_ns"])
            if "cpu_time_ns" in timing:
                manifest.cpu_time_ns = int(timing["cpu_time_ns"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            manifest.warnings.append(f"invalid compiler_timing.json: {error}")

    stats_path = temporary / "compiler_stats.json"
    if stats_path.is_file():
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            for key in ("qiskit_version", "mqt_qmap_version",
                        "mqt_core_version", "python_version"):
                if key in stats:
                    manifest.package_versions[key] = str(stats[key])
        except (OSError, TypeError, json.JSONDecodeError) as error:
            manifest.warnings.append(f"invalid compiler_stats.json: {error}")

    if manifest.status == RunStatus.SUCCESS.value:
        try:
            manifest.validate(require_success_metrics=True)
        except ValueError as error:
            manifest.status = RunStatus.SCORER_ERROR.value
            manifest.error = f"incomplete success contract: {error}"

    # Verification, unified scoring, and package extraction have consumed the
    # raw compiler outputs.  Archive them before atomic promotion so every
    # terminal attempt is compact but remains independently replayable.
    try:
        _archive_attempt_artifacts(temporary)
    except BaseException as error:
        manifest.warnings.append(
            f"artifact archival failed: {type(error).__name__}: {error}")

    manifest.end_to_end_time_ns = time.perf_counter_ns() - start_wall
    manifest.ended_at_utc = _utc_now()
    manifest.write(temporary / "manifest.json")
    os.replace(temporary, final)
    manifest.artifact_dir = str(final)
    manifest.write(final / "manifest.json")
    return manifest


__all__ = ["AttemptSpec", "run_attempt"]
