"""Balanced Chinese-paper experiment orchestration.

This module is intentionally separate from the older formal tuning gates.  It
freezes the accepted ABI-8 seed-0 delivery, reuses the actually executed M1/M2
manifests, and adds only the evidence needed by the Chinese paper:

* a deterministic 12-circuit seed-0 parity gate;
* M3/M4 seeds 1 and 2 (or a full seed-0 rerun when parity fails);
* the shared H0/H8 and GA/greedy controlled ablations;
* nine deterministic M4 sensitivity settings; and
* the 12-circuit, serial three-repeat timing cohort.

Every compiler execution still goes through :func:`run_attempt`; this file
does not infer success from filenames and never reads a legacy metric file.
"""

from __future__ import annotations

import concurrent.futures
import copy
import gzip
import hashlib
import json
import math
import os
import random
import subprocess
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from zzx.algorithm_v2 import validate_schema2_setting

from .ablation import QMAP_STRATA, select_ablation_cohort
from .cli import UnifiedEvaluationGate, _attempt_spec, _manifest_path, _spec_key
from .contracts import (CanonicalCircuitManifest, RunManifest, RunStatus,
                        load_run_manifest, repository_snapshot, sha256_file,
                        stable_sha256)
from .plan import ExperimentPlan, effective_zac_setting
from .runner import AttemptSpec, run_attempt


PAPER_PROTOCOL_ID = "paper-zh-balanced-v1"
PAPER_FREEZE_SCHEMA = 1
PAPER_MAIN_SEEDS = (0, 1, 2)
PAPER_NEW_SEEDS = (1, 2)
PAPER_WORKERS = 4
PAPER_HEAVY_RSS_BYTES = 2 * (1 << 30)
PAPER_DATASET_SIZES = {"zac18": 18, "qmap154": 154}
PAPER_METHODS = ("M1", "M2", "M3", "M4")
PAPER_OURS = ("M3", "M4")
PAPER_TIMING_REPETITIONS = 3
PAPER_TIMING_SCHEDULE_SEED = 20260828
PAPER_ABLATION_VARIANTS = {
    "h0": "paper_h0_ga",
    "h8": "paper_h8_ga",
    "greedy": "paper_h8_greedy_only",
}
TERMINAL_STATUSES = frozenset(item.value for item in RunStatus)

ZAC_QUBIT_STRATA = (
    ("le_32", 0, 32),
    ("33_64", 33, 64),
    ("gt_64", 65, None),
)


@dataclass(frozen=True)
class PaperJob:
    """One immutable compiler attempt plus its canonical verifier input."""

    spec: AttemptSpec
    canonical: CanonicalCircuitManifest
    historical_heavy: bool = False


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _is_sha256(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64 and
            all(character in "0123456789abcdef" for character in value))


def _circuit_id(row: CanonicalCircuitManifest) -> str:
    return Path(row.canonical_path).stem


def _stratum(value: int, strata: Sequence[tuple[str, int, int | None]]) -> str:
    for label, lower, upper in strata:
        if value >= lower and (upper is None or value <= upper):
            return label
    raise ValueError(f"value {value} is outside the registered strata")


def select_paper_twelve(
        suites: Mapping[str, Sequence[CanonicalCircuitManifest]],
        ) -> tuple[list[tuple[str, CanonicalCircuitManifest]], Mapping[str, Any]]:
    """Choose two SHA-min circuits from each size stratum in each dataset."""
    if set(suites) != set(PAPER_DATASET_SIZES):
        raise ValueError("paper cohort requires exactly zac18 and qmap154")
    selected: list[tuple[str, CanonicalCircuitManifest]] = []
    report: dict[str, Any] = {}
    for dataset in ("zac18", "qmap154"):
        rows = list(suites[dataset])
        expected = PAPER_DATASET_SIZES[dataset]
        if len(rows) != expected:
            raise ValueError(
                f"{dataset} paper cohort requires {expected} circuits, "
                f"found {len(rows)}")
        strata = ZAC_QUBIT_STRATA if dataset == "zac18" else QMAP_STRATA
        buckets: dict[str, list[CanonicalCircuitManifest]] = {
            label: [] for label, _lower, _upper in strata}
        for row in rows:
            value = row.qubits if dataset == "zac18" else row.gates_2q
            buckets[_stratum(value, strata)].append(row)
        dataset_report: dict[str, Any] = {}
        for label, lower, upper in strata:
            members = sorted(
                buckets[label],
                key=lambda row: (row.canonical_sha256, _circuit_id(row)),
            )
            if len(members) < 2:
                raise ValueError(
                    f"{dataset}/{label} has {len(members)} circuits; needs 2")
            chosen = members[:2]
            selected.extend((dataset, row) for row in chosen)
            dataset_report[label] = {
                "bounds": [lower, upper],
                "metric": "qubits" if dataset == "zac18" else "gates_2q",
                "population": len(members),
                "selected": [_circuit_id(row) for row in chosen],
                "canonical_sha256": [row.canonical_sha256 for row in chosen],
            }
        report[dataset] = dataset_report
    if len(selected) != 12:
        raise AssertionError("registered paper timing/parity cohort is not 12")
    return selected, {
        "selection": "three_strata_sha256_min2",
        "selected": 12,
        "datasets": report,
    }


def select_paper_ablation(
        suites: Mapping[str, Sequence[CanonicalCircuitManifest]],
        ) -> tuple[list[tuple[str, CanonicalCircuitManifest]], Mapping[str, Any]]:
    """Return all ZAC18 plus QMAP SHA-min ten per 2Q stratum."""
    if set(suites) != set(PAPER_DATASET_SIZES):
        raise ValueError("paper ablation requires exactly zac18 and qmap154")
    selected: list[tuple[str, CanonicalCircuitManifest]] = []
    report: dict[str, Any] = {}
    for dataset in ("zac18", "qmap154"):
        rows, selection = select_ablation_cohort(dataset, suites[dataset])
        selected.extend((dataset, row) for row in rows)
        report[dataset] = selection
    if len(selected) != 48:
        raise AssertionError("registered paper ablation cohort is not 48")
    return selected, {
        "selection": "zac18_all_plus_qmap_three_strata_sha256_min10",
        "selected": 48,
        "datasets": report,
    }


def _suite_map(plan: ExperimentPlan) -> dict[str, list[CanonicalCircuitManifest]]:
    if set(plan.datasets) != set(PAPER_DATASET_SIZES):
        raise ValueError(
            "paper protocol accepts only the frozen zac18 and qmap154 datasets")
    suites: dict[str, list[CanonicalCircuitManifest]] = {}
    for dataset, expected in PAPER_DATASET_SIZES.items():
        spec = plan.datasets[dataset]
        if spec.kind != "main":
            raise ValueError(f"paper dataset {dataset} is not kind=main")
        rows = plan.load_suite(spec)
        if len(rows) != expected:
            raise ValueError(
                f"paper dataset {dataset} has {len(rows)} circuits, "
                f"expected {expected}")
        suites[dataset] = rows
    return suites


def _source_keys(suites: Mapping[str, Sequence[CanonicalCircuitManifest]]) -> set[str]:
    return {
        f"{dataset}/{_circuit_id(row)}"
        for dataset, rows in suites.items() for row in rows
    }


