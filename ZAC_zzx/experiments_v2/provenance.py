"""Fail-closed provenance for the formal baseline-reproduction gate."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping

from .contracts import (SCHEMA_VERSION, machine_snapshot,
                        repository_snapshot, sha256_file, stable_sha256)
from .plan import ExperimentPlan


ENVIRONMENT_LOCKS = (
    "environment_zac_qiskit124.lock.txt",
    "environment_iccad_qmap320.lock.txt",
)


def _identity(path: Path) -> Dict[str, str]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"frozen provenance file does not exist: {resolved}")
    return {"path": str(resolved), "sha256": sha256_file(resolved)}


def _plan_relative(plan: ExperimentPlan, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = plan.path.parent / path
    return path.resolve()


def _locked_requirements(path: Path) -> tuple[str, Dict[str, str]]:
    expected_python = ""
    packages: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("# Interpreter: CPython "):
            expected_python = line.removeprefix("# Interpreter: CPython ").strip()
        elif line and not line.startswith("#"):
            if line.count("==") != 1:
                raise ValueError(f"unsupported environment-lock line in {path}: {line}")
            name, version = line.split("==", 1)
            if not name or not version or name in packages:
                raise ValueError(f"invalid environment-lock requirement in {path}: {line}")
            packages[name] = version
    if not expected_python or not packages:
        raise ValueError(f"environment lock is incomplete: {path}")
    return expected_python, packages


def validate_locked_python_environment(
        python: str | Path, lock_path: str | Path) -> Mapping[str, Any]:
    """Probe an interpreter and require every locked distribution exactly."""
    # Do not resolve the final venv symlink: executing its base interpreter
    # would bypass pyvenv.cfg and probe the wrong environment.
    executable = Path(python).expanduser().absolute()
    lock = Path(lock_path).expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"frozen Python interpreter does not exist: {executable}")
    expected_python, packages = _locked_requirements(lock)
    probe = (
        "import importlib.metadata,json,sys;"
        "names=json.loads(sys.argv[1]);"
        "versions={};"
        "missing=[];"
        "\nfor name in names:\n"
        " try: versions[name]=importlib.metadata.version(name)\n"
        " except importlib.metadata.PackageNotFoundError: missing.append(name)\n"
        "print(json.dumps({'python':sys.version.split()[0],"
        "'versions':versions,'missing':missing},sort_keys=True))"
    )
    completed = subprocess.run(
        [str(executable), "-c", probe, json.dumps(sorted(packages))],
        text=True, capture_output=True, timeout=60, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"failed to probe frozen Python {executable}: {completed.stderr.strip()}")
    try:
        observed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"frozen Python probe returned invalid JSON: {completed.stdout!r}") from error
    mismatches = {
        name: {"expected": version,
               "observed": observed.get("versions", {}).get(name)}
        for name, version in packages.items()
        if observed.get("versions", {}).get(name) != version
    }
    if observed.get("python") != expected_python or observed.get("missing") or mismatches:
        raise RuntimeError(
            "frozen Python environment drifted: " + json.dumps({
                "python_expected": expected_python,
                "python_observed": observed.get("python"),
                "missing": observed.get("missing"),
                "mismatches": mismatches,
            }, sort_keys=True))
    return {
        "python_path": str(executable),
        "python_version": expected_python,
        "lock_path": str(lock),
        "lock_sha256": sha256_file(lock),
        "packages": len(packages),
    }


def validate_frozen_environments(plan: ExperimentPlan) -> Mapping[str, Any]:
    """Validate both compiler runtimes against their complete lock files."""
    base = plan.path.parent
    return {
        "zac": validate_locked_python_environment(
            plan.python, base / ENVIRONMENT_LOCKS[0]),
        "iccad": validate_locked_python_environment(
            plan.qmap_python, base / ENVIRONMENT_LOCKS[1]),
    }


def build_reproduction_provenance(
        plan: ExperimentPlan,
        package_evidence: Mapping[str, Any],
        ) -> Mapping[str, Any]:
    """Capture all code/environment identities needed to unlock formal runs.

    The reproduction command itself is only valid on a clean, versioned
    checkout.  External benchmark and native-output hashes remain sealed by
    the method-specific reproduction evidence; this record binds that evidence
    to the experiment plan and the code that interpreted it.
    """
    repository = repository_snapshot(plan.repo_root)
    if repository["commit"] == "unknown" or repository["dirty"] is not False:
        raise RuntimeError(
            "formal baseline reproduction requires a clean, versioned Git commit")

    reproduction = plan.reproduction
    frozen_files: Dict[str, Mapping[str, str]] = {
        "experiment_plan": _identity(plan.path),
        "main_architecture": _identity(plan.architecture_path),
        "fidelity_model": _identity(plan.model_path),
    }
    for filename in ENVIRONMENT_LOCKS:
        frozen_files[filename] = _identity(plan.path.parent / filename)
    for key in ("zac_truth", "iccad_truth"):
        if key not in reproduction:
            raise ValueError(f"reproduction plan is missing {key}")
        frozen_files[key] = _identity(_plan_relative(plan, reproduction[key]))
    zac_execute = reproduction.get("zac_execute")
    if isinstance(zac_execute, Mapping) and "architecture" in zac_execute:
        frozen_files["zac_reproduction_architecture"] = _identity(
            _plan_relative(plan, str(zac_execute["architecture"])))

    payload: Dict[str, Any] = {
        "experiment_schema": SCHEMA_VERSION,
        "repository": repository,
        "machine": machine_snapshot(),
        "frozen_files": dict(sorted(frozen_files.items())),
        "declared_package_versions": dict(sorted(plan.package_versions.items())),
        "observed_package_versions": dict(package_evidence),
    }
    payload["provenance_payload_sha256"] = stable_sha256(payload)
    return payload


def validate_reproduction_provenance(
        plan: ExperimentPlan, payload: Mapping[str, Any]) -> None:
    """Recompute all live identities before a reproduction report is reused."""
    required = {
        "experiment_schema", "repository", "machine", "frozen_files",
        "declared_package_versions", "observed_package_versions",
        "provenance_payload_sha256",
    }
    if set(payload) != required or payload.get("experiment_schema") != SCHEMA_VERSION:
        raise ValueError("invalid baseline reproduction provenance contract")
    unhashed = {key: value for key, value in payload.items()
                if key != "provenance_payload_sha256"}
    if stable_sha256(unhashed) != payload.get("provenance_payload_sha256"):
        raise ValueError("baseline reproduction provenance payload changed")

    repository = repository_snapshot(plan.repo_root)
    recorded_repository = payload.get("repository")
    if (repository.get("commit") == "unknown" or repository.get("dirty") is not False or
            not isinstance(recorded_repository, Mapping) or
            recorded_repository.get("dirty") is not False or
            recorded_repository.get("commit") != repository.get("commit")):
        raise RuntimeError(
            "baseline reproduction is stale: formal runs require its exact clean commit")

    package_evidence = payload.get("observed_package_versions")
    expected = build_reproduction_provenance(
        plan, package_evidence if isinstance(package_evidence, Mapping) else {})
    if (payload.get("frozen_files") != expected["frozen_files"] or
            payload.get("declared_package_versions") !=
            expected["declared_package_versions"]):
        raise RuntimeError("baseline reproduction frozen inputs or environment locks changed")


__all__ = [
    "ENVIRONMENT_LOCKS", "build_reproduction_provenance",
    "validate_frozen_environments", "validate_locked_python_environment",
    "validate_reproduction_provenance",
]
