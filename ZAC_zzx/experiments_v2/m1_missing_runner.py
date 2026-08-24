"""Complete the paper-native M1 main cohort without running M2/M3/M4.

The registered cohort is split by the frozen raw-trace audit:

* every ``ready`` source is replayed from its compiler-authored ZAIR trace;
* every non-ready target is freshly compiled by the frozen paper ZAC class.

The two sets are disjoint and are written to one Schema-2 ``runs/main``
registry.  In particular, a ready native verifier failure remains a replayed
verifier failure and is never replaced by a fresh compilation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import os
import shutil
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .baseline_raw_replay import (
    M1LineageFreeze,
    ReplayTarget,
    SourceAttempt,
    _hash_gzip_payload,
    _load_m1_lineage_manifest,
    _source_hashes,
    _source_policy_error,
    _target_hashes,
    audit_baseline_raw_replay,
    default_m1_lineage_manifest,
    replay_one_baseline_raw,
)
from .cli import UnifiedEvaluationGate, _attempt_spec, command_verify_run
from .contracts import (
    CanonicalCircuitManifest,
    RunManifest,
    load_run_manifest,
    repository_snapshot,
    sha256_file,
    stable_sha256,
)
from .plan import DatasetSpec, ExperimentPlan, load_experiment_plan
from .protocol import (
    ghost_policy_for_method,
    physicalization_policy_for_method,
    trace_protocol_for_method,
)
from .runner import run_attempt


PROTOCOL_ID = "paper-native-m1-missing-cohort-v1"
REGISTERED_TOTAL = 172
REGISTERED_READY = 30
REGISTERED_FRESH = 142
REGISTERED_READY_VERIFIER_FAIL = 4
REGISTERED_MISSING_SHA256 = (
    "5bfcec48a0a5051add4e0279f6945a1a8794e826c34694eccd99f1cc225bbe1b"
)
SINGLE_THREAD_ENVIRONMENT = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}
TERMINAL_STATUSES = frozenset((
    "success", "timeout", "oom", "compiler_error", "verifier_fail",
    "scorer_error",
))


@dataclass(frozen=True)
class M1CohortEntry:
    dataset: DatasetSpec
    canonical: CanonicalCircuitManifest
    circuit: str
    audit_state: str
    selected_manifest: Path | None
    source_status: str
    audit_record: Mapping[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return self.dataset.name, self.circuit

    @property
    def action(self) -> str:
        return "raw_replay" if self.audit_state == "ready" else "fresh_compile"


def _canonical_registry(plan: ExperimentPlan) -> Mapping[
        tuple[str, str], tuple[DatasetSpec, CanonicalCircuitManifest]]:
    result: dict[tuple[str, str], tuple[DatasetSpec, CanonicalCircuitManifest]] = {}
    for dataset in plan.select_datasets(None, kind="main"):
        for canonical in plan.load_suite(dataset):
            circuit = Path(canonical.canonical_path).stem
            key = dataset.name, circuit
            if key in result:
                raise ValueError(f"duplicate canonical M1 identity: {key}")
            result[key] = dataset, canonical
    return result


def _missing_sha256(entries: Sequence[M1CohortEntry]) -> str:
    payload = [
        {"dataset": entry.dataset.name, "circuit": entry.circuit}
        for entry in entries if entry.audit_state != "ready"
    ]
    return stable_sha256(payload)


def build_m1_cohort(
        plan: ExperimentPlan, *, lineage_manifest: Path | None = None,
        enforce_registered_cohort: bool = True
        ) -> tuple[M1LineageFreeze, Mapping[str, Any], tuple[M1CohortEntry, ...]]:
    """Build the disjoint ready/fresh queue from the frozen M1 audit."""
    lineage_path = (
        default_m1_lineage_manifest(plan)
        if lineage_manifest is None else Path(lineage_manifest).resolve())
    lineage = _load_m1_lineage_manifest(lineage_path)
    audit = audit_baseline_raw_replay(
        plan, [lineage.source_root], methods=("M1",),
        m1_lineage_manifest=lineage.manifest_path)
    registry = _canonical_registry(plan)
    entries: list[M1CohortEntry] = []
    seen: set[tuple[str, str]] = set()
    for record in audit["entries"]:
        key = str(record["dataset"]), str(record["circuit"])
        if key in seen or key not in registry:
            raise ValueError(f"M1 audit/canonical registry drift: {key}")
        seen.add(key)
        dataset, canonical = registry[key]
        selected = record.get("selected_manifest")
        evidence = record.get("eligible_raw_evidence", [])
        source_status = ""
        if record["state"] == "ready":
            if not isinstance(selected, str) or not selected:
                raise ValueError(f"ready M1 audit lacks selected source: {key}")
            if not isinstance(evidence, list) or len(evidence) != 1:
                raise ValueError(
                    f"ready M1 source is not one frozen raw attempt: {key}")
            source_status = str(evidence[0].get("source_status", ""))
            if source_status not in {"success", "verifier_fail"}:
                raise ValueError(
                    f"ready M1 source has invalid terminal status: {key}: "
                    f"{source_status!r}")
        elif selected is not None:
            raise ValueError(f"non-ready M1 audit selected a source: {key}")
        entries.append(M1CohortEntry(
            dataset=dataset,
            canonical=canonical,
            circuit=key[1],
            audit_state=str(record["state"]),
            selected_manifest=(None if selected is None else Path(selected).resolve()),
            source_status=source_status,
            audit_record=dict(record),
        ))
    if seen != set(registry):
        raise ValueError(
            "M1 audit does not cover the immutable main suites: "
            f"missing={sorted(set(registry) - seen)[:5]}")

    rows = tuple(entries)
    ready = tuple(row for row in rows if row.audit_state == "ready")
    fresh = tuple(row for row in rows if row.audit_state != "ready")
    missing_sha = _missing_sha256(rows)
    ready_failures = sum(row.source_status == "verifier_fail" for row in ready)
    if enforce_registered_cohort:
        observed = {
            "total": len(rows),
            "ready": len(ready),
            "fresh": len(fresh),
            "ready_verifier_fail": ready_failures,
            "missing_sha256": missing_sha,
        }
        expected = {
            "total": REGISTERED_TOTAL,
            "ready": REGISTERED_READY,
            "fresh": REGISTERED_FRESH,
            "ready_verifier_fail": REGISTERED_READY_VERIFIER_FAIL,
            "missing_sha256": REGISTERED_MISSING_SHA256,
        }
        if observed != expected:
            raise ValueError(
                "registered M1 missing cohort drifted; refuse an implicit "
                f"experiment revision: observed={observed}, expected={expected}")
    return lineage, audit, rows


def _read_json_artifact(directory: Path, name: str) -> Mapping[str, Any]:
    raw = directory / name
    archived = directory / f"{name}.gz"
    matches = [path for path in (raw, archived) if path.is_file()]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one {name} or {name}.gz under {directory}")
    path = matches[0]
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON artifact is not an object: {path}")
    return payload


def _ready_source(plan: ExperimentPlan, entry: M1CohortEntry,
                  lineage: M1LineageFreeze) -> tuple[ReplayTarget, SourceAttempt]:
    if entry.audit_state != "ready" or entry.selected_manifest is None:
        raise ValueError(f"M1 entry is not replay-ready: {entry.key}")
    manifest_path = entry.selected_manifest.resolve()
    if not manifest_path.is_relative_to(lineage.source_root):
        raise ValueError(f"M1 selected source escaped frozen lineage: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    hashes = _target_hashes(plan, "M1", entry.canonical)
    if _source_hashes(payload) != hashes:
        raise ValueError(f"M1 replay source hashes drifted: {manifest_path}")
    error, raw, stats, stats_path = _source_policy_error(
        "M1", payload, manifest_path.parent)
    if error is not None:
        raise ValueError(f"M1 replay source policy drifted: {error}")
    assert raw is not None and stats is not None and stats_path is not None
    run_id = str(payload.get("run_id", ""))
    if not run_id:
        raise ValueError(f"M1 replay source lacks run_id: {manifest_path}")
    source = SourceAttempt(
        manifest_path=manifest_path,
        manifest=payload,
        raw_trace_path=raw,
        compiler_stats=stats,
        compiler_stats_path=stats_path,
        equivalent_manifest_paths=(manifest_path,),
        equivalent_run_ids=(run_id,),
        raw_payload_sha256=_hash_gzip_payload(raw),
        source_root=lineage.source_root,
        lineage_manifest_path=lineage.manifest_path,
        lineage_manifest_sha256=lineage.manifest_sha256,
        lineage_sha256=lineage.lineage_sha256,
    )
    target = ReplayTarget(
        dataset=entry.dataset,
        canonical=entry.canonical,
        method="M1",
        circuit=entry.circuit,
        hashes=hashes,
    )
    return target, source


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _disk_free(root: Path) -> int:
    probe = root.resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _expected_hashes(plan: ExperimentPlan, entry: M1CohortEntry
                     ) -> Mapping[str, str]:
    return _target_hashes(plan, "M1", entry.canonical)


def _validate_m1_manifest(
        plan: ExperimentPlan, entry: M1CohortEntry, manifest_path: Path,
        repository: Mapping[str, Any], lineage: M1LineageFreeze,
        *, reverify_success: bool) -> RunManifest:
    manifest = load_run_manifest(manifest_path)
    expected_identity = {
        "dataset": entry.dataset.name,
        "circuit": entry.circuit,
        "method": "M1",
        "seed": 0,
        "repetition": 0,
        "run_kind": "main",
        "experiment_id": plan.experiment_id(entry.dataset),
    }
    drift = {
        key: (getattr(manifest, key), value)
        for key, value in expected_identity.items()
        if getattr(manifest, key) != value
    }
    hashes = _expected_hashes(plan, entry)
    drift.update({
        key: (getattr(manifest, key), value)
        for key, value in hashes.items()
        if getattr(manifest, key) != value
    })
    policies = {
        "trace_protocol": trace_protocol_for_method("M1"),
        "ghost_policy": ghost_policy_for_method("M1"),
        "physicalization_policy": physicalization_policy_for_method("M1"),
    }
    drift.update({
        key: (getattr(manifest, key), value)
        for key, value in policies.items()
        if getattr(manifest, key) != value
    })
    for key in ("ghost_repairs", "ghost_splits"):
        observed = getattr(manifest, key)
        # A killed/failed compiler may emit no counters at all.  Absence is
        # acceptable for that terminal result, while any positive counter is
        # still forbidden.  Raw-bearing results must prove an explicit zero.
        allowed = (
            {0} if manifest.status == "success" or entry.audit_state == "ready"
            else {None, 0})
        if observed not in allowed:
            drift[key] = (observed, sorted(
                allowed, key=lambda value: -1 if value is None else value))
    if (repository.get("dirty") is not False
            or repository.get("commit") in (None, "", "unknown")
            or manifest.git_dirty is not False
            or manifest.git_commit != repository.get("commit")):
        drift["repository"] = (
            (manifest.git_commit, manifest.git_dirty),
            (repository.get("commit"), False),
        )
    if manifest.status not in TERMINAL_STATUSES:
        drift["status"] = (manifest.status, sorted(TERMINAL_STATUSES))
    if drift:
        raise ValueError(f"M1 cohort manifest drift: {manifest_path}: {drift}")

    directory = manifest_path.parent
    if entry.audit_state == "ready":
        receipt_path = directory / "replay_receipt.json"
        if not receipt_path.is_file():
            raise ValueError(f"ready M1 resume lacks replay receipt: {manifest_path}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        seal = receipt.get("seal_sha256")
        unsealed = {key: value for key, value in receipt.items()
                    if key != "seal_sha256"}
        source = receipt.get("source", {})
        if (seal != stable_sha256(unsealed)
                or receipt.get("protocol") !=
                "paper-native-baseline-raw-replay-v1"
                or receipt.get("compilation_executed") is not False
                or source.get("lineage_sha256") != lineage.lineage_sha256
                or Path(str(source.get("manifest", ""))).resolve() !=
                entry.selected_manifest):
            raise ValueError(f"ready M1 replay receipt drift: {receipt_path}")
        if (entry.source_status == "verifier_fail"
                and manifest.status != "verifier_fail"):
            raise ValueError(
                "native M1 verifier failure may not be overwritten by a "
                f"fresh/pass result: {entry.key}: {manifest.status}")
    else:
        if (directory / "replay_receipt.json").exists():
            raise ValueError(
                f"missing-source M1 target was replayed instead of compiled: "
                f"{manifest_path}")
        if manifest.status in {"success", "verifier_fail", "scorer_error"}:
            stats = _read_json_artifact(directory, "compiler_stats.json")
            identity = {
                "compiler_module": "zac.zac",
                "routing_strategy": "maximalis_sort",
                "physicalization": "paper_native_unmodified",
                "ghost_splits": 0,
            }
            stats_drift = {
                key: (stats.get(key), value)
                for key, value in identity.items()
                if stats.get(key) != value
            }
            if stats.get("ghost_repairs", 0) != 0:
                stats_drift["ghost_repairs"] = (
                    stats.get("ghost_repairs"), 0)
            if stats_drift:
                raise ValueError(
                    f"fresh M1 compiler identity drift: {manifest_path}: "
                    f"{stats_drift}")
    if reverify_success and manifest.status == "success":
        command_verify_run(plan, manifest_path)
    return manifest


def _scan_current_registry(
        plan: ExperimentPlan, entries: Sequence[M1CohortEntry], root: Path,
        repository: Mapping[str, Any], lineage: M1LineageFreeze,
        *, reverify_success: bool) -> tuple[
            dict[tuple[str, str], tuple[RunManifest, Path]], list[str]]:
    expected = {entry.key: entry for entry in entries}
    current: dict[tuple[str, str], tuple[RunManifest, Path]] = {}
    ignored: list[str] = []
    if not root.exists():
        return current, ignored
    for path in sorted(root.rglob("manifest.json"), key=str):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        manifest = load_run_manifest(path)
        if manifest.method != "M1":
            continue
        key = manifest.dataset, manifest.circuit
        is_current = bool(
            repository.get("dirty") is False
            and repository.get("commit") not in (None, "", "unknown")
            and manifest.git_dirty is False
            and manifest.git_commit == repository.get("commit"))
        if not is_current:
            ignored.append(str(path.resolve()))
            continue
        if key not in expected:
            raise ValueError(f"unexpected current M1 main identity: {path}")
        if key in current:
            raise ValueError(
                "multiple current M1 manifests match one cohort identity: "
                f"{current[key][1]} and {path}")
        validated = _validate_m1_manifest(
            plan, expected[key], path.resolve(), repository, lineage,
            reverify_success=reverify_success)
        current[key] = validated, path.resolve()
    return current, ignored


def _summary(
        *, plan: ExperimentPlan, lineage: M1LineageFreeze,
        audit: Mapping[str, Any], entries: Sequence[M1CohortEntry],
        registry: Mapping[tuple[str, str], tuple[RunManifest, Path]],
        repository: Mapping[str, Any], workers: int, dry_run: bool,
        ignored_stale: Sequence[str], paused_reason: str | None,
        fatal_errors: Sequence[str], output_root: Path) -> dict[str, Any]:
    rows = []
    for entry in sorted(entries, key=lambda row: row.key):
        completed = registry.get(entry.key)
        rows.append({
            "dataset": entry.dataset.name,
            "circuit": entry.circuit,
            "action": entry.action,
            "audit_state": entry.audit_state,
            "source_status": entry.source_status or None,
            "status": None if completed is None else completed[0].status,
            "manifest": None if completed is None else str(completed[1]),
        })
    statuses = Counter(
        row["status"] for row in rows if row["status"] is not None)
    actions = Counter(row["action"] for row in rows)
    completed_actions = Counter(
        row["action"] for row in rows if row["status"] is not None)
    return {
        "experiment_schema": 2,
        "protocol": PROTOCOL_ID,
        "dry_run": dry_run,
        "workers": workers,
        "single_thread_environment": dict(SINGLE_THREAD_ENVIRONMENT),
        "repository": dict(repository),
        "plan": str(plan.path),
        "output_root": str(output_root),
        "lineage": lineage.to_dict(),
        "audit_state_counts": dict(audit.get("state_counts", {})),
        "missing_queue_sha256": _missing_sha256(entries),
        "planned": len(entries),
        "planned_by_action": dict(sorted(actions.items())),
        "completed": len(registry),
        "completed_by_action": dict(sorted(completed_actions.items())),
        "status_counts": dict(sorted(statuses.items())),
        "complete": len(registry) == len(entries) and not fatal_errors,
        "paused": paused_reason is not None,
        "paused_reason": paused_reason,
        "fatal_errors": list(fatal_errors),
        "ignored_stale_manifests": list(ignored_stale),
        "entries": rows,
    }


def run_m1_cohort(
        plan: ExperimentPlan, *, lineage_manifest: Path | None = None,
        output_root: Path | None = None, summary_path: Path | None = None,
        workers: int = 3, resume: bool = False, dry_run: bool = False,
        enforce_registered_cohort: bool = True) -> Mapping[str, Any]:
    """Replay ready M1 raw evidence, then compile only the missing set."""
    if isinstance(workers, bool) or not isinstance(workers, int) \
            or workers < 1 or workers > 3:
        raise ValueError("M1 missing runner workers must be an integer in [1, 3]")
    lineage, audit, entries = build_m1_cohort(
        plan, lineage_manifest=lineage_manifest,
        enforce_registered_cohort=enforce_registered_cohort)
    destination = (
        (plan.output_root / "runs" / "main")
        if output_root is None else Path(output_root)).resolve()
    if destination == lineage.source_root or destination.is_relative_to(
            lineage.source_root):
        raise ValueError(
            "M1 cohort destination may not mutate its frozen source lineage")
    repository = repository_snapshot(plan.repo_root)
    if not dry_run and (
            repository.get("commit") in (None, "", "unknown")
            or repository.get("dirty") is not False):
        raise RuntimeError(
            "M1 formal cohort requires one clean Git commit before replay/compile")

    current, ignored_stale = _scan_current_registry(
        plan, entries, destination, repository, lineage,
        reverify_success=not dry_run)
    if current and not resume:
        raise FileExistsError(
            "current M1 cohort manifests already exist; use --resume: "
            f"{len(current)} identities")
    if dry_run:
        return _summary(
            plan=plan, lineage=lineage, audit=audit, entries=entries,
            registry=current, repository=repository, workers=workers,
            dry_run=True, ignored_stale=ignored_stale, paused_reason=None,
            fatal_errors=(), output_root=destination)

    destination.mkdir(parents=True, exist_ok=True)
    paused_reason: str | None = None
    fatal_errors: list[str] = []

    # Stage 1 is a strict barrier: preserve all frozen raw sources (including
    # their native verifier failures) before launching any fresh compiler.
    for entry in (row for row in entries if row.audit_state == "ready"):
        if entry.key in current:
            continue
        free = _disk_free(destination)
        if free < plan.minimum_free_bytes:
            paused_reason = (
                f"disk below pause threshold before M1 raw replay: "
                f"free={free}, required={plan.minimum_free_bytes}")
            break
        target, source = _ready_source(plan, entry, lineage)
        result = replay_one_baseline_raw(
            plan, target, source, destination / entry.dataset.name)
        manifest_path = Path(str(result["manifest"])).resolve()
        manifest = _validate_m1_manifest(
            plan, entry, manifest_path, repository, lineage,
            reverify_success=True)
        current[entry.key] = manifest, manifest_path

    # Stage 2 launches only audit-missing identities.  A bounded producer keeps
    # at most three isolated compiler process groups active and stops adding
    # work as soon as the registered 5-GiB free-space gate is crossed.
    fresh = [
        row for row in entries
        if row.audit_state != "ready" and row.key not in current
    ]
    fresh.sort(key=lambda row: (
        -(row.canonical.gates_1q + row.canonical.gates_2q),
        -row.canonical.gates_2q,
        row.dataset.name,
        row.circuit,
    ))

    def execute(entry: M1CohortEntry) -> tuple[M1CohortEntry, RunManifest, Path]:
        spec = _attempt_spec(
            plan, entry.dataset, entry.canonical, "M1", 0, 0, "main")
        spec = replace(
            spec,
            output_root=destination / entry.dataset.name,
            environment={**spec.environment, **SINGLE_THREAD_ENVIRONMENT},
        )
        gate = UnifiedEvaluationGate(plan, entry.canonical, "M1")
        manifest = run_attempt(
            spec, verifier=gate.verifier, scorer=gate.scorer)
        manifest_path = (Path(manifest.artifact_dir) / "manifest.json").resolve()
        manifest = _validate_m1_manifest(
            plan, entry, manifest_path, repository, lineage,
            reverify_success=False)
        return entry, manifest, manifest_path

    if paused_reason is None and fresh:
        iterator = iter(fresh)
        exhausted = False
        inflight: dict[concurrent.futures.Future[
            tuple[M1CohortEntry, RunManifest, Path]], M1CohortEntry] = {}
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="paper-native-m1") as executor:
            while (not exhausted or inflight) and not fatal_errors:
                while not exhausted and len(inflight) < workers \
                        and paused_reason is None:
                    free = _disk_free(destination)
                    if free < plan.minimum_free_bytes:
                        paused_reason = (
                            "disk below pause threshold before fresh M1 "
                            f"compile: free={free}, "
                            f"required={plan.minimum_free_bytes}")
                        break
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    future = executor.submit(execute, entry)
                    inflight[future] = entry
                if not inflight:
                    break
                done, _ = concurrent.futures.wait(
                    inflight, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    entry = inflight.pop(future)
                    try:
                        completed_entry, manifest, manifest_path = future.result()
                    except RuntimeError as error:
                        if str(error).startswith("runner paused:"):
                            paused_reason = str(error)
                        else:
                            fatal_errors.append(
                                f"{entry.dataset.name}/{entry.circuit}: "
                                f"{type(error).__name__}: {error}")
                    except BaseException as error:
                        fatal_errors.append(
                            f"{entry.dataset.name}/{entry.circuit}: "
                            f"{type(error).__name__}: {error}")
                    else:
                        current[completed_entry.key] = manifest, manifest_path

    # Re-scan from disk so the summary never trusts only in-memory returns.
    current, stale_after = _scan_current_registry(
        plan, entries, destination, repository, lineage,
        reverify_success=False)
    ignored_stale = sorted(set(ignored_stale) | set(stale_after))
    report = _summary(
        plan=plan, lineage=lineage, audit=audit, entries=entries,
        registry=current, repository=repository, workers=workers,
        dry_run=False, ignored_stale=ignored_stale,
        paused_reason=paused_reason, fatal_errors=fatal_errors,
        output_root=destination)
    report_path = (
        destination / "m1_paper_native_cohort_summary.json"
        if summary_path is None else Path(summary_path).resolve())
    _atomic_json(report_path, report)
    report = {**report, "summary_path": str(report_path),
              "summary_sha256": sha256_file(report_path)}
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m experiments_v2.m1_missing_runner",
        description=(
            "Replay the frozen 30 paper-native M1 raw traces and compile only "
            "the registered 142 missing M1 circuits."),
    )
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--lineage-manifest", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = load_experiment_plan(args.plan)
        report = run_m1_cohort(
            plan,
            lineage_manifest=args.lineage_manifest,
            output_root=args.output_root,
            summary_path=args.summary,
            workers=args.workers,
            resume=args.resume,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report.get("fatal_errors") else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "M1CohortEntry", "PROTOCOL_ID", "REGISTERED_FRESH",
    "REGISTERED_MISSING_SHA256", "REGISTERED_READY", "REGISTERED_TOTAL",
    "build_m1_cohort", "build_parser", "main", "run_m1_cohort",
]
