"""Strict Schema-2 experiment plans for the four registered methods.

The plan is the only orchestration input accepted by :mod:`experiments_v2.cli`.
Paths are resolved relative to the plan file, method configurations are loaded
before any compiler is started, and the M3/M4 fairness contract is checked on
their effective ``zac_setting`` objects.

A minimal plan has this shape::

    {
      "experiment_schema": 2,
      "repo_root": "../..",
      "output_root": "results/schema2",
      "architecture": "hardware_spec/full_architecture.json",
      "model": "exp_setting/fidelity_model_v2.json",
      "methods": {
        "M1": {"config": "exp_setting/zac_v2.json"},
        "M2": {"config": "exp_setting/iccad_v2.json", "python": ".venv-qmap/bin/python"},
        "M3": {"config": "exp_setting/ours_nl_v2.json"},
        "M4": {"config": "exp_setting/ours_lk_v2.json"}
      },
      "datasets": {
        "zac": {
          "kind": "main",
          "sources": ["benchmark/hpca"],
          "canonical_directory": "canonical/zac"
        },
        "large": {
          "kind": "large",
          "sources": ["benchmark/large"],
          "canonical_directory": "canonical/large"
        }
      }
    }

The fidelity-model file may either contain the nine model fields directly or
wrap them as ``{"experiment_schema": 2, "model": {...}}``.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

from evaluation import FidelityModel
from zzx.algorithm_v2 import (
    FORMAL_NATIVE_TUNING_PROTOCOL_ID,
    validate_schema2_pair,
    validate_schema2_setting,
)

from .contracts import (CanonicalCircuitManifest, SCHEMA_VERSION,
                        repository_snapshot, sha256_file, stable_sha256)
from .method_driver import M2_FROZEN_CONFIG


METHODS = ("M1", "M2", "M3", "M4")
MAIN_METHODS = frozenset(METHODS)
DATASET_KINDS = frozenset(("main", "large"))
QASMBENCH_LARGE_COMMIT = "357b942396d5c2b7cbc1c229c585a6ef5ccaebac"
def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _reject_unknown(value: Mapping[str, Any], allowed: Iterable[str], label: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(f"unknown {label} fields: {unknown}")


def _path(base: Path, value: str | os.PathLike[str]) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _executable(base: Path, value: str | os.PathLike[str]) -> str:
    text = os.fspath(value)
    if os.sep in text or (os.altsep is not None and os.altsep in text):
        # Do not call Path.resolve() here.  A virtual environment's ``python``
        # is normally a symlink to the base interpreter; resolving it silently
        # drops the venv identity and launches the compiler without the frozen
        # packages.  ``abspath`` normalizes a relative executable path while
        # deliberately preserving the final symlink.
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        return os.path.abspath(os.fspath(candidate))
    return text


def effective_zac_setting(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the one effective setting accepted by ``method_driver``."""
    if "zac_setting" not in payload:
        return dict(payload)
    settings = payload["zac_setting"]
    if not isinstance(settings, list) or len(settings) != 1 or not isinstance(settings[0], Mapping):
        raise ValueError("Schema-2 method config must contain exactly one zac_setting")
    return dict(settings[0])


def _validate_method_config(method: str, payload: Mapping[str, Any]) -> None:
    setting = effective_zac_setting(payload)
    if setting.get("experiment_schema") != SCHEMA_VERSION:
        raise ValueError(f"{method} config is not Schema 2")
    if method == "M1":
        expected = {"experiment_schema": 2, "method_id": "M1"}
        if setting != expected:
            raise ValueError(
                "M1 config must contain only experiment_schema=2 and "
                "method_id=M1")
    elif method == "M2":
        if setting != M2_FROZEN_CONFIG:
            differences = {
                key: (setting.get(key), M2_FROZEN_CONFIG.get(key))
                for key in sorted(set(setting) | set(M2_FROZEN_CONFIG))
                if setting.get(key) != M2_FROZEN_CONFIG.get(key)
            }
            raise ValueError(f"M2 frozen routing-aware configuration mismatch: {differences}")
    else:
        validate_schema2_setting(setting)
        expected = "ours_nl" if method == "M3" else "ours_lk"
        if setting.get("method_id") != expected:
            raise ValueError(f"{method} config requires method_id={expected}")


