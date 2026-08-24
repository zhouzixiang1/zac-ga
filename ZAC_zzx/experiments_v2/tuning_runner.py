"""Fail-closed execution and resume for the deterministic native-GA tuning.

The tuning ledger is intentionally separate from the paper's formal main-run
ledger.  Every scheduled trial has one deterministic receipt path and every
actual compiler launch still receives a UUID-bearing directory from
``run_attempt``.  Resume trusts only a sealed receipt whose schedule, config and
attempt-manifest hashes all match; it never scans old result directories.
"""

from __future__ import annotations

import copy
import csv
import fcntl
import gzip
import hashlib
import json
import math
import os
import socket
import statistics
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from zzx.algorithm_v2 import (FORMAL_NATIVE_TUNING_PROTOCOL_ID,
                              decay_lookahead_spec, validate_schema2_pair,
                              validate_schema2_setting)

from .cli import UnifiedEvaluationGate, _attempt_spec
from .contracts import (RunStatus, load_run_manifest, repository_snapshot,
                        sha256_file, stable_sha256)
from .initial_placement_runner import validate_initial_selection_for_plan
from .plan import (ExperimentPlan, effective_zac_setting,
                   load_experiment_plan)
from .runner import run_attempt
from .tuning import (
    DEFAULT_CANDIDATE,
    PHASE_CONTRACTS,
    TUNING_METHODS,
    TUNING_PROTOCOL_ID,
    TUNING_QUALITY_POLICY_ID,
    TUNING_SPACE,
    ScheduledTrial,
    TuningTrial,
    build_trial_schedule,
    build_tuning_split,
    candidate_id,
    generate_candidates,
    promoted_candidates,
    rank_candidates,
    stage_leaderboard,
    validate_trial_schedule,
)


