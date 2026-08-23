"""Fail-closed ABI3 build attestation and freeze protocol.

The native wheel is not reproducible byte-for-byte on every supported build
host, so this module deliberately creates a *build attestation*, not a claim
that a binary can be reversed back into its sources.  The two-stage protocol
binds one clean Git commit and a documented ``tracked-tree-v1`` source digest
to the registered wheel/extension pair.  A later freeze replays that binding
and validates the micro, real-boundary, and full-pipeline benchmark gates
before atomically publishing ``artifact_status=frozen``.

An older manifest whose source digest does not declare ``tracked-tree-v1`` is
never accepted as an attestation and therefore cannot be promoted in place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zipfile import ZipFile


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO_ROOT = PACKAGE_ROOT.parent
DEFAULT_NATIVE_ROOT = PACKAGE_ROOT / "native"

ATTESTATION_PROTOCOL = "native-build-attestation-v1"
FREEZE_PROTOCOL = "native-build-freeze-v1"
SOURCE_HASH_ALGORITHM = "tracked-tree-v1"
NATIVE_ABI_VERSION = 3
RNG_VERSION = "python-random-mt19937-v1"
RICH_BOUNDARY_WIRE_VERSION = 2
FLAT_WIRE_VERSION = 1

MINIMUM_ONE_CALL_SPEEDUP = 5.0
MINIMUM_FITNESS_SPEEDUP = 10.0
MINIMUM_GHOST_SPEEDUP = 10.0
MINIMUM_BOUNDARY_SPEEDUP = 5.0
NLL_TOLERANCE = 1e-12

# These files are the inputs which determine the compiled wheel.  Tests,
# benchmark drivers, and prose are intentionally excluded: changing them does
# not alter the extension bytes and should not force a wheel rebuild.
BUILD_INPUT_PREFIXES = ("bindings/", "include/", "src/")
BUILD_INPUT_FILES = frozenset({"CMakeLists.txt", "pyproject.toml"})


class NativeBuildFreezeError(RuntimeError):
    """Raised whenever a formal build cannot be safely frozen."""


def _run_git(repo_root: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *arguments], cwd=repo_root, text=True,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise NativeBuildFreezeError(
            f"Git inspection failed: {detail.strip()}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeBuildFreezeError(
            f"invalid {label} JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise NativeBuildFreezeError(f"{label} must be a JSON object: {path}")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                value, handle, indent=2, sort_keys=True, ensure_ascii=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _with_record_sha256(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("record_sha256", None)
    result["record_sha256"] = _stable_sha256(result)
    return result


def _validate_record_sha256(value: Mapping[str, Any], label: str) -> None:
    observed = value.get("record_sha256")
    unsigned = dict(value)
    unsigned.pop("record_sha256", None)
    expected = _stable_sha256(unsigned)
    if observed != expected:
        raise NativeBuildFreezeError(f"{label} record SHA256 mismatch")


def _repo_snapshot(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    actual_root = Path(_run_git(repo_root, "rev-parse", "--show-toplevel"))
    if actual_root.resolve() != repo_root:
        raise NativeBuildFreezeError(
            f"repo root mismatch: requested={repo_root}, actual={actual_root}")
    dirty = _run_git(
        repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        first = dirty.splitlines()[0]
        raise NativeBuildFreezeError(
            f"native build freeze requires a clean repository: {first}")
    branch = _run_git(repo_root, "branch", "--show-current") or "detached"
    return {
        "root": str(repo_root),
        "git_commit": _run_git(repo_root, "rev-parse", "HEAD"),
        "git_branch": branch,
        "git_dirty": False,
    }


def _is_build_input(relative_to_native: str) -> bool:
    return (relative_to_native in BUILD_INPUT_FILES
            or relative_to_native.startswith(BUILD_INPUT_PREFIXES))


def tracked_native_tree(repo_root: Path,
                        native_root: Path) -> dict[str, Any]:
    """Return the documented digest of tracked native build inputs.

    ``tracked-tree-v1`` is SHA256 over canonical JSON containing a sorted list
    of ``{"path": <native-relative path>, "sha256": <file digest>}`` rows.
    """
    repo_root = repo_root.resolve()
    native_root = native_root.resolve()
    try:
        native_relative = native_root.relative_to(repo_root)
    except ValueError as exc:
        raise NativeBuildFreezeError(
            "native root must be inside the repository") from exc
    if not native_root.is_dir():
        raise NativeBuildFreezeError(f"native root is missing: {native_root}")
    tracked = _run_git(
        repo_root, "ls-files", "--cached", "--", native_relative.as_posix())
    rows: list[dict[str, str]] = []
    prefix = native_relative.as_posix().rstrip("/") + "/"
    for repo_relative in sorted(filter(None, tracked.splitlines())):
        if not repo_relative.startswith(prefix):
            raise NativeBuildFreezeError(
                f"Git returned a path outside native root: {repo_relative}")
        relative = repo_relative[len(prefix):]
        if not _is_build_input(relative):
            continue
        source = repo_root / repo_relative
        if source.is_symlink() or not source.is_file():
            raise NativeBuildFreezeError(
                f"native build input must be a regular file: {source}")
        rows.append({"path": relative, "sha256": _sha256_file(source)})
    required = BUILD_INPUT_FILES
    present = {row["path"] for row in rows}
    missing = sorted(required - present)
    if missing or not any(row["path"].startswith("src/") for row in rows):
        raise NativeBuildFreezeError(
            "tracked native build inputs are incomplete: "
            + ", ".join(missing or ["src/**"]))
    return {
        "algorithm": SOURCE_HASH_ALGORITHM,
        "source_set": "CMakeLists.txt, pyproject.toml, bindings/**, include/**, src/**",
        "file_count": len(rows),
        "files": rows,
        "sha256": _stable_sha256(rows),
    }


def _default_build_info(*, expected_wheel_sha256: str) -> Mapping[str, Any]:
    from zzx.native_backend import build_info

    return build_info(
        require_registered_wheel=True,
        expected_wheel_sha256=expected_wheel_sha256,
    )


def _observe_registered_build(
        wheel_path: Path) -> dict[str, Any]:
    wheel_path = wheel_path.resolve()
    if not wheel_path.is_file() or wheel_path.suffix != ".whl":
        raise NativeBuildFreezeError(
            f"registered native wheel is missing: {wheel_path}")
    wheel_sha256 = _sha256_file(wheel_path)
    info = dict(_default_build_info(expected_wheel_sha256=wheel_sha256))
    checks = {
        "native_abi_version": NATIVE_ABI_VERSION,
        "flat_wire_version": FLAT_WIRE_VERSION,
        "rich_boundary_wire_version": RICH_BOUNDARY_WIRE_VERSION,
        "rng_version": RNG_VERSION,
        "cxx_standard": 17,
        "build_type": "Release",
        "openmp": False,
        "fast_math": False,
        "wheel_registered": True,
        "native_wheel_sha256": wheel_sha256,
    }
    for key, expected in checks.items():
        if info.get(key) != expected:
            raise NativeBuildFreezeError(
                f"native build_info {key} mismatch: "
                f"expected={expected!r}, observed={info.get(key)!r}")
    for key in ("compiler_id", "compiler_version", "extension_sha256",
                "extension_path", "wheel_registration_path"):
        if not info.get(key):
            raise NativeBuildFreezeError(
                f"native build_info is missing {key}")

    extension_path = Path(str(info["extension_path"])).resolve()
    if not extension_path.is_file():
        raise NativeBuildFreezeError(
            f"loaded native extension is missing: {extension_path}")
    extension_sha256 = _sha256_file(extension_path)
    if extension_sha256 != info["extension_sha256"]:
        raise NativeBuildFreezeError(
            "loaded extension SHA256 differs from build_info")

    registration_path = Path(str(info["wheel_registration_path"])).resolve()
    registration = _load_json(registration_path, "wheel registration")
    for key, expected in {
            "wheel_sha256": wheel_sha256,
            "extension_sha256": extension_sha256,
            }.items():
        if registration.get(key) != expected:
            raise NativeBuildFreezeError(
                f"wheel registration {key} mismatch")
    registered_wheel = registration.get("wheel_path")
    if not registered_wheel or Path(str(registered_wheel)).resolve() != wheel_path:
        raise NativeBuildFreezeError(
            "wheel registration path differs from the supplied wheel")
    member = str(registration.get("extension_member", ""))
    if not member:
        raise NativeBuildFreezeError(
            "wheel registration is missing extension_member")
    try:
        with ZipFile(wheel_path) as archive:
            archive_sha256 = hashlib.sha256(archive.read(member)).hexdigest()
    except (OSError, KeyError) as exc:
        raise NativeBuildFreezeError(
            "registered extension member is absent from the wheel") from exc
    if archive_sha256 != extension_sha256:
        raise NativeBuildFreezeError(
            "registered wheel member differs from the loaded extension")

    return {
        "wheel": {
            "path": str(wheel_path),
            "sha256": wheel_sha256,
            "size_bytes": wheel_path.stat().st_size,
            "native_abi_version": NATIVE_ABI_VERSION,
            "flat_wire_version": FLAT_WIRE_VERSION,
            "rich_boundary_wire_version": RICH_BOUNDARY_WIRE_VERSION,
            "rng_version": RNG_VERSION,
        },
        "loaded_extension": {
            "path": str(extension_path),
            "sha256": extension_sha256,
            "wheel_registered": True,
            "wheel_registration_path": str(registration_path),
            "wheel_member": member,
        },
        "compiler": {
            "id": str(info["compiler_id"]),
            "version": str(info["compiler_version"]),
            "cxx_standard": 17,
            "build_type": "Release",
            "openmp": False,
            "fast_math": False,
        },
    }


def create_native_build_attestation(
        *, repo_root: Path, native_root: Path, wheel_path: Path,
        output_path: Path,
        ) -> Mapping[str, Any]:
    """Create a clean-commit source/wheel attestation atomically."""
    repository = _repo_snapshot(repo_root)
    source_tree = tracked_native_tree(repo_root, native_root)
    native_build = _observe_registered_build(wheel_path)
    source = dict(repository)
    source["native_source_tree"] = source_tree
    value = _with_record_sha256({
        "schema": 1,
        "protocol": ATTESTATION_PROTOCOL,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "artifact_status": "candidate",
        "attestation_scope": (
            "build attestation; not a reversible or reproducible-build proof"),
        "requires_benchmark_freeze": True,
        "source": source,
        **native_build,
    })
    _atomic_json(output_path, value)
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeBuildFreezeError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not result > 0.0:
        raise NativeBuildFreezeError(f"{label} must be finite and positive")
    return result


def _nll_error_below_tolerance(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    result = float(value)
    return math.isfinite(result) and 0.0 <= result < NLL_TOLERANCE


def _require_native_identity(value: Mapping[str, Any], expected: Mapping[str, Any],
                             label: str) -> None:
    observed_wheel = value.get("native_wheel_sha256")
    observed_extension = value.get("extension_sha256")
    if observed_wheel != expected["wheel"]["sha256"]:
        raise NativeBuildFreezeError(f"{label} wheel SHA256 mismatch")
    if observed_extension != expected["loaded_extension"]["sha256"]:
        raise NativeBuildFreezeError(f"{label} extension SHA256 mismatch")


def _validate_micro_benchmark(path: Path,
                              native_build: Mapping[str, Any]) -> dict[str, Any]:
    value = _load_json(path, "native microbenchmark")
    if value.get("schema") != 2 or value.get("protocol") != (
            "abi3-registered-native-microbenchmark-v1"):
        raise NativeBuildFreezeError(
            "microbenchmark must use registered ABI3 schema 2")
    build = value.get("native_build")
    if not isinstance(build, Mapping):
        raise NativeBuildFreezeError("microbenchmark lacks native_build")
    _require_native_identity(build, native_build, "microbenchmark")
    gates = {
        "one_call_speedup": _number(
            value.get("one_call", {}).get("speedup"),
            "one-call speedup"),
        "fitness_core_speedup": _number(
            value.get("fitness_core", {}).get("speedup"),
            "fitness-core speedup"),
        "ghost_core_speedup": _number(
            value.get("ghost_core", {}).get("speedup"),
            "ghost-core speedup"),
    }
    thresholds = {
        "one_call_speedup": MINIMUM_ONE_CALL_SPEEDUP,
        "fitness_core_speedup": MINIMUM_FITNESS_SPEEDUP,
        "ghost_core_speedup": MINIMUM_GHOST_SPEEDUP,
    }
    failed = [name for name, threshold in thresholds.items()
              if gates[name] < threshold]
    if failed:
        raise NativeBuildFreezeError(
            "native microbenchmark gate failed: " + ", ".join(failed))
    return {
        "path": str(path.resolve()), "sha256": _sha256_file(path),
        "gates": gates, "thresholds": thresholds, "all_passed": True,
    }


def _validate_real_boundary(path: Path,
                            native_build: Mapping[str, Any]) -> dict[str, Any]:
    value = _load_json(path, "real-boundary benchmark")
    if value.get("benchmark_id") != "abi3-exact-real-boundary-ising-n42-v1":
        raise NativeBuildFreezeError("unexpected real-boundary benchmark id")
    build = value.get("native_build")
    if not isinstance(build, Mapping):
        raise NativeBuildFreezeError("real-boundary benchmark lacks native_build")
    _require_native_identity(build, native_build, "real-boundary benchmark")
    parity = value.get("parity")
    timing = value.get("timing")
    if not isinstance(parity, Mapping) or parity.get("all_passed") is not True:
        raise NativeBuildFreezeError("real-boundary parity gate failed")
    if not isinstance(timing, Mapping):
        raise NativeBuildFreezeError("real-boundary timing is missing")
    speedup = _number(timing.get("primary_speedup"),
                      "real-boundary primary speedup")
    if speedup < MINIMUM_BOUNDARY_SPEEDUP or (
            timing.get("meets_5x_complete_boundary_gate") is not True):
        raise NativeBuildFreezeError("real-boundary 5x gate failed")
    repetitions = parity.get("repetitions")
    if not isinstance(repetitions, list) or not repetitions:
        raise NativeBuildFreezeError("real-boundary parity repetitions missing")
    for index, row in enumerate(repetitions):
        if not isinstance(row, Mapping):
            raise NativeBuildFreezeError("invalid real-boundary parity row")
        for key in ("mapping", "rng", "safety_rng", "stay_return", "winner",
                    "unique_evaluations"):
            if row.get(key) is not True:
                raise NativeBuildFreezeError(
                    f"real-boundary parity {key} failed at repetition {index}")
        for key in ("current_nll_max_abs_error", "forecast_nll_max_abs_error",
                    "search_nll_max_abs_error"):
            error = row.get(key)
            if not _nll_error_below_tolerance(error):
                raise NativeBuildFreezeError(
                    f"real-boundary parity {key} exceeds tolerance")
    return {
        "path": str(path.resolve()), "sha256": _sha256_file(path),
        "primary_speedup": speedup,
        "minimum_speedup": MINIMUM_BOUNDARY_SPEEDUP,
        "parity_repetitions": len(repetitions), "all_passed": True,
    }


def _validate_full_pipeline(path: Path,
                            native_build: Mapping[str, Any]) -> dict[str, Any]:
    value = _load_json(path, "full-pipeline benchmark")
    if value.get("protocol") != "resident-python-vs-abi3-medium-v1":
        raise NativeBuildFreezeError("unexpected full-pipeline protocol")
    if value.get("wheel_sha256") != native_build["wheel"]["sha256"]:
        raise NativeBuildFreezeError("full-pipeline wheel SHA256 mismatch")
    if value.get("accepted") is not True:
        raise NativeBuildFreezeError("full-pipeline benchmark gate failed")
    horizons = value.get("horizons")
    if not isinstance(horizons, Mapping) or set(horizons) != {"0", "8"}:
        raise NativeBuildFreezeError(
            "full-pipeline benchmark must contain H0 and H8")
    speedups: dict[str, float] = {}
    for horizon in ("0", "8"):
        row = horizons[horizon]
        if not isinstance(row, Mapping) or row.get("accepted") is not True:
            raise NativeBuildFreezeError(
                f"full-pipeline H{horizon} gate failed")
        speedup = _number(row.get("speedup"),
                          f"full-pipeline H{horizon} speedup")
        if speedup < MINIMUM_BOUNDARY_SPEEDUP:
            raise NativeBuildFreezeError(
                f"full-pipeline H{horizon} is below 5x")
        parity = row.get("parity")
        if not isinstance(parity, Mapping):
            raise NativeBuildFreezeError(
                f"full-pipeline H{horizon} parity missing")
        for key in ("mapping_equal", "registry_equal", "rng_equal"):
            if parity.get(key) is not True:
                raise NativeBuildFreezeError(
                    f"full-pipeline H{horizon} {key} failed")
        for key in ("max_current_nll_abs_error",
                    "max_forecast_nll_abs_error"):
            error = parity.get(key)
            if not _nll_error_below_tolerance(error):
                raise NativeBuildFreezeError(
                    f"full-pipeline H{horizon} {key} exceeds tolerance")
        speedups[horizon] = speedup
    return {
        "path": str(path.resolve()), "sha256": _sha256_file(path),
        "speedups": speedups, "minimum_speedup": MINIMUM_BOUNDARY_SPEEDUP,
        "all_passed": True,
    }


def freeze_native_build(
        *, repo_root: Path, native_root: Path, wheel_path: Path,
        attestation_path: Path, micro_benchmark_path: Path,
        real_boundary_benchmark_path: Path,
        full_pipeline_benchmark_path: Path, output_path: Path,
        ) -> Mapping[str, Any]:
    """Replay every build/evidence gate and atomically publish a freeze."""
    repository = _repo_snapshot(repo_root)
    current_tree = tracked_native_tree(repo_root, native_root)
    attestation = _load_json(attestation_path, "native build attestation")
    if (attestation.get("schema") != 1
            or attestation.get("protocol") != ATTESTATION_PROTOCOL
            or attestation.get("artifact_status") != "candidate"):
        raise NativeBuildFreezeError(
            "legacy or malformed build attestation cannot be frozen")
    _validate_record_sha256(attestation, "native build attestation")
    attested_source = attestation.get("source")
    if not isinstance(attested_source, Mapping):
        raise NativeBuildFreezeError("attestation source is missing")
    attested_tree = attested_source.get("native_source_tree")
    if not isinstance(attested_tree, Mapping) or (
            attested_tree.get("algorithm") != SOURCE_HASH_ALGORITHM):
        raise NativeBuildFreezeError(
            "attestation does not use tracked-tree-v1")
    if attested_tree != current_tree:
        raise NativeBuildFreezeError(
            "native source tree hash drifted after build attestation")
    for key in ("root", "git_commit", "git_branch", "git_dirty"):
        if attested_source.get(key) != repository.get(key):
            raise NativeBuildFreezeError(
                f"repository {key} drifted after build attestation")

    native_build = _observe_registered_build(wheel_path)
    for section in ("wheel", "loaded_extension", "compiler"):
        if attestation.get(section) != native_build[section]:
            raise NativeBuildFreezeError(
                f"registered native {section} drifted after attestation")

    micro = _validate_micro_benchmark(
        micro_benchmark_path.resolve(), native_build)
    real = _validate_real_boundary(
        real_boundary_benchmark_path.resolve(), native_build)
    pipeline = _validate_full_pipeline(
        full_pipeline_benchmark_path.resolve(), native_build)
    source = dict(repository)
    source["native_source_tree"] = current_tree
    value = _with_record_sha256({
        "schema": 2,
        "protocol": FREEZE_PROTOCOL,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "algorithm_revision": "native-ga-v1",
        "artifact_status": "frozen",
        "requires_clean_commit_resign": False,
        "attestation_scope": (
            "build attestation; not a reversible or reproducible-build proof"),
        "source": source,
        **native_build,
        "evidence": {
            "build_attestation": {
                "path": str(attestation_path.resolve()),
                "sha256": _sha256_file(attestation_path.resolve()),
                "record_sha256": attestation["record_sha256"],
            },
            "microbenchmark": micro,
            "real_boundary_benchmark": real,
            "full_pipeline_benchmark": pipeline,
        },
        # Preserve the concise gate projection expected by existing reports.
        "benchmark": {
            "path": micro["path"],
            "sha256": micro["sha256"],
            "one_call_speedup": micro["gates"]["one_call_speedup"],
            "fitness_core_speedup": micro["gates"]["fitness_core_speedup"],
            "ghost_core_speedup": micro["gates"]["ghost_core_speedup"],
            "complete_boundary_speedup": real["primary_speedup"],
            "all_gates_passed": True,
        },
    })
    _atomic_json(output_path, value)
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Attest and freeze the registered ABI3 native build")
    subparsers = parser.add_subparsers(dest="command", required=True)
    attest = subparsers.add_parser("attest")
    attest.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    attest.add_argument("--native-root", type=Path, default=DEFAULT_NATIVE_ROOT)
    attest.add_argument("--wheel", type=Path, required=True)
    attest.add_argument("--output", type=Path, required=True)

    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    freeze.add_argument("--native-root", type=Path, default=DEFAULT_NATIVE_ROOT)
    freeze.add_argument("--wheel", type=Path, required=True)
    freeze.add_argument("--attestation", type=Path, required=True)
    freeze.add_argument("--micro-benchmark", type=Path, required=True)
    freeze.add_argument("--real-boundary-benchmark", type=Path, required=True)
    freeze.add_argument("--full-pipeline-benchmark", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "attest":
        value = create_native_build_attestation(
            repo_root=args.repo_root, native_root=args.native_root,
            wheel_path=args.wheel, output_path=args.output)
    else:
        value = freeze_native_build(
            repo_root=args.repo_root, native_root=args.native_root,
            wheel_path=args.wheel, attestation_path=args.attestation,
            micro_benchmark_path=args.micro_benchmark,
            real_boundary_benchmark_path=args.real_boundary_benchmark,
            full_pipeline_benchmark_path=args.full_pipeline_benchmark,
            output_path=args.output)
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "NativeBuildFreezeError",
    "create_native_build_attestation",
    "freeze_native_build",
    "tracked_native_tree",
]