def _validate_seed0_source_manifest(
        source_path: Path,
        suites: Mapping[str, Sequence[CanonicalCircuitManifest]],
        plan: ExperimentPlan,
        wheel_sha256: str,
        ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    payload = _read_json(source_path)
    if not isinstance(payload, Mapping) or set(payload) != set(PAPER_METHODS):
        raise ValueError("seed0 source manifest must contain exactly M1/M2/M3/M4")
    expected_keys = _source_keys(suites)
    heavy: set[tuple[str, str, str]] = set()
    status_counts: dict[str, Mapping[str, int]] = {}
    config_hashes = {
        method: sha256_file(plan.methods[method].config_path)
        for method in PAPER_OURS
    }
    for method in PAPER_METHODS:
        rows = payload[method]
        if not isinstance(rows, Mapping) or set(rows) != expected_keys:
            missing = expected_keys - set(rows) if isinstance(rows, Mapping) else expected_keys
            extra = set(rows) - expected_keys if isinstance(rows, Mapping) else set()
            raise ValueError(
                f"{method} source rows differ from the frozen 172 circuits: "
                f"missing={len(missing)}, extra={len(extra)}")
        counts: Counter[str] = Counter()
        for identity in sorted(rows):
            row = rows[identity]
            if not isinstance(row, Mapping):
                raise ValueError(f"invalid source row: {method}/{identity}")
            manifest_path = Path(str(row.get("path", ""))).resolve()
            expected_hash = row.get("sha256")
            if not manifest_path.is_file() or not _is_sha256(expected_hash):
                raise FileNotFoundError(
                    f"source manifest evidence is missing: {method}/{identity}")
            if sha256_file(manifest_path) != expected_hash:
                raise ValueError(
                    f"source manifest evidence hash drift: {method}/{identity}")
            manifest = load_run_manifest(manifest_path)
            dataset, circuit = identity.split("/", 1)
            if ((manifest.dataset, manifest.circuit, manifest.method,
                 manifest.seed, manifest.repetition) !=
                    (dataset, circuit, method, 0, 0)):
                raise ValueError(f"source identity drift: {method}/{identity}")
            if manifest.status != row.get("status"):
                raise ValueError(f"source status drift: {method}/{identity}")
            counts[manifest.status] += 1
            if method in PAPER_OURS:
                if manifest.config_sha256 != config_hashes[method]:
                    raise ValueError(
                        f"{method} seed0 source does not use the frozen config")
                if manifest.status == RunStatus.SUCCESS.value:
                    if (manifest.backend != "native" or
                            manifest.native_abi_version != 8 or
                            manifest.native_wheel_sha256 != wheel_sha256 or
                            manifest.ghost_hits != 0 or
                            manifest.verifier_ok is not True):
                        raise ValueError(
                            f"{method} seed0 success is not valid ABI8 evidence: "
                            f"{identity}")
                if ((manifest.peak_rss_bytes or 0) > PAPER_HEAVY_RSS_BYTES):
                    heavy.add((dataset, circuit, method))
        status_counts[method] = dict(sorted(counts.items()))
    return payload, {
        "status_counts": status_counts,
        "historical_heavy": [list(value) for value in sorted(heavy)],
    }


def _copy_json_snapshot(source: Path, destination: Path) -> str:
    payload = _read_json(source)
    _atomic_json(destination, payload)
    return sha256_file(destination)


def command_paper_freeze(
        plan: ExperimentPlan, *, output_root: str | Path,
        source_manifest: str | Path, abi8_wheel: str | Path,
        require_clean_git: bool = True) -> Mapping[str, Any]:
    """Freeze all identities required to accept old seed0 after parity."""
    root = Path(output_root).resolve()
    freeze_root = root / "freeze"
    source_path = Path(source_manifest).resolve()
    wheel_path = Path(abi8_wheel).resolve()
    if not source_path.is_file() or not wheel_path.is_file():
        raise FileNotFoundError("paper freeze requires source manifest and ABI8 wheel")
    repository = repository_snapshot(plan.repo_root)
    if require_clean_git and (repository.get("dirty") is not False or
                              repository.get("commit") in {None, "", "unknown"}):
        raise RuntimeError("paper evidence freeze requires a clean Git commit")
    suites = _suite_map(plan)
    twelve, twelve_report = select_paper_twelve(suites)
    ablation, ablation_report = select_paper_ablation(suites)

    wheel_sha = sha256_file(wheel_path)
    if wheel_sha != "b9109e6556e032a8c148b33c9ca1f213eac4226c6801182e67e8ef4a577786b9":
        raise ValueError("paper main freeze requires the accepted ABI8 wheel")
    for method in PAPER_OURS:
        setting = effective_zac_setting(plan.methods[method].payload)
        if (setting.get("backend") != "native" or
                setting.get("native_abi_version") != 8 or
                setting.get("native_wheel_sha256") != wheel_sha):
            raise ValueError(f"{method} does not declare the frozen ABI8 wheel")

    source_payload, source_report = _validate_seed0_source_manifest(
        source_path, suites, plan, wheel_sha)
    source_snapshot = freeze_root / "seed0_source_manifest.json"
    _atomic_json(source_snapshot, source_payload)
    config_snapshots: dict[str, Any] = {}
    for method in PAPER_METHODS:
        source = plan.methods[method].config_path
        destination = freeze_root / "configs" / f"{method}.json"
        snapshot_sha = _copy_json_snapshot(source, destination)
        if snapshot_sha != sha256_file(source):
            # Semantic JSON copy can change insignificant whitespace.  The
            # exact source hash is retained separately and execution uses an
            # atomically copied byte-for-byte file below.
            _atomic_bytes(destination, source.read_bytes())
            snapshot_sha = sha256_file(destination)
        config_snapshots[method] = {
            "path": str(destination.resolve()),
            "sha256": snapshot_sha,
            "source_path": str(source.resolve()),
            "source_sha256": sha256_file(source),
        }
    suite_snapshots: dict[str, Any] = {}
    for dataset in ("zac18", "qmap154"):
        spec = plan.datasets[dataset]
        destination = freeze_root / "canonical" / f"{dataset}.suite.manifest.json"
        snapshot_sha = _copy_json_snapshot(spec.suite_manifest, destination)
        suite_snapshots[dataset] = {
            "path": str(destination.resolve()),
            "sha256": snapshot_sha,
            "source_path": str(spec.suite_manifest.resolve()),
            "source_sha256": sha256_file(spec.suite_manifest),
            "canonical_inputs": {
                _circuit_id(row): row.canonical_sha256 for row in suites[dataset]
            },
        }
    payload: dict[str, Any] = {
        "experiment_schema": 2,
        "paper_freeze_schema": PAPER_FREEZE_SCHEMA,
        "protocol_id": PAPER_PROTOCOL_ID,
        "status": "frozen",
        "repository": repository,
        "plan": {"path": str(plan.path), "sha256": sha256_file(plan.path)},
        "architecture": {
            "path": str(plan.architecture_path),
            "sha256": sha256_file(plan.architecture_path),
        },
        "model": {
            "path": str(plan.model_path),
            "sha256": sha256_file(plan.model_path),
        },
        "abi8_wheel": {"path": str(wheel_path), "sha256": wheel_sha},
        "configs": config_snapshots,
        "canonical_suites": suite_snapshots,
        "seed0_source_manifest": {
            "path": str(source_snapshot.resolve()),
            "sha256": sha256_file(source_snapshot),
            "original_path": str(source_path),
            "original_sha256": sha256_file(source_path),
            **source_report,
        },
        "parity_timing_cohort": {
            **twelve_report,
            "identities": [
                [dataset, _circuit_id(row)] for dataset, row in twelve
            ],
        },
        "ablation_cohort": {
            **ablation_report,
            "identities": [
                [dataset, _circuit_id(row)] for dataset, row in ablation
            ],
        },
        "resource_contract": {
            "workers": PAPER_WORKERS,
            "heavy_threshold_bytes": PAPER_HEAVY_RSS_BYTES,
            "maximum_simultaneous_heavy": 1,
            "timeout_seconds": plan.timeout_seconds,
        },
    }
    payload["freeze_id"] = stable_sha256(payload)
    payload["seal_sha256"] = stable_sha256(
        {key: value for key, value in payload.items() if key != "seal_sha256"})
    freeze_path = freeze_root / "freeze_manifest.json"
    _atomic_json(freeze_path, payload)
    return {**payload, "freeze_manifest": str(freeze_path.resolve())}


def load_paper_freeze(path: str | Path, *, verify_live: bool = True
                      ) -> Mapping[str, Any]:
    freeze_path = Path(path).resolve()
    payload = _read_json(freeze_path)
    if (not isinstance(payload, Mapping) or payload.get("experiment_schema") != 2 or
            payload.get("paper_freeze_schema") != PAPER_FREEZE_SCHEMA or
            payload.get("protocol_id") != PAPER_PROTOCOL_ID or
            payload.get("status") != "frozen"):
        raise ValueError("invalid paper freeze manifest")
    seal = payload.get("seal_sha256")
    unsigned = {key: value for key, value in payload.items() if key != "seal_sha256"}
    if not _is_sha256(seal) or stable_sha256(unsigned) != seal:
        raise ValueError("paper freeze seal mismatch")
    frozen_id_payload = {key: value for key, value in unsigned.items()
                         if key != "freeze_id"}
    if stable_sha256(frozen_id_payload) != payload.get("freeze_id"):
        raise ValueError("paper freeze identity mismatch")
    if verify_live:
        evidence = [payload["plan"], payload["architecture"], payload["model"],
                    payload["abi8_wheel"], payload["seed0_source_manifest"]]
        evidence.extend(payload["configs"].values())
        evidence.extend(payload["canonical_suites"].values())
        for row in evidence:
            evidence_path = Path(str(row["path"])).resolve()
            if not evidence_path.is_file() or sha256_file(evidence_path) != row["sha256"]:
                raise ValueError(f"frozen evidence drift: {evidence_path}")
        for dataset, suite in payload["canonical_suites"].items():
            suite_rows = _read_json(suite["path"])
            if not isinstance(suite_rows, list):
                raise ValueError(f"frozen suite is not a list: {suite['path']}")
            live_inputs: dict[str, str] = {}
            for row in suite_rows:
                if not isinstance(row, Mapping):
                    raise ValueError(f"invalid frozen suite row: {dataset}")
                canonical_path = Path(str(row.get("canonical_path", ""))).resolve()
                digest = row.get("canonical_sha256")
                circuit = canonical_path.stem
                if not _is_sha256(digest):
                    raise ValueError(f"invalid canonical digest: {dataset}/{circuit}")
                if (not canonical_path.is_file() or
                        sha256_file(canonical_path) != digest):
                    raise ValueError(
                        f"frozen canonical input drift: {dataset}/{circuit}")
                live_inputs[circuit] = str(digest)
            if live_inputs != suite["canonical_inputs"]:
                raise ValueError(f"frozen canonical inventory drift: {dataset}")
    return payload


def _resolved_main_config(freeze: Mapping[str, Any], method: str, seed: int,
                          destination: Path) -> Path:
    if method not in PAPER_OURS:
        return Path(freeze["configs"][method]["path"])
    source = Path(freeze["configs"][method]["path"])
    payload = copy.deepcopy(_read_json(source))
    setting = effective_zac_setting(payload)
    if int(setting.get("seed", 0)) == int(seed):
        # The seed-0 parity gate must exercise the byte-identical config hash
        # accepted by the old delivery, not merely equivalent JSON.
        _atomic_bytes(destination, source.read_bytes())
        return destination
    if "zac_setting" in payload:
        payload["zac_setting"][0]["seed"] = int(seed)
        validate_schema2_setting(payload["zac_setting"][0])
    else:
        payload["seed"] = int(seed)
        validate_schema2_setting(payload)
    _atomic_json(destination, payload)
    return destination


def _paper_experiment_id(freeze: Mapping[str, Any], dataset: str,
                         track: str, extra: Mapping[str, Any] | None = None) -> str:
    return stable_sha256({
        "protocol_id": PAPER_PROTOCOL_ID,
        "freeze_id": freeze["freeze_id"],
        "dataset": dataset,
        "track": track,
        "extra": dict(extra or {}),
    })


def paper_native_python_identity(
        python_path: str | Path, *, expected_abi: int = 9,
        ) -> Mapping[str, str]:
    """Fail closed unless an isolated interpreter loads the ABI9 extension."""
    # Keep the venv entry-point path itself.  Resolving its symlink to the base
    # interpreter would discard the virtual-environment prefix and could load
    # the accepted ABI8 site-packages instead of the isolated ABI9 wheel.
    executable = Path(os.path.abspath(
        os.fspath(Path(python_path).expanduser())))
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise FileNotFoundError(
            f"paper native Python is missing or not executable: {executable}")
    probe = (
        "import json,zac_native_core as n\n"
        "i=dict(n.build_info())\n"
        "print(json.dumps(dict(abi=int(n.NATIVE_ABI_VERSION), "
        "backend=str(i.get('backend','')), version=str(i.get('version','')), "
        "extension_path=str(n.__file__))))\n"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    try:
        raw = subprocess.check_output(
            [str(executable), "-c", probe], text=True,
            stderr=subprocess.STDOUT, env=environment).strip()
        value = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError,
            TypeError, ValueError) as error:
        details = getattr(error, "output", "") or str(error)
        raise RuntimeError(
            f"cannot verify paper native Python: {details}") from error
    if (value.get("abi") != expected_abi or
            value.get("backend") != f"cpp-native-v{expected_abi}" or
            value.get("version") != "0.5.33"):
        raise RuntimeError(
            "paper native Python does not load the registered ABI9 backend: "
            f"{value!r}")
    extension = Path(str(value.get("extension_path", ""))).resolve()
    if not extension.is_file():
        raise RuntimeError(
            f"paper native extension path is missing: {extension}")
    return {
        "paper_native_python_path": str(executable),
        "paper_native_python_sha256": sha256_file(executable),
        "paper_native_extension_path": str(extension),
        "paper_native_extension_sha256": sha256_file(extension),
        "paper_native_abi_version": str(expected_abi),
        "paper_native_backend": str(value["backend"]),
        "paper_native_version": str(value["version"]),
    }


def _paper_spec(
        plan: ExperimentPlan, freeze: Mapping[str, Any], dataset: str,
        canonical: CanonicalCircuitManifest, method: str, seed: int,
        repetition: int, run_kind: str, output_root: Path,
        config_path: Path, *, track: str, ablation_variant: str = "",
        search_policy: str = "ga", concurrency_limit: int = 1,
        native_python_identity: Mapping[str, str] | None = None,
        ) -> AttemptSpec:
    if search_policy not in {"ga", "greedy_only"}:
        raise ValueError(f"unknown paper search policy: {search_policy}")
    if search_policy != "ga" and run_kind != "ablation":
        raise ValueError("greedy_only is allowed only in the paper ablation track")
    spec = _attempt_spec(
        plan, plan.datasets[dataset], canonical, method, seed, repetition,
        run_kind, config_path=config_path,
        ablation_variant=ablation_variant,
        experiment_id=_paper_experiment_id(
            freeze, dataset, track,
            {"ablation_variant": ablation_variant,
             "search_policy": search_policy}),
    )
    command = list(spec.command)
    if run_kind == "ablation":
        if native_python_identity is None:
            raise ValueError(
                "paper ablation requires an audited ABI9 Python interpreter")
        command[0] = native_python_identity["paper_native_python_path"]
        command.extend(("--search-policy", search_policy))
    return replace(
        spec, output_root=output_root, command=command,
        concurrency_limit=concurrency_limit, require_clean_git=True,
        package_versions={
            **spec.package_versions,
            "paper_protocol_id": PAPER_PROTOCOL_ID,
            "paper_freeze_id": str(freeze["freeze_id"]),
            "paper_search_policy": search_policy,
            **dict(native_python_identity or {}),
        },
    )


def _existing_specs(root: Path) -> dict[tuple[Any, ...], tuple[RunManifest, Path]]:
    result: dict[tuple[Any, ...], tuple[RunManifest, Path]] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("manifest.json"), key=str):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        manifest = load_run_manifest(path)
        if manifest.status not in TERMINAL_STATUSES:
            raise ValueError(f"non-terminal paper attempt: {path}")
        key = (
            manifest.experiment_id, manifest.dataset, manifest.circuit,
            manifest.method, manifest.ablation_variant, manifest.seed,
            manifest.repetition, manifest.input_sha256, manifest.config_sha256,
            manifest.architecture_sha256, manifest.model_sha256,
        )
        if key in result:
            raise ValueError(
                f"duplicate paper attempt identity: {result[key][1]} and {path}")
        result[key] = (manifest, path.resolve())
    return result