ALGORITHM_REVISION = "native-ga-v1"
NATIVE_BACKEND = "native"
NATIVE_FAIL_CLOSED = True
TERMINAL_STATUSES = {item.value for item in RunStatus}
CLAIM_SCHEMA = "tuning-trial-claim-v1"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_create_json(path: Path, payload: Any) -> None:
    """Atomically publish a fully written JSON file without replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A hard link is an atomic create-if-absent publication.  Unlike
            # os.replace it can never overwrite another worker's receipt.
            os.link(temporary, path)
        except FileExistsError as error:
            raise FileExistsError(
                f"refusing to replace immutable tuning record: {path}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_immutable_json(path: Path, payload: Any) -> None:
    """Create once or prove that the existing protocol artifact is identical."""
    try:
        _atomic_create_json(path, payload)
    except FileExistsError:
        try:
            existing = _read_json(path)
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise FileExistsError(
                f"refusing unreadable immutable tuning artifact: {path}") from error
        if existing != payload:
            raise FileExistsError(
                f"refusing to replace immutable tuning artifact: {path}")


def _archive_immutable_file(source: Path, destination: Path) -> str:
    """Hard-link one complete record into history before removing its alias."""
    if not source.is_file():
        raise FileNotFoundError(f"immutable history source is missing: {source}")
    digest = sha256_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except FileExistsError:
        if (not destination.is_file()
                or sha256_file(destination) != digest):
            raise FileExistsError(
                f"immutable history record differs: {destination}")
    if sha256_file(source) != digest:
        raise RuntimeError(
            f"immutable history source changed before removal: {source}")
    source.unlink()
    if not destination.is_file() or sha256_file(destination) != digest:
        raise RuntimeError(
            f"immutable history publication failed: {destination}")
    return digest


def _self_hash(payload: Mapping[str, Any], field: str) -> str:
    unsigned = {key: value for key, value in payload.items() if key != field}
    return hashlib.sha256(_stable_json(unsigned).encode()).hexdigest()


def _candidate_values(row: Mapping[str, Any]) -> dict[str, Any]:
    values = {key: row[key] for key in TUNING_SPACE}
    if not all(values[key] in TUNING_SPACE[key] for key in TUNING_SPACE):
        raise ValueError("candidate contains a value outside the frozen space")
    if candidate_id(values) != row.get("candidate_id"):
        raise ValueError("candidate id does not match its parameter hash")
    budget = (int(values["population_size"]) * int(values["iterations"]) *
              int(values["neighbor_sample_size"]))
    if budget > 3456:
        raise ValueError("candidate exceeds the frozen unique-evaluation budget")
    return values


def _setting_ref(payload: dict[str, Any]) -> dict[str, Any]:
    if "zac_setting" not in payload:
        return payload
    settings = payload["zac_setting"]
    if (not isinstance(settings, list) or len(settings) != 1 or
            not isinstance(settings[0], dict)):
        raise ValueError("tuning config requires exactly one zac_setting object")
    return settings[0]


def build_candidate_config_pair(
        m3_base: Mapping[str, Any], m4_base: Mapping[str, Any],
        candidate: Mapping[str, Any], *, seed: int) -> dict[str, dict[str, Any]]:
    """Resolve one shared M3/M4 pair and prove its only three differences."""
    if candidate.get("candidate_id") is None:
        candidate = {**candidate, "candidate_id": candidate_id(candidate)}
    values = _candidate_values(candidate)
    payloads = {
        "M3": copy.deepcopy(dict(m3_base)),
        "M4": copy.deepcopy(dict(m4_base)),
    }
    wrapped = {method: "zac_setting" in payload
               for method, payload in payloads.items()}
    if wrapped["M3"] != wrapped["M4"]:
        raise ValueError("M3/M4 tuning bases must use the same config shape")
    for method, payload in payloads.items():
        setting = _setting_ref(payload)
        # ``rho`` is a shared forecast-contract parameter, not an unrelated
        # top-level GA knob.  Materialise it into both nested lookahead specs so
        # every trial preserves the M3/M4 only-max_horizon difference.
        resolved_values = dict(values)
        rho = float(resolved_values.pop("rho"))
        setting.update(resolved_values)
        setting.update({
            "algorithm_revision": ALGORITHM_REVISION,
            "backend": NATIVE_BACKEND,
            "native_fail_closed": NATIVE_FAIL_CLOSED,
            # The outer v2 tuning ledger is intentionally independent of the
            # compiler/native ABI identity.  The search algorithm, wheel and
            # RNG did not change, so trial configs retain the registered v1
            # native protocol while schedules/receipts use tuning v2.
            "tuning_protocol_id": FORMAL_NATIVE_TUNING_PROTOCOL_ID,
            "seed": int(seed),
            "method_id": "ours_nl" if method == "M3" else "ours_lk",
            "lookahead_horizon": decay_lookahead_spec(
                0 if method == "M3" else 8, rho=rho),
        })
        # The method driver replaces this with its UUID-bearing attempt path.
        # Keeping distinct sentinels makes the registered exemption explicit.
        setting["dir"] = f"__runner_owned__/{method.lower()}/"
        validate_schema2_setting(setting)
        if "zac_setting" in payload:
            payload["tuning"] = {
                "algorithm_revision": ALGORITHM_REVISION,
                "candidate_id": str(candidate["candidate_id"]),
                "protocol_id": TUNING_PROTOCOL_ID,
                "track": "shared",
            }
    validate_schema2_pair(
        effective_zac_setting(payloads["M3"]),
        effective_zac_setting(payloads["M4"]),
    )
    if wrapped["M3"]:
        outer_m3 = {key: value for key, value in payloads["M3"].items()
                    if key != "zac_setting"}
        outer_m4 = {key: value for key, value in payloads["M4"].items()
                    if key != "zac_setting"}
        if outer_m3 != outer_m4:
            raise ValueError("M3/M4 tuning wrapper metadata differs")
    return payloads


def materialize_candidate_configs(
        plan: ExperimentPlan, root: Path, candidate: Mapping[str, Any],
        *, seed: int) -> dict[str, Path]:
    pair = build_candidate_config_pair(
        plan.methods["M3"].payload, plan.methods["M4"].payload,
        candidate, seed=seed)
    cid = str(candidate["candidate_id"])
    paths = {}
    for method in TUNING_METHODS:
        path = root / "configs" / cid / f"seed-{seed}" / f"{method}.json"
        _write_immutable_json(path, pair[method])
        paths[method] = path
    return paths


def _candidate_map(root: Path) -> dict[str, Mapping[str, Any]]:
    payload = _read_json(root / "candidates.json")
    if payload.get("protocol_id") != TUNING_PROTOCOL_ID:
        raise ValueError("candidate protocol mismatch")
    rows = payload.get("candidates")
    if not isinstance(rows, list):
        raise ValueError("candidates.json must contain a candidates array")
    result = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("candidate row must be an object")
        _candidate_values(row)
        cid = str(row["candidate_id"])
        if cid in result:
            raise ValueError(f"duplicate candidate id: {cid}")
        result[cid] = dict(row)
    return result


def _schedule_path(root: Path, phase: str) -> Path:
    return root / "schedules" / f"{phase}.json"


def _receipt_path(root: Path, trial: ScheduledTrial) -> Path:
    return root / "ledger" / trial.phase / f"{trial.trial_id}.json"


def _claim_path(root: Path, trial: ScheduledTrial) -> Path:
    return root / "claims" / trial.phase / f"{trial.trial_id}.json"


def _phase_seal_path(root: Path, phase: str) -> Path:
    return root / "phase_seals" / f"{phase}.json"


def _phase_snapshot_path(root: Path, phase: str) -> Path:
    return root / "phase_seals" / f"{phase}.receipts.json"


@contextmanager
def _phase_lock(root: Path, phase: str, *, exclusive: bool):
    """Coordinate the short claim/seal handshake across worker processes."""
    path = root / "phase_locks" / f"{phase}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(
            handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _phase_seal_payload(schedule: Mapping[str, Any], phase: str,
                        purpose: str) -> dict[str, Any]:
    payload = {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "phase": phase,
        "purpose": purpose,
        "schedule_sha256": schedule["schedule_sha256"],
    }
    payload["record_sha256"] = _self_hash(payload, "record_sha256")
    return payload


def _validate_phase_seal(path: Path, schedule: Mapping[str, Any],
                         phase: str, purpose: str) -> Mapping[str, Any]:
    payload = _read_json(path)
    expected = _phase_seal_payload(schedule, phase, purpose)
    if payload != expected:
        raise ValueError(f"tuning phase seal identity mismatch: {path}")
    return payload


def _assert_phase_open(root: Path, schedule: Mapping[str, Any], phase: str,
                       *, purpose: str) -> None:
    path = _phase_seal_path(root, phase)
    if path.exists():
        _validate_phase_seal(path, schedule, phase, purpose)
        raise RuntimeError(
            f"tuning phase {phase} is sealed for {purpose}; no retry is allowed")
    downstream = {
        "screen": (
            root / "leaderboard_screen.json",
            _schedule_path(root, "successive_halving"),
        ),
        "successive_halving": (
            root / "leaderboard_successive_halving.json",
            _schedule_path(root, "validation"),
        ),
        "validation": (
            root / "selected_config_manifest.json",
        ),
    }
    published = [path for path in downstream.get(phase, ()) if path.exists()]
    if published:
        raise RuntimeError(
            f"tuning phase {phase} already has downstream evidence; "
            f"no retry is allowed: {published[0]}")


def _receipt_snapshot_payload(
        root: Path, schedule: Mapping[str, Any],
        trials: Sequence[ScheduledTrial], *, purpose: str
        ) -> dict[str, Any]:
    receipts = []
    for trial in trials:
        path = _receipt_path(root, trial).resolve()
        if not path.is_file():
            raise RuntimeError(
                f"sealed phase is missing receipt: {trial.trial_id}")
        claim = _claim_path(root, trial)
        if claim.exists():
            _validate_claim(claim, schedule, trial)
            raise RuntimeError(
                f"sealed phase still has an active claim: {trial.trial_id}")
        receipts.append({
            "trial_id": trial.trial_id,
            "path": str(path),
            "sha256": sha256_file(path),
        })
    payload = {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "phase": trials[0].phase if trials else str(schedule.get("phase", "")),
        "purpose": purpose,
        "schedule_sha256": schedule["schedule_sha256"],
        "receipts": receipts,
    }
    payload["record_sha256"] = _self_hash(payload, "record_sha256")
    return payload


def _validate_receipt_snapshot(
        root: Path, schedule: Mapping[str, Any],
        trials: Sequence[ScheduledTrial], snapshot: Mapping[str, Any], *,
        purpose: str) -> None:
    current = _receipt_snapshot_payload(
        root, schedule, trials, purpose=purpose)
    if current != snapshot:
        raise RuntimeError("sealed tuning receipt snapshot changed before publish")


def _seal_completed_phase(
        root: Path, schedule: Mapping[str, Any],
        scheduled_trials: Sequence[ScheduledTrial], *, phase: str,
        purpose: str) -> tuple[Mapping[str, Any], list[TuningTrial],
                              Mapping[str, Any]]:
    """Freeze a complete phase and return its stable validated trial ledger."""
    # Preflight before sealing so an accidental early promote/finalize never
    # prevents missing trials from being completed.
    _, _ = load_phase_trials(root, phase)
    seal_path = _phase_seal_path(root, phase)
    seal = _phase_seal_payload(schedule, phase, purpose)
    _write_immutable_json(seal_path, seal)
    _validate_phase_seal(seal_path, schedule, phase, purpose)

    # A worker may have acquired its claim in the narrow preflight/seal gap.
    # It retains the claim until completion; fail closed now and let an
    # idempotent later invocation continue after that owner releases/recovery.
    _, trials = load_phase_trials(root, phase)
    snapshot = _receipt_snapshot_payload(
        root, schedule, scheduled_trials, purpose=purpose)
    snapshot_path = _phase_snapshot_path(root, phase)
    _write_immutable_json(snapshot_path, snapshot)
    observed = _read_json(snapshot_path)
    if observed != snapshot:
        raise ValueError(f"tuning phase snapshot identity mismatch: {snapshot_path}")
    _validate_receipt_snapshot(
        root, schedule, scheduled_trials, observed, purpose=purpose)
    return seal, trials, observed


def _load_published_phase_snapshot(
        root: Path, schedule: Mapping[str, Any],
        scheduled_trials: Sequence[ScheduledTrial], *, phase: str,
        purpose: str) -> Mapping[str, Any]:
    _validate_phase_seal(
        _phase_seal_path(root, phase), schedule, phase, purpose)
    path = _phase_snapshot_path(root, phase)
    snapshot = _read_json(path)
    if snapshot.get("record_sha256") != _self_hash(
            snapshot, "record_sha256"):
        raise ValueError(f"tuning phase snapshot hash mismatch: {path}")
    _validate_receipt_snapshot(
        root, schedule, scheduled_trials, snapshot, purpose=purpose)
    return snapshot


def _claim_payload(schedule: Mapping[str, Any], trial: ScheduledTrial, *,
                   worker_count: int, worker_index: int) -> dict[str, Any]:
    payload = {
        "experiment_schema": 2,
        "claim_schema": CLAIM_SCHEMA,
        "protocol_id": TUNING_PROTOCOL_ID,
        "schedule_sha256": schedule["schedule_sha256"],
        "trial": asdict(trial),
        "worker": {"count": worker_count, "index": worker_index},
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    payload["record_sha256"] = _self_hash(payload, "record_sha256")
    return payload


def _validate_claim(
        path: Path, schedule: Mapping[str, Any], trial: ScheduledTrial
        ) -> Mapping[str, Any]:
    payload = _read_json(path)
    required = {
        "experiment_schema", "claim_schema", "protocol_id",
        "schedule_sha256", "trial", "worker", "hostname", "pid",
        "created_at_utc", "record_sha256",
    }
    if set(payload) != required:
        raise ValueError(f"invalid tuning claim fields: {path}")
    if payload["record_sha256"] != _self_hash(payload, "record_sha256"):
        raise ValueError(f"tuning claim hash mismatch: {path}")
    if (payload["experiment_schema"] != 2 or
            payload["claim_schema"] != CLAIM_SCHEMA or
            payload["protocol_id"] != TUNING_PROTOCOL_ID or
            payload["schedule_sha256"] != schedule["schedule_sha256"] or
            payload["trial"] != asdict(trial)):
        raise ValueError(f"tuning claim identity mismatch: {path}")
    worker = payload["worker"]
    if not isinstance(worker, Mapping) or set(worker) != {"count", "index"}:
        raise ValueError(f"invalid tuning claim worker identity: {path}")
    count, index = worker["count"], worker["index"]
    if (not isinstance(count, int) or isinstance(count, bool) or count <= 0 or
            not isinstance(index, int) or isinstance(index, bool) or
            not 0 <= index < count or trial.ordinal % count != index):
        raise ValueError(f"invalid tuning claim shard identity: {path}")
    if (not isinstance(payload["pid"], int) or
            isinstance(payload["pid"], bool) or payload["pid"] <= 0 or
            not isinstance(payload["hostname"], str) or
            not payload["hostname"]):
        raise ValueError(f"invalid tuning claim process identity: {path}")
    try:
        created = datetime.fromisoformat(str(payload["created_at_utc"]))
    except ValueError as error:
        raise ValueError(f"invalid tuning claim timestamp: {path}") from error
    if created.tzinfo is None:
        raise ValueError(f"tuning claim timestamp is not timezone-aware: {path}")
    return payload


def _acquire_claim(
        root: Path, schedule: Mapping[str, Any], trial: ScheduledTrial, *,
        worker_count: int, worker_index: int) -> Mapping[str, Any]:
    """Atomically reserve one trial; an existing claim is always fatal.

    The record is written and fsynced under a unique temporary name before a
    no-replace hard link publishes it.  A killed worker therefore leaves either
    a complete, recoverable claim or no claim at all, never a truncated owner
    record.
    """
    path = _claim_path(root, trial)
    payload = _claim_payload(
        schedule, trial, worker_count=worker_count, worker_index=worker_index)
    try:
        _atomic_create_json(path, payload)
    except FileExistsError as error:
        owner = _validate_claim(path, schedule, trial)
        raise RuntimeError(
            "tuning trial is already claimed by "
            f"pid={owner['pid']} worker={owner['worker']}: {trial.trial_id}") from error
    return payload


def _release_claim(
        root: Path, schedule: Mapping[str, Any], trial: ScheduledTrial,
        expected: Mapping[str, Any]) -> None:
    path = _claim_path(root, trial)
    observed = _validate_claim(path, schedule, trial)
    if observed != expected or observed["pid"] != os.getpid():
        raise RuntimeError(f"refusing to release a foreign tuning claim: {path}")
    path.unlink()


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def recover_tuning_claim(root: Path, phase: str,
                         trial_id: str) -> Mapping[str, Any]:
    """Archive one validated local stale claim; never steals a live claim."""
    root = root.resolve()
    schedule, trials = load_schedule(root, phase)
    matches = [trial for trial in trials if trial.trial_id == trial_id]
    if len(matches) != 1:
        raise ValueError(f"trial id is absent from the frozen {phase} schedule")
    trial = matches[0]
    path = _claim_path(root, trial)
    if not path.is_file():
        raise FileNotFoundError(f"tuning claim is missing: {path}")
    payload = _validate_claim(path, schedule, trial)
    if payload["hostname"] != socket.gethostname():
        raise RuntimeError("cannot prove that a claim owner on another host stopped")
    if _pid_is_alive(int(payload["pid"])):
        raise RuntimeError(
            f"refusing to recover live tuning claim owned by pid={payload['pid']}")
    receipt_path = _receipt_path(root, trial)
    restored_receipt = None
    # A promoter may seal the phase while a retry that crossed the seal/claim
    # handshake is still running.  If that owner dies after archiving and
    # unlinking the failed receipt, restore the newest validated history alias
    # before releasing its claim; otherwise the permanent seal would make the
    # complete phase unrecoverable.
    if _phase_seal_path(root, phase).is_file() and not receipt_path.is_file():
        history_dir = root / "ledger_history" / phase / trial.trial_id
        histories = sorted(
            (item for item in history_dir.glob("*.json") if item.is_file()),
            key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
        if not histories:
            raise RuntimeError(
                "sealed tuning retry lost its receipt and has no history to restore")
        os.link(histories[0], receipt_path)
        receipt_payload = _read_json(receipt_path)
        config_path = Path(str(receipt_payload.get("config_path", "")))
        try:
            validate_trial_receipt(
                receipt_path, schedule, trial, config_path)
        except BaseException:
            receipt_path.unlink(missing_ok=True)
            raise
        restored_receipt = str(receipt_path)
    history = (root / "claim_history" / phase / trial.trial_id /
               f"{payload['record_sha256']}.json")
    archived_sha256 = _archive_immutable_file(path, history)
    return {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "phase": phase,
        "trial_id": trial_id,
        "recovered_claim": str(history),
        "claim_record_sha256": payload["record_sha256"],
        "claim_file_sha256": archived_sha256,
        "restored_receipt": restored_receipt,
    }


def _native_identity(plan: ExperimentPlan) -> Mapping[str, Any]:
    """Return the M3/M4 native identity that every tuning phase must retain."""
    settings = {
        method: effective_zac_setting(plan.methods[method].payload)
        for method in TUNING_METHODS
    }
    keys = (
        "algorithm_revision", "backend", "native_fail_closed",
        "native_abi_version", "native_wheel_sha256", "rng_version",
        "operator_profile", "init_engine", "formal_native",
        "tuning_protocol_id",
    )
    identity = {key: settings["M3"].get(key) for key in keys}
    drift = {
        key: (settings["M3"].get(key), settings["M4"].get(key))
        for key in keys
        if settings["M3"].get(key) != settings["M4"].get(key)
    }
    if drift:
        raise ValueError(f"M3/M4 native tuning identity differs: {drift}")
    if (identity["backend"] != NATIVE_BACKEND
            or identity["native_fail_closed"] is not True):
        raise ValueError("tuning requires the fail-closed native backend")
    return identity


def _canonical_bindings(suite) -> Mapping[str, Mapping[str, Any]]:
    return {
        Path(item.canonical_path).stem: {
            "path": str(Path(item.canonical_path).resolve()),
            "canonical_sha256": item.canonical_sha256,
            "source_sha256": item.source_sha256,
        }
        for item in suite
    }


def _replayed_promotion_report(root: Path, phase: str) -> Mapping[str, Any]:
    """Recompute one promotion report from its complete receipt ledger."""
    if phase not in {"screen", "successive_halving"}:
        raise ValueError(f"phase has no promotion report: {phase}")
    schedule, scheduled_trials = load_schedule(root, phase)
    _load_published_phase_snapshot(
        root, schedule, scheduled_trials, phase=phase, purpose="promotion")
    replayed_schedule, trials = load_phase_trials(root, phase)
    if replayed_schedule != schedule:
        raise ValueError(f"{phase} schedule changed during promotion replay")
    candidates_payload = _read_json(root / "candidates.json")
    default_id = str(candidates_payload["default_candidate_id"])
    leaderboard = stage_leaderboard(
        trials, candidate_ids=schedule["candidate_ids"], default_id=default_id,
        expected_circuits=schedule["circuits"],
        expected_seeds=schedule["seeds"])
    promoted = promoted_candidates(
        phase, leaderboard, default_id=default_id)
    expected = {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "quality_policy": TUNING_QUALITY_POLICY_ID,
        "transition_timing_scope": "concurrent_observational_nonclaim",
        "promotion_tiebreak": "quality_move_metrics_candidate_id",
        "phase": phase,
        "schedule_sha256": schedule["schedule_sha256"],
        "promoted_candidate_ids": promoted,
        "leaderboard": leaderboard,
    }
    expected["report_sha256"] = _self_hash(expected, "report_sha256")
    path = root / f"leaderboard_{phase}.json"
    if not path.is_file() or _read_json(path) != expected:
        raise ValueError(f"{phase} promotion report differs from ledger replay")
    return expected


def _validate_schedule_chain(
        root: Path, phase: str, workspace: Mapping[str, Any]
        ) -> tuple[Mapping[str, Any], list[ScheduledTrial]]:
    """Validate a phase schedule and its immutable promotion parent."""
    schedule, trials = load_schedule(root, phase)
    if phase == "screen":
        if schedule["schedule_sha256"] != workspace["screen_schedule_sha256"]:
            raise ValueError("screen schedule differs from tuning workspace")
        return schedule, trials
    predecessor = ("screen" if phase == "successive_halving"
                   else "successive_halving")
    # Recursively prove every earlier phase.  The final validation gate must
    # not be satisfiable by replacing only the top-3 report and recomputing its
    # self hash.
    prior_schedule, _ = _validate_schedule_chain(
        root, predecessor, workspace)
    report = _replayed_promotion_report(root, predecessor)
    split = _read_json(root / "split_manifest.json")
    contract = PHASE_CONTRACTS[phase]
    expected_circuits = list(split[str(contract["circuit_split"])])
    expected_seeds = list(contract["seeds"])
    chain_drift = {
        "parent_sha256": (
            schedule.get("parent_sha256"), report["report_sha256"]),
        "candidate_ids": (
            schedule.get("candidate_ids"), report["promoted_candidate_ids"]),
        "circuits": (schedule.get("circuits"), expected_circuits),
        "seeds": (schedule.get("seeds"), expected_seeds),
        "predecessor_schedule": (
            report.get("schedule_sha256"),
            prior_schedule["schedule_sha256"]),
    }
    chain_drift = {
        key: pair for key, pair in chain_drift.items() if pair[0] != pair[1]
    }
    if chain_drift:
        raise ValueError(f"{phase} schedule promotion chain drift: {chain_drift}")
    return schedule, trials


def _validate_tuning_workspace(
        plan: ExperimentPlan, dataset_name: str, root: Path
        ) -> Mapping[str, Any]:
    """Revalidate every frozen input before a tuning phase mutates state."""
    manifest = _read_json(root / "workspace_manifest.json")
    if manifest.get("manifest_sha256") != _self_hash(
            manifest, "manifest_sha256"):
        raise ValueError("tuning workspace manifest hash mismatch")
    if (manifest.get("protocol_id") != TUNING_PROTOCOL_ID
            or manifest.get("dataset") != dataset_name):
        raise ValueError("tuning workspace protocol/dataset mismatch")
    if manifest.get("quality_policy") != TUNING_QUALITY_POLICY_ID:
        raise ValueError("tuning workspace quality policy mismatch")
    repository = repository_snapshot(plan.repo_root)
    if (repository.get("dirty") or repository.get("commit") == "unknown"
            or repository.get("commit") !=
            manifest.get("repository", {}).get("commit")):
        raise RuntimeError("tuning requires its frozen clean repository commit")
    selection_path = Path(str(manifest["initial_selection_path"]))
    selection = validate_initial_selection_for_plan(
        plan, selection_path, enforce_pre_tuning_core=True)
    dataset = plan.datasets[dataset_name]
    suite = plan.load_suite(dataset)
    expected = {
        "plan_path": str(plan.path.resolve()),
        "plan_sha256": sha256_file(plan.path),
        "dataset_suite_path": str(dataset.suite_manifest.resolve()),
        "dataset_suite_sha256": sha256_file(dataset.suite_manifest),
        "experiment_id": plan.experiment_id(dataset),
        "canonical_inputs": _canonical_bindings(suite),
        "method_config_sha256": {
            method: sha256_file(plan.methods[method].config_path)
            for method in TUNING_METHODS
        },
        "native_identity": _native_identity(plan),
        "architecture_sha256": sha256_file(plan.architecture_path),
        "model_sha256": sha256_file(plan.model_path),
        "initial_selection_sha256": sha256_file(selection_path),
        "initial_selection_record_sha256": selection["record_sha256"],
        "initial_placement_engine": selection["selected_engine"],
        "split_sha256": sha256_file(root / "split_manifest.json"),
        "space_sha256": sha256_file(root / "search_space.json"),
        "candidates_sha256": sha256_file(root / "candidates.json"),
    }
    drift = {
        key: (manifest.get(key), value) for key, value in expected.items()
        if manifest.get(key) != value
    }
    if drift:
        raise ValueError(f"tuning workspace evidence drift: {drift}")
    _validate_schedule_chain(root, "screen", manifest)
    return manifest


def prepare_tuning_workspace(
        plan: ExperimentPlan, dataset_name: str, root: Path,
        *, split_seed: int = 0, schedule_seed: int = 0) -> Mapping[str, Any]:
    """Freeze split, candidates, configs and the 324-row screen schedule."""
    root = root.resolve()
    initial_selection_path = (
        root.parent / "initial-placement" / "selected_engine.json")
    initial_selection = validate_initial_selection_for_plan(
        plan, initial_selection_path, enforce_pre_tuning_core=True)
    repository = repository_snapshot(plan.repo_root)
    if repository.get("dirty") or repository.get("commit") == "unknown":
        raise RuntimeError("tuning prepare requires a clean versioned repository")
    dataset = plan.datasets[dataset_name]
    if dataset.kind != "main":
        raise ValueError("tuning accepts only a canonical main dataset")
    suite = plan.load_suite(dataset)
    rows = [manifest.to_dict() for manifest in suite]
    split = build_tuning_split(rows, seed=split_seed)
    candidates = generate_candidates(count=18, seed=split_seed)
    default_id = candidate_id(DEFAULT_CANDIDATE)
    split_payload = split
    space_payload = {
        "protocol_id": TUNING_PROTOCOL_ID,
        "space": {key: list(value) for key, value in TUNING_SPACE.items()},
        "budget_constraint":
            "population_size*iterations*neighbor_sample_size<=3456",
    }
    candidate_payload = {
        "protocol_id": TUNING_PROTOCOL_ID,
        "default_candidate_id": default_id,
        "candidates": candidates,
    }
    _write_immutable_json(root / "split_manifest.json", split_payload)
    _write_immutable_json(root / "search_space.json", space_payload)
    _write_immutable_json(root / "candidates.json", candidate_payload)

    # Materialising every seed before the first compiler launch makes config
    # drift a preflight error instead of a late partial-run failure.
    for candidate in candidates:
        for seed in range(5):
            materialize_candidate_configs(plan, root, candidate, seed=seed)

    screen = build_trial_schedule(
        "screen",
        candidate_ids=[str(row["candidate_id"]) for row in candidates],
        circuits=split["screen"], seeds=(0,), schedule_seed=schedule_seed,
        parent_sha256=split["sha256"],
    )
    _write_immutable_json(_schedule_path(root, "screen"), screen)
    manifest = {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "quality_policy": TUNING_QUALITY_POLICY_ID,
        "algorithm_revision": ALGORITHM_REVISION,
        "dataset": dataset_name,
        "repository": repository,
        "plan_path": str(plan.path.resolve()),
        "plan_sha256": sha256_file(plan.path),
        "dataset_suite_path": str(dataset.suite_manifest.resolve()),
        "dataset_suite_sha256": sha256_file(dataset.suite_manifest),
        "experiment_id": plan.experiment_id(dataset),
        "canonical_inputs": _canonical_bindings(suite),
        "method_config_sha256": {
            method: sha256_file(plan.methods[method].config_path)
            for method in TUNING_METHODS
        },
        "native_identity": _native_identity(plan),
        "architecture_sha256": sha256_file(plan.architecture_path),
        "model_sha256": sha256_file(plan.model_path),
        "initial_selection_path": str(initial_selection_path.resolve()),
        "initial_selection_sha256": sha256_file(initial_selection_path),
        "initial_selection_record_sha256":
            initial_selection["record_sha256"],
        "initial_placement_engine": initial_selection["selected_engine"],
        "split_sha256": sha256_file(root / "split_manifest.json"),
        "space_sha256": sha256_file(root / "search_space.json"),
        "candidates_sha256": sha256_file(root / "candidates.json"),
        "screen_schedule_sha256": screen["schedule_sha256"],
        "default_candidate_id": default_id,
        "backend": NATIVE_BACKEND,
        "native_fail_closed": True,
    }
    manifest["manifest_sha256"] = _self_hash(manifest, "manifest_sha256")
    _write_immutable_json(root / "workspace_manifest.json", manifest)
    return _validate_tuning_workspace(plan, dataset_name, root)


def load_schedule(root: Path, phase: str) -> tuple[Mapping[str, Any], list[ScheduledTrial]]:
    payload = _read_json(_schedule_path(root, phase))
    return payload, validate_trial_schedule(payload)


def _read_optional_artifact_json(directory: Path, name: str) -> Mapping[str, Any]:
    raw = directory / name
    archived = directory / f"{name}.gz"
    matches = [path for path in (raw, archived) if path.is_file()]
    if len(matches) != 1:
        return {}
    path = matches[0]
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
    else:
        value = _read_json(path)
    return value if isinstance(value, Mapping) else {}


def _artifact_json_sha256(directory: Path, name: str) -> str | None:
    raw = directory / name
    archived = directory / f"{name}.gz"
    matches = [path for path in (raw, archived) if path.is_file()]
    if len(matches) != 1:
        return None
    return sha256_file(matches[0])


def _transition_ns(manifest: Mapping[str, Any],
                   stats: Mapping[str, Any]) -> int | None:
    candidates = [
        manifest.get("transition_decision_ns"),
        stats.get("transition_decision_ns"),
        (stats.get("stage_timing", {}).get("transition_decision_ns")
         if isinstance(stats.get("stage_timing"), Mapping) else None),
    ]
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _native_evidence(stats: Mapping[str, Any]) -> tuple[str, bool | None]:
    backend = stats.get("backend", stats.get("resident_backend", ""))
    fallback = stats.get("fallback", stats.get("native_fallback"))
    if isinstance(fallback, bool):
        return str(backend), fallback
    return str(backend), None


def _tuning_receipt_projection(
        manifest_record: RunManifest, stats: Mapping[str, Any],
        workspace: Mapping[str, Any]) -> Mapping[str, Any]:
    """Reconstruct every receipt field allowed to affect tuning."""
    manifest = manifest_record.to_dict()
    backend, fallback = _native_evidence(stats)
    transition_ns = _transition_ns(manifest, stats)
    compiler_status = str(manifest.get("status", "compiler_error"))
    fidelity_ood = manifest.get("fidelity_ood") is True
    log_fidelity = manifest.get("log_fidelity")
    exponential_log_fidelity = manifest.get(
        "exponential_sensitivity_log_fidelity")

    def finite_number(value: Any) -> bool:
        return (not isinstance(value, bool) and
                isinstance(value, (int, float)) and
                math.isfinite(float(value)))

    errors = []
    if compiler_status == "success":
        if manifest.get("verifier_ok") is not True:
            errors.append("independent verifier did not pass")
        if manifest.get("ghost_hits") != 0:
            errors.append("formal M3/M4 result does not have ghost_hits=0")
        if backend not in {"native", "cpp-native-v7"}:
            errors.append(f"missing native backend evidence: {backend!r}")
        if fallback is not False:
            errors.append("native fallback evidence is absent or true")
        if transition_ns is None:
            errors.append("transition_decision_ns is absent")
        native_identity = workspace["native_identity"]
        for key in ("algorithm_revision", "native_abi_version",
                    "native_wheel_sha256", "rng_version"):
            if manifest.get(key) != native_identity[key]:
                errors.append(
                    f"native identity {key} differs from workspace")
        if fidelity_ood:
            if log_fidelity is not None:
                errors.append("OOD result invents a linear log_fidelity")
        elif not finite_number(log_fidelity):
            errors.append("in-domain log_fidelity is absent or non-finite")
        if not finite_number(exponential_log_fidelity):
            errors.append(
                "exponential_sensitivity_log_fidelity is absent or non-finite")
        for key in ("move_time_us", "move_batches"):
            if manifest.get(key) is None:
                errors.append(f"{key} is absent")
    tuning_status = (
        "scorer_error" if errors and compiler_status == "success"
        else compiler_status)
    return {
        "status": tuning_status,
        "compiler_status": compiler_status,
        "verifier_ok": manifest.get("verifier_ok") is True,
        "ghost_hits": manifest.get("ghost_hits"),
        "fallback": fallback,
        "backend": backend,
        "log_fidelity": log_fidelity,
        "fidelity_ood": fidelity_ood,
        "exponential_sensitivity_log_fidelity": exponential_log_fidelity,
        "transition_decision_ns": transition_ns,
        "move_time_us": manifest.get("move_time_us"),
        "move_batches": manifest.get("move_batches"),
        "errors": errors,
    }


def seal_trial_result(
        root: Path, schedule: Mapping[str, Any], trial: ScheduledTrial,
        config_path: Path, manifest_path: Path) -> Mapping[str, Any]:
    """Seal one manifest into the narrow tuning ledger contract."""
    manifest_record = load_run_manifest(
        manifest_path, require_success_metrics=True)
    manifest = manifest_record.to_dict()
    workspace = _read_json(root / "workspace_manifest.json")
    artifact = manifest_path.parent
    stats = _read_optional_artifact_json(artifact, "compiler_stats.json")
    projection = _tuning_receipt_projection(
        manifest_record, stats, workspace)
    expected_experiment_id = stable_sha256({
        "protocol_id": TUNING_PROTOCOL_ID,
        "schedule_sha256": schedule["schedule_sha256"],
        "trial_id": trial.trial_id,
    })
    canonical = workspace["canonical_inputs"][trial.circuit]
    immutable_identity = {
        "experiment_schema": 2,
        "dataset": workspace["dataset"],
        "circuit": trial.circuit,
        "method": trial.method,
        "seed": trial.seed,
        "repetition": 0,
        "run_kind": "smoke",
        "ablation_variant": "",
        "config_sha256": sha256_file(config_path),
        "input_sha256": canonical["canonical_sha256"],
        "architecture_sha256": workspace["architecture_sha256"],
        "model_sha256": workspace["model_sha256"],
        "experiment_id": expected_experiment_id,
        "git_commit": workspace["repository"]["commit"],
        "git_dirty": False,
        "algorithm_revision": workspace["native_identity"][
            "algorithm_revision"],
        "backend": NATIVE_BACKEND,
        "native_abi_version": workspace["native_identity"][
            "native_abi_version"],
        "native_wheel_sha256": workspace["native_identity"][
            "native_wheel_sha256"],
        "rng_version": workspace["native_identity"]["rng_version"],
        "tuning_protocol_id": workspace["native_identity"][
            "tuning_protocol_id"],
    }
    identity_drift = {
        key: (manifest.get(key), expected)
        for key, expected in immutable_identity.items()
        if manifest.get(key) != expected
    }
    if identity_drift:
        raise ValueError(f"tuning attempt immutable identity drift: {identity_drift}")
    if (not manifest_record.artifact_dir or
            Path(manifest_record.artifact_dir).resolve() !=
            manifest_path.parent.resolve()):
        raise ValueError("tuning attempt artifact_dir does not identify its manifest")
    result = {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "workspace_manifest_sha256": workspace["manifest_sha256"],
        "schedule_sha256": schedule["schedule_sha256"],
        "dataset": workspace["dataset"],
        "run_kind": "smoke",
        "repetition": 0,
        "experiment_id": expected_experiment_id,
        "input_sha256": canonical["canonical_sha256"],
        "architecture_sha256": workspace["architecture_sha256"],
        "model_sha256": workspace["model_sha256"],
        "git_commit": workspace["repository"]["commit"],
        "native_identity_sha256": stable_sha256(
            workspace["native_identity"]),
        "compiler_and_flags_sha256": stable_sha256(
            manifest_record.compiler_and_flags),
        "trial": asdict(trial),
        "config_path": str(config_path.resolve()),
        "config_sha256": sha256_file(config_path),
        "attempt_manifest": str(manifest_path.resolve()),
        "attempt_manifest_sha256": sha256_file(manifest_path),
        "compiler_stats_sha256": _artifact_json_sha256(
            artifact, "compiler_stats.json"),
        **projection,
    }
    result["record_sha256"] = _self_hash(result, "record_sha256")
    _atomic_create_json(_receipt_path(root, trial), result)
    return result


def validate_trial_receipt(
        path: Path, schedule: Mapping[str, Any], trial: ScheduledTrial,
        config_path: Path) -> Mapping[str, Any]:
    payload = _read_json(path)
    required = {
        "experiment_schema", "protocol_id", "workspace_manifest_sha256",
        "schedule_sha256", "dataset", "run_kind", "repetition",
        "experiment_id", "input_sha256", "architecture_sha256",
        "model_sha256", "git_commit", "native_identity_sha256",
        "compiler_and_flags_sha256", "trial",
        "config_path", "config_sha256", "attempt_manifest",
        "attempt_manifest_sha256", "compiler_stats_sha256",
        "status", "compiler_status",
        "verifier_ok", "ghost_hits", "fallback", "backend", "log_fidelity",
        "fidelity_ood", "exponential_sensitivity_log_fidelity",
        "transition_decision_ns", "move_time_us", "move_batches", "errors",
        "record_sha256",
    }
    unknown = set(payload) - required
    missing = required - set(payload)
    if unknown or missing:
        raise ValueError(
            f"invalid tuning receipt fields at {path}: "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}")
    if payload["record_sha256"] != _self_hash(payload, "record_sha256"):
        raise ValueError(f"tuning receipt hash mismatch: {path}")
    if (payload["experiment_schema"] != 2 or
            payload["protocol_id"] != TUNING_PROTOCOL_ID or
            payload["schedule_sha256"] != schedule["schedule_sha256"] or
            payload["trial"] != asdict(trial)):
        raise ValueError(f"tuning receipt identity mismatch: {path}")
    root = path.resolve().parents[2]
    workspace = _read_json(root / "workspace_manifest.json")
    expected_experiment_id = stable_sha256({
        "protocol_id": TUNING_PROTOCOL_ID,
        "schedule_sha256": schedule["schedule_sha256"],
        "trial_id": trial.trial_id,
    })
    receipt_binding = {
        "workspace_manifest_sha256": workspace["manifest_sha256"],
        "dataset": workspace["dataset"],
        "run_kind": "smoke",
        "repetition": 0,
        "experiment_id": expected_experiment_id,
        "input_sha256": workspace["canonical_inputs"][trial.circuit][
            "canonical_sha256"],
        "architecture_sha256": workspace["architecture_sha256"],
        "model_sha256": workspace["model_sha256"],
        "git_commit": workspace["repository"]["commit"],
        "native_identity_sha256": stable_sha256(
            workspace["native_identity"]),
    }
    binding_drift = {
        key: (payload.get(key), expected)
        for key, expected in receipt_binding.items()
        if payload.get(key) != expected
    }
    if binding_drift:
        raise ValueError(f"tuning receipt workspace drift: {binding_drift}")
    if Path(str(payload["config_path"])).resolve() != config_path.resolve():
        raise ValueError(f"tuning receipt config path drift: {path}")
    if payload["config_sha256"] != sha256_file(config_path):
        raise ValueError(f"tuning receipt config drift: {path}")
    manifest_path = Path(str(payload["attempt_manifest"]))
    if (not manifest_path.is_file() or
            sha256_file(manifest_path) != payload["attempt_manifest_sha256"]):
        raise ValueError(f"tuning receipt attempt manifest drift: {path}")
    manifest_record = load_run_manifest(
        manifest_path, require_success_metrics=True)
    manifest = manifest_record.to_dict()
    identity = {
        "experiment_schema": 2,
        "dataset": workspace["dataset"],
        "circuit": trial.circuit,
        "method": trial.method,
        "seed": trial.seed,
        "repetition": 0,
        "run_kind": "smoke",
        "ablation_variant": "",
        "config_sha256": sha256_file(config_path),
        "input_sha256": receipt_binding["input_sha256"],
        "architecture_sha256": workspace["architecture_sha256"],
        "model_sha256": workspace["model_sha256"],
        "experiment_id": expected_experiment_id,
        "git_commit": workspace["repository"]["commit"],
        "git_dirty": False,
        "algorithm_revision": workspace["native_identity"][
            "algorithm_revision"],
        "backend": NATIVE_BACKEND,
        "native_abi_version": workspace["native_identity"][
            "native_abi_version"],
        "native_wheel_sha256": workspace["native_identity"][
            "native_wheel_sha256"],
        "rng_version": workspace["native_identity"]["rng_version"],
        "tuning_protocol_id": workspace["native_identity"][
            "tuning_protocol_id"],
    }
    drift = {key: (manifest.get(key), expected) for key, expected in identity.items()
             if manifest.get(key) != expected}
    if drift:
        raise ValueError(f"tuning attempt identity drift: {drift}")
    if (not manifest_record.artifact_dir or
            Path(manifest_record.artifact_dir).resolve() !=
            manifest_path.parent.resolve()):
        raise ValueError("tuning attempt artifact_dir drift")
    if (payload["compiler_and_flags_sha256"] !=
            stable_sha256(manifest_record.compiler_and_flags)):
        raise ValueError("tuning attempt compiler flags drift")
    stats = _read_optional_artifact_json(
        manifest_path.parent, "compiler_stats.json")
    stats_sha256 = _artifact_json_sha256(
        manifest_path.parent, "compiler_stats.json")
    if payload["compiler_stats_sha256"] != stats_sha256:
        raise ValueError("tuning attempt compiler stats drift")
    expected_projection = _tuning_receipt_projection(
        manifest_record, stats, workspace)
    projection_drift = {
        key: (payload.get(key), expected)
        for key, expected in expected_projection.items()
        if payload.get(key) != expected
    }
    if projection_drift:
        raise ValueError(
            f"tuning receipt projection drift: {projection_drift}")
    if payload["status"] not in TERMINAL_STATUSES:
        raise ValueError(f"non-terminal tuning receipt status: {payload['status']}")
    return payload


def _receipt_as_trial(payload: Mapping[str, Any]) -> TuningTrial:
    trial = payload["trial"]
    return TuningTrial(
        candidate_id=str(trial["candidate_id"]),
        circuit=str(trial["circuit"]), method=str(trial["method"]),
        seed=int(trial["seed"]), status=str(payload["status"]),
        verifier_ok=payload["verifier_ok"] is True,
        ghost_hits=(-1 if payload["ghost_hits"] is None
                    else int(payload["ghost_hits"])),
        fallback=(True if payload["fallback"] is None
                  else bool(payload["fallback"])),
        log_fidelity=(None if payload["log_fidelity"] is None
                      else float(payload["log_fidelity"])),
        transition_decision_ns=(
            None if payload["transition_decision_ns"] is None
            else int(payload["transition_decision_ns"])),
        move_time_us=(None if payload["move_time_us"] is None
                      else float(payload["move_time_us"])),
        move_batches=(None if payload["move_batches"] is None
                      else int(payload["move_batches"])),
        fidelity_ood=bool(payload["fidelity_ood"]),
        exponential_sensitivity_log_fidelity=(
            None
            if payload["exponential_sensitivity_log_fidelity"] is None
            else float(payload["exponential_sensitivity_log_fidelity"])),
    )


def _select_worker_trials(
        trials: Sequence[ScheduledTrial], phase: str, *, worker_count: int,
        worker_index: int, limit: int | None = None) -> list[ScheduledTrial]:
    if (not isinstance(worker_count, int) or isinstance(worker_count, bool)
            or worker_count <= 0):
        raise ValueError("worker_count must be a positive integer")
    if (not isinstance(worker_index, int) or isinstance(worker_index, bool)
            or not 0 <= worker_index < worker_count):
        raise ValueError("worker_index must satisfy 0 <= index < count")
    if phase == "validation" and (worker_count != 1 or worker_index != 0):
        raise ValueError("validation tuning must run serially")
    selected = [
        trial for trial in trials
        if trial.ordinal % worker_count == worker_index
    ]
    if limit is not None:
        selected = selected[:max(0, int(limit))]
    return selected


def run_tuning_phase(
        plan: ExperimentPlan, dataset_name: str, root: Path, phase: str, *,
        resume: bool, retry_failed: bool = False, dry_run: bool = False,
        limit: int | None = None, worker_count: int = 1,
        worker_index: int = 0) -> Mapping[str, Any]:
    """Execute one deterministic schedule shard; receipts remain global."""
    root = root.resolve()
    workspace = _validate_tuning_workspace(plan, dataset_name, root)
    schedule, trials = _validate_schedule_chain(root, phase, workspace)
    phase_purpose = "finalization" if phase == "validation" else "promotion"
    _assert_phase_open(
        root, schedule, phase, purpose=phase_purpose)
    candidates = _candidate_map(root)
    dataset = plan.datasets[dataset_name]
    canonical_by_name = {
        Path(item.canonical_path).stem: item for item in plan.load_suite(dataset)}
    if set(schedule["circuits"]) - set(canonical_by_name):
        raise ValueError("tuning schedule references a circuit outside the suite")
    worker_trials = _select_worker_trials(
        trials, phase, worker_count=worker_count, worker_index=worker_index)
    selected = _select_worker_trials(
        trials, phase, worker_count=worker_count, worker_index=worker_index,
        limit=limit)
    if not resume:
        existing = [_receipt_path(root, trial) for trial in selected
                    if _receipt_path(root, trial).exists()]
        if existing:
            raise FileExistsError(
                "tuning receipts already exist; use --resume or a new root: "
                f"{existing[0]}")

    attempted = []
    skipped = []
    commands = []
    for trial in selected:
        candidate = candidates.get(trial.candidate_id)
        if candidate is None:
            raise ValueError(f"unknown scheduled candidate: {trial.candidate_id}")
        configs = materialize_candidate_configs(
            plan, root, candidate, seed=trial.seed)
        config_path = configs[trial.method]
        receipt_path = _receipt_path(root, trial)
        if receipt_path.is_file():
            if _claim_path(root, trial).exists():
                raise RuntimeError(
                    "sealed tuning receipt still has a claim; recover it "
                    f"explicitly: {trial.trial_id}")
            old = validate_trial_receipt(
                receipt_path, schedule, trial, config_path)
            if not retry_failed or old["status"] == "success":
                skipped.append({"trial_id": trial.trial_id,
                                "status": old["status"]})
                continue

        canonical = canonical_by_name[trial.circuit]
        # RunManifest currently has no tuning run_kind; the outer immutable
        # tuning receipt carries phase identity while the compiler uses the
        # registered smoke contract.  No smoke artifact is ever accepted by a
        # main-paper aggregator.
        spec = _attempt_spec(
            plan, dataset, canonical, trial.method, trial.seed, 0, "smoke",
            config_path=config_path,
            experiment_id=stable_sha256({
                "protocol_id": TUNING_PROTOCOL_ID,
                "schedule_sha256": schedule["schedule_sha256"],
                "trial_id": trial.trial_id,
            }),
        )
        spec = replace(
            spec,
            output_root=(root / "trials" / phase / trial.trial_id / "attempts"),
            run_kind="smoke",
        )
        if dry_run:
            commands.append({
                "trial": asdict(trial), "command": list(spec.command),
                "config": str(config_path),
                "output_root": str(spec.output_root),
            })
            continue
        with _phase_lock(root, phase, exclusive=False):
            _assert_phase_open(
                root, schedule, phase, purpose=phase_purpose)
            claim = _acquire_claim(
                root, schedule, trial, worker_count=worker_count,
                worker_index=worker_index)
            try:
                _assert_phase_open(
                    root, schedule, phase, purpose=phase_purpose)
            except BaseException:
                _release_claim(root, schedule, trial, claim)
                raise
        try:
            # Close the receipt/claim TOCTOU window.  A sealed receipt wins,
            # but the owner must release the claim it just acquired.
            if receipt_path.is_file():
                old = validate_trial_receipt(
                    receipt_path, schedule, trial, config_path)
                if not retry_failed or old["status"] == "success":
                    skipped.append({"trial_id": trial.trial_id,
                                    "status": old["status"]})
                    _release_claim(root, schedule, trial, claim)
                    continue
            if receipt_path.is_file():
                history = (root / "ledger_history" / phase / trial.trial_id /
                           f"{old['record_sha256']}.json")
                archived_sha256 = _archive_immutable_file(
                    receipt_path, history)
                if archived_sha256 != sha256_file(history):
                    raise RuntimeError(
                        f"retry receipt history hash mismatch: {history}")

            gate = UnifiedEvaluationGate(plan, canonical, trial.method)
            manifest = run_attempt(
                spec, verifier=gate.verifier, scorer=gate.scorer)
            manifest_path = Path(manifest.artifact_dir) / "manifest.json"
            receipt = seal_trial_result(
                root, schedule, trial, config_path, manifest_path)
            attempted.append({
                "trial_id": trial.trial_id,
                "status": receipt["status"],
                "receipt": str(receipt_path),
                "attempt_manifest": receipt["attempt_manifest"],
            })
        except BaseException:
            # The claim is the durable evidence that this trial needs explicit
            # operator recovery before another worker may retry it.
            raise
        else:
            _release_claim(root, schedule, trial, claim)
    return {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "phase": phase,
        "schedule_sha256": schedule["schedule_sha256"],
        "planned": len(trials),
        "worker_count": worker_count,
        "worker_index": worker_index,
        "worker_planned": len(worker_trials),
        "selected": len(selected),
        "attempted": attempted,
        "skipped": skipped,
        "dry_run_commands": commands,
    }


def load_phase_trials(root: Path, phase: str) -> tuple[Mapping[str, Any], list[TuningTrial]]:
    schedule, rows = load_schedule(root, phase)
    candidates = _candidate_map(root)
    trials = []
    # Receipts are addressed directly from the frozen schedule.  Extra files do
    # not enter the ledger; a missing scheduled receipt is fatal.
    for row in rows:
        candidate = candidates[row.candidate_id]
        path = _receipt_path(root, row)
        if not path.is_file():
            raise RuntimeError(f"tuning phase is incomplete; missing receipt: {path}")
        claim = _claim_path(root, row)
        if claim.exists():
            _validate_claim(claim, schedule, row)
            raise RuntimeError(
                "tuning phase has an unrecovered trial claim: "
                f"{row.trial_id}")
        payload = _read_json(path)
        config_path = Path(str(payload.get("config_path", "")))
        if not config_path.is_file():
            raise ValueError(f"tuning receipt config is missing: {path}")
        payload = validate_trial_receipt(
            path, schedule, row, config_path)
        if candidate_id(_candidate_values(candidate)) != row.candidate_id:
            raise ValueError("candidate ledger drift")
        trials.append(_receipt_as_trial(payload))
    return schedule, trials


def _write_leaderboard_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fields = [
        "rank", "candidate_id", "valid", "shared_score",
        "m3_delta_logf", "m4_delta_logf", "m3_transition_ns",
        "m4_transition_ns", "m3_move_time_us", "m4_move_time_us",
        "m3_move_batches", "m4_move_batches",
    ]
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            methods = row.get("methods", {})
            m3 = methods.get("M3", {})
            m4 = methods.get("M4", {})
            writer.writerow({
                "rank": row.get("rank"),
                "candidate_id": row["candidate_id"],
                "valid": row["valid"],
                "shared_score": row.get("shared_score"),
                "m3_delta_logf": m3.get("median_delta_log_fidelity"),
                "m4_delta_logf": m4.get("median_delta_log_fidelity"),
                "m3_transition_ns": m3.get("median_transition_decision_ns"),
                "m4_transition_ns": m4.get("median_transition_decision_ns"),
                "m3_move_time_us": m3.get("median_move_time_us"),
                "m4_move_time_us": m4.get("median_move_time_us"),
                "m3_move_batches": m3.get("median_move_batches"),
                "m4_move_batches": m4.get("median_move_batches"),
            })
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_pareto_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Write the validation Pareto membership without generating a figure."""
    points = []
    for row in rows:
        if not row.get("valid"):
            continue
        methods = row["methods"]
        points.append({
            "candidate_id": row["candidate_id"],
            "shared_score": float(row["shared_score"]),
            "transition_decision_ns": float(statistics.median(
                methods[method]["median_transition_decision_ns"]
                for method in TUNING_METHODS)),
            "move_time_us": float(statistics.median(
                methods[method]["median_move_time_us"]
                for method in TUNING_METHODS)),
            "move_batches": float(statistics.median(
                methods[method]["median_move_batches"]
                for method in TUNING_METHODS)),
        })
    for point in points:
        point["pareto"] = not any(
            (other["shared_score"] >= point["shared_score"] and
             other["transition_decision_ns"] <= point["transition_decision_ns"] and
             other["move_time_us"] <= point["move_time_us"] and
             other["move_batches"] <= point["move_batches"] and
             (other["shared_score"] > point["shared_score"] or
              other["transition_decision_ns"] < point["transition_decision_ns"] or
              other["move_time_us"] < point["move_time_us"] or
              other["move_batches"] < point["move_batches"]))
            for other in points if other is not point)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fields = ["candidate_id", "pareto", "shared_score",
              "transition_decision_ns", "move_time_us", "move_batches"]
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(points, key=lambda row: (
            not row["pareto"], -row["shared_score"], row["candidate_id"])))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def promote_tuning_phase(root: Path, phase: str, *, schedule_seed: int = 0
                         ) -> Mapping[str, Any]:
    if phase not in {"screen", "successive_halving"}:
        raise ValueError("only screen or successive_halving can be promoted")
    root = root.resolve()
    workspace_record = _read_json(root / "workspace_manifest.json")
    plan = load_experiment_plan(workspace_record["plan_path"])
    workspace = _validate_tuning_workspace(
        plan, str(workspace_record["dataset"]), root)
    schedule, scheduled_trials = _validate_schedule_chain(
        root, phase, workspace)
    candidates_payload = _read_json(root / "candidates.json")
    default_id = str(candidates_payload["default_candidate_id"])
    with _phase_lock(root, phase, exclusive=True):
        preflight_schedule, preflight_trials = load_phase_trials(root, phase)
        if preflight_schedule != schedule:
            raise ValueError(f"{phase} schedule changed during promotion preflight")
        preflight_leaderboard = stage_leaderboard(
            preflight_trials, candidate_ids=schedule["candidate_ids"],
            default_id=default_id, expected_circuits=schedule["circuits"],
            expected_seeds=schedule["seeds"])
        preflight_promoted = promoted_candidates(
            phase, preflight_leaderboard, default_id=default_id)

        _, trials, snapshot = _seal_completed_phase(
            root, schedule, scheduled_trials, phase=phase, purpose="promotion")
        leaderboard = stage_leaderboard(
            trials, candidate_ids=schedule["candidate_ids"], default_id=default_id,
            expected_circuits=schedule["circuits"],
            expected_seeds=schedule["seeds"])
        promoted = promoted_candidates(phase, leaderboard, default_id=default_id)
        if (leaderboard != preflight_leaderboard
                or promoted != preflight_promoted):
            raise RuntimeError(
                f"{phase} promotion result changed across phase sealing")
    report = {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "quality_policy": TUNING_QUALITY_POLICY_ID,
        "transition_timing_scope": "concurrent_observational_nonclaim",
        "promotion_tiebreak": "quality_move_metrics_candidate_id",
        "phase": phase,
        "schedule_sha256": schedule["schedule_sha256"],
        "promoted_candidate_ids": promoted,
        "leaderboard": leaderboard,
    }
    report["report_sha256"] = _self_hash(report, "report_sha256")
    report_path = root / f"leaderboard_{phase}.json"
    _validate_receipt_snapshot(
        root, schedule, scheduled_trials, snapshot, purpose="promotion")
    _write_immutable_json(report_path, report)
    _write_leaderboard_csv(root / f"leaderboard_{phase}.csv", leaderboard)

    split = _read_json(root / "split_manifest.json")
    next_phase = ("successive_halving" if phase == "screen" else "validation")
    contract = PHASE_CONTRACTS[next_phase]
    next_schedule = build_trial_schedule(
        next_phase, candidate_ids=promoted,
        circuits=split[str(contract["circuit_split"])],
        seeds=contract["seeds"], schedule_seed=schedule_seed,
        parent_sha256=report["report_sha256"],
    )
    _validate_receipt_snapshot(
        root, schedule, scheduled_trials, snapshot, purpose="promotion")
    _write_immutable_json(_schedule_path(root, next_phase), next_schedule)
    return {**report, "next_phase": next_phase,
            "next_schedule_sha256": next_schedule["schedule_sha256"]}


