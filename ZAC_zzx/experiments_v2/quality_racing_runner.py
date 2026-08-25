"""Fail-closed execution for ``resident-ga-quality-racing-v2``.

Each scheduled receipt is addressed from a deterministic identity, and resume
accepts only that exact receipt and its hashed attempt manifest.  It never
scans an old result tree to infer success.  Quality trials may run in isolated
worker processes, while each compiler attempt remains single-threaded and the
five-circuit racing checkpoints remain strict barriers.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import multiprocessing
import os
import statistics
import sys
import uuid
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cli import UnifiedEvaluationGate, _attempt_spec, command_verify_run
from .contracts import (RunManifest, load_run_manifest, sha256_file,
                        repository_snapshot, stable_sha256)
from .plan import (ExperimentPlan, effective_zac_setting,
                   load_experiment_plan)
from .quality_racing import (
    DEVELOPMENT_CIRCUITS,
    DEVELOPMENT_COVERAGE_ONLY,
    DEVELOPMENT_TUNING_CIRCUITS,
    DEVELOPMENT_SEED,
    INCUMBENT_NON_DEGRADATION_TOLERANCE,
    PROTOCOL_ID,
    RACE_BLOCK_SIZE,
    RacingTrial,
    VALIDATION_CIRCUITS,
    VALIDATION_SEEDS,
    decision_candidates,
    incumbent_candidate,
    lookahead_candidates,
    materialize_formal_setting,
    race_checkpoint,
    search_profile_candidates,
    select_non_degrading,
    select_top,
    shared_forward_pair,
    split_manifest,
    search_space_manifest,
)
from .runner import run_attempt


STAGES = (
    "baselines", "profiles", "decisions", "lookahead", "validation",
    "shared_forward_check",
)

_SINGLE_THREAD_ENVIRONMENT = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}

_REUSE_ARTIFACT_VALIDATION_CACHE: set[tuple[str, str]] = set()
_HELD_ATTEMPT_LOCKS: set[str] = set()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True,
                      allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_same_or_fail(path: Path, payload: Any) -> None:
    if path.is_file():
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise FileExistsError(f"quality-racing artifact differs: {path}")
        return
    _atomic_json(path, payload)


def _record_sha256(payload: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items()
                if key != "record_sha256"}
    return hashlib.sha256(_stable_json(unsigned).encode()).hexdigest()


def _seal(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    value["record_sha256"] = _record_sha256(value)
    return value


def _validate_sealed(payload: Mapping[str, Any]) -> None:
    if payload.get("protocol_id") != PROTOCOL_ID:
        raise ValueError("quality-racing receipt protocol mismatch")
    if payload.get("record_sha256") != _record_sha256(payload):
        raise ValueError("quality-racing receipt hash mismatch")


def _validated_workers(workers: int) -> int:
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    return workers


def _record_parallel_execution(root: Path, workers: int) -> Mapping[str, Any] | None:
    """Record the quality-only parallel amendment without rewriting workspace."""
    workers = _validated_workers(workers)
    if workers == 1:
        return None
    payload = _seal({
        "experiment_schema": 2,
        "protocol_id": PROTOCOL_ID,
        "execution_scope": "quality-and-tuning-only",
        "workers": workers,
        "worker_isolation": "spawned-process",
        "receipt_identity_isolation": True,
        "single_thread_environment": dict(_SINGLE_THREAD_ENVIRONMENT),
        "racing_checkpoint_barrier": 5,
        "timing_benchmark_parallel": False,
    })
    _write_same_or_fail(
        root / "execution" / f"parallel-workers-{workers}.json", payload)
    return payload


def default_root(plan: ExperimentPlan) -> Path:
    return plan.output_root / "tuning-quality-v1"


def _candidate_registry_payload() -> dict[str, Any]:
    candidates = {
        method: search_profile_candidates(method) for method in ("M3", "M4")}
    payload = {
        "experiment_schema": 2,
        "protocol_id": PROTOCOL_ID,
        "stage": "profiles",
        "candidates": candidates,
    }
    payload["sha256"] = stable_sha256(payload)
    return payload


def _setting_native_identity(setting: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: setting.get(key) for key in (
            "algorithm_revision", "backend", "formal_native",
            "native_abi_version", "native_wheel_sha256",
            "tuning_protocol_id", "rng_version",
        )
    }


def _native_runtime_identity_payload(
        expected: Mapping[str, Any], *, python: str | None = None
) -> dict[str, Any]:
    """Verify and freeze the extension that the formal interpreter loads.

    Reading a wheel digest from JSON is not an environment check.  This calls
    the native backend's registered-wheel verifier, which binds the loaded
    extension bytes to the wheel archive before the first racing attempt can
    be scheduled.
    """
    if (python is not None
            and Path(python).resolve() != Path(sys.executable).resolve()):
        raise RuntimeError(
            "quality-racing prepare must run under the compiler Python "
            f"interpreter: {python}")

    from zzx.native_backend import build_info

    info = build_info(
        require_registered_wheel=True,
        expected_wheel_sha256=str(expected.get("native_wheel_sha256", "")),
    )
    required_flags = {
        "cxx_standard": 17,
        "openmp": False,
        "fast_math": False,
    }
    observed = {
        "native_abi_version": info.get("native_abi_version"),
        "native_wheel_sha256": info.get("native_wheel_sha256"),
        "rng_version": info.get("rng_version"),
        **required_flags,
    }
    expected_values = {
        "native_abi_version": expected.get("native_abi_version"),
        "native_wheel_sha256": expected.get("native_wheel_sha256"),
        "rng_version": expected.get("rng_version"),
        **required_flags,
    }
    observed.update({key: info.get(key) for key in required_flags})
    drift = {
        key: (observed.get(key), value)
        for key, value in expected_values.items()
        if observed.get(key) != value
    }
    if info.get("wheel_registered") is not True:
        drift["wheel_registered"] = (info.get("wheel_registered"), True)
    extension_sha256 = info.get("extension_sha256")
    if (not isinstance(extension_sha256, str)
            or len(extension_sha256) != 64
            or any(value not in "0123456789abcdef"
                   for value in extension_sha256)):
        drift["extension_sha256"] = (extension_sha256, "lowercase SHA256")
    if drift:
        raise ValueError(f"quality-racing loaded native runtime drift: {drift}")
    return {
        "native_abi_version": int(observed["native_abi_version"]),
        "native_wheel_sha256": str(observed["native_wheel_sha256"]),
        "rng_version": str(observed["rng_version"]),
        "extension_sha256": str(extension_sha256),
        "native_version": str(info.get("version", "")),
        "build_type": str(info.get("build_type", "")),
        "compiler_id": str(info.get("compiler_id", "")),
        "compiler_version": str(info.get("compiler_version", "")),
        "python": str(Path(sys.executable).resolve()),
        "cxx_standard": 17,
        "openmp": False,
        "fast_math": False,
    }


def _execution_identity_payload(plan: ExperimentPlan) -> dict[str, Any]:
    """Freeze the clean compiler/native identity shared by every tuning stage."""
    repository = repository_snapshot(plan.repo_root)
    if (repository.get("dirty") is not False or
            repository.get("commit") in (None, "", "unknown")):
        raise RuntimeError(
            "quality racing requires a clean, known Git commit")
    methods = {}
    native_identities = []
    for method in ("M3", "M4"):
        spec = plan.methods[method]
        setting = effective_zac_setting(spec.payload)
        native = _setting_native_identity(setting)
        methods[method] = {
            "config": str(spec.config_path),
            "config_sha256": sha256_file(spec.config_path),
            "native": native,
        }
        native_identities.append(native)
    if native_identities[0] != native_identities[1]:
        raise ValueError("M3/M4 native execution identities differ")
    native_runtime = _native_runtime_identity_payload(
        native_identities[0], python=plan.python)
    return _seal({
        "experiment_schema": 2,
        "protocol_id": PROTOCOL_ID,
        "kind": "quality-racing-execution-identity",
        "plan_sha256": sha256_file(plan.path),
        "repository": repository,
        "methods": methods,
        "native_runtime": native_runtime,
    })


def _incumbent_payload(plan: ExperimentPlan,
                       execution: Mapping[str, Any]) -> dict[str, Any]:
    methods = {}
    for method in ("M3", "M4"):
        spec = plan.methods[method]
        methods[method] = {
            "source_config": str(spec.config_path),
            "source_config_sha256": sha256_file(spec.config_path),
            "candidate": incumbent_candidate(
                method, effective_zac_setting(spec.payload)),
        }
    return _seal({
        "experiment_schema": 2,
        "protocol_id": PROTOCOL_ID,
        "kind": "tracked-pre-tuning-incumbents",
        "execution_identity_record_sha256": execution["record_sha256"],
        "methods": methods,
    })


def _read_execution_identity(root: Path) -> Mapping[str, Any]:
    path = root / "protocol" / "tuning_execution_identity.json"
    if not path.is_file():
        raise RuntimeError(
            "quality-racing execution identity is absent; rerun prepare")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_sealed(payload)
    if payload.get("kind") != "quality-racing-execution-identity":
        raise ValueError("quality-racing execution identity kind mismatch")
    repository = payload.get("repository")
    methods = payload.get("methods")
    if (not isinstance(repository, Mapping)
            or repository.get("dirty") is not False
            or not isinstance(repository.get("commit"), str)
            or not repository["commit"]
            or not isinstance(methods, Mapping)
            or set(methods) != {"M3", "M4"}):
        raise ValueError("quality-racing execution identity is incomplete")
    native_identities = []
    for method in ("M3", "M4"):
        row = methods[method]
        native = row.get("native") if isinstance(row, Mapping) else None
        config_hash = row.get("config_sha256") if isinstance(row, Mapping) else None
        if (not isinstance(native, Mapping)
                or native.get("backend") != "native"
                or native.get("formal_native") is not True
                or native.get("tuning_protocol_id") != PROTOCOL_ID
                or not isinstance(native.get("native_abi_version"), int)
                or isinstance(native.get("native_abi_version"), bool)
                or native["native_abi_version"] <= 0
                or not isinstance(native.get("native_wheel_sha256"), str)
                or len(native["native_wheel_sha256"]) != 64
                or not isinstance(config_hash, str) or len(config_hash) != 64):
            raise ValueError(
                f"quality-racing {method} execution identity is malformed")
        native_identities.append(dict(native))
    if native_identities[0] != native_identities[1]:
        raise ValueError("quality-racing M3/M4 native identities differ")
    runtime = payload.get("native_runtime")
    if (not isinstance(runtime, Mapping)
            or runtime.get("native_abi_version") !=
            native_identities[0].get("native_abi_version")
            or runtime.get("native_wheel_sha256") !=
            native_identities[0].get("native_wheel_sha256")
            or runtime.get("rng_version") !=
            native_identities[0].get("rng_version")
            or runtime.get("cxx_standard") != 17
            or runtime.get("openmp") is not False
            or runtime.get("fast_math") is not False
            or not isinstance(runtime.get("extension_sha256"), str)
            or len(runtime["extension_sha256"]) != 64):
        raise ValueError("quality-racing loaded native runtime is malformed")
    return payload


def _load_execution_identity(plan: ExperimentPlan,
                             root: Path) -> Mapping[str, Any]:
    frozen = _read_execution_identity(root)
    current = _execution_identity_payload(plan)
    if frozen != current:
        raise ValueError(
            "quality-racing Git/config/native execution identity changed")
    return frozen


def _read_incumbents(root: Path) -> Mapping[str, Any]:
    path = root / "protocol" / "incumbent_configs.json"
    if not path.is_file():
        raise RuntimeError("quality-racing incumbent freeze is absent; rerun prepare")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_sealed(payload)
    if payload.get("kind") != "tracked-pre-tuning-incumbents":
        raise ValueError("quality-racing incumbent freeze kind mismatch")
    methods = payload.get("methods")
    if not isinstance(methods, Mapping) or set(methods) != {"M3", "M4"}:
        raise ValueError("quality-racing incumbent freeze is incomplete")
    for method in ("M3", "M4"):
        row = methods[method]
        candidate = row.get("candidate") if isinstance(row, Mapping) else None
        if (not isinstance(candidate, Mapping)
                or candidate.get("method") != method
                or not isinstance(candidate.get("candidate_id"), str)
                or not candidate["candidate_id"]):
            raise ValueError(f"quality-racing {method} incumbent is malformed")
    execution = _read_execution_identity(root)
    if payload.get("execution_identity_record_sha256") != \
            execution.get("record_sha256"):
        raise ValueError("incumbent freeze uses another execution identity")
    for method in ("M3", "M4"):
        row = methods[method]
        frozen = execution["methods"][method]
        if (row.get("source_config") != frozen.get("config")
                or row.get("source_config_sha256") !=
                frozen.get("config_sha256")):
            raise ValueError(
                f"quality-racing {method} incumbent source config drift")
    return payload


def prepare_workspace(plan: ExperimentPlan, root: Path | None = None
                      ) -> Mapping[str, Any]:
    root = (root or default_root(plan)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    execution = _execution_identity_payload(plan)
    incumbents = _incumbent_payload(plan, execution)
    _write_same_or_fail(
        root / "protocol" / "tuning_execution_identity.json", execution)
    _write_same_or_fail(
        root / "protocol" / "incumbent_configs.json", incumbents)
    _write_same_or_fail(
        root / "protocol" / "split_manifest.json", split_manifest())
    _write_same_or_fail(
        root / "protocol" / "tuning_space.json", search_space_manifest())
    committed = plan.package_root / "exp_setting" / "native_ga_v1"
    if json.loads((committed / "split_manifest.json").read_text()) != \
            split_manifest():
        raise ValueError("committed quality-racing split differs from code")
    registry = _candidate_registry_payload()
    _write_same_or_fail(root / "candidates" / "profiles.json", registry)
    workspace = {
        "experiment_schema": 2,
        "protocol_id": PROTOCOL_ID,
        "root": str(root),
        "plan": str(plan.path),
        "plan_sha256": sha256_file(plan.path),
        "split_sha256": split_manifest()["sha256"],
        "candidate_registry_sha256": registry["sha256"],
        "serial_execution": True,
        "stages": list(STAGES),
    }
    workspace["record_sha256"] = _record_sha256(workspace)
    _write_same_or_fail(root / "workspace.json", workspace)
    return workspace


def _load_workspace(plan: ExperimentPlan, root: Path) -> Mapping[str, Any]:
    payload = json.loads((root / "workspace.json").read_text())
    _validate_sealed(payload)
    if payload.get("plan") != str(plan.path):
        raise ValueError("quality-racing workspace plan mismatch")
    if payload.get("plan_sha256") != sha256_file(plan.path):
        raise ValueError("quality-racing plan changed after prepare")
    if payload.get("split_sha256") != split_manifest()["sha256"]:
        raise ValueError("quality-racing split changed after prepare")
    if payload.get("candidate_registry_sha256") != \
            _candidate_registry_payload()["sha256"]:
        raise ValueError("quality-racing candidate registry changed after prepare")
    _load_execution_identity(plan, root)
    _read_incumbents(root)
    return payload


def _canonical_registry(plan: ExperimentPlan
                        ) -> dict[str, tuple[Any, Any]]:
    registry = {}
    for dataset in plan.select_datasets(None, kind="main"):
        for canonical in plan.load_suite(dataset):
            stem = Path(canonical.canonical_path).stem
            name = (stem[:-len("_transpiled")]
                    if stem.endswith("_transpiled") else stem)
            if name in registry:
                raise ValueError(f"duplicate circuit across main suites: {name}")
            registry[name] = (dataset, canonical)
    expected = set(DEVELOPMENT_CIRCUITS) | set(VALIDATION_CIRCUITS)
    missing = expected - set(registry)
    if missing:
        raise ValueError(
            f"quality-racing circuits absent from canonical suites: {sorted(missing)}")
    return registry


def _config_payload(plan: ExperimentPlan, candidate: Mapping[str, Any],
                    *, seed: int, destination: Path) -> Path:
    method = str(candidate["method"])
    base = copy.deepcopy(dict(plan.methods[method].payload))
    setting = materialize_formal_setting(
        effective_zac_setting(base), candidate, seed=seed,
        output_dir=f"results/tuning-quality-v1/{candidate['candidate_id']}/")
    if "zac_setting" in base:
        base["zac_setting"] = [setting]
        base["tuning"] = {
            "algorithm_revision": setting["algorithm_revision"],
            "candidate_id": candidate["candidate_id"],
            "protocol_id": PROTOCOL_ID,
            "selection_status": "quality_racing_trial",
            "track": "independent",
            "stage": candidate["stage"],
        }
    else:
        base = setting
    _write_same_or_fail(destination, base)
    return destination


def _candidate_execution_identity(
        plan: ExperimentPlan, root: Path, *, method: str, config: Path,
        frozen: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    frozen = frozen or _load_execution_identity(plan, root)
    method_freeze = frozen["methods"][method]
    config_payload = json.loads(config.read_text(encoding="utf-8"))
    setting = effective_zac_setting(config_payload)
    native = _setting_native_identity(setting)
    if native != method_freeze["native"]:
        raise ValueError(
            f"{method} candidate changes frozen native execution identity")
    repository = frozen["repository"]
    return {
        "execution_identity_record_sha256": frozen["record_sha256"],
        "git_commit": repository["commit"],
        "git_dirty": False,
        "config_sha256": sha256_file(config),
        **native,
    }


def _receipt_path(root: Path, stage: str, method: str, candidate_id: str,
                  dataset: str, circuit: str, seed: int) -> Path:
    return (root / "receipts" / stage / method / candidate_id / dataset /
            f"{circuit}.seed-{seed}.json")


def _attempt_manifest_path(manifest: RunManifest) -> Path:
    return Path(manifest.artifact_dir) / "manifest.json"


def _attempt_artifact_sha256(manifest_path: Path) -> dict[str, str]:
    """Hash every promoted attempt file except the separately hashed manifest."""
    root = manifest_path.parent.resolve()
    values = {}
    for artifact in sorted(root.rglob("*"), key=lambda value: str(value)):
        if not artifact.is_file() or artifact.resolve() == manifest_path.resolve():
            continue
        relative = artifact.resolve().relative_to(root).as_posix()
        values[relative] = sha256_file(artifact)
    return values


@contextmanager
def _attempt_identity_lock(receipt: Path, identity: Mapping[str, Any]):
    """Hold one OS-released lock for a receipt identity across CLI processes."""
    receipt.parent.mkdir(parents=True, exist_ok=True)
    claim = receipt.with_name(f".{receipt.name}.lock")
    claim_key = str(claim.resolve())
    if claim_key in _HELD_ATTEMPT_LOCKS:
        raise RuntimeError(f"quality-racing identity is already running: {receipt}")
    try:
        descriptor = os.open(
            claim, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = os.open(claim, os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                f"quality-racing identity is already running: {receipt}") from error
        _HELD_ATTEMPT_LOCKS.add(claim_key)
        metadata = (_stable_json({
            "pid": os.getpid(),
            "identity_sha256": stable_sha256(dict(identity)),
        }) + "\n").encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.write(descriptor, metadata)
        os.fsync(descriptor)
        yield
    finally:
        _HELD_ATTEMPT_LOCKS.discard(claim_key)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _native_manifest_identity_complete(
        manifest: RunManifest, execution: Mapping[str, Any]) -> bool:
    observed = {
        "algorithm_revision": manifest.algorithm_revision,
        "backend": manifest.backend,
        "native_abi_version": manifest.native_abi_version,
        "native_wheel_sha256": manifest.native_wheel_sha256,
        "tuning_protocol_id": manifest.tuning_protocol_id,
        "rng_version": manifest.rng_version,
    }
    return all(
        value not in (None, "") and value == execution.get(key)
        for key, value in observed.items())


def _retryable_native_environment_failure(
        payload: Mapping[str, Any]) -> bool:
    """Return true when a compiler error never proved the frozen runtime."""
    identity = payload.get("identity")
    execution = identity.get("execution") if isinstance(identity, Mapping) else None
    if (payload.get("status") != "compiler_error"
            or not isinstance(execution, Mapping)):
        return False
    manifest_path = Path(str(payload.get("attempt_manifest", "")))
    if not manifest_path.is_file():
        return False
    manifest = load_run_manifest(manifest_path)
    return not _native_manifest_identity_complete(manifest, execution)


def _validate_attempt_receipt(path: Path, expected: Mapping[str, Any]
                              ) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_sealed(payload)
    identity = payload.get("identity")
    if identity != dict(expected):
        raise ValueError(f"quality-racing receipt identity drift: {path}")
    manifest_path = Path(str(payload["attempt_manifest"]))
    if (not manifest_path.is_file() or
            sha256_file(manifest_path) != payload["attempt_manifest_sha256"]):
        raise ValueError(f"quality-racing attempt manifest drift: {path}")
    manifest = load_run_manifest(manifest_path)
    for key in ("dataset", "circuit", "method", "seed"):
        if getattr(manifest, key) != identity[key]:
            raise ValueError(f"attempt identity {key} drift: {path}")
    if manifest.status != payload["status"]:
        raise ValueError(f"attempt status drift: {path}")
    execution = identity.get("execution") if isinstance(identity, Mapping) else None
    if isinstance(execution, Mapping):
        config_path = Path(str(payload.get("config", "")))
        config_sha256 = str(execution.get("config_sha256", ""))
        if (payload.get("config_sha256") != config_sha256
                or not config_path.is_file()
                or sha256_file(config_path) != config_sha256
                or manifest.config_sha256 != config_sha256):
            raise ValueError(f"quality-racing candidate config drift: {path}")
        if (manifest.git_commit != execution.get("git_commit")
                or manifest.git_dirty is not False):
            raise ValueError(f"quality-racing candidate Git drift: {path}")
        manifest_fields = {
            "algorithm_revision": manifest.algorithm_revision,
            "backend": manifest.backend,
            "native_abi_version": manifest.native_abi_version,
            "native_wheel_sha256": manifest.native_wheel_sha256,
            "tuning_protocol_id": manifest.tuning_protocol_id,
            "rng_version": manifest.rng_version,
        }
        expected_fields = {
            key: execution.get(key) for key in manifest_fields
        }
        compiler_completed = manifest.status in {
            "success", "verifier_fail", "scorer_error"
        }
        for key, observed in manifest_fields.items():
            if ((compiler_completed or observed not in (None, ""))
                    and observed != expected_fields[key]):
                raise ValueError(
                    f"quality-racing candidate {key} drift: {path}")
        if manifest.status == "success":
            artifact_hashes = payload.get("attempt_artifact_sha256")
            if not isinstance(artifact_hashes, Mapping) or not artifact_hashes:
                raise ValueError(
                    f"quality-racing success artifact hashes are absent: {path}")
            if dict(artifact_hashes) != _attempt_artifact_sha256(manifest_path):
                raise ValueError(
                    f"quality-racing success artifact drift: {path}")
    reuse = payload.get("reuse")
    if isinstance(reuse, Mapping):
        cache_key = (str(path.resolve()), str(payload["record_sha256"]))
        if cache_key not in _REUSE_ARTIFACT_VALIDATION_CACHE:
            source_receipt = Path(str(reuse.get("source_receipt", "")))
            if (not source_receipt.is_file() or
                    sha256_file(source_receipt) !=
                    reuse.get("source_receipt_sha256")):
                raise ValueError(f"reused source receipt drift: {path}")
            artifact_hashes = reuse.get("source_artifact_sha256")
            if not isinstance(artifact_hashes, Mapping) or not artifact_hashes:
                raise ValueError(f"reused artifact hashes are absent: {path}")
            for name, expected_hash in artifact_hashes.items():
                artifact = manifest_path.parent / str(name)
                if (not artifact.is_file() or
                        sha256_file(artifact) != expected_hash):
                    raise ValueError(
                        f"reused source artifact drift: {artifact}")
            _REUSE_ARTIFACT_VALIDATION_CACHE.add(cache_key)
    return payload


def _run_one(
        plan: ExperimentPlan, root: Path, registry: Mapping[str, tuple[Any, Any]],
        *, stage: str, method: str, candidate: Mapping[str, Any] | None,
        circuit: str, seed: int, resume: bool, dry_run: bool
) -> Mapping[str, Any]:
    dataset, canonical = registry[circuit]
    canonical_circuit = Path(canonical.canonical_path).stem
    candidate_id = ("paper-original" if candidate is None
                    else str(candidate["candidate_id"]))
    if candidate is None:
        config = plan.methods[method].config_path
    else:
        config = _config_payload(
            plan, candidate, seed=seed,
            destination=(root / "configs" / candidate_id /
                         f"seed-{seed}.json"))
    identity = {
        "stage": stage,
        "dataset": dataset.name,
        "circuit": canonical_circuit,
        "circuit_key": circuit,
        "method": method,
        "candidate_id": candidate_id,
        "seed": int(seed),
    }
    if candidate is not None:
        identity["execution"] = _candidate_execution_identity(
            plan, root, method=method, config=config)
    receipt = _receipt_path(
        root, stage, method, candidate_id, dataset.name, circuit, seed)
    if receipt.is_file():
        if not resume:
            raise FileExistsError(
                f"quality-racing receipt exists; use resume: {receipt}")
        existing = _validate_attempt_receipt(receipt, identity)
        if not _retryable_native_environment_failure(existing):
            return existing

    spec = _attempt_spec(
        plan, dataset, canonical, method, seed, 0, "smoke",
        config_path=config,
        experiment_id=stable_sha256({
            "protocol_id": PROTOCOL_ID, **identity,
            "config_sha256": sha256_file(config),
        }),
    )
    spec = replace(
        spec,
        output_root=(root / "attempts" / stage / method / candidate_id /
                     dataset.name / circuit / f"seed-{seed}"),
        run_kind="smoke",
    )
    if dry_run:
        return {
            "protocol_id": PROTOCOL_ID,
            "identity": identity,
            "config": str(config),
            "command": list(spec.command),
            "dry_run": True,
        }
    with _attempt_identity_lock(receipt, identity):
        # Another CLI may have completed this identity between the optimistic
        # existence check above and acquisition of the per-receipt lock.
        if receipt.is_file():
            if not resume:
                raise FileExistsError(
                    f"quality-racing receipt exists; use resume: {receipt}")
            existing = _validate_attempt_receipt(receipt, identity)
            if not _retryable_native_environment_failure(existing):
                return existing
        gate = UnifiedEvaluationGate(plan, canonical, method)
        manifest = run_attempt(spec, verifier=gate.verifier, scorer=gate.scorer)
        manifest_path = _attempt_manifest_path(manifest)
        receipt_body = {
            "experiment_schema": 2,
            "protocol_id": PROTOCOL_ID,
            "identity": identity,
            "config": str(config),
            "config_sha256": sha256_file(config),
            "status": manifest.status,
            "attempt_manifest": str(manifest_path),
            "attempt_manifest_sha256": sha256_file(manifest_path),
        }
        if candidate is not None and manifest.status == "success":
            artifact_hashes = _attempt_artifact_sha256(manifest_path)
            if not artifact_hashes:
                raise RuntimeError(
                    "quality-racing success attempt has no replay artifacts")
            receipt_body["attempt_artifact_sha256"] = artifact_hashes
        payload = _seal(receipt_body)
        _atomic_json(receipt, payload)
        return _validate_attempt_receipt(receipt, identity)


def _run_one_process(task: Mapping[str, Any]) -> Mapping[str, Any]:
    """Run one identity in an isolated process for accurate child accounting."""
    for key, value in _SINGLE_THREAD_ENVIRONMENT.items():
        os.environ[key] = value
    plan = load_experiment_plan(Path(str(task["plan_path"])))
    root = Path(str(task["root"])).resolve()
    return _run_one(
        plan, root, _canonical_registry(plan),
        stage=str(task["stage"]), method=str(task["method"]),
        candidate=task.get("candidate"), circuit=str(task["circuit"]),
        seed=int(task["seed"]), resume=bool(task["resume"]),
        dry_run=bool(task["dry_run"]),
    )


def _trial_tasks(
        plan: ExperimentPlan, root: Path, *, stage: str, method: str,
        candidates: Sequence[Mapping[str, Any] | None],
        circuits: Sequence[str], seeds: Sequence[int], resume: bool,
        dry_run: bool) -> list[dict[str, Any]]:
    return [
        {
            "plan_path": str(plan.path),
            "root": str(root),
            "stage": stage,
            "method": method,
            "candidate": None if candidate is None else dict(candidate),
            "circuit": circuit,
            "seed": int(seed),
            "resume": bool(resume),
            "dry_run": bool(dry_run),
        }
        for circuit in circuits
        for candidate in candidates
        for seed in seeds
    ]


def _receipt_manifest(payload: Mapping[str, Any]) -> RunManifest:
    return load_run_manifest(Path(str(payload["attempt_manifest"])))


def _valid_original_baselines(
        baselines: Sequence[RunManifest]) -> tuple[RunManifest, ...]:
    return tuple(
        row for row in baselines
        if (row.status == "success" and row.verifier_ok is True
            and row.exponential_sensitivity_log_fidelity is not None
            and (row.fidelity_ood or row.log_fidelity is not None)))


def _strongest_original_scores(
        baselines: Sequence[RunManifest]) -> tuple[float | None, float]:
    """Return the strongest usable linear and exponential baseline scores.

    A linear-coherence OOD trace deliberately has no linear ``log_fidelity``.
    It is nevertheless a valid comparator under the registered exponential
    sensitivity model, so its missing linear value must not abort the race.
    """
    valid = _valid_original_baselines(baselines)
    if not valid:
        raise ValueError("no valid original baseline scores")
    linear_values = [
        float(row.log_fidelity) for row in valid
        if row.log_fidelity is not None
    ]
    return (
        max(linear_values) if linear_values else None,
        max(float(row.exponential_sensitivity_log_fidelity)
            for row in valid),
    )


def run_baselines(plan: ExperimentPlan, root: Path, *, resume: bool = True,
                  dry_run: bool = False,
                  workers: int = 1) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    workers = _validated_workers(workers)
    tasks = [
        {
            "plan_path": str(plan.path),
            "root": str(root),
            "stage": "baselines",
            "method": method,
            "candidate": None,
            "circuit": circuit,
            "seed": 0,
            "resume": bool(resume),
            "dry_run": bool(dry_run),
        }
        for circuit in DEVELOPMENT_CIRCUITS + VALIDATION_CIRCUITS
        for method in ("M1", "M2")
    ]
    if workers == 1:
        registry = _canonical_registry(plan)
        outputs = [
            _run_one(
                plan, root, registry, stage="baselines",
                method=str(task["method"]), candidate=None,
                circuit=str(task["circuit"]), seed=0,
                resume=resume, dry_run=dry_run)
            for task in tasks
        ]
    else:
        _record_parallel_execution(root, workers)
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers,
                                 mp_context=context) as pool:
            outputs = list(pool.map(
                _run_one_process, tasks, chunksize=1))
    return {"protocol_id": PROTOCOL_ID, "stage": "baselines",
            "planned": 60, "outputs": outputs}


def _source_attempt_manifest(source_root: Path,
                             receipt: Mapping[str, Any]) -> Path:
    """Resolve a moved attempt only through its ``attempts/`` suffix.

    Quality-racing workspaces may be recoverably archived after a protocol
    revision.  Their sealed receipts retain the former absolute path, so an
    importer must not trust that path directly.  Re-rooting the immutable
    suffix both supports archival moves and prevents a receipt from selecting
    an unrelated manifest outside the explicitly supplied source workspace.
    """
    recorded = Path(str(receipt["attempt_manifest"]))
    try:
        marker = recorded.parts.index("attempts")
    except ValueError as error:
        raise ValueError(
            "source baseline receipt lacks an attempts/ manifest suffix"
        ) from error
    manifest = (source_root / Path(*recorded.parts[marker:])).resolve()
    attempts_root = (source_root / "attempts").resolve()
    if not manifest.is_relative_to(attempts_root):
        raise ValueError("source baseline manifest escapes source attempts")
    return manifest


def _baseline_split_inventory(split: Mapping[str, Any]
                              ) -> dict[str, dict[str, tuple[str, ...]]]:
    """Extract only the circuit inventory relevant to immutable baselines.

    A tuning-protocol amendment may change ranking/coverage-only metadata
    without changing any M1/M2 input.  Baseline reuse is safe exactly when the
    ordered development and validation circuit inventories remain identical;
    requiring the whole split JSON to match would force needless baseline
    reruns after such an amendment.
    """
    if int(split.get("experiment_schema", -1)) != 2:
        raise ValueError("source baseline split has the wrong schema")
    inventory: dict[str, dict[str, tuple[str, ...]]] = {}
    for cohort in ("development", "validation"):
        section = split.get(cohort)
        if not isinstance(section, Mapping):
            raise ValueError(f"source baseline split lacks {cohort}")
        inventory[cohort] = {}
        for dataset in ("ZAC18", "QMAP154"):
            circuits = section.get(dataset)
            if (not isinstance(circuits, list)
                    or not all(isinstance(row, str) for row in circuits)):
                raise ValueError(
                    f"source baseline split has invalid {cohort}/{dataset}")
            inventory[cohort][dataset] = tuple(circuits)
    return inventory


def import_baselines(plan: ExperimentPlan, root: Path, source_root: Path
                     ) -> Mapping[str, Any]:
    """Reuse actual paper-original M1/M2 attempts after immutable revalidation.

    This is deliberately separate from normal resume: only the 60 registered
    baseline identities are accepted, and every source manifest must match the
    current canonical input, method config, architecture, and scoring model.
    M3/M4 attempts are never imported.  The new receipt records the archived
    source and continues to hash the original immutable manifest.
    """
    workspace = _load_workspace(plan, root)
    source_root = source_root.resolve()
    if source_root == root.resolve():
        raise ValueError("source and destination quality workspaces are equal")
    source_split = json.loads((
        source_root / "protocol" / "split_manifest.json"
    ).read_text(encoding="utf-8"))
    if (_baseline_split_inventory(source_split)
            != _baseline_split_inventory(split_manifest())):
        raise ValueError(
            "source baseline circuit inventory differs from current protocol")

    registry = _canonical_registry(plan)
    outputs = []
    status_counts: dict[str, int] = {}
    for circuit in DEVELOPMENT_CIRCUITS + VALIDATION_CIRCUITS:
        dataset, canonical = registry[circuit]
        canonical_circuit = Path(canonical.canonical_path).stem
        for method in ("M1", "M2"):
            identity = {
                "stage": "baselines", "dataset": dataset.name,
                "circuit": canonical_circuit, "circuit_key": circuit,
                "method": method, "candidate_id": "paper-original",
                "seed": 0,
            }
            source_receipt_path = _receipt_path(
                source_root, "baselines", method, "paper-original",
                dataset.name, circuit, 0)
            source_receipt = json.loads(
                source_receipt_path.read_text(encoding="utf-8"))
            _validate_sealed(source_receipt)
            if source_receipt.get("identity") != identity:
                raise ValueError(
                    f"source baseline identity drift: {source_receipt_path}")
            manifest_path = _source_attempt_manifest(
                source_root, source_receipt)
            if (not manifest_path.is_file() or
                    sha256_file(manifest_path) !=
                    source_receipt.get("attempt_manifest_sha256")):
                raise ValueError(
                    f"source baseline manifest drift: {manifest_path}")
            manifest = load_run_manifest(manifest_path)
            for key in ("dataset", "circuit", "method", "seed"):
                if getattr(manifest, key) != identity[key]:
                    raise ValueError(
                        f"source baseline manifest {key} drift: "
                        f"{manifest_path}")
            current_hashes = {
                "input_sha256": sha256_file(Path(canonical.canonical_path)),
                "config_sha256": sha256_file(
                    plan.methods[method].config_path),
                "architecture_sha256": sha256_file(plan.architecture_path),
                "model_sha256": sha256_file(plan.model_path),
            }
            for field, expected in current_hashes.items():
                if getattr(manifest, field) != expected:
                    raise ValueError(
                        f"source baseline immutable {field} drift: "
                        f"{manifest_path}")
            if manifest.status != source_receipt.get("status"):
                raise ValueError(
                    f"source baseline status drift: {source_receipt_path}")

            if manifest.status == "success":
                revalidation = dict(command_verify_run(plan, manifest_path))
                revalidation["status"] = "success"
            elif manifest.status == "verifier_fail":
                try:
                    UnifiedEvaluationGate(
                        plan, canonical, method, write_artifacts=False
                    ).evaluate(manifest_path.parent)
                except Exception as error:  # preserve the original failure
                    observed = f"{type(error).__name__}: {error}"
                    if observed not in str(manifest.error):
                        raise ValueError(
                            "source verifier failure changed under current "
                            f"gate: {manifest_path}: {observed}") from error
                    revalidation = {
                        "verified": True,
                        "status": "verifier_fail",
                        "observed_exception": observed,
                    }
                else:
                    raise ValueError(
                        "source verifier failure now passes current gate: "
                        f"{manifest_path}")
            else:
                raise ValueError(
                    "only success/verifier_fail baseline evidence may be "
                    f"reused: {manifest_path}: {manifest.status}")

            source_artifact_hashes = {
                artifact.name: sha256_file(artifact)
                for artifact in sorted(manifest_path.parent.iterdir())
                if artifact.is_file() and artifact != manifest_path
            }
            if not source_artifact_hashes:
                raise ValueError(
                    f"source baseline has no raw artifacts: {manifest_path}")

            destination = _receipt_path(
                root, "baselines", method, "paper-original", dataset.name,
                circuit, 0)
            payload = _seal({
                "experiment_schema": 2,
                "protocol_id": PROTOCOL_ID,
                "identity": identity,
                "config": str(plan.methods[method].config_path),
                "config_sha256": current_hashes["config_sha256"],
                "status": manifest.status,
                "attempt_manifest": str(manifest_path),
                "attempt_manifest_sha256": sha256_file(manifest_path),
                "reuse": {
                    "kind": "immutable-paper-original-attempt",
                    "source_root": str(source_root),
                    "source_receipt": str(source_receipt_path),
                    "source_receipt_sha256": sha256_file(
                        source_receipt_path),
                    "source_artifact_sha256": source_artifact_hashes,
                    "verified_hashes": current_hashes,
                    "source_compile": {
                        "git_commit": manifest.git_commit,
                        "git_dirty": manifest.git_dirty,
                    },
                    "current_validation": {
                        "repository": repository_snapshot(plan.package_root),
                        "workspace_record_sha256": workspace["record_sha256"],
                        "plan_sha256": sha256_file(plan.path),
                        "split_sha256": split_manifest()["sha256"],
                        "result": revalidation,
                    },
                },
            })
            _write_same_or_fail(destination, payload)
            _validate_attempt_receipt(destination, identity)
            outputs.append(payload)
            status_counts[manifest.status] = (
                status_counts.get(manifest.status, 0) + 1)
    return {
        "protocol_id": PROTOCOL_ID,
        "stage": "baselines",
        "mode": "immutable-reuse",
        "planned": 60,
        "imported": len(outputs),
        "status_counts": status_counts,
        "source_root": str(source_root),
        "outputs": outputs,
    }


def _baseline_manifests(plan: ExperimentPlan, root: Path, circuit: str
                        ) -> tuple[RunManifest, RunManifest]:
    """Load both original baselines while requiring one valid comparator.

    An original compiler may itself emit an invalid physical trace (the ZAC
    many-to-one AOD-column merge is a real example).  Such a result remains a
    verifier failure and is never repaired or assigned a paper value.  Racing
    uses the strongest *valid* original method for that circuit and rejects the
    circuit only when neither baseline is valid.
    """
    registry = _canonical_registry(plan)
    dataset, canonical = registry[circuit]
    dataset = dataset.name
    canonical_circuit = Path(canonical.canonical_path).stem
    values = []
    for method in ("M1", "M2"):
        path = _receipt_path(
            root, "baselines", method, "paper-original", dataset, circuit, 0)
        if not path.is_file():
            raise RuntimeError(f"missing tuning baseline receipt: {path}")
        payload = _validate_attempt_receipt(path, {
            "stage": "baselines", "dataset": dataset,
            "circuit": canonical_circuit, "circuit_key": circuit,
            "method": method, "candidate_id": "paper-original", "seed": 0,
        })
        manifest = _receipt_manifest(payload)
        values.append(manifest)
    valid = _valid_original_baselines(values)
    if not valid:
        raise RuntimeError(
            f"no valid original tuning baseline for {dataset}/{circuit}")
    return values[0], values[1]


def _as_racing_trial(plan: ExperimentPlan, root: Path,
                     payload: Mapping[str, Any]) -> RacingTrial:
    identity = payload["identity"]
    manifest = _receipt_manifest(payload)
    baselines = _baseline_manifests(plan, root, str(identity["circuit_key"]))
    valid_baselines = _valid_original_baselines(baselines)
    baseline_linear, baseline_exponential = _strongest_original_scores(
        valid_baselines)
    return RacingTrial(
        candidate_id=str(identity["candidate_id"]),
        method=str(identity["method"]),
        dataset=str(identity["dataset"]),
        circuit=str(identity["circuit_key"]),
        seed=int(identity["seed"]),
        status=manifest.status,
        verifier_ok=manifest.verifier_ok is True,
        ghost_hits=(-1 if manifest.ghost_hits is None
                    else int(manifest.ghost_hits)),
        fallback=(manifest.backend != "native" or any(
            "fallback" in warning.lower() for warning in manifest.warnings)),
        log_fidelity=manifest.log_fidelity,
        baseline_log_fidelity=baseline_linear,
        move_time_us=manifest.move_time_us,
        move_batches=manifest.move_batches,
        transition_decision_ns=manifest.transition_decision_ns,
        fidelity_ood=bool(manifest.fidelity_ood),
        exponential_sensitivity_log_fidelity=(
            manifest.exponential_sensitivity_log_fidelity),
        baseline_fidelity_ood=any(
            row.fidelity_ood for row in valid_baselines),
        baseline_exponential_sensitivity_log_fidelity=baseline_exponential,
    )


def _run_candidate_trials(
        plan: ExperimentPlan, root: Path, *, stage: str, method: str,
        candidates: Sequence[Mapping[str, Any]], circuits: Sequence[str],
        seeds: Sequence[int], resume: bool, dry_run: bool, workers: int = 1,
) -> list[Mapping[str, Any]]:
    workers = _validated_workers(workers)
    tasks = _trial_tasks(
        plan, root, stage=stage, method=method, candidates=candidates,
        circuits=circuits, seeds=seeds, resume=resume, dry_run=dry_run)
    if workers == 1:
        registry = _canonical_registry(plan)
        return [
            _run_one(
                plan, root, registry, stage=stage, method=method,
                candidate=task.get("candidate"), circuit=str(task["circuit"]),
                seed=int(task["seed"]), resume=resume, dry_run=dry_run)
            for task in tasks
        ]
    _record_parallel_execution(root, workers)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        return list(pool.map(_run_one_process, tasks, chunksize=1))


def _load_candidate_rows(
        plan: ExperimentPlan, root: Path, *, stage: str, method: str,
        candidates: Sequence[Mapping[str, Any]], circuits: Sequence[str],
        seeds: Sequence[int]) -> list[RacingTrial]:
    registry = _canonical_registry(plan)
    frozen_execution = _load_execution_identity(plan, root)
    rows = []
    for circuit in circuits:
        dataset, canonical = registry[circuit]
        dataset = dataset.name
        canonical_circuit = Path(canonical.canonical_path).stem
        for candidate in candidates:
            for seed in seeds:
                config = _config_payload(
                    plan, candidate, seed=seed,
                    destination=(root / "configs" /
                                 str(candidate["candidate_id"]) /
                                 f"seed-{seed}.json"))
                identity = {
                    "stage": stage, "dataset": dataset,
                    "circuit": canonical_circuit, "circuit_key": circuit,
                    "method": method,
                    "candidate_id": candidate["candidate_id"], "seed": seed,
                    "execution": _candidate_execution_identity(
                        plan, root, method=method, config=config,
                        frozen=frozen_execution),
                }
                path = _receipt_path(
                    root, stage, method, candidate["candidate_id"], dataset,
                    circuit, seed)
                if not path.is_file():
                    raise RuntimeError(f"missing quality-racing receipt: {path}")
                payload = _validate_attempt_receipt(path, identity)
                rows.append(_as_racing_trial(plan, root, payload))
    return rows


def _candidate_map(candidates: Sequence[Mapping[str, Any]]
                   ) -> dict[str, dict[str, Any]]:
    values = {str(row["candidate_id"]): dict(row) for row in candidates}
    if len(values) != len(candidates):
        raise ValueError("duplicate candidate id")
    return values


def _run_development_race(
        plan: ExperimentPlan, root: Path, *, stage: str, method: str,
        candidates: Sequence[Mapping[str, Any]], resume: bool,
        dry_run: bool, workers: int = 1) -> Mapping[str, Any]:
    active = _candidate_map(candidates)
    checkpoints = []
    outputs = []
    circuit_count = len(DEVELOPMENT_TUNING_CIRCUITS)
    stops = list(range(RACE_BLOCK_SIZE, circuit_count, RACE_BLOCK_SIZE))
    stops.append(circuit_count)
    start = 0
    for stop in stops:
        block = DEVELOPMENT_TUNING_CIRCUITS[start:stop]
        start = stop
        outputs.extend(_run_candidate_trials(
            plan, root, stage=stage, method=method,
            candidates=list(active.values()), circuits=block,
            seeds=DEVELOPMENT_SEED, resume=resume, dry_run=dry_run,
            workers=workers))
        if dry_run:
            continue
        cumulative = DEVELOPMENT_TUNING_CIRCUITS[:stop]
        rows = _load_candidate_rows(
            plan, root, stage=stage, method=method,
            candidates=list(active.values()), circuits=cumulative,
            seeds=DEVELOPMENT_SEED)
        checkpoint = race_checkpoint(
            rows, active_candidate_ids=list(active), method=method,
            completed_circuits=cumulative)
        checkpoints.append(checkpoint)
        active = {key: active[key] for key in checkpoint["retained"]}
        if len(active) < 2:
            raise RuntimeError(
                f"{stage}/{method} race retained fewer than two candidates")
    result: dict[str, Any] = {
        "experiment_schema": 2, "protocol_id": PROTOCOL_ID,
        "stage": stage, "method": method,
        "dry_run": dry_run, "outputs": outputs, "checkpoints": checkpoints,
        "ranking_circuits": list(DEVELOPMENT_TUNING_CIRCUITS),
        "coverage_only_circuits": list(DEVELOPMENT_COVERAGE_ONLY),
    }
    if not dry_run:
        rows = _load_candidate_rows(
            plan, root, stage=stage, method=method,
            candidates=list(active.values()),
            circuits=DEVELOPMENT_TUNING_CIRCUITS,
            seeds=DEVELOPMENT_SEED)
        selection = select_top(
            rows, candidate_ids=list(active), method=method,
            circuits=DEVELOPMENT_TUNING_CIRCUITS,
            seeds=DEVELOPMENT_SEED, count=2)
        result["selection"] = selection
        result["promoted"] = [active[key] for key in selection["selected"]]
        _write_same_or_fail(
            root / "selections" / f"{stage}.{method}.json", _seal(result))
    return result


def _load_selection(root: Path, stage: str, method: str
                    ) -> Mapping[str, Any]:
    payload = json.loads(
        (root / "selections" / f"{stage}.{method}.json").read_text())
    _validate_sealed(payload)
    return payload


def run_profiles(plan: ExperimentPlan, root: Path, *, resume: bool = True,
                 dry_run: bool = False, workers: int = 1) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    return {method: _run_development_race(
        plan, root, stage="profiles", method=method,
        candidates=search_profile_candidates(method), resume=resume,
        dry_run=dry_run, workers=workers) for method in ("M3", "M4")}


def run_decisions(plan: ExperimentPlan, root: Path, *, resume: bool = True,
                  dry_run: bool = False, workers: int = 1) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    results = {}
    for method in ("M3", "M4"):
        parents = _load_selection(root, "profiles", method)["promoted"]
        candidates = decision_candidates(method, parents)
        results[method] = _run_development_race(
            plan, root, stage="decisions", method=method,
            candidates=candidates, resume=resume, dry_run=dry_run,
            workers=workers)
    return results


def run_lookahead(plan: ExperimentPlan, root: Path, *, resume: bool = True,
                  dry_run: bool = False, workers: int = 1) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    parents = _load_selection(root, "decisions", "M4")["promoted"]
    return _run_development_race(
        plan, root, stage="lookahead", method="M4",
        candidates=lookahead_candidates(parents), resume=resume,
        dry_run=dry_run, workers=workers)


def run_validation(plan: ExperimentPlan, root: Path, *, resume: bool = True,
                   dry_run: bool = False, workers: int = 1) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    incumbent_freeze = _read_incumbents(root)
    candidates = {
        "M3": list(_load_selection(root, "decisions", "M3")["promoted"]),
        "M4": list(_load_selection(root, "lookahead", "M4")["promoted"]),
    }
    for method in ("M3", "M4"):
        incumbent = dict(incumbent_freeze["methods"][method]["candidate"])
        by_id = _candidate_map([*candidates[method], incumbent])
        candidates[method] = list(by_id.values())
    outputs = {}
    selections = {}
    for method in ("M3", "M4"):
        outputs[method] = _run_candidate_trials(
            plan, root, stage="validation", method=method,
            candidates=candidates[method], circuits=VALIDATION_CIRCUITS,
            seeds=VALIDATION_SEEDS, resume=resume, dry_run=dry_run,
            workers=workers)
        if not dry_run:
            rows = _load_candidate_rows(
                plan, root, stage="validation", method=method,
                candidates=candidates[method], circuits=VALIDATION_CIRCUITS,
                seeds=VALIDATION_SEEDS)
            selections[method] = select_non_degrading(
                rows, candidate_ids=[row["candidate_id"]
                                     for row in candidates[method]],
                incumbent_candidate_id=str(
                    incumbent_freeze["methods"][method]["candidate"][
                        "candidate_id"]),
                method=method, circuits=VALIDATION_CIRCUITS,
                seeds=VALIDATION_SEEDS)
    result: dict[str, Any] = {
        "experiment_schema": 2, "protocol_id": PROTOCOL_ID,
        "stage": "validation", "dry_run": dry_run, "outputs": outputs,
        "selections": selections,
        "incumbent_config_record_sha256": incumbent_freeze["record_sha256"],
    }
    if not dry_run:
        selected = {
            method: next(row for row in candidates[method]
                         if row["candidate_id"] ==
                         selections[method]["selected"][0])
            for method in ("M3", "M4")
        }
        result["selected_independent"] = selected
        _assert_validation_non_degradation(result, incumbent_freeze)
        _write_same_or_fail(
            root / "selections" / "validation.json", _seal(result))
    return result


def _assert_validation_non_degradation(
        validation: Mapping[str, Any], incumbents: Mapping[str, Any]
) -> Mapping[str, Any]:
    if validation.get("incumbent_config_record_sha256") != \
            incumbents.get("record_sha256"):
        raise ValueError("validation uses another incumbent freeze")
    selections = validation.get("selections")
    selected = validation.get("selected_independent")
    if (not isinstance(selections, Mapping)
            or not isinstance(selected, Mapping)):
        raise ValueError("validation lacks non-degradation selection evidence")
    evidence = {}
    for method in ("M3", "M4"):
        selection = selections.get(method)
        incumbent_id = incumbents["methods"][method]["candidate"][
            "candidate_id"]
        if not isinstance(selection, Mapping):
            raise ValueError(f"validation lacks {method} selection")
        winner_ids = selection.get("selected")
        if (not isinstance(winner_ids, list) or len(winner_ids) != 1
                or not isinstance(winner_ids[0], str)):
            raise ValueError(f"validation {method} must select one winner")
        winner_id = winner_ids[0]
        if selection.get("incumbent_candidate_id") != incumbent_id:
            raise ValueError(f"validation {method} incumbent identity drift")
        if selection.get("non_degradation_passed") is not True:
            raise ValueError(f"validation {method} non-degradation gate failed")
        if selection.get("non_degradation_tolerance") != \
                INCUMBENT_NON_DEGRADATION_TOLERANCE:
            raise ValueError(f"validation {method} tolerance drift")
        summaries = selection.get("summaries")
        if not isinstance(summaries, list):
            raise ValueError(f"validation {method} summaries are absent")
        by_id = {
            str(row.get("candidate_id")): row for row in summaries
            if isinstance(row, Mapping)
        }
        if incumbent_id not in by_id or winner_id not in by_id:
            raise ValueError(f"validation {method} gate cohort is incomplete")
        incumbent = by_id[incumbent_id]
        winner = by_id[winner_id]
        if incumbent.get("valid") is not True or winner.get("valid") is not True:
            raise ValueError(f"validation {method} winner/incumbent is invalid")
        incumbent_quality = float(incumbent["median_delta_log_fidelity"])
        winner_quality = float(winner["median_delta_log_fidelity"])
        if (winner_quality + INCUMBENT_NON_DEGRADATION_TOLERANCE
                < incumbent_quality):
            raise ValueError(f"validation {method} winner degrades incumbent")
        if (float(selection.get(
                "incumbent_median_delta_log_fidelity")) != incumbent_quality
                or float(selection.get(
                    "winner_median_delta_log_fidelity")) != winner_quality):
            raise ValueError(f"validation {method} quality evidence drift")
        incumbent_by_dataset = selection.get(
            "incumbent_median_delta_log_fidelity_by_dataset")
        winner_by_dataset = selection.get(
            "winner_median_delta_log_fidelity_by_dataset")
        dataset_summaries = selection.get("dataset_summaries")
        expected_datasets = {"ZAC18", "QMAP154"}
        if (not isinstance(incumbent_by_dataset, Mapping)
                or set(incumbent_by_dataset) != expected_datasets
                or not isinstance(winner_by_dataset, Mapping)
                or set(winner_by_dataset) != expected_datasets
                or not isinstance(dataset_summaries, Mapping)
                or set(dataset_summaries) != expected_datasets):
            raise ValueError(
                f"validation {method} per-dataset evidence is incomplete")
        dataset_evidence = {}
        for dataset in sorted(expected_datasets):
            rows = dataset_summaries[dataset]
            if not isinstance(rows, list):
                raise ValueError(
                    f"validation {method}/{dataset} summaries are absent")
            rows_by_id = {
                str(row.get("candidate_id")): row for row in rows
                if isinstance(row, Mapping)
            }
            if incumbent_id not in rows_by_id or winner_id not in rows_by_id:
                raise ValueError(
                    f"validation {method}/{dataset} gate cohort is incomplete")
            dataset_incumbent = rows_by_id[incumbent_id]
            dataset_winner = rows_by_id[winner_id]
            if (dataset_incumbent.get("valid") is not True
                    or dataset_winner.get("valid") is not True):
                raise ValueError(
                    f"validation {method}/{dataset} winner/incumbent is invalid")
            incumbent_dataset_quality = float(
                dataset_incumbent["median_delta_log_fidelity"])
            winner_dataset_quality = float(
                dataset_winner["median_delta_log_fidelity"])
            if (winner_dataset_quality
                    + INCUMBENT_NON_DEGRADATION_TOLERANCE
                    < incumbent_dataset_quality):
                raise ValueError(
                    f"validation {method} winner degrades incumbent on {dataset}")
            if (float(incumbent_by_dataset[dataset]) !=
                    incumbent_dataset_quality
                    or float(winner_by_dataset[dataset]) !=
                    winner_dataset_quality):
                raise ValueError(
                    f"validation {method}/{dataset} quality evidence drift")
            dataset_evidence[dataset] = {
                "incumbent_median_delta_log_fidelity":
                    incumbent_dataset_quality,
                "winner_median_delta_log_fidelity": winner_dataset_quality,
            }
        selected_candidate = selected.get(method)
        if (not isinstance(selected_candidate, Mapping)
                or selected_candidate.get("candidate_id") != winner_id):
            raise ValueError(f"validation {method} selected candidate drift")
        evidence[method] = {
            "incumbent_candidate_id": incumbent_id,
            "selected_candidate_id": winner_id,
            "incumbent_median_delta_log_fidelity": incumbent_quality,
            "winner_median_delta_log_fidelity": winner_quality,
            "by_dataset": dataset_evidence,
        }
    return evidence


def selected_independent(root: Path) -> Mapping[str, Mapping[str, Any]]:
    payload = json.loads((root / "selections" / "validation.json").read_text())
    _validate_sealed(payload)
    _assert_validation_non_degradation(payload, _read_incumbents(root))
    return payload["selected_independent"]


def run_shared_forward_check(
        plan: ExperimentPlan, root: Path, *, resume: bool = True,
        dry_run: bool = False, workers: int = 1) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    selected = selected_independent(root)
    m3, m4 = shared_forward_pair(selected["M4"])
    candidates = {"M3": m3, "M4": m4}
    circuits = DEVELOPMENT_CIRCUITS + VALIDATION_CIRCUITS
    outputs = {}
    for method in ("M3", "M4"):
        outputs[method] = _run_candidate_trials(
            plan, root, stage="shared_forward_check", method=method,
            candidates=(candidates[method],), circuits=circuits,
            seeds=(0,), resume=resume, dry_run=dry_run, workers=workers)
    result: dict[str, Any] = {
        "experiment_schema": 2, "protocol_id": PROTOCOL_ID,
        "stage": "shared_forward_check", "dry_run": dry_run,
        "candidates": candidates, "outputs": outputs,
    }
    if not dry_run:
        rows = {
            method: _load_candidate_rows(
                plan, root, stage="shared_forward_check", method=method,
                candidates=(candidates[method],), circuits=circuits, seeds=(0,))
            for method in ("M3", "M4")
        }
        m3_by_circuit = {row.circuit: row for row in rows["M3"]}
        differences = []
        for m4_row in rows["M4"]:
            m3_row = m3_by_circuit[m4_row.circuit]
            model = "exponential" if (
                m3_row.fidelity_ood or m4_row.fidelity_ood or
                m3_row.baseline_fidelity_ood) else "linear"
            differences.append(
                m4_row.delta_for(model) - m3_row.delta_for(model))
        result["median_m4_minus_m3_log_fidelity"] = float(
            statistics.median(differences))
        result["m4_forward_better"] = (
            result["median_m4_minus_m3_log_fidelity"] > 0.0)
        _write_same_or_fail(
            root / "selections" / "shared_forward_check.json",
            _seal(result))
    return result


def selected_config_payloads(plan: ExperimentPlan, root: Path, *, seed: int = 0
                             ) -> Mapping[str, Any]:
    """Return (but do not overwrite) the four selected tracked config payloads."""
    selected = selected_independent(root)
    shared_m3, shared_m4 = shared_forward_pair(selected["M4"])
    tracks = {
        "ours_nl_independent.json": selected["M3"],
        "ours_lk_independent.json": selected["M4"],
        "ours_nl_shared.json": shared_m3,
        "ours_lk_shared.json": shared_m4,
    }
    payloads = {}
    for name, candidate in tracks.items():
        method = str(candidate["method"])
        base = copy.deepcopy(dict(plan.methods[method].payload))
        setting = materialize_formal_setting(
            effective_zac_setting(base), candidate, seed=seed,
            output_dir=("results/native_ga_v1/independent/" if
                        "independent" in name else
                        "results/native_ga_v1/shared/") +
                       ("ours_nl/" if method == "M3" else "ours_lk/"))
        base["zac_setting"] = [setting]
        base["tuning"] = {
            "algorithm_revision": setting["algorithm_revision"],
            "candidate_id": candidate["candidate_id"],
            "protocol_id": PROTOCOL_ID,
            "selection_status": "selected",
            "track": ("independent" if "independent" in name else "shared"),
        }
        payloads[name] = base
    return payloads


def write_selection_artifacts(plan: ExperimentPlan, root: Path) -> Mapping[str, Any]:
    from .initial_placement_runner import validate_initial_selection_for_plan

    _load_workspace(plan, root)
    execution = _read_execution_identity(root)
    incumbents = _read_incumbents(root)
    payloads = selected_config_payloads(plan, root)
    output = root / "selected_configs"
    for name, payload in payloads.items():
        _write_same_or_fail(output / name, payload)
    shared = json.loads(
        (root / "selections" / "shared_forward_check.json").read_text())
    _validate_sealed(shared)
    initial_path = (
        plan.output_root / "initial-placement" / "selected_engine.json")
    initial = validate_initial_selection_for_plan(
        plan, initial_path, enforce_pre_tuning_core=False)
    validation = json.loads(
        (root / "selections" / "validation.json").read_text())
    _validate_sealed(validation)
    non_degradation = _assert_validation_non_degradation(
        validation, incumbents)
    manifest = _seal({
        "experiment_schema": 2,
        "protocol_id": PROTOCOL_ID,
        "selected_independent": selected_independent(root),
        "execution_identity_record_sha256": execution["record_sha256"],
        "incumbent_config_record_sha256": incumbents["record_sha256"],
        "validation_non_degradation": non_degradation,
        "validation_selection_record_sha256": validation["record_sha256"],
        "shared_forward_check_record_sha256": shared["record_sha256"],
        "initial_selection_record_sha256": initial["record_sha256"],
        "config_sha256": {
            name: stable_sha256(payload) for name, payload in payloads.items()},
    })
    _write_same_or_fail(root / "selected_config_manifest.json", manifest)
    return manifest


def validate_quality_selection_for_plan(
        plan: ExperimentPlan, path: Path | None = None) -> Mapping[str, Any]:
    """Bind the selected artifact configs to the tracked formal plan configs."""
    root = default_root(plan)
    path = (path or root / "selected_config_manifest.json").resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_sealed(payload)
    selection_root = root / "selections"
    validation = json.loads(
        (selection_root / "validation.json").read_text(encoding="utf-8"))
    shared = json.loads((
        selection_root / "shared_forward_check.json"
    ).read_text(encoding="utf-8"))
    _validate_sealed(validation)
    _validate_sealed(shared)
    execution = _read_execution_identity(root)
    incumbents = _read_incumbents(root)
    if (payload.get("execution_identity_record_sha256") !=
            execution.get("record_sha256")):
        raise ValueError("quality selection execution identity drift")
    if (payload.get("incumbent_config_record_sha256") !=
            incumbents.get("record_sha256")):
        raise ValueError("quality selection incumbent freeze drift")
    non_degradation = _assert_validation_non_degradation(
        validation, incumbents)
    if payload.get("validation_non_degradation") != non_degradation:
        raise ValueError("quality selection non-degradation evidence drift")
    if (payload.get("validation_selection_record_sha256") !=
            validation.get("record_sha256")):
        raise ValueError("quality selection validation receipt drift")
    if (payload.get("shared_forward_check_record_sha256") !=
            shared.get("record_sha256")):
        raise ValueError("quality selection shared-forward receipt drift")
    selected_independent_payload = validation.get("selected_independent")
    if (not isinstance(selected_independent_payload, Mapping) or
            set(selected_independent_payload) != {"M3", "M4"} or
            payload.get("selected_independent") !=
            selected_independent_payload):
        raise ValueError("quality selection independent candidates drift")
    shared_candidates = shared.get("candidates")
    if (not isinstance(shared_candidates, Mapping) or
            set(shared_candidates) != {"M3", "M4"}):
        raise ValueError("quality selection lacks shared-forward candidates")

    from .initial_placement_runner import validate_initial_selection_for_plan
    initial_path = (
        plan.output_root / "initial-placement" / "selected_engine.json")
    initial = validate_initial_selection_for_plan(
        plan, initial_path, enforce_pre_tuning_core=False)
    if (payload.get("initial_selection_record_sha256") !=
            initial.get("record_sha256")):
        raise ValueError(
            "quality selection was produced from a different initial-placement gate")
    expected_names = (
        "ours_nl_independent.json", "ours_lk_independent.json",
        "ours_nl_shared.json", "ours_lk_shared.json",
    )
    config_hashes = payload.get("config_sha256")
    if not isinstance(config_hashes, Mapping) or \
            set(config_hashes) != set(expected_names):
        raise ValueError("quality selection has incomplete config hashes")
    committed_root = plan.package_root / "exp_setting" / "native_ga_v1"
    selected_root = root / "selected_configs"
    selected_payloads = {}
    for name in expected_names:
        selected_path = selected_root / name
        committed_path = committed_root / name
        selected_value = json.loads(selected_path.read_text(encoding="utf-8"))
        committed_value = json.loads(committed_path.read_text(encoding="utf-8"))
        if stable_sha256(selected_value) != config_hashes[name]:
            raise ValueError(f"selected config hash drift: {selected_path}")
        if committed_value != selected_value:
            raise ValueError(
                f"tracked config differs from quality selection: {name}")
        selected_payloads[name] = selected_value
    frozen_methods = execution.get("methods")
    if not isinstance(frozen_methods, Mapping):
        raise ValueError("quality selection execution identity is incomplete")
    for method, name in (("M3", "ours_nl_independent.json"),
                         ("M4", "ours_lk_independent.json"),
                         ("M3", "ours_nl_shared.json"),
                         ("M4", "ours_lk_shared.json")):
        expected_native = frozen_methods.get(method, {}).get("native")
        observed_native = _setting_native_identity(effective_zac_setting(
            selected_payloads[name]))
        if observed_native != expected_native:
            raise ValueError(
                f"tracked {method} config changes tuning native identity")
    for method, name in (("M3", "ours_nl_independent.json"),
                         ("M4", "ours_lk_independent.json")):
        expected = selected_independent_payload[method].get("candidate_id")
        observed = selected_payloads[name].get("tuning", {}).get(
            "candidate_id")
        if observed != expected:
            raise ValueError(
                f"tracked {method} candidate differs from validation winner")
    for method, name in (("M3", "ours_nl_shared.json"),
                         ("M4", "ours_lk_shared.json")):
        expected = shared_candidates[method].get("candidate_id")
        observed = selected_payloads[name].get("tuning", {}).get(
            "candidate_id")
        if observed != expected:
            raise ValueError(
                f"tracked {method} shared candidate differs from forward check")
    from .plan import _validate_pair_payloads
    _validate_pair_payloads(
        selected_payloads["ours_nl_independent.json"],
        selected_payloads["ours_lk_independent.json"])
    _validate_pair_payloads(
        selected_payloads["ours_nl_shared.json"],
        selected_payloads["ours_lk_shared.json"])
    for method, name in (("M3", "ours_nl_independent.json"),
                         ("M4", "ours_lk_independent.json")):
        if dict(plan.methods[method].payload) != selected_payloads[name]:
            raise ValueError(
                f"formal plan {method} does not use selected independent config")
    result = dict(payload)
    result.update({
        "path": str(path),
        "manifest_sha256": sha256_file(path),
        "shared_candidate_id": selected_payloads[
            "ours_lk_shared.json"]["tuning"]["candidate_id"],
        "initial_selection_record_sha256": initial["record_sha256"],
        "m4_forward_better": shared.get("m4_forward_better"),
    })
    return result


def load_plan_and_root(plan_path: Path, root: Path | None = None
                       ) -> tuple[ExperimentPlan, Path]:
    plan = load_experiment_plan(plan_path)
    return plan, (root or default_root(plan)).resolve()


__all__ = [
    "STAGES", "default_root", "load_plan_and_root", "prepare_workspace",
    "import_baselines", "run_baselines", "run_decisions", "run_lookahead",
    "run_profiles",
    "run_shared_forward_check", "run_validation", "selected_config_payloads",
    "selected_independent", "validate_quality_selection_for_plan",
    "write_selection_artifacts",
]