def execute_paper_jobs(
        plan: ExperimentPlan, jobs: Sequence[PaperJob], *, workers: int,
        resume: bool, dry_run: bool,
        runner: Callable[..., RunManifest] = run_attempt,
        ) -> Mapping[str, Any]:
    """Execute jobs with one global heavy-job semaphore and exact resume."""
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("paper workers must be a positive integer")
    keys = [_spec_key(job.spec) for job in jobs]
    if len(keys) != len(set(keys)):
        raise ValueError("paper job matrix contains duplicate attempt identities")
    roots = sorted({Path(job.spec.output_root).resolve() for job in jobs}, key=str)
    existing: dict[tuple[Any, ...], tuple[RunManifest, Path]] = {}
    for root in roots:
        for key, value in _existing_specs(root).items():
            if key in existing:
                raise ValueError("paper run roots contain a duplicate attempt")
            existing[key] = value
    pending: list[PaperJob] = []
    skipped: list[Mapping[str, Any]] = []
    commands: list[Mapping[str, Any]] = []
    for job, key in zip(jobs, keys):
        row = {
            "dataset": job.spec.dataset,
            "circuit": job.spec.circuit,
            "method": job.spec.method,
            "seed": job.spec.seed,
            "repetition": job.spec.repetition,
            "ablation_variant": job.spec.ablation_variant,
            "historical_heavy": job.historical_heavy,
            "command": list(job.spec.command),
            "config": str(job.spec.config_path),
        }
        if key in existing:
            if not resume:
                raise FileExistsError(
                    "paper attempt already exists; use --resume: "
                    f"{existing[key][1]}")
            manifest, path = existing[key]
            skipped.append({**row, "status": manifest.status,
                            "manifest": str(path),
                            "manifest_sha256": sha256_file(path)})
        elif dry_run:
            commands.append(row)
        else:
            pending.append(job)

    heavy_gate = threading.Semaphore(1)

    def execute(job: PaperJob) -> tuple[RunManifest, Path, bool]:
        def invoke() -> RunManifest:
            gate = UnifiedEvaluationGate(plan, job.canonical, job.spec.method)
            return runner(job.spec, verifier=gate.verifier, scorer=gate.scorer)
        if job.historical_heavy:
            with heavy_gate:
                manifest = invoke()
        else:
            manifest = invoke()
        return manifest, _manifest_path(manifest), job.historical_heavy

    if dry_run or not pending:
        completed: list[tuple[RunManifest, Path, bool]] = []
    elif workers == 1:
        completed = [execute(job) for job in pending]
    else:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="paper-zh") as executor:
            completed = list(executor.map(execute, pending))

    attempted = []
    for manifest, path, heavy in completed:
        attempted.append({
            "dataset": manifest.dataset, "circuit": manifest.circuit,
            "method": manifest.method, "seed": manifest.seed,
            "repetition": manifest.repetition,
            "ablation_variant": manifest.ablation_variant,
            "historical_heavy": heavy, "status": manifest.status,
            "manifest": str(path), "manifest_sha256": sha256_file(path),
        })
    return {
        "planned": len(jobs),
        "workers": workers,
        "maximum_simultaneous_heavy": 1,
        "attempted": attempted,
        "skipped_existing": skipped,
        "commands": commands,
        "status_counts": dict(sorted(Counter(
            row["status"] for row in attempted).items())),
    }