def _selection_config(
        plan: ExperimentPlan, root: Path, candidate: Mapping[str, Any],
        method: str, *, track: str) -> Mapping[str, Any]:
    pair = build_candidate_config_pair(
        plan.methods["M3"].payload, plan.methods["M4"].payload,
        candidate, seed=0)
    payload = pair[method]
    if "zac_setting" in payload:
        payload["tuning"]["track"] = track
    return payload


def _ranked_validation_selection(
        trials: Sequence[TuningTrial], *, default_id: str,
        expected_circuits: Sequence[str], expected_seeds: Sequence[int]
        ) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    """Recreate the exact ranked selection payload used by finalization."""
    selection = rank_candidates(
        trials, default_id=default_id,
        expected_circuits=expected_circuits,
        expected_seeds=expected_seeds)
    summaries = selection["summaries"]
    ordered = sorted(summaries, key=lambda row: (
        not bool(row["valid"]),
        -(float(row["shared_score"]) if row["shared_score"] is not None
          else float("-inf")),
        row["candidate_id"],
    ))
    for index, row in enumerate(ordered, start=1):
        row["rank"] = index if row["valid"] else None
    return selection, ordered


def finalize_tuning(
        plan: ExperimentPlan, root: Path, config_output: Path) -> Mapping[str, Any]:
    """Select shared/independent configs only from the complete validation ledger."""
    root = root.resolve()
    workspace = _validate_tuning_workspace(
        plan, str(_read_json(root / "workspace_manifest.json")["dataset"]), root)
    schedule, scheduled_trials = _validate_schedule_chain(
        root, "validation", workspace)
    candidates = _candidate_map(root)
    default_id = str(_read_json(root / "candidates.json")["default_candidate_id"])
    with _phase_lock(root, "validation", exclusive=True):
        preflight_schedule, preflight_trials = load_phase_trials(
            root, "validation")
        if preflight_schedule != schedule:
            raise ValueError(
                "validation schedule changed during finalization preflight")
        preflight_selection, preflight_ordered = _ranked_validation_selection(
            preflight_trials, default_id=default_id,
            expected_circuits=schedule["circuits"],
            expected_seeds=schedule["seeds"])
        if preflight_selection["shared_selected"] is None:
            raise RuntimeError(
                "no valid shared tuning configuration was selected")

        _, trials, snapshot = _seal_completed_phase(
            root, schedule, scheduled_trials, phase="validation",
            purpose="finalization")
        selection, ordered = _ranked_validation_selection(
            trials, default_id=default_id,
            expected_circuits=schedule["circuits"],
            expected_seeds=schedule["seeds"])
        if selection != preflight_selection or ordered != preflight_ordered:
            raise RuntimeError(
                "validation selection changed across phase sealing")
    _validate_receipt_snapshot(
        root, schedule, scheduled_trials, snapshot, purpose="finalization")
    _write_leaderboard_csv(root / "leaderboard.csv", ordered)
    _write_pareto_csv(root / "pareto.csv", ordered)
    _atomic_json(root / "leaderboard.json", {
        **selection,
        "validation_schedule_sha256": schedule["schedule_sha256"],
    })

    shared_id = str(selection["shared_selected"])
    independent_ids = {
        method: str(selection["independent_selected"][method] or default_id)
        for method in TUNING_METHODS}
    selected_dir = root / "selected"
    shared_payloads = {
        method: _selection_config(
            plan, root, candidates[shared_id], method, track="shared")
        for method in TUNING_METHODS}
    independent_payloads = {
        method: _selection_config(
            plan, root, candidates[independent_ids[method]], method,
            track="independent")
        for method in TUNING_METHODS}
    output_names = {
        "M3_shared": "ours_nl_shared.json",
        "M4_shared": "ours_lk_shared.json",
        "M3_independent": "ours_nl_independent.json",
        "M4_independent": "ours_lk_independent.json",
    }
    output_payloads = {
        "M3_shared": shared_payloads["M3"],
        "M4_shared": shared_payloads["M4"],
        "M3_independent": independent_payloads["M3"],
        "M4_independent": independent_payloads["M4"],
    }
    config_hashes = {}
    for label, name in output_names.items():
        for destination_root in (selected_dir, config_output):
            _atomic_json(destination_root / name, output_payloads[label])
        config_hashes[name] = sha256_file(config_output / name)
    selected_manifest = {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "quality_policy": TUNING_QUALITY_POLICY_ID,
        "algorithm_revision": ALGORITHM_REVISION,
        "selection_status": "frozen_validation_selection",
        "workspace_manifest_sha256": workspace["manifest_sha256"],
        "repository": workspace["repository"],
        "plan_path": workspace["plan_path"],
        "plan_sha256": workspace["plan_sha256"],
        "dataset": workspace["dataset"],
        "dataset_suite_sha256": workspace["dataset_suite_sha256"],
        "experiment_id": workspace["experiment_id"],
        "native_identity": workspace["native_identity"],
        "initial_selection_record_sha256":
            workspace["initial_selection_record_sha256"],
        "validation_schedule_sha256": schedule["schedule_sha256"],
        "default_candidate_id": default_id,
        "shared_candidate_id": shared_id,
        "independent_candidate_ids": independent_ids,
        "config_sha256": config_hashes,
        "selection_sha256": stable_sha256(selection),
    }
    selected_manifest["manifest_sha256"] = _self_hash(
        selected_manifest, "manifest_sha256")
    _validate_receipt_snapshot(
        root, schedule, scheduled_trials, snapshot, purpose="finalization")
    _atomic_json(root / "selected_shared.json", {
        "candidate_id": shared_id,
        "config_sha256": {
            name: value for name, value in config_hashes.items()
            if "shared" in name},
    })
    _atomic_json(root / "selected_independent.json", {
        "candidate_ids": independent_ids,
        "config_sha256": {
            name: value for name, value in config_hashes.items()
            if "independent" in name},
    })
    _atomic_json(root / "selected_config_manifest.json", selected_manifest)
    _atomic_json(config_output / "selected_config_manifest.json", selected_manifest)
    return selected_manifest


