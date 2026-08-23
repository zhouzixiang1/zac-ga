"""Serial, fail-closed execution for ``resident-ga-quality-racing-v1``.

The runner intentionally has no worker/shard mode.  Each scheduled receipt is
addressed from a deterministic identity, and resume accepts only that exact
receipt and its hashed attempt manifest.  It never scans an old result tree to
infer success.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import statistics
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cli import UnifiedEvaluationGate, _attempt_spec
from .contracts import (RunManifest, load_run_manifest, sha256_file,
                        stable_sha256)
from .plan import (ExperimentPlan, effective_zac_setting,
                   load_experiment_plan)
from .quality_racing import (
    DEVELOPMENT_CIRCUITS,
    DEVELOPMENT_SEED,
    PROTOCOL_ID,
    RacingTrial,
    VALIDATION_CIRCUITS,
    VALIDATION_SEEDS,
    decision_candidates,
    lookahead_candidates,
    materialize_formal_setting,
    race_checkpoint,
    search_profile_candidates,
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


def prepare_workspace(plan: ExperimentPlan, root: Path | None = None
                      ) -> Mapping[str, Any]:
    root = (root or default_root(plan)).resolve()
    root.mkdir(parents=True, exist_ok=True)
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


def _receipt_path(root: Path, stage: str, method: str, candidate_id: str,
                  dataset: str, circuit: str, seed: int) -> Path:
    return (root / "receipts" / stage / method / candidate_id / dataset /
            f"{circuit}.seed-{seed}.json")


def _attempt_manifest_path(manifest: RunManifest) -> Path:
    return Path(manifest.artifact_dir) / "manifest.json"


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
    identity = {
        "stage": stage,
        "dataset": dataset.name,
        "circuit": canonical_circuit,
        "circuit_key": circuit,
        "method": method,
        "candidate_id": candidate_id,
        "seed": int(seed),
    }
    receipt = _receipt_path(
        root, stage, method, candidate_id, dataset.name, circuit, seed)
    if receipt.is_file():
        if not resume:
            raise FileExistsError(
                f"quality-racing receipt exists; use resume: {receipt}")
        return _validate_attempt_receipt(receipt, identity)

    if candidate is None:
        config = plan.methods[method].config_path
    else:
        config = _config_payload(
            plan, candidate, seed=seed,
            destination=(root / "configs" / candidate_id /
                         f"seed-{seed}.json"))
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
    gate = UnifiedEvaluationGate(plan, canonical, method)
    manifest = run_attempt(spec, verifier=gate.verifier, scorer=gate.scorer)
    manifest_path = _attempt_manifest_path(manifest)
    payload = _seal({
        "experiment_schema": 2,
        "protocol_id": PROTOCOL_ID,
        "identity": identity,
        "config": str(config),
        "config_sha256": sha256_file(config),
        "status": manifest.status,
        "attempt_manifest": str(manifest_path),
        "attempt_manifest_sha256": sha256_file(manifest_path),
    })
    _atomic_json(receipt, payload)
    return payload


def _receipt_manifest(payload: Mapping[str, Any]) -> RunManifest:
    return load_run_manifest(Path(str(payload["attempt_manifest"])))


def run_baselines(plan: ExperimentPlan, root: Path, *, resume: bool = True,
                  dry_run: bool = False) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    registry = _canonical_registry(plan)
    outputs = []
    for circuit in DEVELOPMENT_CIRCUITS + VALIDATION_CIRCUITS:
        for method in ("M1", "M2"):
            outputs.append(_run_one(
                plan, root, registry, stage="baselines", method=method,
                candidate=None, circuit=circuit, seed=0,
                resume=resume, dry_run=dry_run))
    return {"protocol_id": PROTOCOL_ID, "stage": "baselines",
            "planned": 60, "outputs": outputs}


def _baseline_manifests(plan: ExperimentPlan, root: Path, circuit: str
                        ) -> tuple[RunManifest, RunManifest]:
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
        if (manifest.status != "success" or manifest.verifier_ok is not True or
                manifest.log_fidelity is None or
                manifest.exponential_sensitivity_log_fidelity is None):
            raise RuntimeError(
                f"invalid tuning baseline {method}/{dataset}/{circuit}")
        values.append(manifest)
    return values[0], values[1]


def _as_racing_trial(plan: ExperimentPlan, root: Path,
                     payload: Mapping[str, Any]) -> RacingTrial:
    identity = payload["identity"]
    manifest = _receipt_manifest(payload)
    baselines = _baseline_manifests(plan, root, str(identity["circuit_key"]))
    baseline_linear = max(float(row.log_fidelity) for row in baselines)
    baseline_exponential = max(
        float(row.exponential_sensitivity_log_fidelity) for row in baselines)
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
        baseline_fidelity_ood=any(row.fidelity_ood for row in baselines),
        baseline_exponential_sensitivity_log_fidelity=baseline_exponential,
    )


def _run_candidate_trials(
        plan: ExperimentPlan, root: Path, *, stage: str, method: str,
        candidates: Sequence[Mapping[str, Any]], circuits: Sequence[str],
        seeds: Sequence[int], resume: bool, dry_run: bool
) -> list[Mapping[str, Any]]:
    registry = _canonical_registry(plan)
    outputs = []
    for circuit in circuits:
        for candidate in candidates:
            for seed in seeds:
                outputs.append(_run_one(
                    plan, root, registry, stage=stage, method=method,
                    candidate=candidate, circuit=circuit, seed=seed,
                    resume=resume, dry_run=dry_run))
    return outputs


def _load_candidate_rows(
        plan: ExperimentPlan, root: Path, *, stage: str, method: str,
        candidates: Sequence[Mapping[str, Any]], circuits: Sequence[str],
        seeds: Sequence[int]) -> list[RacingTrial]:
    registry = _canonical_registry(plan)
    rows = []
    for circuit in circuits:
        dataset, canonical = registry[circuit]
        dataset = dataset.name
        canonical_circuit = Path(canonical.canonical_path).stem
        for candidate in candidates:
            for seed in seeds:
                identity = {
                    "stage": stage, "dataset": dataset,
                    "circuit": canonical_circuit, "circuit_key": circuit,
                    "method": method,
                    "candidate_id": candidate["candidate_id"], "seed": seed,
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
        dry_run: bool) -> Mapping[str, Any]:
    active = _candidate_map(candidates)
    checkpoints = []
    outputs = []
    for stop in range(5, len(DEVELOPMENT_CIRCUITS) + 1, 5):
        block = DEVELOPMENT_CIRCUITS[stop - 5:stop]
        outputs.extend(_run_candidate_trials(
            plan, root, stage=stage, method=method,
            candidates=list(active.values()), circuits=block,
            seeds=DEVELOPMENT_SEED, resume=resume, dry_run=dry_run))
        if dry_run:
            continue
        cumulative = DEVELOPMENT_CIRCUITS[:stop]
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
    }
    if not dry_run:
        rows = _load_candidate_rows(
            plan, root, stage=stage, method=method,
            candidates=list(active.values()), circuits=DEVELOPMENT_CIRCUITS,
            seeds=DEVELOPMENT_SEED)
        selection = select_top(
            rows, candidate_ids=list(active), method=method,
            circuits=DEVELOPMENT_CIRCUITS, seeds=DEVELOPMENT_SEED, count=2)
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
                 dry_run: bool = False) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    return {method: _run_development_race(
        plan, root, stage="profiles", method=method,
        candidates=search_profile_candidates(method), resume=resume,
        dry_run=dry_run) for method in ("M3", "M4")}


def run_decisions(plan: ExperimentPlan, root: Path, *, resume: bool = True,
                  dry_run: bool = False) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    results = {}
    for method in ("M3", "M4"):
        parents = _load_selection(root, "profiles", method)["promoted"]
        candidates = decision_candidates(method, parents)
        results[method] = _run_development_race(
            plan, root, stage="decisions", method=method,
            candidates=candidates, resume=resume, dry_run=dry_run)
    return results


def run_lookahead(plan: ExperimentPlan, root: Path, *, resume: bool = True,
                  dry_run: bool = False) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    parents = _load_selection(root, "decisions", "M4")["promoted"]
    return _run_development_race(
        plan, root, stage="lookahead", method="M4",
        candidates=lookahead_candidates(parents), resume=resume,
        dry_run=dry_run)


def run_validation(plan: ExperimentPlan, root: Path, *, resume: bool = True,
                   dry_run: bool = False) -> Mapping[str, Any]:
    _load_workspace(plan, root)
    candidates = {
        "M3": _load_selection(root, "decisions", "M3")["promoted"],
        "M4": _load_selection(root, "lookahead", "M4")["promoted"],
    }
    outputs = {}
    selections = {}
    for method in ("M3", "M4"):
        outputs[method] = _run_candidate_trials(
            plan, root, stage="validation", method=method,
            candidates=candidates[method], circuits=VALIDATION_CIRCUITS,
            seeds=VALIDATION_SEEDS, resume=resume, dry_run=dry_run)
        if not dry_run:
            rows = _load_candidate_rows(
                plan, root, stage="validation", method=method,
                candidates=candidates[method], circuits=VALIDATION_CIRCUITS,
                seeds=VALIDATION_SEEDS)
            selections[method] = select_top(
                rows, candidate_ids=[row["candidate_id"]
                                     for row in candidates[method]],
                method=method, circuits=VALIDATION_CIRCUITS,
                seeds=VALIDATION_SEEDS, count=2)
    result: dict[str, Any] = {
        "experiment_schema": 2, "protocol_id": PROTOCOL_ID,
        "stage": "validation", "dry_run": dry_run, "outputs": outputs,
        "selections": selections,
    }
    if not dry_run:
        selected = {
            method: next(row for row in candidates[method]
                         if row["candidate_id"] ==
                         selections[method]["selected"][0])
            for method in ("M3", "M4")
        }
        result["selected_independent"] = selected
        _write_same_or_fail(
            root / "selections" / "validation.json", _seal(result))
    return result


def selected_independent(root: Path) -> Mapping[str, Mapping[str, Any]]:
    payload = json.loads((root / "selections" / "validation.json").read_text())
    _validate_sealed(payload)
    return payload["selected_independent"]


def run_shared_forward_check(
        plan: ExperimentPlan, root: Path, *, resume: bool = True,
        dry_run: bool = False) -> Mapping[str, Any]:
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
            seeds=(0,), resume=resume, dry_run=dry_run)
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
    manifest = _seal({
        "experiment_schema": 2,
        "protocol_id": PROTOCOL_ID,
        "selected_independent": selected_independent(root),
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
    "run_baselines", "run_decisions", "run_lookahead", "run_profiles",
    "run_shared_forward_check", "run_validation", "selected_config_payloads",
    "selected_independent", "validate_quality_selection_for_plan",
    "write_selection_artifacts",
]
