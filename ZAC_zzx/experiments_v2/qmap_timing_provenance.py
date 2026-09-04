"""Binary- and parity-bound provenance for the QMAP 3.2 timing-only wheel."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any, Mapping
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = (
    ROOT / "third_party" / "qmap32_streaming" /
    "timing_freeze_manifest.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve(base: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"QMAP timing freeze lacks {label}")
    path = (base / value).resolve()
    if not path.is_file():
        raise RuntimeError(f"QMAP timing {label} is missing: {path}")
    return path


def _require_hash(path: Path, expected: object, label: str) -> str:
    actual = _sha256(path)
    if actual != expected:
        raise RuntimeError(
            f"QMAP timing {label} hash mismatch: {actual} != {expected}")
    return actual


def _verify_installed_wheel(wheel: Path) -> Mapping[str, str]:
    distribution = importlib.metadata.distribution("mqt.qmap")
    installed_version = distribution.version
    with ZipFile(wheel) as archive:
        binary_members = sorted(
            name for name in archive.namelist()
            if Path(name).suffix in {".so", ".pyd", ".dll", ".dylib"})
        if not binary_members:
            raise RuntimeError("QMAP timing wheel contains no native binary")
        verified: dict[str, str] = {}
        for member in binary_members:
            installed = Path(distribution.locate_file(member)).resolve()
            if not installed.is_file():
                raise RuntimeError(
                    f"installed QMAP timing binary is missing: {installed}")
            wheel_hash = hashlib.sha256(archive.read(member)).hexdigest()
            installed_hash = _sha256(installed)
            if installed_hash != wheel_hash:
                raise RuntimeError(
                    f"installed QMAP binary differs from frozen wheel: {member}")
            verified[member] = installed_hash
    return {"installed_version": installed_version, **verified}


def validate_qmap_timing_freeze() -> Mapping[str, Any]:
    freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
    if (freeze.get("protocol") != "qmap-3.2-timing-instrumentation-v1"
            or freeze.get("status") != "frozen"):
        raise RuntimeError("QMAP timing freeze is not registered and frozen")
    base = FREEZE_PATH.parent
    instrumentation = freeze.get("instrumentation")
    build = freeze.get("build")
    parity = freeze.get("parity")
    if not all(isinstance(value, Mapping)
               for value in (instrumentation, build, parity)):
        raise RuntimeError("QMAP timing freeze sections are malformed")

    patch = _resolve(base, instrumentation.get("patch"), "patch")
    patch_sha = _require_hash(
        patch, instrumentation.get("patch_sha256"), "patch")
    report = _resolve(base, parity.get("report"), "parity report")
    report_sha = _require_hash(
        report, parity.get("report_sha256"), "parity report")
    wheel = _resolve(base, build.get("wheel_path"), "wheel")
    wheel_sha = _require_hash(wheel, build.get("wheel_sha256"), "wheel")
    lock = _resolve(base, build.get("environment_lock"), "environment lock")
    lock_sha = _require_hash(
        lock, build.get("environment_lock_sha256"), "environment lock")

    parity_payload = json.loads(report.read_text(encoding="utf-8"))
    circuits = parity_payload.get("circuits")
    if (parity_payload.get("protocol") != freeze["protocol"]
            or parity_payload.get("valid") is not True
            or not isinstance(circuits, list) or not circuits):
        raise RuntimeError("QMAP timing parity report did not pass")
    names = [row.get("circuit") for row in circuits]
    if (len(set(names)) != len(names)
            or any(row.get("same_native") is not True
                   or row.get("timing_fields_complete") is not True
                   for row in circuits)
            or names != parity.get("circuits")
            or parity.get("all_native_trace_hashes_equal") is not True
            or parity.get("all_timing_fields_present") is not True):
        raise RuntimeError("QMAP timing parity evidence is incomplete or drifted")

    installed = _verify_installed_wheel(wheel)
    if (installed["installed_version"] !=
            "3.2.1.dev0+g745d56b26.d20260823"):
        raise RuntimeError("unexpected installed QMAP timing package version")
    if importlib.metadata.version("mqt-core") != freeze["base"]["mqt_core"]:
        raise RuntimeError("unexpected installed mqt-core timing version")
    return {
        "protocol": freeze["protocol"],
        "patch_sha256": patch_sha,
        "parity_report_sha256": report_sha,
        "wheel_sha256": wheel_sha,
        "environment_lock_sha256": lock_sha,
        "parity_circuits": names,
        "installed_binary_sha256": {
            key: value for key, value in installed.items()
            if key != "installed_version"},
        "mqt_qmap_version": installed["installed_version"],
        "mqt_core_version": importlib.metadata.version("mqt-core"),
    }


__all__ = ["FREEZE_PATH", "validate_qmap_timing_freeze"]