def _sha256_uncompressed(path: Path) -> str:
    digest = hashlib.sha256()
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_gzip_json(path: Path) -> Mapping[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _decision_mapping_projection(stats: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    projection = []
    decisions = stats.get("decision_log", [])
    if not isinstance(decisions, list):
        raise ValueError("compiler stats decision_log is not a list")
    for row in decisions:
        if not isinstance(row, Mapping):
            raise ValueError("compiler stats decision row is not an object")
        rich = row.get("rich_search", {})
        production = row.get("production_candidate", {})
        projection.append({
            "layer": row.get("layer"),
            "stay": row.get("stay"),
            "return": row.get("return"),
            "reseat": row.get("reseat"),
            "selected_horizon": row.get("selected_horizon"),
            "return_assignments": row.get("return_assignments", []),
            "participant_parking_assignments":
                row.get("participant_parking_assignments", []),
            "current_gate_anchor_assignment_site_ids": (
                rich.get("current_gate_anchor_assignment_site_ids", [])
                if isinstance(rich, Mapping) else []),
            "current_gate_final_assignment_site_ids": (
                rich.get("current_gate_final_assignment_site_ids", [])
                if isinstance(rich, Mapping) else []),
            "source_back_batches": (
                production.get("source_back_batches", [])
                if isinstance(production, Mapping) else []),
            "target_out_batches": (
                production.get("target_out_batches", [])
                if isinstance(production, Mapping) else []),
        })
    return projection


def _run_signature(manifest_path: str | Path) -> Mapping[str, Any]:
    path = Path(manifest_path).resolve()
    manifest = load_run_manifest(path)
    directory = path.parent
    canonical = directory / "canonical_trace.jsonl.gz"
    stats_path = directory / "compiler_stats.json.gz"
    if not canonical.is_file() or not stats_path.is_file():
        raise FileNotFoundError(
            f"parity evidence lacks retained trace/stats: {directory}")
    stats = _load_gzip_json(stats_path)
    return {
        "status": manifest.status,
        "event_stream_sha256": _sha256_uncompressed(canonical),
        "mapping_sha256": stable_sha256(_decision_mapping_projection(stats)),
        "fidelity_hex": (None if manifest.fidelity is None
                         else float(manifest.fidelity).hex()),
        "move_batches": manifest.move_batches,
        "move_time_us_hex": (None if manifest.move_time_us is None
                             else float(manifest.move_time_us).hex()),
        "stay_count": manifest.stay_count,
        "return_count": manifest.return_count,
        "reseat_count": manifest.reseat_count,
        "ghost_hits": manifest.ghost_hits,
        "gate_ledger_sha256": manifest.observed_gate_ledger_sha256,
    }


def compare_seed0_parity(
        freeze: Mapping[str, Any], parity_rows: Sequence[Mapping[str, Any]],
        ) -> Mapping[str, Any]:
    source = _read_json(freeze["seed0_source_manifest"]["path"])
    new_paths: dict[tuple[str, str, str], Path] = {}
    for row in parity_rows:
        key = (str(row["dataset"]), str(row["circuit"]), str(row["method"]))
        if key in new_paths:
            raise ValueError(f"duplicate parity result: {key}")
        new_paths[key] = Path(str(row["manifest"])).resolve()
    expected = {
        (dataset, circuit, method)
        for dataset, circuit in freeze["parity_timing_cohort"]["identities"]
        for method in PAPER_OURS
    }
    if set(new_paths) != expected:
        raise ValueError(
            f"parity result set incomplete: expected={len(expected)}, "
            f"found={len(new_paths)}")
    comparisons = []
    passed = True
    for dataset, circuit, method in sorted(expected):
        old_row = source[method][f"{dataset}/{circuit}"]
        old_path = Path(old_row["path"]).resolve()
        if sha256_file(old_path) != old_row["sha256"]:
            raise ValueError(f"old seed0 evidence drift: {method}/{dataset}/{circuit}")
        try:
            old_signature = _run_signature(old_path)
            new_signature = _run_signature(new_paths[(dataset, circuit, method)])
            differences = {
                key: [old_signature.get(key), new_signature.get(key)]
                for key in sorted(set(old_signature) | set(new_signature))
                if old_signature.get(key) != new_signature.get(key)
            }
        except (OSError, ValueError) as error:
            old_signature = {}
            new_signature = {}
            differences = {"evidence_error": [None, f"{type(error).__name__}: {error}"]}
        row_passed = not differences
        passed = passed and row_passed
        comparisons.append({
            "dataset": dataset, "circuit": circuit, "method": method,
            "passed": row_passed, "differences": differences,
            "old_manifest": str(old_path),
            "new_manifest": str(new_paths[(dataset, circuit, method)]),
            "old_signature": old_signature,
            "new_signature": new_signature,
        })
    return {
        "protocol_id": PAPER_PROTOCOL_ID,
        "freeze_id": freeze["freeze_id"],
        "status": "passed" if passed else "failed",
        "passed": passed,
        "comparisons": comparisons,
        "compared": len(comparisons),
    }


def _canonical_lookup(suites: Mapping[str, Sequence[CanonicalCircuitManifest]]
                      ) -> dict[tuple[str, str], CanonicalCircuitManifest]:
    return {(dataset, _circuit_id(row)): row
            for dataset, rows in suites.items() for row in rows}


def _historical_heavy(freeze: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    return {tuple(str(value) for value in row)
            for row in freeze["seed0_source_manifest"]["historical_heavy"]}


def _rows_from_execution(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(report["attempted"]) + list(report["skipped_existing"])


def command_run_paper_main(
        plan: ExperimentPlan, freeze_path: str | Path, *,
        output_root: str | Path, workers: int = PAPER_WORKERS,
        resume: bool = False, dry_run: bool = False,
        parity_only: bool = False) -> Mapping[str, Any]:
    """Run parity first, then seeds 1/2 and conditional full seed0."""
    freeze = load_paper_freeze(freeze_path)
    root = Path(output_root).resolve()
    suites = _suite_map(plan)
    lookup = _canonical_lookup(suites)
    heavy = _historical_heavy(freeze)
    config_paths = {
        (method, seed): _resolved_main_config(
            freeze, method, seed,
            root / "configs" / "main" / f"{method}-seed{seed}.json")
        for method in PAPER_OURS for seed in PAPER_MAIN_SEEDS
    }

    parity_jobs: list[PaperJob] = []
    for dataset, circuit in freeze["parity_timing_cohort"]["identities"]:
        canonical = lookup[(dataset, circuit)]
        for method in PAPER_OURS:
            spec = _paper_spec(
                plan, freeze, dataset, canonical, method, 0, 0, "main",
                root / "runs" / "parity" / dataset,
                config_paths[(method, 0)], track="seed0-parity",
                concurrency_limit=workers)
            parity_jobs.append(PaperJob(
                spec, canonical, (dataset, circuit, method) in heavy))
    parity_execution = execute_paper_jobs(
        plan, parity_jobs, workers=workers, resume=resume,
        dry_run=dry_run)
    if dry_run:
        parity = {
            "status": "dry_run", "passed": None,
            "contingent_full_seed0_attempts": 344,
        }
    else:
        parity = compare_seed0_parity(
            freeze, _rows_from_execution(parity_execution))
        _atomic_json(root / "reports" / "seed0_parity.json", parity)
    if parity_only:
        report = {
            "protocol_id": PAPER_PROTOCOL_ID,
            "freeze_id": freeze["freeze_id"],
            "phase": "main-parity", "parity": parity,
            "parity_execution": parity_execution,
        }
        if not dry_run:
            _atomic_json(root / "reports" / "run-paper-main-parity.json", report)
        return report

    accepted_old_seed0 = parity.get("passed") is True
    seeds = PAPER_NEW_SEEDS if accepted_old_seed0 else PAPER_MAIN_SEEDS
    main_jobs: list[PaperJob] = []
    for dataset in ("zac18", "qmap154"):
        for canonical in suites[dataset]:
            circuit = _circuit_id(canonical)
            for method in PAPER_OURS:
                for seed in seeds:
                    spec = _paper_spec(
                        plan, freeze, dataset, canonical, method, seed, 0,
                        "main", root / "runs" / "main" / dataset,
                        config_paths[(method, seed)], track="paper-main",
                        concurrency_limit=workers)
                    main_jobs.append(PaperJob(
                        spec, canonical,
                        (dataset, circuit, method) in heavy))
    main_execution = execute_paper_jobs(
        plan, main_jobs, workers=workers, resume=resume, dry_run=dry_run)

    source = _read_json(freeze["seed0_source_manifest"]["path"])
    ours_rows = _rows_from_execution(main_execution)
    ours_index = {
        (row["dataset"], row["circuit"], row["method"], int(row["seed"])): {
            "path": row["manifest"], "sha256": row["manifest_sha256"],
            "status": row["status"],
        }
        for row in ours_rows if "manifest" in row
    }
    quality_sources: dict[str, Any] = {
        "protocol_id": PAPER_PROTOCOL_ID,
        "freeze_id": freeze["freeze_id"],
        "accepted_old_seed0": accepted_old_seed0,
        "parity_report": str((root / "reports" / "seed0_parity.json").resolve()),
        "baselines": {method: source[method] for method in ("M1", "M2")},
        "ours": {},
    }
    if not dry_run:
        for method in PAPER_OURS:
            method_rows: dict[str, Any] = {}
            for dataset in ("zac18", "qmap154"):
                for canonical in suites[dataset]:
                    circuit = _circuit_id(canonical)
                    identity = f"{dataset}/{circuit}"
                    seeds_payload: dict[str, Any] = {}
                    if accepted_old_seed0:
                        seeds_payload["0"] = source[method][identity]
                    else:
                        seeds_payload["0"] = ours_index[(dataset, circuit, method, 0)]
                    for seed in PAPER_NEW_SEEDS:
                        seeds_payload[str(seed)] = ours_index[(dataset, circuit, method, seed)]
                    method_rows[identity] = seeds_payload
            quality_sources["ours"][method] = method_rows
        _atomic_json(root / "quality_source_manifest.json", quality_sources)
    report = {
        "protocol_id": PAPER_PROTOCOL_ID,
        "freeze_id": freeze["freeze_id"],
        "phase": "paper-main", "dry_run": dry_run,
        "accepted_old_seed0": accepted_old_seed0,
        "new_seed0_required": not accepted_old_seed0,
        "parity": parity, "parity_execution": parity_execution,
        "main_execution": main_execution,
        "quality_source_manifest": str(
            (root / "quality_source_manifest.json").resolve()),
    }
    if not dry_run:
        _atomic_json(root / "reports" / "run-paper-main.json", report)
    return report


def _set_setting(payload: dict[str, Any], **updates: Any) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    setting = (result["zac_setting"][0]
               if "zac_setting" in result else result)
    for key, value in updates.items():
        setting[key] = copy.deepcopy(value)
    try:
        validate_schema2_setting(setting)
    except ValueError:
        # H=2/H=4 and ABI9 are registered only inside paper ablation wrappers.
        # Validate every other field against the accepted ABI8/H8 contract,
        # then let method_driver/runner bind the wrapper's explicit ABI/depth.
        # No main-table configuration uses this narrow exception.
        horizon = setting.get("lookahead_horizon")
        validation_copy = copy.deepcopy(setting)
        changed = False
        if (setting.get("method_id") == "ours_lk" and
                isinstance(horizon, Mapping) and
                horizon.get("max_horizon") in {2, 4}):
            validation_copy["lookahead_horizon"]["max_horizon"] = 8
            changed = True
        if setting.get("native_abi_version") == 9:
            if not _is_sha256(setting.get("native_wheel_sha256")):
                raise ValueError("ABI9 paper config lacks a native wheel SHA256")
            validation_copy["native_abi_version"] = 8
            validation_copy["native_wheel_sha256"] = (
                "b9109e6556e032a8c148b33c9ca1f213eac4226c6801182e67e8ef4a577786b9")
            changed = True
        if not changed:
            raise
        validate_schema2_setting(validation_copy)
    return result


def _validate_paper_native_setting(setting: Mapping[str, Any]) -> None:
    """Reuse the frozen Schema-2 contract for a registered ABI9 paper base."""
    validation_copy = copy.deepcopy(dict(setting))
    if validation_copy.get("native_abi_version") == 9:
        if not _is_sha256(validation_copy.get("native_wheel_sha256")):
            raise ValueError("ABI9 paper config lacks a native wheel SHA256")
        validation_copy["native_abi_version"] = 8
    validate_schema2_setting(validation_copy)


def build_shared_lookahead_configs(
        m4_payload: Mapping[str, Any], *, native_abi_version: int,
        native_wheel_sha256: str, seed: int,
        ) -> tuple[dict[str, Any], dict[str, Any], Mapping[str, Any]]:
    """Build a causal H0/H8 pair from one M4 base and audit the exact diff."""
    if native_abi_version != 9:
        raise ValueError("paper controlled ablations require native ABI9")
    if not _is_sha256(native_wheel_sha256):
        raise ValueError("native wheel digest must be a SHA256")
    base = copy.deepcopy(dict(m4_payload))
    base_setting = effective_zac_setting(base)
    base_horizon = copy.deepcopy(base_setting["lookahead_horizon"])
    if not isinstance(base_horizon, Mapping):
        raise ValueError("paper lookahead base must use a decay horizon object")
    h8_horizon = {**base_horizon, "rho": 0.7, "max_horizon": 8}
    common = {
        "alpha_lookahead": 0.5,
        "native_abi_version": int(native_abi_version),
        "native_wheel_sha256": native_wheel_sha256,
        "seed": int(seed),
        "lookahead_horizon": h8_horizon,
    }
    h8 = _set_setting(base, **common)
    h8_setting = effective_zac_setting(h8)
    h8_setting["method_id"] = "ours_lk"
    h8_setting["dir"] = "results/paper_zh_v1/ablation/h8/"
    if "zac_setting" in h8:
        h8["zac_setting"][0] = h8_setting
    else:
        h8 = h8_setting
    _validate_paper_native_setting(h8_setting)

    h0 = copy.deepcopy(h8)
    h0_setting = effective_zac_setting(h0)
    h0_setting["method_id"] = "ours_nl"
    h0_setting["dir"] = "results/paper_zh_v1/ablation/h0/"
    h0_setting["lookahead_horizon"] = {
        **copy.deepcopy(h8_setting["lookahead_horizon"]),
        "max_horizon": 0,
    }
    if "zac_setting" in h0:
        h0["zac_setting"][0] = h0_setting
    else:
        h0 = h0_setting
    _validate_paper_native_setting(h0_setting)

    ignored = {"method_id", "dir", "lookahead_horizon"}
    left = {key: value for key, value in h0_setting.items() if key not in ignored}
    right = {key: value for key, value in h8_setting.items() if key not in ignored}
    if left != right:
        differences = {key: [left.get(key), right.get(key)]
                       for key in sorted(set(left) | set(right))
                       if left.get(key) != right.get(key)}
        raise ValueError(f"shared H0/H8 config drift: {differences}")
    diff = {
        key: [h0_setting.get(key), h8_setting.get(key)]
        for key in sorted(set(h0_setting) | set(h8_setting))
        if h0_setting.get(key) != h8_setting.get(key)
    }
    if set(diff) != ignored:
        raise ValueError(f"unexpected shared H0/H8 differences: {diff}")
    return h0, h8, {
        "allowed_differences": sorted(ignored),
        "differences": diff,
        "common_sha256": stable_sha256(left),
    }


def _paper_ablation_wrapper(base: Mapping[str, Any], *, method: str,
                             variant: str, horizon: int,
                             search_policy: str) -> dict[str, Any]:
    if method not in PAPER_OURS or search_policy not in {"ga", "greedy_only"}:
        raise ValueError("invalid paper ablation wrapper")
    return {
        "experiment_schema": 2,
        "run_kind": "ablation",
        "ablation_protocol": 2,
        "ablation_variant": variant,
        "base_method": method,
        "base_config": copy.deepcopy(dict(base)),
        "controls": {
            "lookahead_horizon": int(horizon),
            "decision_policy": "optimize",
            "fitness_phase_mode": "phase",
            "routing_batcher": "coloring",
            "search_policy": search_policy,
        },
        "search_policy": search_policy,
    }


def command_run_paper_ablation(
        plan: ExperimentPlan, freeze_path: str | Path, *,
        output_root: str | Path, abi9_wheel: str | Path,
        native_python: str | Path,
        workers: int = PAPER_WORKERS, resume: bool = False,
        dry_run: bool = False,
        components: Sequence[str] = ("lookahead", "greedy"),
        ) -> Mapping[str, Any]:
    freeze = load_paper_freeze(freeze_path)
    requested = tuple(dict.fromkeys(components))
    if not requested or set(requested) - {"lookahead", "greedy"}:
        raise ValueError(f"invalid paper ablation components: {requested}")
    wheel = Path(abi9_wheel).resolve()
    if not wheel.is_file():
        raise FileNotFoundError(f"ABI9 wheel is missing: {wheel}")
    wheel_sha = sha256_file(wheel)
    native_identity = paper_native_python_identity(native_python)
    root = Path(output_root).resolve()
    suites = _suite_map(plan)
    cohort, cohort_report = select_paper_ablation(suites)
    heavy = _historical_heavy(freeze)
    base = _read_json(freeze["configs"]["M4"]["path"])

    configs: dict[tuple[str, int], Path] = {}
    diff_reports: dict[str, Any] = {}
    for seed in PAPER_MAIN_SEEDS:
        h0, h8, diff = build_shared_lookahead_configs(
            base, native_abi_version=9,
            native_wheel_sha256=wheel_sha, seed=seed)
        diff_reports[str(seed)] = diff
        wrappers = {
            "h0": _paper_ablation_wrapper(
                h0, method="M3", variant=PAPER_ABLATION_VARIANTS["h0"],
                horizon=0, search_policy="ga"),
            "h8": _paper_ablation_wrapper(
                h8, method="M4", variant=PAPER_ABLATION_VARIANTS["h8"],
                horizon=8, search_policy="ga"),
            "greedy": _paper_ablation_wrapper(
                h8, method="M4", variant=PAPER_ABLATION_VARIANTS["greedy"],
                horizon=8, search_policy="greedy_only"),
        }
        for name, payload in wrappers.items():
            if name == "greedy" and seed != 0:
                continue
            destination = root / "configs" / "ablation" / f"{name}-seed{seed}.json"
            _atomic_json(destination, payload)
            configs[(name, seed)] = destination
    _atomic_json(root / "reports" / "shared_config_diff.json", {
        "protocol_id": PAPER_PROTOCOL_ID,
        "freeze_id": freeze["freeze_id"],
        "abi9_wheel": {"path": str(wheel), "sha256": wheel_sha},
        "seeds": diff_reports,
    })

    jobs: list[PaperJob] = []
    for dataset, canonical in cohort:
        circuit = _circuit_id(canonical)
        if "lookahead" in requested:
            for name, method in (("h0", "M3"), ("h8", "M4")):
                for seed in PAPER_MAIN_SEEDS:
                    spec = _paper_spec(
                        plan, freeze, dataset, canonical, method, seed, 0,
                        "ablation", root / "runs" / "ablation" / dataset,
                        configs[(name, seed)], track="paper-ablation",
                        ablation_variant=PAPER_ABLATION_VARIANTS[name],
                        search_policy="ga", concurrency_limit=workers,
                        native_python_identity=native_identity)
                    jobs.append(PaperJob(
                        spec, canonical, (dataset, circuit, method) in heavy))
        if "greedy" in requested:
            spec = _paper_spec(
                plan, freeze, dataset, canonical, "M4", 0, 0,
                "ablation", root / "runs" / "ablation" / dataset,
                configs[("greedy", 0)], track="paper-ablation",
                ablation_variant=PAPER_ABLATION_VARIANTS["greedy"],
                search_policy="greedy_only", concurrency_limit=workers,
                native_python_identity=native_identity)
            jobs.append(PaperJob(
                spec, canonical, (dataset, circuit, "M4") in heavy))
    execution = execute_paper_jobs(
        plan, jobs, workers=workers, resume=resume, dry_run=dry_run)
    report = {
        "protocol_id": PAPER_PROTOCOL_ID,
        "freeze_id": freeze["freeze_id"],
        "phase": "paper-ablation", "dry_run": dry_run,
        "components": list(requested), "cohort": cohort_report,
        "abi9_wheel": {"path": str(wheel), "sha256": wheel_sha},
        "native_python": dict(native_identity),
        "shared_config_diff": str(
            (root / "reports" / "shared_config_diff.json").resolve()),
        "execution": execution,
    }
    if not dry_run:
        _atomic_json(root / "reports" / "run-paper-ablation.json", report)
    return report


SENSITIVITY_PROFILE_IDS = (
    "default", "budget_192", "budget_1152", "return_4_2",
    "return_10_8", "horizon_2", "horizon_4",
    "decay_0p2_0p5", "decay_0p35_0p6",
)


def build_sensitivity_configs(
        m4_payload: Mapping[str, Any], *, native_abi_version: int,
        native_wheel_sha256: str) -> Mapping[str, dict[str, Any]]:
    """Return the nine unique settings in the registered one-factor design."""
    if native_abi_version != 9:
        raise ValueError("paper sensitivity requires native ABI9")
    base = _set_setting(
        copy.deepcopy(dict(m4_payload)), seed=0,
        native_abi_version=int(native_abi_version),
        native_wheel_sha256=native_wheel_sha256,
        alpha_lookahead=0.5,
        max_unique_evaluations=576,
        return_candidate_limit=6,
        return_assignment_k=4,
    )
    setting = effective_zac_setting(base)
    base_horizon = {**copy.deepcopy(setting["lookahead_horizon"]),
                    "rho": 0.7, "max_horizon": 8}
    base = _set_setting(base, lookahead_horizon=base_horizon)

    configs = {
        "default": base,
        "budget_192": _set_setting(base, max_unique_evaluations=192),
        "budget_1152": _set_setting(base, max_unique_evaluations=1152),
        "return_4_2": _set_setting(
            base, return_candidate_limit=4, return_assignment_k=2),
        "return_10_8": _set_setting(
            base, return_candidate_limit=10, return_assignment_k=8),
        "horizon_2": _set_setting(
            base, lookahead_horizon={**base_horizon, "max_horizon": 2}),
        "horizon_4": _set_setting(
            base, lookahead_horizon={**base_horizon, "max_horizon": 4}),
        "decay_0p2_0p5": _set_setting(
            base, alpha_lookahead=0.2,
            lookahead_horizon={**base_horizon, "rho": 0.5}),
        "decay_0p35_0p6": _set_setting(
            base, alpha_lookahead=0.35,
            lookahead_horizon={**base_horizon, "rho": 0.6}),
    }
    if tuple(configs) != SENSITIVITY_PROFILE_IDS:
        raise AssertionError("sensitivity profile order drift")
    digests = [stable_sha256(value) for value in configs.values()]
    if len(digests) != len(set(digests)):
        raise ValueError("registered sensitivity settings are not unique")
    return configs


def command_run_paper_sensitivity(
        plan: ExperimentPlan, freeze_path: str | Path, *,
        output_root: str | Path, native_wheel: str | Path,
        native_abi_version: int, native_python: str | Path,
        workers: int = PAPER_WORKERS,
        resume: bool = False, dry_run: bool = False) -> Mapping[str, Any]:
    freeze = load_paper_freeze(freeze_path)
    wheel = Path(native_wheel).resolve()
    if not wheel.is_file():
        raise FileNotFoundError(f"native wheel is missing: {wheel}")
    wheel_sha = sha256_file(wheel)
    native_identity = paper_native_python_identity(native_python)
    root = Path(output_root).resolve()
    suites = _suite_map(plan)
    cohort, cohort_report = select_paper_twelve(suites)
    heavy = _historical_heavy(freeze)
    configs = build_sensitivity_configs(
        _read_json(freeze["configs"]["M4"]["path"]),
        native_abi_version=native_abi_version,
        native_wheel_sha256=wheel_sha)
    config_paths: dict[str, Path] = {}
    for profile, payload in configs.items():
        destination = root / "configs" / "sensitivity" / f"{profile}.json"
        _atomic_json(destination, payload)
        config_paths[profile] = destination

    jobs = []
    for dataset, canonical in cohort:
        circuit = _circuit_id(canonical)
        for profile in SENSITIVITY_PROFILE_IDS:
            variant = f"paper_sensitivity_{profile}"
            wrapper = _paper_ablation_wrapper(
                configs[profile], method="M4", variant=variant,
                horizon=int(effective_zac_setting(
                    configs[profile])["lookahead_horizon"]["max_horizon"]),
                search_policy="ga")
            wrapper_path = (root / "configs" / "sensitivity-wrappers" /
                            f"{profile}.json")
            if not wrapper_path.exists():
                _atomic_json(wrapper_path, wrapper)
            spec = _paper_spec(
                plan, freeze, dataset, canonical, "M4", 0, 0,
                "ablation", root / "runs" / "sensitivity" / dataset,
                wrapper_path, track="paper-sensitivity",
                ablation_variant=variant, search_policy="ga",
                concurrency_limit=workers,
                native_python_identity=native_identity)
            jobs.append(PaperJob(
                spec, canonical, (dataset, circuit, "M4") in heavy))
    execution = execute_paper_jobs(
        plan, jobs, workers=workers, resume=resume, dry_run=dry_run)
    report = {
        "protocol_id": PAPER_PROTOCOL_ID,
        "freeze_id": freeze["freeze_id"],
        "phase": "paper-sensitivity", "dry_run": dry_run,
        "cohort": cohort_report,
        "profiles": list(SENSITIVITY_PROFILE_IDS),
        "native_wheel": {"path": str(wheel), "sha256": wheel_sha,
                         "abi": int(native_abi_version)},
        "native_python": dict(native_identity),
        "execution": execution,
    }
    if not dry_run:
        _atomic_json(root / "reports" / "run-paper-sensitivity.json", report)
    return report


def command_run_paper_timing(
        plan: ExperimentPlan, freeze_path: str | Path, *,
        output_root: str | Path, resume: bool = False,
        dry_run: bool = False,
        schedule_seed: int = PAPER_TIMING_SCHEDULE_SEED) -> Mapping[str, Any]:
    """Run 8 excluded warmups then the fixed serial 144-attempt schedule."""
    freeze = load_paper_freeze(freeze_path)
    root = Path(output_root).resolve()
    suites = _suite_map(plan)
    cohort, cohort_report = select_paper_twelve(suites)
    by_dataset: dict[str, list[CanonicalCircuitManifest]] = {
        "zac18": [], "qmap154": []}
    for dataset, canonical in cohort:
        by_dataset[dataset].append(canonical)
    configs = {
        method: (Path(freeze["configs"][method]["path"])
                 if method in ("M1", "M2") else
                 _resolved_main_config(
                     freeze, method, 0,
                     root / "configs" / "timing" / f"{method}-seed0.json"))
        for method in PAPER_METHODS
    }

    warmup_jobs: list[PaperJob] = []
    for dataset in ("zac18", "qmap154"):
        canonical = min(
            by_dataset[dataset],
            key=lambda row: (row.gates_1q + row.gates_2q,
                             row.canonical_sha256))
        for method in PAPER_METHODS:
            spec = _paper_spec(
                plan, freeze, dataset, canonical, method, 0, 0, "timing",
                root / "runs" / "timing-warmup" / dataset,
                configs[method], track="paper-timing-warmup")
            warmup_jobs.append(PaperJob(spec, canonical, False))
    warmup = execute_paper_jobs(
        plan, warmup_jobs, workers=1, resume=resume, dry_run=dry_run)

    matrix: list[tuple[str, CanonicalCircuitManifest, str, int]] = []
    for dataset, canonical in cohort:
        for method in PAPER_METHODS:
            for repetition in range(PAPER_TIMING_REPETITIONS):
                matrix.append((dataset, canonical, method, repetition))
    random.Random(int(schedule_seed)).shuffle(matrix)
    schedule = [
        {
            "order": order, "dataset": dataset,
            "circuit": _circuit_id(canonical), "method": method,
            "repetition": repetition,
        }
        for order, (dataset, canonical, method, repetition)
        in enumerate(matrix)
    ]
    schedule_sha = stable_sha256(schedule)
    jobs: list[PaperJob] = []
    for order, (dataset, canonical, method, repetition) in enumerate(matrix):
        spec = _paper_spec(
            plan, freeze, dataset, canonical, method, 0, repetition,
            "timing", root / "runs" / "timing" / dataset,
            configs[method], track="paper-timing",
            concurrency_limit=1)
        spec = replace(spec, package_versions={
            **spec.package_versions,
            "paper_timing_schedule_sha256": schedule_sha,
            "paper_timing_schedule_order": str(order),
        })
        jobs.append(PaperJob(spec, canonical, False))
    execution = execute_paper_jobs(
        plan, jobs, workers=1, resume=resume, dry_run=dry_run)
    report = {
        "protocol_id": PAPER_PROTOCOL_ID,
        "freeze_id": freeze["freeze_id"],
        "phase": "paper-timing", "dry_run": dry_run,
        "cohort": cohort_report,
        "schedule_seed": int(schedule_seed),
        "schedule_sha256": schedule_sha,
        "schedule": schedule,
        "warmup": warmup,
        "execution": execution,
    }
    if not dry_run:
        _atomic_json(root / "reports" / "run-paper-timing.json", report)
    return report


__all__ = [
    "PAPER_ABLATION_VARIANTS", "PAPER_MAIN_SEEDS", "PAPER_PROTOCOL_ID",
    "PAPER_TIMING_REPETITIONS", "PAPER_WORKERS", "PaperJob",
    "SENSITIVITY_PROFILE_IDS", "build_sensitivity_configs",
    "build_shared_lookahead_configs", "command_paper_freeze",
    "command_run_paper_ablation", "command_run_paper_main",
    "command_run_paper_sensitivity", "command_run_paper_timing",
    "compare_seed0_parity", "execute_paper_jobs", "load_paper_freeze",
    "paper_native_python_identity", "select_paper_ablation",
    "select_paper_twelve",
]