def validate_tuning_selection_for_plan(
        plan: ExperimentPlan, selection_path: Path) -> Mapping[str, Any]:
    """Recompute and bind the frozen validation selection to formal configs.

    Tuning necessarily precedes the commit used for the formal main/timing
    runs, because finalization materializes the selected shared configs.  This
    validator therefore does not require the current commit to equal the
    pre-tuning workspace commit.  Instead it replays the complete immutable
    validation ledger, checks the selection hash and requires the current
    clean plan to point byte-for-byte at the selected shared configurations.
    """
    selection_path = selection_path.expanduser().resolve()
    if not selection_path.is_file():
        raise RuntimeError(f"tuning selection is missing: {selection_path}")
    root = selection_path.parent
    manifest = _read_json(selection_path)
    if (manifest.get("manifest_sha256") !=
            _self_hash(manifest, "manifest_sha256")):
        raise ValueError("tuning selection manifest hash mismatch")
    expected_header = {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "quality_policy": TUNING_QUALITY_POLICY_ID,
        "algorithm_revision": ALGORITHM_REVISION,
        "selection_status": "frozen_validation_selection",
    }
    header_drift = {
        key: (manifest.get(key), expected)
        for key, expected in expected_header.items()
        if manifest.get(key) != expected
    }
    if header_drift:
        raise ValueError(f"tuning selection is not frozen: {header_drift}")

    workspace = _read_json(root / "workspace_manifest.json")
    if (workspace.get("manifest_sha256") !=
            _self_hash(workspace, "manifest_sha256")):
        raise ValueError("tuning workspace manifest hash mismatch")
    linked_fields = {
        "workspace_manifest_sha256": workspace["manifest_sha256"],
        "repository": workspace["repository"],
        "plan_path": workspace["plan_path"],
        "plan_sha256": workspace["plan_sha256"],
        "dataset": workspace["dataset"],
        "dataset_suite_sha256": workspace["dataset_suite_sha256"],
        "experiment_id": workspace["experiment_id"],
        "native_identity": workspace["native_identity"],
        "initial_selection_record_sha256":
            workspace["initial_selection_record_sha256"],
    }
    linked_drift = {
        key: (manifest.get(key), expected)
        for key, expected in linked_fields.items()
        if manifest.get(key) != expected
    }
    if linked_drift:
        raise ValueError(f"tuning selection/workspace drift: {linked_drift}")

    dataset_name = str(workspace["dataset"])
    if dataset_name not in plan.datasets:
        raise ValueError("tuning selection dataset is absent from current plan")
    dataset = plan.datasets[dataset_name]
    current_inputs = {
        "plan_path": str(plan.path.resolve()),
        "plan_sha256": sha256_file(plan.path),
        "dataset_suite_path": str(dataset.suite_manifest.resolve()),
        "dataset_suite_sha256": sha256_file(dataset.suite_manifest),
        "canonical_inputs": _canonical_bindings(plan.load_suite(dataset)),
        "architecture_sha256": sha256_file(plan.architecture_path),
        "model_sha256": sha256_file(plan.model_path),
        "native_identity": _native_identity(plan),
    }
    current_input_drift = {
        key: (workspace.get(key), expected)
        for key, expected in current_inputs.items()
        if workspace.get(key) != expected
    }
    if current_input_drift:
        raise ValueError(
            "tuning selection no longer matches current frozen inputs: "
            f"{current_input_drift}")

    schedule, scheduled_trials = load_schedule(root, "validation")
    _load_published_phase_snapshot(
        root, schedule, scheduled_trials, phase="validation",
        purpose="finalization")
    replayed_schedule, trials = load_phase_trials(root, "validation")
    if replayed_schedule != schedule:
        raise ValueError("validation schedule changed during selection replay")
    _validate_schedule_chain(root, "validation", workspace)
    if manifest.get("validation_schedule_sha256") != schedule["schedule_sha256"]:
        raise ValueError("tuning validation schedule hash mismatch")
    candidates = _candidate_map(root)
    default_id = str(_read_json(root / "candidates.json")["default_candidate_id"])
    selection, ordered = _ranked_validation_selection(
        trials, default_id=default_id,
        expected_circuits=schedule["circuits"],
        expected_seeds=schedule["seeds"])
    if selection["shared_selected"] is None:
        raise RuntimeError("frozen tuning ledger has no valid shared selection")
    shared_id = str(selection["shared_selected"])
    independent_ids = {
        method: str(selection["independent_selected"][method] or default_id)
        for method in TUNING_METHODS
    }
    selection_drift = {
        key: (manifest.get(key), expected)
        for key, expected in {
            "default_candidate_id": default_id,
            "shared_candidate_id": shared_id,
            "independent_candidate_ids": independent_ids,
            "selection_sha256": stable_sha256(selection),
        }.items()
        if manifest.get(key) != expected
    }
    if selection_drift:
        raise ValueError(f"tuning selection result drift: {selection_drift}")
    expected_leaderboard = {
        **selection,
        "validation_schedule_sha256": schedule["schedule_sha256"],
    }
    if _read_json(root / "leaderboard.json") != expected_leaderboard:
        raise ValueError("tuning validation leaderboard drift")

    expected_names = {
        "M3": "ours_nl_shared.json",
        "M4": "ours_lk_shared.json",
    }
    config_hashes = manifest.get("config_sha256")
    if not isinstance(config_hashes, Mapping):
        raise ValueError("tuning selection lacks config hashes")
    config_root = plan.methods["M3"].config_path.parent
    if plan.methods["M4"].config_path.parent != config_root:
        raise ValueError("formal M3/M4 configs do not share one frozen directory")
    tracked_manifest = config_root / "selected_config_manifest.json"
    if (not tracked_manifest.is_file() or
            _read_json(tracked_manifest) != manifest):
        raise ValueError("tracked tuning selection manifest differs from artifact")
    for method, name in expected_names.items():
        current = plan.methods[method].config_path.resolve()
        selected = (root / "selected" / name).resolve()
        expected_hash = config_hashes.get(name)
        if (not isinstance(expected_hash, str) or len(expected_hash) != 64 or
                not current.is_file() or not selected.is_file() or
                sha256_file(current) != expected_hash or
                sha256_file(selected) != expected_hash):
            raise ValueError(
                f"{method} formal config is not the frozen shared selection")
        tuning = plan.methods[method].payload.get("tuning", {})
        if (not isinstance(tuning, Mapping) or
                tuning.get("candidate_id") != shared_id or
                tuning.get("track") != "shared"):
            raise ValueError(f"{method} tuning metadata does not select shared config")
    # Re-run the strict M3/M4 pair check through the loaded plan and require a
    # clean, versioned commit for every formal consumer of this gate.
    repository = repository_snapshot(plan.repo_root)
    if repository.get("dirty") or repository.get("commit") == "unknown":
        raise RuntimeError("formal experiments require a clean selected-config commit")
    return {
        **manifest,
        "current_repository": repository,
        "ranked_validation_candidates": len(ordered),
    }


