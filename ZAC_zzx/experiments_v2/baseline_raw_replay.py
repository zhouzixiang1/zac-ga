"""Fail-closed replay of compiler-authored M1/M2 raw traces.

This module deliberately does not resume or copy a prior score.  It discovers
an exactly matching compiler attempt, copies only its compressed raw trace into
a new atomic attempt directory, and runs the current normalizer, verifier, and
ZAC-physics scorer over that raw evidence.  In particular, the legacy QMAP
``trace.na.gz`` and every prior ``canonical_trace``/``fidelity`` file are
ineligible inputs.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import (CanonicalCircuitManifest, RunManifest, RunStatus,
                        sha256_file, stable_sha256)
from .plan import DatasetSpec, ExperimentPlan
from .protocol import (ghost_policy_for_method,
                       physicalization_policy_for_method,
                       trace_protocol_for_method)
from .runner import _apply_metrics


REPLAY_PROTOCOL = "paper-native-baseline-raw-replay-v1"
M1_LINEAGE_PROTOCOL = "paper-native-m1-source-lineage-v1"
DEFAULT_M1_LINEAGE_DIRECTORY = "tuning-quality-v1-51583a5"
DEFAULT_M1_LINEAGE_MANIFEST = "workspace.json"
BASELINE_METHODS = ("M1", "M2")
RAW_TRACE_FILES = {
    "M1": "trace.zair.json.gz",
    "M2": "trace.na.raw.gz",
}
GATE_TRACE_NAMES = {
    "M1": None,
    "M2": "trace.na.raw",
}
IGNORED_DERIVED_ARTIFACTS = (
    "canonical_trace.jsonl.gz",
    "fidelity.json",
    "trace.na.gz",
)
SOURCE_TERMINAL_STATUSES = frozenset((
    RunStatus.SUCCESS.value,
    RunStatus.VERIFIER_FAIL.value,
))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON evidence is not an object: {path}")
    return payload


def _read_json_artifact(directory: Path, name: str) -> tuple[Mapping[str, Any], Path]:
    raw = directory / name
    archived = directory / f"{name}.gz"
    matches = [path for path in (raw, archived) if path.is_file()]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one {name} or {name}.gz under {directory}; "
            f"found {[path.name for path in matches]}"
        )
    path = matches[0]
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON evidence is not an object: {path}")
    return payload, path


def _canonical_identity(canonical: CanonicalCircuitManifest) -> tuple[str, Path]:
    path = Path(canonical.canonical_path).resolve()
    return path.stem, path


def _target_hashes(plan: ExperimentPlan, method: str,
                   canonical: CanonicalCircuitManifest) -> dict[str, str]:
    _, canonical_path = _canonical_identity(canonical)
    return {
        "input_sha256": sha256_file(canonical_path),
        "config_sha256": sha256_file(plan.resolved_config(method, 0)),
        "architecture_sha256": sha256_file(plan.architecture_path),
        "model_sha256": sha256_file(plan.model_path),
    }


def _source_key(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(payload.get("dataset", "")),
        str(payload.get("circuit", "")),
        str(payload.get("method", "")),
    )


def _source_hashes(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: str(payload.get(field, ""))
        for field in (
            "input_sha256", "config_sha256", "architecture_sha256",
            "model_sha256",
        )
    }


def _contains_fallback_claim(payload: Mapping[str, Any]) -> bool:
    text: list[str] = []
    warnings = payload.get("warnings", [])
    if isinstance(warnings, list):
        text.extend(str(value) for value in warnings)
    if payload.get("error") is not None:
        text.append(str(payload["error"]))
    command = payload.get("command", [])
    if isinstance(command, list):
        text.extend(str(value) for value in command)
    joined = " ".join(text).lower()
    return "agnostic" in joined or "routing-agnostic" in joined


def _source_policy_error(method: str, manifest: Mapping[str, Any],
                         directory: Path) -> tuple[str | None, Path | None,
                                                   Mapping[str, Any] | None,
                                                   Path | None]:
    """Return a policy error and the exact raw/compiler evidence paths."""
    if manifest.get("experiment_schema") != 2:
        return "source manifest is not Schema 2", None, None, None
    if manifest.get("status") not in SOURCE_TERMINAL_STATUSES:
        return (
            f"source compiler did not produce reusable terminal evidence: "
            f"status={manifest.get('status')!r}", None, None, None)
    if manifest.get("seed", 0) != 0 or manifest.get("repetition", 0) != 0:
        return "baseline source must be seed=0 repetition=0", None, None, None

    raw = directory / RAW_TRACE_FILES[method]
    uncompressed_twin = raw.with_suffix("")
    if not raw.is_file():
        return f"required raw trace is missing: {raw.name}", None, None, None
    if uncompressed_twin.is_file():
        return (
            f"ambiguous raw trace: both {raw.name} and "
            f"{uncompressed_twin.name} exist", None, None, None)
    try:
        stats, stats_path = _read_json_artifact(directory, "compiler_stats.json")
    except ValueError as error:
        return str(error), None, None, None

    if method == "M1":
        identity = {
            "compiler_module": (stats.get("compiler_module"), "zac.zac"),
            "routing_strategy": (stats.get("routing_strategy"), "maximalis_sort"),
            "physicalization": (
                stats.get("physicalization"), "paper_native_unmodified"),
            "ghost_repairs": (stats.get("ghost_repairs", 0), 0),
            "ghost_splits": (stats.get("ghost_splits", 0), 0),
        }
        drift = {
            key: values for key, values in identity.items()
            if values[0] != values[1]
        }
        if drift:
            return (
                "M1 source is not an unmodified paper-native trace: "
                f"{drift}", None, None, None)
    else:
        identity = {
            "compiler_class": (
                stats.get("compiler_class"), "RoutingAwareCompiler"),
            "fallback": (stats.get("fallback"), False),
            "mqt_qmap_version": (stats.get("mqt_qmap_version"), "3.2.0"),
            "mqt_core_version": (stats.get("mqt_core_version"), "3.1.0"),
        }
        drift = {
            key: values for key, values in identity.items()
            if values[0] != values[1]
        }
        strategy = str(stats.get("routing_strategy", "")).lower()
        agnostic = (
            _contains_fallback_claim(manifest)
            or stats.get("routing_agnostic_fallback") not in (None, False)
            or "agnostic" in strategy
        )
        if drift or agnostic:
            return (
                "M2 source is not frozen routing-aware A* without fallback: "
                f"identity_drift={drift}, agnostic_evidence={agnostic}",
                None, None, None)
        # Legacy attempts may report repair counters for trace.na.gz.  They do
        # not disqualify the separately preserved trace.na.raw.gz, which is the
        # only file copied or normalized by this importer.

    return None, raw, stats, stats_path


@dataclass(frozen=True)
class SourceAttempt:
    manifest_path: Path
    manifest: Mapping[str, Any]
    raw_trace_path: Path
    compiler_stats: Mapping[str, Any]
    compiler_stats_path: Path
    equivalent_manifest_paths: tuple[Path, ...] = ()
    equivalent_run_ids: tuple[str, ...] = ()
    raw_payload_sha256: str = ""
    source_root: Path | None = None
    lineage_manifest_path: Path | None = None
    lineage_manifest_sha256: str = ""
    lineage_sha256: str = ""


@dataclass(frozen=True)
class M1LineageFreeze:
    """A sealed, single-root source lineage for paper-native M1 replay."""

    manifest_path: Path
    source_root: Path
    manifest_sha256: str
    lineage_sha256: str

    def to_dict(self) -> Mapping[str, str]:
        return {
            "protocol": M1_LINEAGE_PROTOCOL,
            "manifest": str(self.manifest_path),
            "manifest_sha256": self.manifest_sha256,
            "source_root": str(self.source_root),
            "lineage_sha256": self.lineage_sha256,
        }


@dataclass(frozen=True)
class ReplayTarget:
    dataset: DatasetSpec
    canonical: CanonicalCircuitManifest
    method: str
    circuit: str
    hashes: Mapping[str, str]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.dataset.name, self.circuit, self.method)


def default_m1_lineage_manifest(plan: ExperimentPlan) -> Path:
    """Return the registered formal M1 source-lineage manifest path."""
    return (
        plan.output_root / DEFAULT_M1_LINEAGE_DIRECTORY /
        DEFAULT_M1_LINEAGE_MANIFEST
    ).resolve()


def _load_m1_lineage_manifest(path: Path) -> M1LineageFreeze:
    manifest_path = Path(path).expanduser().resolve()
    payload = _read_json(manifest_path)
    if payload.get("experiment_schema") != 2:
        raise ValueError("M1 lineage manifest is not Schema 2")
    source_value = payload.get("root", payload.get("source_root"))
    if not isinstance(source_value, str) or not source_value:
        raise ValueError(
            "M1 lineage manifest must specify exactly one root/source_root")
    source_root = Path(source_value).expanduser()
    if not source_root.is_absolute():
        source_root = manifest_path.parent / source_root
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(
            f"M1 lineage source root does not exist: {source_root}")
    lineage_sha256 = payload.get("record_sha256")
    if not isinstance(lineage_sha256, str) or len(lineage_sha256) != 64:
        raise ValueError("M1 lineage manifest lacks a SHA-256 record seal")
    computed = stable_sha256({
        key: value for key, value in payload.items()
        if key != "record_sha256"
    })
    if computed != lineage_sha256:
        raise ValueError(
            "M1 lineage manifest record seal differs: "
            f"{computed} != {lineage_sha256}")
    return M1LineageFreeze(
        manifest_path=manifest_path,
        source_root=source_root,
        manifest_sha256=sha256_file(manifest_path),
        lineage_sha256=lineage_sha256,
    )


def _owning_source_root(path: Path, roots: Sequence[Path]) -> Path:
    resolved = path.resolve()
    candidates = [
        Path(root).expanduser().resolve() for root in roots
        if resolved == Path(root).expanduser().resolve()
        or resolved.is_relative_to(Path(root).expanduser().resolve())
    ]
    if not candidates:
        raise ValueError(f"source manifest is outside every scan root: {path}")
    return max(candidates, key=lambda value: len(value.parts))


def _targets(plan: ExperimentPlan, dataset_names: Sequence[str] | None,
             methods: Sequence[str]) -> list[ReplayTarget]:
    invalid = sorted(set(methods) - set(BASELINE_METHODS))
    if not methods or invalid:
        raise ValueError(f"raw baseline replay accepts only M1/M2: {invalid}")
    targets: list[ReplayTarget] = []
    for dataset in plan.select_datasets(dataset_names, kind="main"):
        for canonical in sorted(
                plan.load_suite(dataset), key=lambda row: _canonical_identity(row)[0]):
            circuit, _ = _canonical_identity(canonical)
            for method in methods:
                targets.append(ReplayTarget(
                    dataset=dataset,
                    canonical=canonical,
                    method=method,
                    circuit=circuit,
                    hashes=_target_hashes(plan, method, canonical),
                ))
    return targets


def _source_manifests(source_roots: Sequence[Path]) -> tuple[
        dict[tuple[str, str, str], list[tuple[Path, Mapping[str, Any]]]],
        list[Mapping[str, str]]]:
    index: dict[tuple[str, str, str], list[tuple[Path, Mapping[str, Any]]]] = {}
    errors: list[Mapping[str, str]] = []
    seen: set[Path] = set()
    for supplied in source_roots:
        root = Path(supplied).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"baseline source root does not exist: {root}")
        for path in sorted(root.rglob("manifest.json")):
            resolved = path.resolve()
            relative_parts = resolved.relative_to(root).parts[:-1]
            if any(part.startswith(".") and part.endswith(".tmp")
                   for part in relative_parts):
                # run_attempt promotes only atomically renamed directories.
                # An abandoned hidden staging directory is never an attempt,
                # even if a compiler happened to leave a partial raw file.
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                payload = _read_json(resolved)
            except ValueError as error:
                errors.append({"manifest": str(resolved), "error": str(error)})
                continue
            key = _source_key(payload)
            if key[2] not in BASELINE_METHODS:
                continue
            index.setdefault(key, []).append((resolved, payload))
    return index, errors


def _select_exact_source(method: str,
                         exact: Sequence[tuple[Path, Mapping[str, Any]]],
                         eligible: Sequence[SourceAttempt]
                         ) -> SourceAttempt | None:
    """Select one source, collapsing only byte-identical M2 reruns.

    Multiple M1 attempts remain ambiguous.  Multiple M2 attempts are
    equivalent only when *every raw-bearing* exact-hash candidate passes the
    frozen routing-aware identity gate and their decompressed native programs
    are byte-identical.  Failed launches with no raw file remain audit-visible
    but are not competing source evidence.  Path order is then a deterministic
    representative choice, while the complete equivalence class remains sealed
    in the receipt.
    """
    # A failed compiler launch with no raw file is not a competing raw source.
    # Keep it visible in the audit, but do not let it make byte-identical,
    # successfully emitted native programs ambiguous.
    raw_exact_count = sum(
        (path.parent / RAW_TRACE_FILES[method]).is_file()
        for path, _ in exact)
    if raw_exact_count == 1 and len(eligible) == 1:
        source = eligible[0]
        run_id = str(source.manifest.get("run_id", ""))
        if not run_id:
            return None
        return SourceAttempt(
            source.manifest_path, source.manifest, source.raw_trace_path,
            source.compiler_stats, source.compiler_stats_path,
            equivalent_manifest_paths=(source.manifest_path,),
            equivalent_run_ids=(run_id,),
            raw_payload_sha256=_hash_gzip_payload(source.raw_trace_path),
            source_root=source.source_root,
            lineage_manifest_path=source.lineage_manifest_path,
            lineage_manifest_sha256=source.lineage_manifest_sha256,
            lineage_sha256=source.lineage_sha256,
        )
    if (method != "M2" or raw_exact_count <= 1
            or len(eligible) != raw_exact_count):
        return None

    rows: list[tuple[tuple[Any, ...], SourceAttempt, str, str]] = []
    for source in eligible:
        run_id = str(source.manifest.get("run_id", ""))
        if not run_id:
            return None
        payload_sha256 = _hash_gzip_payload(source.raw_trace_path)
        signature = (
            payload_sha256,
            tuple(sorted(_source_hashes(source.manifest).items())),
            source.compiler_stats.get("compiler_class"),
            source.compiler_stats.get("fallback"),
            source.compiler_stats.get("mqt_qmap_version"),
            source.compiler_stats.get("mqt_core_version"),
        )
        rows.append((signature, source, run_id, payload_sha256))
    if len({row[0] for row in rows}) != 1:
        return None
    ordered = sorted(rows, key=lambda row: str(row[1].manifest_path))
    representative = ordered[0][1]
    return SourceAttempt(
        representative.manifest_path,
        representative.manifest,
        representative.raw_trace_path,
        representative.compiler_stats,
        representative.compiler_stats_path,
        equivalent_manifest_paths=tuple(
            row[1].manifest_path for row in ordered),
        equivalent_run_ids=tuple(row[2] for row in ordered),
        raw_payload_sha256=ordered[0][3],
        source_root=representative.source_root,
        lineage_manifest_path=representative.lineage_manifest_path,
        lineage_manifest_sha256=representative.lineage_manifest_sha256,
        lineage_sha256=representative.lineage_sha256,
    )


def audit_baseline_raw_replay(
        plan: ExperimentPlan, source_roots: Sequence[Path], *,
        dataset_names: Sequence[str] | None = None,
        methods: Sequence[str] = BASELINE_METHODS,
        m1_lineage_manifest: Path | None = None) -> Mapping[str, Any]:
    """Report exact, ambiguous, missing, and policy-incompatible sources."""
    targets = _targets(plan, dataset_names, methods)
    roots = tuple(Path(root).expanduser().resolve() for root in source_roots)
    m1_lineage = (
        None if m1_lineage_manifest is None
        else _load_m1_lineage_manifest(m1_lineage_manifest)
    )
    needs_general_index = (
        "M2" in methods or ("M1" in methods and m1_lineage is None))
    if needs_general_index:
        index, scan_errors = _source_manifests(roots)
    else:
        index, scan_errors = {}, []
    m1_roots: tuple[Path, ...] = roots
    m1_index = index
    if m1_lineage is not None:
        m1_roots = (m1_lineage.source_root,)
        m1_index, lineage_errors = _source_manifests(m1_roots)
        scan_errors.extend(lineage_errors)
    entries: list[dict[str, Any]] = []
    state_counts: Counter[str] = Counter()
    dataset_counts: dict[str, Counter[str]] = {}

    for target in targets:
        target_index = m1_index if target.method == "M1" else index
        target_roots = m1_roots if target.method == "M1" else roots
        identity_candidates = target_index.get(target.key, [])
        exact = [
            (path, payload) for path, payload in identity_candidates
            if _source_hashes(payload) == dict(target.hashes)
        ]
        eligible: list[SourceAttempt] = []
        rejected: list[Mapping[str, str]] = []
        for path, payload in exact:
            error, raw, stats, stats_path = _source_policy_error(
                target.method, payload, path.parent)
            if error is not None:
                rejected.append({"manifest": str(path), "reason": error})
            else:
                assert raw is not None and stats is not None and stats_path is not None
                eligible.append(SourceAttempt(
                    path, payload, raw, stats, stats_path,
                    source_root=_owning_source_root(path, target_roots),
                    lineage_manifest_path=(
                        None if target.method != "M1" or m1_lineage is None
                        else m1_lineage.manifest_path),
                    lineage_manifest_sha256=(
                        "" if target.method != "M1" or m1_lineage is None
                        else m1_lineage.manifest_sha256),
                    lineage_sha256=(
                        "" if target.method != "M1" or m1_lineage is None
                        else m1_lineage.lineage_sha256),
                ))

        selection = _select_exact_source(target.method, exact, eligible)
        raw_evidence = [
            {
                "run_id": str(value.manifest.get("run_id", "")),
                "source_status": str(value.manifest.get("status", "")),
                "source_root": str(value.source_root),
                "manifest": str(value.manifest_path),
                "raw_payload_sha256": _hash_gzip_payload(
                    value.raw_trace_path),
            }
            for value in sorted(
                eligible, key=lambda value: str(value.manifest_path))
        ]
        if selection is not None:
            state = "ready"
        elif len(eligible) > 1:
            state = "ambiguous"
        elif exact:
            state = "source_policy_incompatible"
        elif identity_candidates:
            state = "hash_mismatch"
        else:
            state = "missing"
        state_counts[state] += 1
        dataset_counts.setdefault(target.dataset.name, Counter())[state] += 1
        entries.append({
            "dataset": target.dataset.name,
            "circuit": target.circuit,
            "method": target.method,
            "state": state,
            "expected_hashes": dict(target.hashes),
            "identity_candidate_count": len(identity_candidates),
            "exact_hash_candidate_count": len(exact),
            "eligible_candidate_count": len(eligible),
            "eligible_manifests": [
                str(value.manifest_path) for value in eligible],
            "selected_manifest": (
                None if selection is None else str(selection.manifest_path)),
            "source_root": (
                None if selection is None or selection.source_root is None
                else str(selection.source_root)),
            "lineage_sha256": (
                "" if selection is None else selection.lineage_sha256),
            "equivalent_source_count": (
                0 if selection is None
                else len(selection.equivalent_manifest_paths)),
            "equivalent_source_run_ids": (
                [] if selection is None
                else list(selection.equivalent_run_ids)),
            "raw_payload_sha256": (
                "" if selection is None else selection.raw_payload_sha256),
            "exact_canonical_sha256": target.hashes["input_sha256"],
            "eligible_raw_evidence": raw_evidence,
            "rejected_exact_candidates": rejected,
        })

    rerun_required = [
        {
            "dataset": entry["dataset"],
            "circuit": entry["circuit"],
            "method": entry["method"],
            "state": entry["state"],
            "required_raw_trace": RAW_TRACE_FILES[entry["method"]],
            "action": (
                "rerun frozen paper-native ZAC without ghost repair/split"
                if entry["method"] == "M1" else
                "rerun frozen routing-aware QMAP 3.2 A* without fallback"),
            "reasons": entry["rejected_exact_candidates"],
            "conflicting_raw_evidence": entry["eligible_raw_evidence"],
        }
        for entry in entries
        if entry["state"] != "ready"
    ]
    rerun_counts = Counter(value["method"] for value in rerun_required)
    return {
        "experiment_schema": 2,
        "protocol": REPLAY_PROTOCOL,
        "mode": "audit",
        "source_roots": [str(root) for root in roots],
        "m1_lineage": (
            {"mode": "unfrozen"} if m1_lineage is None else
            {"mode": "frozen", **m1_lineage.to_dict()}),
        "expected_attempts": len(targets),
        "ready": state_counts["ready"],
        "replay_ready": state_counts["ready"] == len(targets),
        "state_counts": dict(sorted(state_counts.items())),
        "dataset_state_counts": {
            dataset: dict(sorted(counts.items()))
            for dataset, counts in sorted(dataset_counts.items())
        },
        "scan_errors": scan_errors,
        "rerun_required": rerun_required,
        "rerun_required_by_method": dict(sorted(rerun_counts.items())),
        "entries": entries,
    }


def _selected_attempts(plan: ExperimentPlan, source_roots: Sequence[Path], *,
                       dataset_names: Sequence[str] | None,
                       methods: Sequence[str],
                       m1_lineage: M1LineageFreeze | None
                       ) -> list[tuple[ReplayTarget, SourceAttempt]]:
    targets = _targets(plan, dataset_names, methods)
    roots = tuple(Path(root).expanduser().resolve() for root in source_roots)
    needs_general_index = (
        "M2" in methods or ("M1" in methods and m1_lineage is None))
    if needs_general_index:
        index, scan_errors = _source_manifests(roots)
    else:
        index, scan_errors = {}, []
    m1_roots: tuple[Path, ...] = roots
    m1_index = index
    if m1_lineage is not None:
        m1_roots = (m1_lineage.source_root,)
        m1_index, lineage_errors = _source_manifests(m1_roots)
        scan_errors.extend(lineage_errors)
    if scan_errors:
        raise ValueError(
            "source tree contains unreadable baseline manifests: "
            f"{scan_errors[:3]}")
    selected: list[tuple[ReplayTarget, SourceAttempt]] = []
    failures: list[str] = []
    for target in targets:
        target_index = m1_index if target.method == "M1" else index
        target_roots = m1_roots if target.method == "M1" else roots
        exact = [
            (path, payload) for path, payload in target_index.get(target.key, [])
            if _source_hashes(payload) == dict(target.hashes)
        ]
        eligible: list[SourceAttempt] = []
        reasons: list[str] = []
        for path, payload in exact:
            error, raw, stats, stats_path = _source_policy_error(
                target.method, payload, path.parent)
            if error is not None:
                reasons.append(f"{path}: {error}")
            else:
                assert raw is not None and stats is not None and stats_path is not None
                eligible.append(SourceAttempt(
                    path, payload, raw, stats, stats_path,
                    source_root=_owning_source_root(path, target_roots),
                    lineage_manifest_path=(
                        None if target.method != "M1" or m1_lineage is None
                        else m1_lineage.manifest_path),
                    lineage_manifest_sha256=(
                        "" if target.method != "M1" or m1_lineage is None
                        else m1_lineage.manifest_sha256),
                    lineage_sha256=(
                        "" if target.method != "M1" or m1_lineage is None
                        else m1_lineage.lineage_sha256),
                ))
        selection = _select_exact_source(target.method, exact, eligible)
        if selection is None:
            failures.append(
                f"{target.dataset.name}/{target.circuit}/{target.method}: "
                "expected one eligible source or one byte-identical M2 "
                f"equivalence class, found {len(eligible)} eligible; "
                f"exact={len(exact)}; reasons={reasons[:3]}")
        else:
            selected.append((target, selection))
    if failures:
        raise ValueError(
            "baseline raw replay preflight failed before writing outputs: "
            + " | ".join(failures[:10])
            + (f" | ... {len(failures) - 10} more" if len(failures) > 10 else "")
        )
    return selected


def _hash_gzip_payload(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with gzip.open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError as error:
        raise ValueError(f"invalid gzip raw trace {path}: {error}") from error
    return digest.hexdigest()


def _revalidate_selected_source(target: ReplayTarget,
                                source: SourceAttempt) -> SourceAttempt:
    """Close the audit-to-copy race for the complete equivalence class."""
    if target.method == "M1":
        if (source.source_root is None or
                source.lineage_manifest_path is None or
                not source.lineage_sha256):
            raise ValueError("M1 replay source is not sealed to one lineage")
        lineage = _load_m1_lineage_manifest(source.lineage_manifest_path)
        if (lineage.source_root != source.source_root or
                lineage.manifest_sha256 != source.lineage_manifest_sha256 or
                lineage.lineage_sha256 != source.lineage_sha256):
            raise ValueError("M1 source lineage changed after preflight")
    refreshed: list[SourceAttempt] = []
    run_ids: list[str] = []
    expected_paths = source.equivalent_manifest_paths or (source.manifest_path,)
    for manifest_path in expected_paths:
        if (source.source_root is not None and
                not manifest_path.resolve().is_relative_to(source.source_root)):
            raise ValueError(
                f"selected source escaped its frozen root: {manifest_path}")
        manifest = _read_json(manifest_path)
        if (_source_key(manifest) != target.key
                or _source_hashes(manifest) != dict(target.hashes)):
            raise ValueError(
                f"selected source identity/hash changed after preflight: "
                f"{manifest_path}")
        error, raw, stats, stats_path = _source_policy_error(
            target.method, manifest, manifest_path.parent)
        if error is not None:
            raise ValueError(
                f"selected source policy changed after preflight: "
                f"{manifest_path}: {error}")
        assert raw is not None and stats is not None and stats_path is not None
        payload_sha256 = _hash_gzip_payload(raw)
        if payload_sha256 != source.raw_payload_sha256:
            raise ValueError(
                "selected M2-equivalent raw trace changed or differs after "
                f"preflight: {raw}: {payload_sha256} != "
                f"{source.raw_payload_sha256}")
        run_id = str(manifest.get("run_id", ""))
        if not run_id:
            raise ValueError(f"selected source lacks run_id: {manifest_path}")
        run_ids.append(run_id)
        refreshed.append(SourceAttempt(
            manifest_path, manifest, raw, stats, stats_path,
            source_root=source.source_root,
            lineage_manifest_path=source.lineage_manifest_path,
            lineage_manifest_sha256=source.lineage_manifest_sha256,
            lineage_sha256=source.lineage_sha256,
        ))
    if tuple(run_ids) != source.equivalent_run_ids:
        raise ValueError(
            "selected source run-id equivalence class changed after preflight")
    representative = sorted(
        refreshed, key=lambda value: str(value.manifest_path))[0]
    return SourceAttempt(
        representative.manifest_path,
        representative.manifest,
        representative.raw_trace_path,
        representative.compiler_stats,
        representative.compiler_stats_path,
        equivalent_manifest_paths=tuple(expected_paths),
        equivalent_run_ids=tuple(run_ids),
        raw_payload_sha256=source.raw_payload_sha256,
        source_root=source.source_root,
        lineage_manifest_path=source.lineage_manifest_path,
        lineage_manifest_sha256=source.lineage_manifest_sha256,
        lineage_sha256=source.lineage_sha256,
    )


def _copy_source_timing(manifest: RunManifest,
                        source: Mapping[str, Any]) -> None:
    for field in (
            "compiler_time_ns", "compiler_process_wall_ns", "cpu_time_ns",
            "peak_rss_bytes", "transition_decision_ns", "search_kernel_ns",
            "marshal_ns", "fitness_ns", "native_parse_ns",
            "native_serialize_ns", "horizon_selection_ns",
            "initial_placement_ns", "routing_ns", "full_compile_ns"):
        value = source.get(field)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"source manifest has invalid {field}: {value!r}")
            setattr(manifest, field, int(value))
    if manifest.compiler_time_ns is None or manifest.compiler_time_ns < 0:
        raise ValueError("source manifest lacks a valid compiler_time_ns")


def _new_manifest(plan: ExperimentPlan, target: ReplayTarget,
                  source: SourceAttempt, final: Path,
                  run_id: str) -> RunManifest:
    # Import lazily to avoid making the evaluator depend on the importer.
    from .cli import (_canonical_gate_ledger, _canonical_layer_ledger,
                      _ledger_sha256)

    layer = _canonical_layer_ledger(target.canonical)
    source_manifest_sha = sha256_file(source.manifest_path)
    manifest = RunManifest.with_provenance(
        plan.repo_root,
        run_id=run_id,
        dataset=target.dataset.name,
        circuit=target.circuit,
        method=target.method,
        seed=0,
        repetition=0,
        run_kind="main",
        experiment_id=plan.experiment_id(target.dataset),
        status=RunStatus.COMPILER_ERROR.value,
        package_versions={
            **{str(key): str(value)
               for key, value in plan.package_versions.items()},
            "baseline_replay_protocol": REPLAY_PROTOCOL,
            "source_manifest_sha256": source_manifest_sha,
            **({
                "m1_source_lineage_sha256": source.lineage_sha256,
                "m1_source_lineage_manifest_sha256":
                    source.lineage_manifest_sha256,
            } if target.method == "M1" else {}),
        },
        algorithm_revision=(
            "zac-paper-original-v1" if target.method == "M1"
            else "qmap-3.2-paper-native-v1"),
        backend=(
            "zac-python-original" if target.method == "M1"
            else "qmap-cpp-astar"),
        rng_version=(
            "zac-paper-original" if target.method == "M1"
            else "qmap-3.2-upstream"),
        input_sha256=target.hashes["input_sha256"],
        config_sha256=target.hashes["config_sha256"],
        architecture_sha256=target.hashes["architecture_sha256"],
        model_sha256=target.hashes["model_sha256"],
        command=[
            REPLAY_PROTOCOL,
            "--source-manifest-sha256", source_manifest_sha,
            "--raw-trace", RAW_TRACE_FILES[target.method],
            *(["--m1-lineage-sha256", source.lineage_sha256]
              if target.method == "M1" else []),
        ],
        exit_code=source.manifest.get("exit_code"),
        timeout_seconds=plan.timeout_seconds,
        qubits=target.canonical.qubits,
        expected_gates_1q=target.canonical.gates_1q,
        expected_gates_2q=target.canonical.gates_2q,
        expected_gate_ledger_sha256=_ledger_sha256(
            _canonical_gate_ledger(target.canonical)),
        layer_ledger_sha256=str(layer["layer_ledger_sha256"]),
        transition_count=int(layer["transitions"]),
        canonical_input_layer_ledger_sha256=str(layer["layer_ledger_sha256"]),
        canonical_input_transition_count=int(layer["transitions"]),
        ghost_repairs=0,
        ghost_splits=0,
        trace_protocol=trace_protocol_for_method(target.method),
        ghost_policy=ghost_policy_for_method(target.method),
        physicalization_policy=physicalization_policy_for_method(target.method),
        artifact_dir=str(final),
    )
    _copy_source_timing(manifest, source.manifest)
    return manifest


def _write_receipt(directory: Path, target: ReplayTarget,
                   source: SourceAttempt, manifest: RunManifest,
                   raw_payload_sha256: str) -> Mapping[str, Any]:
    outputs = {}
    for name in ("manifest.json", "canonical_trace.jsonl.gz", "fidelity.json"):
        path = directory / name
        if path.is_file():
            outputs[name] = sha256_file(path)
    source_directory = source.manifest_path.parent
    ignored_present = [
        name for name in IGNORED_DERIVED_ARTIFACTS
        if (source_directory / name).is_file()
    ]
    equivalent_sources = []
    source_statuses: set[str] = set()
    for path, run_id in zip(source.equivalent_manifest_paths,
                            source.equivalent_run_ids):
        source_payload = _read_json(path)
        source_status = str(source_payload.get("status", ""))
        source_statuses.add(source_status)
        equivalent_sources.append({
            "run_id": run_id,
            "source_status": source_status,
            "manifest": str(path),
            "manifest_sha256": sha256_file(path),
            "raw_trace": str(path.parent / RAW_TRACE_FILES[target.method]),
            "raw_trace_gzip_sha256": sha256_file(
                path.parent / RAW_TRACE_FILES[target.method]),
            "raw_trace_payload_sha256": _hash_gzip_payload(
                path.parent / RAW_TRACE_FILES[target.method]),
        })
    payload: dict[str, Any] = {
        "experiment_schema": 2,
        "protocol": REPLAY_PROTOCOL,
        "identity": {
            "dataset": target.dataset.name,
            "circuit": target.circuit,
            "method": target.method,
            "seed": 0,
            "repetition": 0,
        },
        "status": manifest.status,
        "compilation_executed": False,
        "source": {
            "source_root": (
                None if source.source_root is None else str(source.source_root)),
            "lineage_manifest": (
                None if source.lineage_manifest_path is None
                else str(source.lineage_manifest_path)),
            "lineage_manifest_sha256": source.lineage_manifest_sha256,
            "lineage_sha256": source.lineage_sha256,
            "manifest": str(source.manifest_path),
            "manifest_sha256": sha256_file(source.manifest_path),
            "compiler_stats": str(source.compiler_stats_path),
            "compiler_stats_sha256": sha256_file(source.compiler_stats_path),
            "raw_trace": str(source.raw_trace_path),
            "raw_trace_gzip_sha256": sha256_file(source.raw_trace_path),
            "raw_trace_payload_sha256": raw_payload_sha256,
            "equivalent_source_count": len(equivalent_sources),
            "equivalent_source_run_ids": list(source.equivalent_run_ids),
            "equivalent_source_statuses": sorted(source_statuses),
            "equivalent_sources": equivalent_sources,
        },
        "exact_match_hashes": dict(target.hashes),
        "exact_canonical_sha256": target.hashes["input_sha256"],
        "normalization_input": RAW_TRACE_FILES[target.method],
        "ignored_source_artifacts": ignored_present,
        "forbidden_as_replay_inputs": list(IGNORED_DERIVED_ARTIFACTS),
        "baseline_postprocessing": {
            "ghost_repair": False,
            "ghost_split": False,
            "routing_agnostic_fallback": False,
        },
        "outputs": outputs,
    }
    payload["seal_sha256"] = stable_sha256(payload)
    path = directory / "replay_receipt.json"
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return payload


def replay_one_baseline_raw(plan: ExperimentPlan, target: ReplayTarget,
                            source: SourceAttempt, output_root: Path
                            ) -> Mapping[str, Any]:
    """Create one new Schema-2 attempt without launching a compiler."""
    from .cli import UnifiedEvaluationGate

    source = _revalidate_selected_source(target, source)
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = (
        f"{target.dataset.name}-{target.circuit}-{target.method}-raw-replay-"
        f"{uuid.uuid4().hex[:12]}"
    )
    final = output_root / run_id
    temporary = output_root / f".{run_id}.tmp"
    temporary.mkdir()
    manifest = _new_manifest(plan, target, source, final, run_id)
    start = time.perf_counter_ns()
    manifest.started_at_utc = _utc_now()
    raw_payload_sha256 = ""
    try:
        raw_payload_sha256 = _hash_gzip_payload(source.raw_trace_path)
        if raw_payload_sha256 != source.raw_payload_sha256:
            raise ValueError(
                "representative raw trace changed after equivalence revalidation")
        shutil.copyfile(
            source.raw_trace_path, temporary / RAW_TRACE_FILES[target.method])
        (temporary / manifest.stdout_path).write_text("", encoding="utf-8")
        (temporary / manifest.stderr_path).write_text("", encoding="utf-8")
        gate = UnifiedEvaluationGate(
            plan, target.canonical, target.method,
            raw_trace_name=GATE_TRACE_NAMES[target.method])
        try:
            verification = gate.verifier(temporary)
            manifest.verifier_ok = bool(verification.get("ok", False))
            manifest.warnings.extend(
                str(value) for value in verification.get("warnings", []))
            if not manifest.verifier_ok:
                manifest.status = RunStatus.VERIFIER_FAIL.value
                manifest.error = "paper-native raw replay verifier returned ok=false"
            else:
                metrics = dict(gate.scorer(temporary))
                # No compiler_stats file is copied.  These explicit zeros are
                # the replay contract, not counters inherited from a repaired
                # legacy derivative.
                metrics["ghost_repairs"] = 0
                metrics["ghost_splits"] = 0
                _apply_metrics(manifest, metrics)
                manifest.status = RunStatus.SUCCESS.value
        except BaseException as error:
            manifest.verifier_ok = False
            manifest.status = RunStatus.VERIFIER_FAIL.value
            manifest.error = (
                f"paper-native raw replay verifier exception: "
                f"{type(error).__name__}: {error}")
        manifest.end_to_end_time_ns = time.perf_counter_ns() - start
        manifest.ended_at_utc = _utc_now()
        manifest.validate(require_success_metrics=True)
        manifest.write(temporary / "manifest.json")
        receipt = _write_receipt(
            temporary, target, source, manifest, raw_payload_sha256)
        os.replace(temporary, final)
        return {
            "dataset": target.dataset.name,
            "circuit": target.circuit,
            "method": target.method,
            "status": manifest.status,
            "manifest": str(final / "manifest.json"),
            "receipt": str(final / "replay_receipt.json"),
            "source_manifest": str(source.manifest_path),
            "receipt_seal_sha256": receipt["seal_sha256"],
        }
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def import_baseline_raw_replays(
        plan: ExperimentPlan, source_roots: Sequence[Path], *,
        dataset_names: Sequence[str] | None = None,
        methods: Sequence[str] = BASELINE_METHODS,
        output_root: Path | None = None,
        m1_lineage_manifest: Path | None = None) -> Mapping[str, Any]:
    """Preflight the full cohort, then replay every uniquely matched raw trace."""
    m1_lineage = None
    if "M1" in methods:
        lineage_path = (
            default_m1_lineage_manifest(plan)
            if m1_lineage_manifest is None else m1_lineage_manifest)
        m1_lineage = _load_m1_lineage_manifest(lineage_path)
    selected = _selected_attempts(
        plan, source_roots, dataset_names=dataset_names, methods=methods,
        m1_lineage=m1_lineage)
    destination = (
        Path(output_root).expanduser().resolve() if output_root is not None
        else (plan.output_root / "runs" / "main").resolve()
    )
    protected_roots = [
        Path(source_root).expanduser().resolve()
        for source_root in source_roots
    ]
    if m1_lineage is not None:
        protected_roots.append(m1_lineage.source_root)
    for source in protected_roots:
        if destination == source or destination.is_relative_to(source):
            raise ValueError(
                "baseline replay destination may not be inside a source root: "
                f"{destination} inside {source}")
    attempts = []
    for target, source in selected:
        attempts.append(replay_one_baseline_raw(
            plan, target, source, destination / target.dataset.name))
    counts = Counter(row["status"] for row in attempts)
    return {
        "experiment_schema": 2,
        "protocol": REPLAY_PROTOCOL,
        "mode": "replay",
        "compilation_executed": False,
        "m1_lineage": (
            None if m1_lineage is None else m1_lineage.to_dict()),
        "attempted": attempts,
        "status_counts": dict(sorted(counts.items())),
    }


__all__ = [
    "BASELINE_METHODS", "DEFAULT_M1_LINEAGE_DIRECTORY",
    "DEFAULT_M1_LINEAGE_MANIFEST", "M1_LINEAGE_PROTOCOL",
    "RAW_TRACE_FILES", "REPLAY_PROTOCOL", "M1LineageFreeze",
    "ReplayTarget", "SourceAttempt", "audit_baseline_raw_replay",
    "default_m1_lineage_manifest", "import_baseline_raw_replays",
    "replay_one_baseline_raw",
]
