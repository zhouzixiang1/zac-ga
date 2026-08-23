"""Fail-closed executor for the shared SA-vs-GA initial-placement gate."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from zzx.algorithm_v2 import validate_schema2_pair, validate_schema2_setting

from .cli import UnifiedEvaluationGate, _attempt_spec
from .contracts import (CanonicalCircuitManifest, RunManifest, RunStatus,
                        load_run_manifest, repository_snapshot, sha256_file,
                        stable_sha256)
from .initial_placement_benchmark import (
    INITIAL_PLACEMENT_PROTOCOL_ID,
    INITIAL_PLACEMENT_QUALITY_POLICY,
    InitialPlacementTrial,
    build_initial_placement_schedule,
    select_initial_placement_engine,
)
from .plan import ExperimentPlan, effective_zac_setting
from .runner import run_attempt
from .tuning import build_tuning_split


TERMINAL_STATUSES = {item.value for item in RunStatus}


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_immutable(path: Path, value: Any) -> None:
    if path.exists():
        if _read_json(path) != value:
            raise FileExistsError(
                f"refusing to replace immutable initial-placement artifact: {path}")
        return
    _atomic_json(path, value)


def _payload_hash(value: Mapping[str, Any], field: str = "record_sha256") -> str:
    unsigned = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(_stable_json(unsigned).encode()).hexdigest()


def _schedule_hash(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "sha256"}
    return hashlib.sha256(_stable_json(unsigned).encode()).hexdigest()


def _job_id(job: Mapping[str, Any]) -> str:
    identity = {key: job[key] for key in ("circuit", "method", "engine", "seed")}
    return hashlib.sha256(_stable_json(identity).encode()).hexdigest()[:20]


def initial_config_core_sha256(payload: Mapping[str, Any]) -> str:
    """Hash algorithm semantics while excluding runner/initializer controls."""
    normalized = copy.deepcopy(dict(payload))
    setting = _setting_ref(normalized)
    for key in ("dir", "init_engine", "seed"):
        setting.pop(key, None)
    normalized.pop("initial_placement", None)
    return stable_sha256(normalized)


def _setting_ref(payload: dict[str, Any]) -> dict[str, Any]:
    if "zac_setting" not in payload:
        return payload
    values = payload["zac_setting"]
    if (not isinstance(values, list) or len(values) != 1
            or not isinstance(values[0], dict)):
        raise ValueError("initial-placement config requires one zac_setting")
    return values[0]


def build_initial_placement_config(
        base: Mapping[str, Any], *, method: str, engine: str,
        seed: int) -> dict[str, Any]:
    if method not in {"M3", "M4"} or engine not in {"sa", "ga"}:
        raise ValueError("initial-placement config requires M3/M4 and sa/ga")
    payload = copy.deepcopy(dict(base))
    setting = _setting_ref(payload)
    expected = "ours_nl" if method == "M3" else "ours_lk"
    if setting.get("method_id") != expected:
        raise ValueError(f"{method} base config does not identify {expected}")
    setting.update({
        "init_engine": engine,
        "seed": int(seed),
        "dir": f"__runner_owned__/initial-placement/{method.lower()}/{engine}/",
    })
    validate_schema2_setting(setting)
    if "zac_setting" in payload:
        payload["initial_placement"] = {
            "protocol_id": INITIAL_PLACEMENT_PROTOCOL_ID,
            "engine": engine,
        }
    return payload


def prepare_initial_placement_workspace(
        plan: ExperimentPlan, dataset_name: str, root: Path, *,
        split_seed: int = 0, schedule_seed: int = 0) -> Mapping[str, Any]:
    root = root.resolve()
    dataset = plan.datasets[dataset_name]
    if dataset.kind != "main":
        raise ValueError("initial-placement gate accepts a main dataset only")
    suite = plan.load_suite(dataset)
    split = build_tuning_split(
        [manifest.to_dict() for manifest in suite], seed=split_seed)
    schedule = build_initial_placement_schedule(
        split["validation"], schedule_seed=schedule_seed)
    schedule.update({
        "dataset": dataset_name,
        "quality_policy": INITIAL_PLACEMENT_QUALITY_POLICY,
        "split_manifest_sha256": split["sha256"],
        "split_file_sha256": stable_sha256(split),
        "suite_manifest_path": str(dataset.suite_manifest.resolve()),
        "suite_manifest_sha256": sha256_file(dataset.suite_manifest),
        "canonical_input_sha256": {
            Path(manifest.canonical_path).stem: manifest.canonical_sha256
            for manifest in suite
        },
        "plan_path": str(plan.path.resolve()),
        "plan_sha256": sha256_file(plan.path),
        "method_config_core_sha256": {
            method: initial_config_core_sha256(plan.methods[method].payload)
            for method in ("M3", "M4")
        },
    })
    repository = repository_snapshot(plan.repo_root)
    if repository["dirty"] or repository["commit"] == "unknown":
        raise RuntimeError(
            "initial-placement gate requires a clean versioned repository")
    schedule["repository"] = repository
    schedule["sha256"] = _schedule_hash(schedule)
    for job in schedule["jobs"]:
        job["job_id"] = _job_id(job)
    # Job ids are evidence, so bind them in the final schedule digest.
    schedule["sha256"] = _schedule_hash(schedule)
    _write_immutable(root / "schedule.json", schedule)
    _write_immutable(root / "split_manifest.json", split)
    return {
        "protocol_id": INITIAL_PLACEMENT_PROTOCOL_ID,
        "dataset": dataset_name,
        "circuits": len(split["validation"]),
        "expected_trials": schedule["expected_trials"],
        "schedule_sha256": schedule["sha256"],
        "root": str(root),
    }


def _load_schedule(
        root: Path, *, require_current_repository: bool = True
        ) -> Mapping[str, Any]:
    schedule = _read_json(root / "schedule.json")
    if schedule.get("protocol_id") != INITIAL_PLACEMENT_PROTOCOL_ID:
        raise ValueError("initial-placement protocol mismatch")
    if schedule.get("quality_policy") != INITIAL_PLACEMENT_QUALITY_POLICY:
        raise ValueError("initial-placement quality policy mismatch")
    if schedule.get("sha256") != _schedule_hash(schedule):
        raise ValueError("initial-placement schedule hash mismatch")
    jobs = schedule.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != schedule.get("expected_trials"):
        raise ValueError("initial-placement schedule has the wrong job count")
    seen = set()
    for ordinal, job in enumerate(jobs):
        if job.get("ordinal") != ordinal or job.get("job_id") != _job_id(job):
            raise ValueError("initial-placement job identity drift")
        if job["job_id"] in seen:
            raise ValueError("duplicate initial-placement job id")
        seen.add(job["job_id"])
    plan_path = Path(str(schedule.get("plan_path", "")))
    suite_path = Path(str(schedule.get("suite_manifest_path", "")))
    split_path = root / "split_manifest.json"
    if (not plan_path.is_file()
            or schedule.get("plan_sha256") != sha256_file(plan_path)):
        raise ValueError("initial-placement frozen plan drift")
    if (not suite_path.is_file()
            or schedule.get("suite_manifest_sha256") != sha256_file(suite_path)):
        raise ValueError("initial-placement frozen suite drift")
    if (not split_path.is_file()
            or schedule.get("split_file_sha256") !=
            stable_sha256(_read_json(split_path))
            or schedule.get("split_manifest_sha256") !=
            _read_json(split_path).get("sha256")):
        raise ValueError("initial-placement frozen split drift")
    repository = schedule.get("repository")
    if not isinstance(repository, Mapping) or not repository.get("root"):
        raise ValueError("initial-placement repository binding is missing")
    if require_current_repository:
        current = repository_snapshot(Path(str(repository["root"])))
        if (current.get("dirty") or current.get("commit") == "unknown"
                or current.get("commit") != repository.get("commit")):
            raise RuntimeError("initial-placement repository identity drift")
    return schedule


def _validate_workspace_binding(
        plan: ExperimentPlan, dataset_name: str, root: Path,
        schedule: Mapping[str, Any]) -> None:
    if schedule.get("dataset") != dataset_name:
        raise ValueError("initial-placement schedule dataset mismatch")
    if (schedule.get("plan_path") != str(plan.path.resolve())
            or schedule.get("plan_sha256") != sha256_file(plan.path)):
        raise ValueError("initial-placement plan binding drift")
    dataset = plan.datasets[dataset_name]
    if (schedule.get("suite_manifest_path") !=
            str(dataset.suite_manifest.resolve())
            or schedule.get("suite_manifest_sha256") !=
            sha256_file(dataset.suite_manifest)):
        raise ValueError("initial-placement canonical suite drift")
    split_path = root / "split_manifest.json"
    if (not split_path.is_file()
            or schedule.get("split_file_sha256") !=
            stable_sha256(_read_json(split_path))
            or schedule.get("split_manifest_sha256") !=
            _read_json(split_path).get("sha256")):
        raise ValueError("initial-placement split manifest drift")
    repository = repository_snapshot(plan.repo_root)
    expected_repository = schedule.get("repository")
    if (repository.get("dirty") or repository.get("commit") == "unknown"
            or repository.get("commit") != expected_repository.get("commit")):
        raise RuntimeError("initial-placement repository identity drift")
    for method in ("M3", "M4"):
        actual = initial_config_core_sha256(plan.methods[method].payload)
        if actual != schedule["method_config_core_sha256"][method]:
            raise ValueError(
                f"initial-placement {method} algorithm core config drift")


def _config_path(root: Path, job: Mapping[str, Any]) -> Path:
    return (root / "configs" / str(job["method"]) / str(job["engine"]) /
            f"seed-{int(job['seed'])}.json")


def _receipt_path(root: Path, job: Mapping[str, Any]) -> Path:
    return root / "ledger" / f"{int(job['ordinal']):03d}-{job['job_id']}.json"


def _materialize_config(
        plan: ExperimentPlan, root: Path, job: Mapping[str, Any]) -> Path:
    path = _config_path(root, job)
    payload = build_initial_placement_config(
        plan.methods[str(job["method"])].payload,
        method=str(job["method"]), engine=str(job["engine"]),
        seed=int(job["seed"]))
    _write_immutable(path, payload)
    return path


def _seal_receipt(
        path: Path, *, schedule: Mapping[str, Any], job: Mapping[str, Any],
        config_path: Path, manifest_path: Path,
        canonical: CanonicalCircuitManifest) -> Mapping[str, Any]:
    manifest = load_run_manifest(manifest_path, require_success_metrics=True)
    _validate_attempt_manifest(
        manifest, schedule=schedule, job=job, config_path=config_path,
        manifest_path=manifest_path, canonical=canonical)
    payload = {
        "experiment_schema": 2,
        "protocol_id": INITIAL_PLACEMENT_PROTOCOL_ID,
        "schedule_sha256": schedule["sha256"],
        "job": dict(job),
        "config_path": str(config_path.resolve()),
        "config_sha256": sha256_file(config_path),
        "attempt_manifest": str(manifest_path.resolve()),
        "attempt_manifest_sha256": sha256_file(manifest_path),
        "status": manifest.status,
        "verifier_ok": bool(manifest.verifier_ok),
        "ghost_hits": manifest.ghost_hits,
        "fallback": False,
        "log_fidelity": manifest.log_fidelity,
        "fidelity_ood": bool(manifest.fidelity_ood),
        "exponential_sensitivity_log_fidelity":
            manifest.exponential_sensitivity_log_fidelity,
        "initial_placement_ns": manifest.initial_placement_ns,
        "full_compile_ns": manifest.full_compile_ns,
    }
    payload["record_sha256"] = _payload_hash(payload)
    _write_immutable(path, payload)
    return payload


def _validate_receipt(
        path: Path, *, schedule: Mapping[str, Any], job: Mapping[str, Any],
        config_path: Path, canonical: CanonicalCircuitManifest | None = None
        ) -> Mapping[str, Any]:
    payload = _read_json(path)
    if payload.get("record_sha256") != _payload_hash(payload):
        raise ValueError(f"initial-placement receipt hash mismatch: {path}")
    if (payload.get("protocol_id") != INITIAL_PLACEMENT_PROTOCOL_ID
            or payload.get("schedule_sha256") != schedule["sha256"]
            or payload.get("job") != dict(job)):
        raise ValueError(f"initial-placement receipt identity mismatch: {path}")
    if (payload.get("config_path") != str(config_path.resolve())
            or payload.get("config_sha256") != sha256_file(config_path)):
        raise ValueError(f"initial-placement receipt config drift: {path}")
    manifest_path = Path(str(payload.get("attempt_manifest", "")))
    if (not manifest_path.is_file()
            or payload.get("attempt_manifest_sha256") != sha256_file(manifest_path)):
        raise ValueError(f"initial-placement attempt manifest drift: {path}")
    manifest = load_run_manifest(manifest_path, require_success_metrics=True)
    _validate_attempt_manifest(
        manifest, schedule=schedule, job=job, config_path=config_path,
        manifest_path=manifest_path, canonical=canonical)
    expected_projection = {
        "status": manifest.status,
        "verifier_ok": bool(manifest.verifier_ok),
        "ghost_hits": manifest.ghost_hits,
        "fallback": False,
        "log_fidelity": manifest.log_fidelity,
        "fidelity_ood": bool(manifest.fidelity_ood),
        "exponential_sensitivity_log_fidelity":
            manifest.exponential_sensitivity_log_fidelity,
        "initial_placement_ns": manifest.initial_placement_ns,
        "full_compile_ns": manifest.full_compile_ns,
    }
    projection_drift = {
        key: (payload.get(key), expected)
        for key, expected in expected_projection.items()
        if payload.get(key) != expected
    }
    if projection_drift:
        raise ValueError(
            f"initial-placement receipt projection drift at {path}: "
            f"{projection_drift}")
    if payload.get("status") not in TERMINAL_STATUSES:
        raise ValueError(f"initial-placement receipt is not terminal: {path}")
    return payload


def _receipt_as_initial_trial(
        job: Mapping[str, Any],
        receipt: Mapping[str, Any]) -> InitialPlacementTrial:
    return InitialPlacementTrial(
        circuit=str(job["circuit"]), method=str(job["method"]),
        engine=str(job["engine"]), seed=int(job["seed"]),
        status=str(receipt["status"]),
        verifier_ok=bool(receipt["verifier_ok"]),
        ghost_hits=int(receipt["ghost_hits"] or 0),
        fallback=bool(receipt["fallback"]),
        log_fidelity=receipt["log_fidelity"],
        initial_placement_ns=receipt["initial_placement_ns"],
        full_compile_ns=receipt["full_compile_ns"],
        fidelity_ood=bool(receipt["fidelity_ood"]),
        exponential_sensitivity_log_fidelity=
            receipt["exponential_sensitivity_log_fidelity"],
    )


def _expected_experiment_id(
        schedule: Mapping[str, Any], job: Mapping[str, Any]) -> str:
    return stable_sha256({
        "protocol_id": INITIAL_PLACEMENT_PROTOCOL_ID,
        "schedule_sha256": schedule["sha256"],
        "job_id": job["job_id"],
    })


def _validate_attempt_manifest(
        manifest: RunManifest, *, schedule: Mapping[str, Any],
        job: Mapping[str, Any], config_path: Path, manifest_path: Path,
        canonical: CanonicalCircuitManifest | None) -> None:
    expected = {
        "dataset": (manifest.dataset, schedule["dataset"]),
        "circuit": (manifest.circuit, job["circuit"]),
        "method": (manifest.method, job["method"]),
        "seed": (manifest.seed, job["seed"]),
        "repetition": (manifest.repetition, 0),
        "run_kind": (manifest.run_kind, "smoke"),
        "experiment_id": (
            manifest.experiment_id, _expected_experiment_id(schedule, job)),
        "config_sha256": (manifest.config_sha256, sha256_file(config_path)),
        "git_commit": (
            manifest.git_commit, schedule["repository"]["commit"]),
        "git_dirty": (manifest.git_dirty, False),
    }
    registered_input = schedule["canonical_input_sha256"].get(job["circuit"])
    if not registered_input:
        raise ValueError("initial-placement circuit has no registered input hash")
    expected["input_sha256"] = (manifest.input_sha256, registered_input)
    if canonical is not None and canonical.canonical_sha256 != registered_input:
        raise ValueError("initial-placement canonical object drift")
    drift = {key: values for key, values in expected.items()
             if values[0] != values[1]}
    if drift:
        raise ValueError(
            f"initial-placement attempt identity drift at {manifest_path}: {drift}")
    setting = effective_zac_setting(_read_json(config_path))
    if manifest.status == RunStatus.SUCCESS.value:
        native_expected = {
            "backend": setting.get("backend"),
            "native_abi_version": setting.get("native_abi_version"),
            "native_wheel_sha256": setting.get("native_wheel_sha256"),
            "rng_version": setting.get("rng_version"),
            "algorithm_revision": setting.get("algorithm_revision"),
        }
        native_drift = {
            key: (getattr(manifest, key), value)
            for key, value in native_expected.items()
            if getattr(manifest, key) != value
        }
        if native_drift:
            raise ValueError(
                "initial-placement native provenance drift: " +
                str(native_drift))


def run_initial_placement_gate(
        plan: ExperimentPlan, dataset_name: str, root: Path, *,
        resume: bool, dry_run: bool = False, limit: int | None = None
        ) -> Mapping[str, Any]:
    root = root.resolve()
    schedule = _load_schedule(root)
    _validate_workspace_binding(plan, dataset_name, root, schedule)
    dataset = plan.datasets[dataset_name]
    canonical_by_name = {
        Path(item.canonical_path).stem: item for item in plan.load_suite(dataset)}
    jobs = list(schedule["jobs"])
    selected = jobs if limit is None else jobs[:max(0, int(limit))]
    attempted, skipped, commands = [], [], []
    for job in selected:
        canonical = canonical_by_name.get(str(job["circuit"]))
        if canonical is None:
            raise ValueError(f"unknown scheduled circuit: {job['circuit']}")
        config_path = _materialize_config(plan, root, job)
        receipt_path = _receipt_path(root, job)
        if receipt_path.exists():
            receipt = _validate_receipt(
                receipt_path, schedule=schedule, job=job,
                config_path=config_path, canonical=canonical)
            if not resume:
                raise FileExistsError(
                    f"initial-placement receipt exists; use --resume: {receipt_path}")
            skipped.append({"job_id": job["job_id"],
                            "status": receipt["status"]})
            continue
        spec = _attempt_spec(
            plan, dataset, canonical, str(job["method"]), int(job["seed"]),
            0, "smoke", config_path=config_path,
            experiment_id=_expected_experiment_id(schedule, job))
        spec = replace(
            spec,
            output_root=root / "trials" / str(job["job_id"]) / "attempts",
            run_kind="smoke")
        if dry_run:
            commands.append({"job": dict(job), "command": list(spec.command),
                             "config": str(config_path)})
            continue
        gate = UnifiedEvaluationGate(plan, canonical, str(job["method"]))
        manifest = run_attempt(spec, verifier=gate.verifier, scorer=gate.scorer)
        receipt = _seal_receipt(
            receipt_path, schedule=schedule, job=job,
            config_path=config_path,
            manifest_path=Path(manifest.artifact_dir) / "manifest.json",
            canonical=canonical)
        attempted.append({"job_id": job["job_id"],
                          "status": receipt["status"],
                          "receipt": str(receipt_path)})
    return {
        "experiment_schema": 2,
        "protocol_id": INITIAL_PLACEMENT_PROTOCOL_ID,
        "planned": len(jobs),
        "selected": len(selected),
        "attempted": attempted,
        "skipped": skipped,
        "dry_run_commands": commands,
    }


def finalize_initial_placement_gate(root: Path) -> Mapping[str, Any]:
    root = root.resolve()
    schedule = _load_schedule(root)
    trials = []
    for job in schedule["jobs"]:
        config_path = _config_path(root, job)
        receipt_path = _receipt_path(root, job)
        if not config_path.is_file() or not receipt_path.is_file():
            raise RuntimeError(
                f"initial-placement ledger is incomplete: {receipt_path}")
        receipt = _validate_receipt(
            receipt_path, schedule=schedule, job=job,
            config_path=config_path)
        trials.append(_receipt_as_initial_trial(job, receipt))
    report = select_initial_placement_engine(
        trials, expected_circuits=schedule["circuits"],
        expected_seeds=schedule["seeds"])
    report = {
        **report,
        "schedule_sha256": schedule["sha256"],
        "dataset": schedule["dataset"],
    }
    report["record_sha256"] = _payload_hash(report)
    _write_immutable(root / "selected_engine.json", report)
    return report


def initial_placement_status(root: Path) -> Mapping[str, Any]:
    root = root.resolve()
    schedule = _load_schedule(root)
    counts = {status: 0 for status in sorted(TERMINAL_STATUSES)}
    missing = 0
    for job in schedule["jobs"]:
        receipt = _receipt_path(root, job)
        config = _config_path(root, job)
        if not receipt.is_file() or not config.is_file():
            missing += 1
            continue
        payload = _validate_receipt(
            receipt, schedule=schedule, job=job, config_path=config)
        counts[str(payload["status"])] += 1
    return {
        "protocol_id": INITIAL_PLACEMENT_PROTOCOL_ID,
        "expected": len(schedule["jobs"]),
        "missing": missing,
        "terminal": len(schedule["jobs"]) - missing,
        "statuses": counts,
        "selection_ready": missing == 0,
    }


def validate_initial_selection_for_plan(
        plan: ExperimentPlan, selection_path: Path, *,
        enforce_pre_tuning_core: bool) -> Mapping[str, Any]:
    """Bind the selected initializer to the plan before tuning/main runs."""
    _, schedule, report = _validate_initial_selection_evidence(selection_path)
    selected = report["selected_engine"]

    for method in ("M3", "M4"):
        setting = effective_zac_setting(plan.methods[method].payload)
        if setting.get("init_engine", "sa") != selected:
            raise ValueError(
                f"{method} init_engine does not match selected {selected}")
        if enforce_pre_tuning_core:
            actual = initial_config_core_sha256(plan.methods[method].payload)
            expected = schedule["method_config_core_sha256"][method]
            if actual != expected:
                raise ValueError(
                    f"{method} changed before the tuning gate consumed its "
                    "initial-placement selection")
    return report


def _validate_initial_selection_evidence(
        selection_path: Path
        ) -> tuple[Path, Mapping[str, Any], Mapping[str, Any]]:
    """Revalidate the immutable 180-attempt ledger without reading configs."""
    selection_path = selection_path.expanduser().resolve()
    root = selection_path.parent
    if not selection_path.is_file():
        raise RuntimeError(
            f"initial-placement selection is missing: {selection_path}")
    schedule = _load_schedule(root, require_current_repository=False)
    report = _read_json(selection_path)
    if (report.get("record_sha256") != _payload_hash(report)
            or report.get("protocol_id") != INITIAL_PLACEMENT_PROTOCOL_ID
            or report.get("schedule_sha256") != schedule["sha256"]
            or report.get("dataset") != schedule["dataset"]):
        raise ValueError("initial-placement selection record drift")
    if report.get("complete") is not True:
        raise RuntimeError("initial-placement selection is incomplete")
    selected = report.get("selected_engine")
    if selected not in {"sa", "ga"}:
        raise ValueError("initial-placement selected_engine is invalid")

    # Revalidate every receipt and deterministically replay the selection.  A
    # self-consistently re-signed summary is not evidence unless it is exactly
    # derivable from the immutable attempt manifests.
    trials = []
    for job in schedule["jobs"]:
        config_path = _config_path(root, job)
        receipt_path = _receipt_path(root, job)
        if not config_path.is_file() or not receipt_path.is_file():
            raise RuntimeError(
                f"initial-placement selection lost evidence: {receipt_path}")
        receipt = _validate_receipt(
            receipt_path, schedule=schedule, job=job,
            config_path=config_path)
        trials.append(_receipt_as_initial_trial(job, receipt))
    replayed = select_initial_placement_engine(
        trials, expected_circuits=schedule["circuits"],
        expected_seeds=schedule["seeds"])
    replayed = {
        **replayed,
        "schedule_sha256": schedule["sha256"],
        "dataset": schedule["dataset"],
    }
    replayed["record_sha256"] = _payload_hash(replayed)
    if report != replayed:
        raise ValueError(
            "initial-placement selection does not match replayed evidence")
    return root, schedule, report


def apply_initial_selection_to_plan(
        plan: ExperimentPlan, selection_path: Path) -> Mapping[str, Any]:
    """Apply only the selected shared ``init_engine`` to M3/M4 configs.

    This is the one intentional bridge between the immutable initial-placement
    experiment and the tracked formal method configs.  It validates every
    receipt and the pre-selection algorithm core before making the two atomic
    JSON replacements.  The resulting changes are then committed before tuning.
    """
    root, schedule, report = _validate_initial_selection_evidence(selection_path)
    selected = str(report["selected_engine"])
    payloads: dict[str, dict[str, Any]] = {}
    before_hashes: dict[str, str] = {}
    for method in ("M3", "M4"):
        spec = plan.methods[method]
        before_hashes[method] = sha256_file(spec.config_path)
        if (initial_config_core_sha256(spec.payload) !=
                schedule["method_config_core_sha256"][method]):
            raise ValueError(
                f"{method} algorithm core changed after the initial-placement gate")
        payload = copy.deepcopy(dict(spec.payload))
        setting = _setting_ref(payload)
        setting["init_engine"] = selected
        validate_schema2_setting(setting)
        payloads[method] = payload
    validate_schema2_pair(
        effective_zac_setting(payloads["M3"]),
        effective_zac_setting(payloads["M4"]))

    for method in ("M3", "M4"):
        _atomic_json(plan.methods[method].config_path, payloads[method])
    applied = {
        "experiment_schema": 2,
        "protocol_id": INITIAL_PLACEMENT_PROTOCOL_ID,
        "selection_path": str(Path(selection_path).expanduser().resolve()),
        "selection_record_sha256": report["record_sha256"],
        "schedule_sha256": schedule["sha256"],
        "selected_engine": selected,
        "plan_path": str(plan.path.resolve()),
        "plan_sha256": sha256_file(plan.path),
        "before_config_sha256": before_hashes,
        "applied_config_sha256": {
            method: sha256_file(plan.methods[method].config_path)
            for method in ("M3", "M4")
        },
    }
    applied["record_sha256"] = _payload_hash(applied)
    _write_immutable(root / "applied_config_manifest.json", applied)
    return applied


__all__ = [
    "apply_initial_selection_to_plan", "build_initial_placement_config",
    "finalize_initial_placement_gate",
    "initial_config_core_sha256",
    "initial_placement_status", "prepare_initial_placement_workspace",
    "run_initial_placement_gate", "validate_initial_selection_for_plan",
]