def _validate_pair_payloads(m3: Mapping[str, Any], m4: Mapping[str, Any]) -> None:
    left, right = effective_zac_setting(m3), effective_zac_setting(m4)
    outer_tuning = []
    for method, payload in (("M3", m3), ("M4", m4)):
        tuning = payload.get("tuning", {})
        if not isinstance(tuning, Mapping):
            raise ValueError(f"{method} tuning metadata must be an object")
        outer_tuning.append(tuning)
    tracks = {str(value.get("track", "shared")) for value in outer_tuning}
    if len(tracks) != 1:
        raise ValueError("M3/M4 cannot mix shared and independent tuning tracks")
    track = tracks.pop()
    if track == "shared":
        validate_schema2_pair(left, right)
    elif track == "independent":
        # The quality-racing main table intentionally uses each method's own
        # selected configuration.  Causal lookahead evidence is produced by a
        # separate shared-forward pair, so the formal main plan validates each
        # method independently instead of pretending the knobs are identical.
        validate_schema2_setting(left)
        validate_schema2_setting(right)
        if left.get("method_id") != "ours_nl" or \
                right.get("method_id") != "ours_lk":
            raise ValueError("independent main track requires ours_nl/ours_lk")
        for method, tuning in (("M3", outer_tuning[0]),
                               ("M4", outer_tuning[1])):
            if tuning.get("protocol_id") != FORMAL_NATIVE_TUNING_PROTOCOL_ID:
                raise ValueError(
                    f"{method} independent track has wrong tuning protocol")
            if not isinstance(tuning.get("candidate_id"), str) or not \
                    tuning["candidate_id"]:
                raise ValueError(
                    f"{method} independent track lacks selected candidate id")
    else:
        raise ValueError(f"unknown M3/M4 tuning track: {track!r}")

    # Wrapper metadata cannot become a hidden method difference.  Only the
    # effective setting may differ.  Shared configs retain the strict wrapper
    # equality check; independent configs carry distinct candidate identities.
    if "zac_setting" not in m3 and "zac_setting" not in m4:
        return
    if ("zac_setting" in m3) != ("zac_setting" in m4):
        raise ValueError("M3/M4 must use the same flat or wrapped config shape")
    outer_left = {key: value for key, value in m3.items() if key != "zac_setting"}
    outer_right = {key: value for key, value in m4.items() if key != "zac_setting"}
    if track == "independent":
        outer_left.pop("tuning", None)
        outer_right.pop("tuning", None)
    if outer_left != outer_right:
        raise ValueError(
            "M3/M4 wrapper metadata differs outside zac_setting: "
            f"M3={outer_left!r}, M4={outer_right!r}"
        )


@dataclass(frozen=True)
class MethodSpec:
    method: str
    config_path: Path
    python: str
    payload: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    kind: str
    sources: tuple[Path, ...]
    canonical_directory: Path
    suite_manifest: Path
    recursive: bool = True
    upstream_commit: str = ""

    def expanded_sources(self) -> list[Path]:
        paths: list[Path] = []
        for source in self.sources:
            if source.is_file():
                if source.suffix.lower() != ".qasm":
                    raise ValueError(f"dataset {self.name} source is not QASM: {source}")
                paths.append(source.resolve())
            elif source.is_dir():
                iterator = source.rglob("*.qasm") if self.recursive else source.glob("*.qasm")
                paths.extend(path.resolve() for path in iterator)
            else:
                raise FileNotFoundError(f"dataset {self.name} source does not exist: {source}")
        ordered = sorted(set(paths), key=str)
        if not ordered:
            raise ValueError(f"dataset {self.name} has no QASM sources")
        return ordered


