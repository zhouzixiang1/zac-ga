"""Failure-safe attempt runner with unique directories and process-group limits."""

from __future__ import annotations

import dataclasses
import gzip
import json
import math
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
from .plan import effective_zac_setting
from .protocol import (ghost_policy_for_method,
                       physicalization_policy_for_method,
                       trace_protocol_for_method)


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
    layer_ledger_sha256: str = ""
    transition_count: Optional[int] = None
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
        if self.layer_ledger_sha256 and (
                len(self.layer_ledger_sha256) != 64 or any(
                    value not in "0123456789abcdef"
                    for value in self.layer_ledger_sha256)):
            raise ValueError("layer_ledger_sha256 must be a lowercase SHA256")
        if (self.transition_count is not None and
                (isinstance(self.transition_count, bool) or
                 not isinstance(self.transition_count, int) or
                 self.transition_count < 0)):
            raise ValueError("transition_count must be a non-negative integer")


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
        "algorithm_revision": "algorithm_revision", "backend": "backend",
        "native_abi_version": "native_abi_version",
        "native_wheel_sha256": "native_wheel_sha256",
        "compiler_and_flags": "compiler_and_flags",
        "tuning_protocol_id": "tuning_protocol_id",
        "rng_version": "rng_version",
        "transition_decision_ns": "transition_decision_ns",
        "search_kernel_ns": "search_kernel_ns", "marshal_ns": "marshal_ns",
        "fitness_ns": "fitness_ns", "native_parse_ns": "native_parse_ns",
        "native_serialize_ns": "native_serialize_ns",
        "horizon_selection_ns": "horizon_selection_ns",
        "initial_placement_ns": "initial_placement_ns",
        "routing_ns": "routing_ns", "full_compile_ns": "full_compile_ns",
        "observed_transition_layer_ledger_sha256":
            "observed_transition_layer_ledger_sha256",
        "observed_transition_count": "observed_transition_count",
        "observed_transition_layer_ledger_source":
            "observed_transition_layer_ledger_source",
        "selected_horizon_counts": "selected_horizon_counts",
        "forecast_summary": "forecast_summary",
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
        layer_ledger_sha256=spec.layer_ledger_sha256,
        transition_count=spec.transition_count,
        canonical_input_layer_ledger_sha256=spec.layer_ledger_sha256,
        canonical_input_transition_count=spec.transition_count,
        trace_protocol=trace_protocol_for_method(spec.method),
        ghost_policy=ghost_policy_for_method(spec.method),
        physicalization_policy=physicalization_policy_for_method(spec.method),
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
            for key in (
                    "transition_decision_ns", "search_kernel_ns", "marshal_ns",
                    "fitness_ns", "native_parse_ns", "native_serialize_ns",
                    "horizon_selection_ns", "initial_placement_ns", "routing_ns",
                    "full_compile_ns"):
                if key in timing and timing[key] is not None:
                    setattr(manifest, key, int(timing[key]))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            manifest.warnings.append(f"invalid compiler_timing.json: {error}")

    stats_path = temporary / "compiler_stats.json"
    if stats_path.is_file():
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            if not isinstance(stats, Mapping):
                raise TypeError("compiler_stats.json must contain a JSON object")
            for key in (
                    "qiskit_version", "mqt_qmap_version",
                    "instrumented_mqt_qmap_version", "mqt_core_version",
                    "python_version", "qmap_timing_protocol",
                    "qmap_timing_patch_sha256",
                    "qmap_timing_parity_report_sha256",
                    "qmap_timing_wheel_sha256",
                    "qmap_timing_environment_lock_sha256"):
                if key in stats:
                    reported = str(stats[key])
                    frozen = manifest.package_versions.get(key)
                    if (key.startswith("qmap_timing_") and frozen is not None
                            and str(frozen) != reported):
                        raise ValueError(
                            f"QMAP timing provenance mismatch for {key}: "
                            f"{reported} != {frozen}")
                    manifest.package_versions[key] = reported
            for key in (
                    "algorithm_revision", "backend", "native_abi_version",
                    "native_wheel_sha256", "compiler_and_flags",
                    "tuning_protocol_id", "rng_version",
                    "observed_transition_layer_ledger_sha256",
                    "observed_transition_count",
                    "selected_horizon_counts", "forecast_summary"):
                if key in stats:
                    setattr(manifest, key, stats[key])
            reported_ledger_source = stats.get(
                "observed_transition_layer_ledger_source")
            if (reported_ledger_source is not None
                    and stats.get(
                        "observed_transition_layer_ledger_sha256")
                    and stats.get("observed_transition_count") is not None
                    and
                    not manifest.observed_transition_layer_ledger_source):
                # QMAP's timing-only C++ patch currently exposes counters but
                # no layer container.  Its placeholder declaration may never
                # replace the independently normalised placement-trace proof
                # supplied by the scorer.
                manifest.observed_transition_layer_ledger_source = str(
                    reported_ledger_source)
            if manifest.status == RunStatus.SUCCESS.value:
                expected_protocol = {
                    "trace_protocol": manifest.trace_protocol,
                    "ghost_policy": manifest.ghost_policy,
                    "physicalization": manifest.physicalization_policy,
                }
                drift = {
                    key: (stats.get(key), expected)
                    for key, expected in expected_protocol.items()
                    if stats.get(key) != expected
                }
                if drift:
                    raise ValueError(
                        f"compiler protocol declaration mismatch: {drift}")
                if manifest.method in {"M1", "M2"}:
                    repairs = {
                        key: stats.get(key, 0)
                        for key in ("ghost_repairs", "ghost_splits")
                    }
                    if any(value != 0 for value in repairs.values()):
                        raise ValueError(
                            "paper baseline compiler reported external ghost repair: "
                            f"{repairs}")
                if manifest.method == "M1":
                    expected_source = (
                        Path(spec.repo_root).resolve() / "ZAC" / "zac" / "zac.py"
                    ).resolve()
                    reported_source = Path(str(
                        stats.get("compiler_source_file", ""))).resolve()
                    identity = {
                        "compiler_module": (stats.get("compiler_module"), "zac.zac"),
                        "routing_strategy": (
                            stats.get("routing_strategy"), "maximalis_sort"),
                        "compiler_source_file": (
                            str(reported_source), str(expected_source)),
                    }
                    identity_drift = {
                        key: value for key, value in identity.items()
                        if value[0] != value[1]
                    }
                    if identity_drift:
                        raise ValueError(
                            f"M1 original ZAC identity mismatch: {identity_drift}")
                if manifest.method == "M2":
                    identity = {
                        "compiler_class": (
                            stats.get("compiler_class"), "RoutingAwareCompiler"),
                        "fallback": (stats.get("fallback"), False),
                        "mqt_qmap_version": (
                            stats.get("mqt_qmap_version"), "3.2.0"),
                        "mqt_core_version": (
                            stats.get("mqt_core_version"), "3.1.0"),
                    }
                    identity_drift = {
                        key: value for key, value in identity.items()
                        if value[0] != value[1]
                    }
                    if identity_drift:
                        raise ValueError(
                            f"M2 routing-aware compiler identity mismatch: "
                            f"{identity_drift}")
                    native_path = temporary / "trace.na"
                    if (not native_path.is_file() or
                            stats.get("native_sha256") != sha256_file(native_path)):
                        raise ValueError(
                            "M2 native trace differs from the official compiler output hash")
                if manifest.method in {"M3", "M4"}:
                    try:
                        config_payload = json.loads(
                            Path(spec.config_path).read_text(encoding="utf-8"))
                        if not isinstance(config_payload, Mapping):
                            raise TypeError("native configuration must be an object")
                        config = effective_zac_setting(config_payload)
                    except (OSError, TypeError, json.JSONDecodeError) as error:
                        raise ValueError(
                            f"cannot read frozen native configuration: {error}") from error
                    formal_native = (
                        config.get("backend") == "native" or
                        manifest.run_kind in {
                            "coverage", "main", "timing", "ablation", "large"
                        })
                    if not formal_native:
                        config = None
                if manifest.method in {"M3", "M4"} and config is not None:
                    flags = stats.get("compiler_and_flags")
                    if not isinstance(flags, Mapping):
                        flags = {}
                    native_identity = {
                        "backend": (stats.get("backend"), "native"),
                        "fallback": (stats.get("fallback"), False),
                        "algorithm_revision": (
                            stats.get("algorithm_revision"),
                            config.get("algorithm_revision")),
                        "native_abi_version": (
                            stats.get("native_abi_version"),
                            config.get("native_abi_version")),
                        "native_wheel_sha256": (
                            stats.get("native_wheel_sha256"),
                            config.get("native_wheel_sha256")),
                        "tuning_protocol_id": (
                            stats.get("tuning_protocol_id"),
                            config.get("tuning_protocol_id")),
                        "rng_version": (
                            stats.get("rng_version"), config.get("rng_version")),
                        "cxx_standard": (flags.get("cxx_standard"), 17),
                        "openmp": (flags.get("openmp"), False),
                        "fast_math": (flags.get("fast_math"), False),
                    }
                    identity_drift = {
                        key: value for key, value in native_identity.items()
                        if value[0] != value[1]
                    }
                    if identity_drift:
                        raise ValueError(
                            f"M3/M4 fail-closed native identity mismatch: "
                            f"{identity_drift}")
                    reported_transition_count = stats.get("transition_count")
                    if manifest.run_kind in {
                            "coverage", "main", "timing", "ablation", "large"}:
                        if not manifest.canonical_input_layer_ledger_sha256:
                            raise ValueError(
                                "M3/M4 is missing the canonical-input "
                                "2Q-layer ledger")
                        if (not manifest.observed_transition_layer_ledger_sha256
                                or manifest.observed_transition_layer_ledger_source !=
                                "compiler.gate_scheduling"):
                            raise ValueError(
                                "M3/M4 is missing its independently observed "
                                "gate_scheduling ledger")
                        if (reported_transition_count !=
                                manifest.observed_transition_count):
                            raise ValueError(
                                "native decision count differs from the observed "
                                "gate_scheduling transitions: "
                                f"{reported_transition_count} != "
                                f"{manifest.observed_transition_count}")
                    if manifest.selected_horizon_counts:
                        raise ValueError(
                            "formal decay run may not publish legacy "
                            "selected_horizon_counts")
                    summary = manifest.forecast_summary
                    if not isinstance(summary, Mapping) or not summary:
                        raise ValueError(
                            "formal decay run is missing forecast_summary")
                    lookahead = config.get("lookahead_horizon")
                    if not isinstance(lookahead, Mapping):
                        raise ValueError("formal lookahead spec must be an object")
                    expected_depth = 0 if manifest.method == "M3" else 8
                    expected_forecast = {
                        "mode": lookahead.get("mode"),
                        "policy": lookahead.get("policy"),
                        "decay": lookahead.get("decay"),
                        "configured_depth": expected_depth,
                        "rho": float(lookahead.get("rho")),
                        "epsilon": float(lookahead.get("epsilon")),
                        "alpha_lookahead": float(config.get("alpha_lookahead")),
                        "transition_count": int(reported_transition_count),
                    }
                    drift = {
                        key: (summary.get(key), value)
                        for key, value in expected_forecast.items()
                        if summary.get(key) != value
                    }
                    if drift:
                        raise ValueError(
                            f"forecast_summary/config drift: {drift}")
                    factor = 1.0
                    expected_weights = []
                    for offset in range(1, expected_depth + 1):
                        factor = float(lookahead["rho"]) ** (offset - 1)
                        if factor < float(lookahead["epsilon"]):
                            break
                        expected_weights.append({
                            "offset": offset,
                            "decay_factor": factor,
                            "weight": float(config["alpha_lookahead"]) * factor,
                        })
                    if summary.get("offset_weights") != expected_weights:
                        raise ValueError(
                            "forecast_summary offset weights do not match the "
                            "bare-factor cutoff contract")
                    effective_depth = len(expected_weights)
                    effective_counts = summary.get("effective_depth_counts")
                    visible_counts = summary.get("visible_depth_counts")
                    if (not isinstance(effective_counts, Mapping)
                            or sum(effective_counts.values()) !=
                            int(reported_transition_count)
                            or any(int(key) != effective_depth
                                   for key in effective_counts)):
                        raise ValueError(
                            "forecast effective-depth counts are inconsistent")
                    if (not isinstance(visible_counts, Mapping)
                            or sum(visible_counts.values()) !=
                            int(reported_transition_count)
                            or any(int(key) > effective_depth
                                   for key in visible_counts)):
                        raise ValueError(
                            "forecast visible-depth counts are inconsistent")
                    weighted_total = summary.get(
                        "weighted_negative_log_fidelity_total")
                    if (isinstance(weighted_total, bool)
                            or not isinstance(weighted_total, (int, float))
                            or not math.isfinite(float(weighted_total))
                            or float(weighted_total) < 0.0):
                        raise ValueError("invalid forecast weighted NLL total")
                    if manifest.method == "M3" and not math.isclose(
                            float(weighted_total), 0.0,
                            rel_tol=0.0, abs_tol=1e-15):
                        raise ValueError("M3 consumed non-zero future heuristic")
        except (OSError, TypeError, ValueError, KeyError,
                json.JSONDecodeError) as error:
            if manifest.status == RunStatus.SUCCESS.value:
                manifest.status = RunStatus.SCORER_ERROR.value
                manifest.error = f"compiler protocol error: {error}"
            else:
                manifest.warnings.append(f"invalid compiler_stats.json: {error}")
    elif manifest.status == RunStatus.SUCCESS.value:
        manifest.status = RunStatus.SCORER_ERROR.value
        manifest.error = "compiler protocol error: missing compiler_stats.json"

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