def tuning_status(root: Path) -> Mapping[str, Any]:
    phases = {}
    for phase in PHASE_CONTRACTS:
        path = _schedule_path(root, phase)
        if not path.is_file():
            phases[phase] = {"scheduled": False, "complete": False}
            continue
        schedule, rows = load_schedule(root, phase)
        present = sum(_receipt_path(root, row).is_file() for row in rows)
        claim_rows = [row for row in rows if _claim_path(root, row).exists()]
        claim_errors = []
        for row in claim_rows:
            try:
                _validate_claim(_claim_path(root, row), schedule, row)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                claim_errors.append(
                    f"{row.trial_id}: {type(error).__name__}: {error}")
        validation_error = None
        validated = False
        if present == len(rows) and not claim_rows:
            try:
                load_phase_trials(root, phase)
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                validation_error = f"{type(error).__name__}: {error}"
            else:
                validated = True
        seal_path = _phase_seal_path(root, phase)
        phases[phase] = {
            "scheduled": True,
            "schedule_sha256": schedule["schedule_sha256"],
            "expected": len(rows),
            "receipts": present,
            "claims": len(claim_rows),
            "claimed_trial_ids": [row.trial_id for row in claim_rows],
            "claim_validation_errors": claim_errors,
            "sealed": seal_path.is_file(),
            "validated": validated,
            "validation_error": validation_error,
            "complete": validated and not claim_rows,
        }
    return {
        "experiment_schema": 2,
        "protocol_id": TUNING_PROTOCOL_ID,
        "quality_policy": TUNING_QUALITY_POLICY_ID,
        "root": str(root.resolve()),
        "phases": phases,
        "finalized": (root / "selected_config_manifest.json").is_file(),
    }


__all__ = [
    "ALGORITHM_REVISION", "build_candidate_config_pair", "finalize_tuning",
    "load_phase_trials", "load_schedule", "materialize_candidate_configs",
    "prepare_tuning_workspace", "promote_tuning_phase",
    "recover_tuning_claim", "run_tuning_phase",
    "seal_trial_result", "tuning_status", "validate_trial_receipt",
    "validate_tuning_selection_for_plan",
]