@dataclass(frozen=True)
class ExperimentPlan:
    path: Path
    repo_root: Path
    output_root: Path
    architecture_path: Path
    model_path: Path
    python: str
    qmap_python: str
    timeout_seconds: float
    rss_limit_bytes: int | None
    large_timeout_seconds: float
    large_rss_limit_bytes: int | None
    minimum_free_bytes: int
    methods: Mapping[str, MethodSpec]
    datasets: Mapping[str, DatasetSpec]
    reproduction: Mapping[str, Any]
    package_versions: Mapping[str, str]
    bootstrap_iterations: int
    bootstrap_seed: int

    @property
    def package_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    @property
    def model(self) -> FidelityModel:
        payload = _read_json(self.model_path)
        if not isinstance(payload, Mapping):
            raise ValueError("fidelity model must be a JSON object")
        if "model" in payload:
            _reject_unknown(payload, ("experiment_schema", "model"), "model wrapper")
            if payload.get("experiment_schema") != SCHEMA_VERSION:
                raise ValueError("fidelity model wrapper is not Schema 2")
            payload = payload["model"]
        if not isinstance(payload, Mapping):
            raise ValueError("fidelity model payload must be a JSON object")
        model = FidelityModel.from_mapping(payload)
        if model != FidelityModel():
            raise ValueError("formal experiment requires the frozen ZAC paper fidelity model")
        return model

    def datasets_of_kind(self, kind: str) -> list[DatasetSpec]:
        if kind not in DATASET_KINDS:
            raise ValueError(f"unknown dataset kind: {kind}")
        return [self.datasets[name] for name in sorted(self.datasets)
                if self.datasets[name].kind == kind]

    def experiment_id(self, dataset: DatasetSpec) -> str:
        """Hash every frozen input that defines a dataset experiment."""
        if not dataset.suite_manifest.is_file():
            raise FileNotFoundError(
                f"cannot freeze experiment before canonical suite exists: "
                f"{dataset.suite_manifest}")
        repository = repository_snapshot(self.repo_root)
        return stable_sha256({
            "experiment_schema": SCHEMA_VERSION,
            "git_commit": repository["commit"],
            "plan_sha256": sha256_file(self.path),
            "dataset": dataset.name,
            "suite_manifest_sha256": sha256_file(dataset.suite_manifest),
            "architecture_sha256": sha256_file(self.architecture_path),
            "model_sha256": sha256_file(self.model_path),
            "method_config_sha256": {
                method: sha256_file(self.methods[method].config_path)
                for method in METHODS
            },
        })

    def select_datasets(self, names: Sequence[str] | None, *, kind: str) -> list[DatasetSpec]:
        selected = (self.datasets_of_kind(kind) if not names else
                    [self.datasets[name] for name in names])
        if not selected:
            raise ValueError(f"plan contains no {kind} datasets")
        wrong = [item.name for item in selected if item.kind != kind]
        if wrong:
            raise ValueError(f"datasets are not kind={kind}: {wrong}")
        return selected

    def load_suite(self, dataset: DatasetSpec) -> list[CanonicalCircuitManifest]:
        if not dataset.suite_manifest.is_file():
            raise FileNotFoundError(
                f"canonical suite manifest does not exist for {dataset.name}: "
                f"{dataset.suite_manifest}"
            )
        payload = _read_json(dataset.suite_manifest)
        if not isinstance(payload, list):
            raise ValueError(f"canonical suite manifest must be a list: {dataset.suite_manifest}")
        manifests: list[CanonicalCircuitManifest] = []
        names: set[str] = set()
        for row in payload:
            if not isinstance(row, Mapping) or row.get("experiment_schema") != SCHEMA_VERSION:
                raise ValueError(
                    f"refusing non-Schema-2 canonical record in {dataset.suite_manifest}"
                )
            try:
                manifest = CanonicalCircuitManifest(**dict(row))
            except TypeError as error:
                raise ValueError(f"invalid canonical record: {error}") from error
            manifest.validate()
            expected_profile = ("large_qasmbench_expand_only"
                                if dataset.kind == "large"
                                else "main_qiskit_1_2_4_opt3")
            if manifest.canonical_profile != expected_profile:
                raise ValueError(
                    f"dataset {dataset.name} requires canonical profile "
                    f"{expected_profile}, found {manifest.canonical_profile}")
            if (dataset.kind == "large" and
                    manifest.upstream_commit != dataset.upstream_commit):
                raise ValueError(
                    f"Large canonical upstream mismatch: {manifest.canonical_path}")
            canonical_path = Path(manifest.canonical_path)
            if not canonical_path.is_absolute():
                canonical_path = dataset.suite_manifest.parent / canonical_path
            canonical_path = canonical_path.resolve()
            if not canonical_path.is_file():
                raise FileNotFoundError(f"canonical QASM does not exist: {canonical_path}")
            if sha256_file(canonical_path) != manifest.canonical_sha256:
                raise ValueError(f"canonical QASM hash mismatch: {canonical_path}")
            name = canonical_path.stem
            if name in names:
                raise ValueError(f"duplicate canonical circuit id in {dataset.name}: {name}")
            names.add(name)
            # Canonicalisation normally records an absolute path.  Rebuild only
            # for portable hand-authored manifests used on another machine.
            if canonical_path != Path(manifest.canonical_path):
                manifest = CanonicalCircuitManifest(
                    **{**manifest.to_dict(), "canonical_path": str(canonical_path)}
                )
            manifests.append(manifest)
        if not manifests:
            raise ValueError(f"canonical suite is empty: {dataset.suite_manifest}")
        return manifests

    def resolved_config(self, method: str, seed: int) -> Path:
        """Freeze the actual per-attempt config, including the requested seed."""
        if method not in self.methods:
            raise ValueError(f"unknown method: {method}")
        payload = copy.deepcopy(dict(self.methods[method].payload))
        if method in ("M3", "M4"):
            if "zac_setting" in payload:
                payload["zac_setting"][0]["seed"] = int(seed)
            else:
                payload["seed"] = int(seed)
            _validate_method_config(method, payload)
        destination = self.output_root / "_resolved_configs" / method / f"seed-{int(seed)}.json"
        _atomic_json(destination, payload)
        return destination

    def validate_resolved_pair(self, seed: int) -> None:
        m3 = _read_json(self.resolved_config("M3", seed))
        m4 = _read_json(self.resolved_config("M4", seed))
        _validate_pair_payloads(m3, m4)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_experiment_plan(path: str | Path) -> ExperimentPlan:
    plan_path = Path(path).expanduser().resolve()
    payload = _read_json(plan_path)
    if not isinstance(payload, Mapping):
        raise ValueError("experiment plan must be a JSON object")
    allowed = {
        "experiment_schema", "repo_root", "output_root", "architecture", "model",
        "python", "qmap_python", "timeout_seconds", "rss_limit_bytes",
        "large_timeout_seconds", "large_rss_limit_bytes",
        "minimum_free_bytes", "methods", "datasets", "reproduction",
        "package_versions", "bootstrap_iterations", "bootstrap_seed",
    }
    _reject_unknown(payload, allowed, "plan")
    if payload.get("experiment_schema") != SCHEMA_VERSION:
        raise ValueError("refusing non-Schema-2 experiment plan")
    base = plan_path.parent
    required = ("repo_root", "output_root", "architecture", "model", "methods", "datasets")
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"experiment plan is missing fields: {missing}")

    repo_root = _path(base, payload["repo_root"])
    architecture = _path(base, payload["architecture"])
    model_path = _path(base, payload["model"])
    for label, value in (("repo root", repo_root), ("architecture", architecture),
                         ("model", model_path)):
        expected = value.is_dir() if label == "repo root" else value.is_file()
        if not expected:
            raise FileNotFoundError(f"{label} does not exist: {value}")

    default_python = _executable(base, payload.get("python", sys.executable))
    qmap_python = _executable(base, payload.get("qmap_python", default_python))
    raw_methods = payload["methods"]
    if not isinstance(raw_methods, Mapping) or set(raw_methods) != MAIN_METHODS:
        raise ValueError(f"methods must be exactly {METHODS}")
    methods: Dict[str, MethodSpec] = {}
    for method in METHODS:
        value = raw_methods[method]
        if not isinstance(value, Mapping):
            raise ValueError(f"method {method} must be a JSON object")
        _reject_unknown(value, ("config", "python"), f"method {method}")
        if "config" not in value:
            raise ValueError(f"method {method} is missing config")
        config = _path(base, value["config"])
        if not config.is_file():
            raise FileNotFoundError(f"method {method} config does not exist: {config}")
        config_payload = _read_json(config)
        if not isinstance(config_payload, Mapping):
            raise ValueError(f"method {method} config must be a JSON object")
        _validate_method_config(method, config_payload)
        methods[method] = MethodSpec(
            method=method,
            config_path=config,
            python=_executable(base, value.get(
                "python", qmap_python if method == "M2" else default_python)),
            payload=dict(config_payload),
        )
        expected_python = qmap_python if method == "M2" else default_python
        if (Path(methods[method].python).absolute() !=
                Path(expected_python).absolute()):
            raise ValueError(
                f"method {method} Python must equal the frozen "
                f"{'qmap_python' if method == 'M2' else 'python'} interpreter")
    _validate_pair_payloads(methods["M3"].payload, methods["M4"].payload)

    raw_datasets = payload["datasets"]
    if not isinstance(raw_datasets, Mapping) or not raw_datasets:
        raise ValueError("datasets must be a non-empty JSON object")
    datasets: Dict[str, DatasetSpec] = {}
    for name, value in raw_datasets.items():
        if not isinstance(name, str) or not name:
            raise ValueError("dataset names must be non-empty strings")
        if not isinstance(value, Mapping):
            raise ValueError(f"dataset {name} must be a JSON object")
        _reject_unknown(value, ("kind", "sources", "canonical_directory", "suite_manifest",
                                "recursive", "upstream_commit"),
                        f"dataset {name}")
        kind = str(value.get("kind", "main"))
        if kind not in DATASET_KINDS:
            raise ValueError(f"dataset {name} has invalid kind: {kind}")
        if "canonical_directory" not in value:
            raise ValueError(f"dataset {name} is missing canonical_directory")
        canonical_directory = _path(base, value["canonical_directory"])
        suite_manifest = _path(
            base, value.get("suite_manifest", canonical_directory / "suite.manifest.json"))
        raw_sources = value.get("sources", [])
        if not isinstance(raw_sources, list):
            raise ValueError(f"dataset {name} sources must be a list")
        recursive = value.get("recursive", True)
        if not isinstance(recursive, bool):
            raise ValueError(f"dataset {name} recursive must be boolean")
        upstream_commit = str(value.get("upstream_commit", ""))
        if kind == "large" and upstream_commit != QASMBENCH_LARGE_COMMIT:
            raise ValueError(
                f"Large dataset {name} must pin QASMBench commit "
                f"{QASMBENCH_LARGE_COMMIT}")
        if kind == "main" and upstream_commit:
            raise ValueError(f"main dataset {name} may not set upstream_commit")
        datasets[name] = DatasetSpec(
            name=name,
            kind=kind,
            sources=tuple(_path(base, item) for item in raw_sources),
            canonical_directory=canonical_directory,
            suite_manifest=suite_manifest,
            recursive=recursive,
            upstream_commit=upstream_commit,
        )

    timeout = float(payload.get("timeout_seconds", 600.0))
    rss = payload.get("rss_limit_bytes")
    large_timeout = float(payload.get("large_timeout_seconds", 24 * 60 * 60))
    large_rss = payload.get("large_rss_limit_bytes", 22 * (1 << 30))
    minimum_free = int(payload.get("minimum_free_bytes", 5 * (1 << 30)))
    bootstrap_iterations = int(payload.get("bootstrap_iterations", 10_000))
    bootstrap_seed = int(payload.get("bootstrap_seed", 0))
    if (timeout <= 0 or large_timeout <= 0 or minimum_free < 0 or
            (rss is not None and int(rss) <= 0) or
            (large_rss is not None and int(large_rss) <= 0)):
        raise ValueError("invalid runner resource limit in plan")
    if bootstrap_iterations <= 0:
        raise ValueError("bootstrap_iterations must be positive")
    package_versions = payload.get("package_versions", {})
    reproduction = payload.get("reproduction", {})
    if not isinstance(package_versions, Mapping) or not isinstance(reproduction, Mapping):
        raise ValueError("package_versions and reproduction must be JSON objects")

    plan = ExperimentPlan(
        path=plan_path,
        repo_root=repo_root,
        output_root=_path(base, payload["output_root"]),
        architecture_path=architecture,
        model_path=model_path,
        python=default_python,
        qmap_python=qmap_python,
        timeout_seconds=timeout,
        rss_limit_bytes=None if rss is None else int(rss),
        large_timeout_seconds=large_timeout,
        large_rss_limit_bytes=None if large_rss is None else int(large_rss),
        minimum_free_bytes=minimum_free,
        methods=methods,
        datasets=datasets,
        reproduction=dict(reproduction),
        package_versions={str(key): str(value) for key, value in package_versions.items()},
        bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed,
    )
    # Eagerly validate the physical parameters rather than failing after compile.
    _ = plan.model
    return plan


__all__ = [
    "DATASET_KINDS", "DatasetSpec", "ExperimentPlan", "METHODS", "MethodSpec",
    "QASMBENCH_LARGE_COMMIT",
    "effective_zac_setting", "load_experiment_plan",
]
