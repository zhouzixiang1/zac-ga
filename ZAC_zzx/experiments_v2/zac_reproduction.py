"""Immutable, evidence-complete reproduction of the 18 original ZAC rows.

This track intentionally invokes the unmodified ``ZAC/zac`` package under the
Qiskit 1.2.4 interpreter named by the experiment plan.  It is separate from
the Schema-2 canonical/unified-scoring track and from the modified ``ZAC_zzx``
compiler.  A completed directory is reusable only for the byte-identical
request; an incomplete directory is evidence of a partial attempt and is
never resumed or overwritten.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Sequence

from .contracts import SCHEMA_VERSION, sha256_file, stable_sha256


ZAC_HPCA18: Sequence[str] = (
    "bv_n14", "bv_n19", "bv_n30", "bv_n70", "cat_n22", "cat_n35",
    "ghz_n23", "ghz_n40", "ghz_n78", "ising_n42", "ising_n98",
    "knn_n31", "multiply_n13", "qft_n18", "qft_n29", "seca_n11",
    "swap_test_n25", "wstate_n27",
)

ZAC_REPRODUCTION_CONFIG: Mapping[str, object] = {
    "dependency": True,
    "routing_strategy": "maximalis_sort",
    "scheduling": "asap",
    "trivial_placement": False,
    "dynamic_placement": True,
    "use_window": True,
    "window_size": 1000,
    "reuse": True,
    "use_verifier": True,
    "resyn": True,
    "simulation": True,
    "animation": False,
    "qiskit_basis_gates": ["cz", "id", "u2", "u1", "u3"],
    "qiskit_optimization_level": 3,
    "seed_transpiler": 0,
}

REQUEST_FILENAME = "zac_hpca18_request.json"
RESULT_FILENAME = "zac_hpca18_schema2_reproduction.json"
EVIDENCE_FILENAME = "zac_hpca18_schema2_reproduction.manifest.json"


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _find_hpca_source(directory: Path, circuit: str) -> Path:
    candidates = [directory / f"{circuit}.qasm",
                  directory / f"{circuit}_transpiled.qasm"]
    matches = [path.resolve() for path in candidates if path.is_file()]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one ZAC HPCA source for {circuit}, found {matches}")
    return matches[0]


def _compiler_sources(root: Path) -> List[Mapping[str, str]]:
    expected = root / "zac" / "zac.py"
    if not expected.is_file():
        raise ValueError(f"not an original ZAC compiler root: {root}")
    files = sorted(
        [path for path in (root / "zac").rglob("*.py") if path.is_file()] +
        ([root / "run.py"] if (root / "run.py").is_file() else [])
    )
    records = [{"path": str(path.resolve()),
                "relative_path": str(path.relative_to(root)),
                "sha256": sha256_file(path)} for path in files]
    if not records:
        raise ValueError(f"original ZAC source tree is empty: {root}")
    return records


def _build_request(hpca_directory: str | Path, architecture_path: str | Path,
                   zac_python: str | Path, compiler_root: str | Path
                   ) -> Mapping[str, Any]:
    hpca = Path(hpca_directory).resolve()
    architecture = Path(architecture_path).resolve()
    executable = Path(zac_python).expanduser().absolute()
    root = Path(compiler_root).resolve()
    if not hpca.is_dir():
        raise ValueError(f"missing ZAC HPCA directory: {hpca}")
    if not architecture.is_file():
        raise ValueError(f"missing ZAC architecture: {architecture}")
    if not executable.is_file():
        raise ValueError(f"missing ZAC Python executable: {executable}")
    sources = [{"circuit": circuit,
                "path": str(_find_hpca_source(hpca, circuit)),
                "sha256": sha256_file(_find_hpca_source(hpca, circuit))}
               for circuit in ZAC_HPCA18]
    compiler_sources = _compiler_sources(root)
    config = dict(ZAC_REPRODUCTION_CONFIG)
    request: Dict[str, Any] = {
        "experiment_schema": SCHEMA_VERSION,
        "track": "zac_original_reproduction_request",
        "hpca_directory": str(hpca),
        "sources": sources,
        "architecture_path": str(architecture),
        "architecture_sha256": sha256_file(architecture),
        "zac_python_path": str(executable),
        "zac_python_sha256": sha256_file(executable),
        "compiler_root": str(root),
        "compiler_sources": compiler_sources,
        "compiler_source_tree_sha256": stable_sha256(compiler_sources),
        "configuration": config,
        "configuration_sha256": stable_sha256(config),
    }
    request["request_id"] = stable_sha256(request)
    return request


def _read_object(path: Path, label: str) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


def _artifact_path(destination: Path, value: object, subdirectory: str) -> Path:
    path = Path(str(value)).resolve()
    allowed = (destination / subdirectory).resolve()
    if allowed not in path.parents:
        raise ValueError(f"ZAC reproduction artifact escaped {subdirectory}: {path}")
    return path


def _validate_result(destination: Path, request: Mapping[str, Any]
                     ) -> tuple[Mapping[str, Any], List[Mapping[str, str]]]:
    result_path = destination / RESULT_FILENAME
    result = _read_object(result_path, "ZAC reproduction result")
    required = {"experiment_schema", "track", "request_id", "package_versions",
                "rows"}
    if set(result) != required:
        raise ValueError("invalid ZAC reproduction result fields")
    if (result["experiment_schema"] != SCHEMA_VERSION or
            result["track"] != "zac_original_reproduction" or
            result["request_id"] != request["request_id"]):
        raise ValueError("ZAC reproduction result request drifted")
    versions = result["package_versions"]
    if (not isinstance(versions, Mapping) or
            str(versions.get("qiskit")) != "1.2.4"):
        raise ValueError(f"ZAC reproduction requires Qiskit 1.2.4, found {versions}")
    rows = result["rows"]
    if not isinstance(rows, list) or len(rows) != len(ZAC_HPCA18):
        raise ValueError("ZAC reproduction requires exactly 18 completed rows")
    source_by_circuit = {str(item["circuit"]): item
                         for item in request["sources"]}
    observed: set[str] = set()
    artifacts: List[Mapping[str, str]] = []
    row_fields = {
        "circuit", "source_path", "source_sha256", "architecture_sha256",
        "configuration_sha256", "compiler_source_tree_sha256",
        "structure", "code_path", "code_sha256", "fidelity_path",
        "fidelity_sha256", "time_path", "time_sha256", "cir_fidelity",
        "cir_duration_us",
    }
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != row_fields:
            raise ValueError("invalid ZAC reproduction row fields")
        circuit = str(row["circuit"])
        if circuit in observed:
            raise ValueError(f"duplicate ZAC reproduction row: {circuit}")
        observed.add(circuit)
        source = source_by_circuit.get(circuit)
        if (source is None or row["source_path"] != source["path"] or
                row["source_sha256"] != source["sha256"] or
                row["architecture_sha256"] != request["architecture_sha256"] or
                row["configuration_sha256"] != request["configuration_sha256"] or
                row["compiler_source_tree_sha256"] !=
                request["compiler_source_tree_sha256"]):
            raise ValueError(f"ZAC row provenance drifted: {circuit}")
        structure = row["structure"]
        if not isinstance(structure, Mapping) or set(structure) != {
                "source", "resynthesized", "scheduler", "emitted"}:
            raise ValueError(f"invalid ZAC structure record: {circuit}")
        for section in ("source", "resynthesized", "scheduler", "emitted"):
            values = structure[section]
            if (not isinstance(values, Mapping) or
                    any(not isinstance(value, int) or value < 0
                        for value in values.values())):
                raise ValueError(f"invalid ZAC {section} structure: {circuit}")
        if (int(structure["resynthesized"].get("qubits", -1)) <= 0 or
                int(structure["resynthesized"].get("gates_1q", -1)) !=
                int(structure["emitted"].get("gates_1q", -2)) or
                int(structure["resynthesized"].get("gates_2q", -1)) !=
                int(structure["emitted"].get("gates_2q", -2))):
            raise ValueError(f"inconsistent ZAC emitted structure: {circuit}")
        for kind in ("code", "fidelity", "time"):
            path = _artifact_path(destination, row[f"{kind}_path"], kind)
            expected_hash = str(row[f"{kind}_sha256"])
            if not path.is_file() or sha256_file(path) != expected_hash:
                raise ValueError(f"ZAC {kind} artifact changed: {path}")
            artifacts.append({"kind": kind, "path": str(path),
                              "sha256": expected_hash})
        fidelity_path = Path(str(row["fidelity_path"]))
        fidelity = _read_object(fidelity_path, "ZAC fidelity artifact")
        actual_fidelity = float(fidelity.get("cir_fidelity", math.nan))
        actual_duration = float(fidelity.get("cir_duration", math.nan))
        if (not math.isfinite(actual_fidelity) or
                not 0.0 <= actual_fidelity <= 1.0 or
                not math.isfinite(actual_duration) or actual_duration < 0.0 or
                actual_fidelity != float(row["cir_fidelity"]) or
                actual_duration != float(row["cir_duration_us"])):
            raise ValueError(f"invalid ZAC fidelity values: {circuit}")
    if observed != set(ZAC_HPCA18):
        raise ValueError("ZAC reproduction cohort has missing or extra rows")
    return result, sorted(artifacts, key=lambda item: (item["kind"], item["path"]))


def _validate_complete(destination: Path) -> Mapping[str, Any]:
    request_path = destination / REQUEST_FILENAME
    result_path = destination / RESULT_FILENAME
    evidence_path = destination / EVIDENCE_FILENAME
    if not all(path.is_file() for path in (request_path, result_path, evidence_path)):
        raise ValueError("ZAC reproduction result/evidence/request set is incomplete")
    request = _read_object(request_path, "ZAC reproduction request")
    if stable_sha256({key: value for key, value in request.items()
                      if key != "request_id"}) != request.get("request_id"):
        raise ValueError("ZAC reproduction request payload changed")
    result, artifacts = _validate_result(destination, request)
    evidence = _read_object(evidence_path, "ZAC reproduction evidence")
    required = {
        "experiment_schema", "track", "status", "request_id",
        "request_path", "request_sha256", "result_path", "result_sha256",
        "hpca_directory", "architecture_path", "architecture_sha256",
        "zac_python_path", "zac_python_sha256", "compiler_root",
        "compiler_source_tree_sha256", "configuration",
        "configuration_sha256", "package_versions", "rows", "artifacts",
        "stdout_path", "stdout_sha256", "stderr_path", "stderr_sha256",
        "evidence_payload_sha256",
    }
    if set(evidence) != required:
        raise ValueError("invalid ZAC reproduction evidence fields")
    unhashed = {key: value for key, value in evidence.items()
                if key != "evidence_payload_sha256"}
    if stable_sha256(unhashed) != evidence["evidence_payload_sha256"]:
        raise ValueError("ZAC reproduction evidence payload changed")
    if (evidence["experiment_schema"] != SCHEMA_VERSION or
            evidence["track"] != "zac_original_reproduction" or
            evidence["status"] != "complete" or
            evidence["request_id"] != request["request_id"] or
            evidence["request_path"] != str(request_path) or
            evidence["request_sha256"] != sha256_file(request_path) or
            evidence["result_path"] != str(result_path) or
            evidence["result_sha256"] != sha256_file(result_path) or
            evidence["package_versions"] != result["package_versions"] or
            evidence["rows"] != len(ZAC_HPCA18) or
            evidence["artifacts"] != artifacts):
        raise ValueError("ZAC reproduction evidence/result drifted")
    request_pairs = {
        "hpca_directory": request["hpca_directory"],
        "architecture_path": request["architecture_path"],
        "architecture_sha256": request["architecture_sha256"],
        "zac_python_path": request["zac_python_path"],
        "zac_python_sha256": request["zac_python_sha256"],
        "compiler_root": request["compiler_root"],
        "compiler_source_tree_sha256": request["compiler_source_tree_sha256"],
        "configuration": request["configuration"],
        "configuration_sha256": request["configuration_sha256"],
    }
    if any(evidence[key] != value for key, value in request_pairs.items()):
        raise ValueError("ZAC reproduction request provenance drifted")
    for field in ("stdout", "stderr"):
        path = Path(str(evidence[f"{field}_path"]))
        if (path != destination / f"{field}.log" or not path.is_file() or
                sha256_file(path) != evidence[f"{field}_sha256"]):
            raise ValueError(f"ZAC reproduction {field} log changed")
    allowed_top_level = {
        REQUEST_FILENAME, RESULT_FILENAME, EVIDENCE_FILENAME,
        "stdout.log", "stderr.log", "code", "fidelity", "time",
    }
    unexpected = sorted(path.name for path in destination.iterdir()
                        if path.name not in allowed_top_level)
    if unexpected:
        raise ValueError(f"untracked files in ZAC reproduction: {unexpected}")
    expected_by_kind = {
        kind: {Path(str(item["path"])).resolve()
               for item in artifacts if item["kind"] == kind}
        for kind in ("code", "fidelity", "time")
    }
    for kind, expected_paths in expected_by_kind.items():
        directory = destination / kind
        actual_paths = {path.resolve() for path in directory.iterdir()
                        if path.is_file()}
        if actual_paths != expected_paths or any(path.is_dir()
                                                  for path in directory.iterdir()):
            raise ValueError(f"untracked files in ZAC {kind} directory")
    # Recompute every live input and compiler hash.  Evidence is not considered
    # complete merely because its own JSON hashes are internally consistent.
    live_request = _build_request(
        request["hpca_directory"], request["architecture_path"],
        request["zac_python_path"], request["compiler_root"])
    if live_request != request:
        raise ValueError("ZAC reproduction source, architecture, Python, or code changed")
    return evidence


def validate_zac_reproduction_evidence(location: str | Path
                                        ) -> Mapping[str, Any]:
    path = Path(location).resolve()
    destination = path if path.is_dir() else path.parent
    if path.is_file() and path.name not in {RESULT_FILENAME, EVIDENCE_FILENAME}:
        raise ValueError(f"not a ZAC reproduction artifact: {path}")
    return _validate_complete(destination)


def _structure(circuit: object) -> Mapping[str, int]:
    data = list(getattr(circuit, "data"))
    return {
        "qubits": int(getattr(circuit, "num_qubits")),
        "gates_1q": sum(len(item.qubits) == 1 and
                        item.operation.name not in {"measure", "barrier"}
                        for item in data),
        "gates_2q": sum(len(item.qubits) == 2 for item in data),
        "depth": int(circuit.depth() or 0),
    }


def _worker(request_path: Path, destination: Path) -> None:
    import qiskit
    from qiskit import QuantumCircuit, transpile

    if qiskit.__version__ != "1.2.4":
        raise RuntimeError(
            f"ZAC reproduction requires Qiskit 1.2.4, found {qiskit.__version__}")
    request = _read_object(request_path, "ZAC reproduction request")
    root = Path(str(request["compiler_root"])).resolve()
    sys.path.insert(0, str(root))
    from zac import __file__ as zac_module_file
    from zac.ds.architecture import Architecture
    from zac.simulator.simulator import Simulator
    from zac.zac import ZAC

    if root not in Path(str(zac_module_file)).resolve().parents:
        raise RuntimeError(
            f"worker imported a non-original ZAC package: {zac_module_file}")
    if _build_request(
            request["hpca_directory"], request["architecture_path"],
            request["zac_python_path"], request["compiler_root"]) != request:
        raise ValueError("ZAC request changed before worker start")
    result_path = destination / RESULT_FILENAME
    if result_path.exists():
        raise ValueError(f"refusing to overwrite ZAC worker output: {result_path}")
    for subdirectory in ("code", "fidelity", "time"):
        target = destination / subdirectory
        if target.exists() and any(target.iterdir()):
            raise ValueError(f"refusing non-empty ZAC {subdirectory} directory")
        target.mkdir(parents=True, exist_ok=True)
    specification = json.loads(
        Path(str(request["architecture_path"])).read_text(encoding="utf-8"))
    architecture = Architecture(specification)
    architecture.preprocessing()
    try:
        scipy_version = importlib.metadata.version("scipy")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        scipy_version = "not-installed"
    try:
        numpy_version = importlib.metadata.version("numpy")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        numpy_version = "not-installed"
    package_versions = {
        "python": sys.version.split()[0],
        "qiskit": qiskit.__version__,
        "numpy": numpy_version,
        "scipy": scipy_version,
    }
    rows: List[Mapping[str, Any]] = []
    output_prefix = str(destination) + os.sep
    for source_record in request["sources"]:
        circuit_name = str(source_record["circuit"])
        source = Path(str(source_record["path"]))
        source_circuit = QuantumCircuit.from_qasm_file(str(source))
        resynthesized = transpile(
            source_circuit,
            basis_gates=list(request["configuration"]["qiskit_basis_gates"]),
            optimization_level=int(
                request["configuration"]["qiskit_optimization_level"]),
            seed_transpiler=int(request["configuration"]["seed_transpiler"]),
        )
        setting = {
            key: value for key, value in request["configuration"].items()
            if key not in {"simulation", "animation", "qiskit_basis_gates",
                           "qiskit_optimization_level", "seed_transpiler"}
        }
        setting.update({"name": circuit_name, "dir": output_prefix,
                        "arch_spec": str(request["architecture_path"])})
        compiler = ZAC()
        compiler.parse_setting(setting)
        compiler.set_architecture_spec_path(str(request["architecture_path"]))
        compiler.set_architecture(architecture)
        compiler.set_program(str(source))
        parsed_one_qubit = sum(len(gates) for gates in
                               compiler.dict_g_1q_parent.values())
        expected_structure = _structure(resynthesized)
        if ({"qubits": compiler.n_q, "gates_1q": parsed_one_qubit,
             "gates_2q": compiler.n_g} !=
                {key: expected_structure[key]
                 for key in ("qubits", "gates_1q", "gates_2q")}):
            raise ValueError(f"original ZAC parse drifted for {circuit_name}")
        code = compiler.solve(save_file=True)
        code_path = Path(compiler.code_filename).resolve()
        time_path = (destination / "time" / f"{circuit_name}_time.json").resolve()
        simulator = Simulator()
        simulator.set_arch_spec(specification)
        simulator.parse(str(code_path))
        fidelity = simulator.simulate()
        fidelity_path = (destination / "fidelity" /
                         f"{circuit_name}_fidelity.json").resolve()
        _atomic_json(fidelity_path, fidelity)
        scheduler_layers = list(compiler.gate_scheduling or [])
        emitted_instructions = list(code.get("instructions", []))
        emitted = {
            "gates_1q": sum(len(item.get("gates", [])) for item in
                            emitted_instructions if item.get("type") == "1qGate"),
            "gates_2q": sum(len(item.get("gates", [])) for item in
                            emitted_instructions if item.get("type") == "rydberg"),
            "instructions": len(emitted_instructions),
        }
        row = {
            "circuit": circuit_name,
            "source_path": str(source.resolve()),
            "source_sha256": sha256_file(source),
            "architecture_sha256": request["architecture_sha256"],
            "configuration_sha256": request["configuration_sha256"],
            "compiler_source_tree_sha256":
                request["compiler_source_tree_sha256"],
            "structure": {
                "source": _structure(source_circuit),
                "resynthesized": expected_structure,
                "scheduler": {
                    "two_qubit_layers": len(scheduler_layers),
                    "max_two_qubit_gates": max(
                        (len(layer) for layer in scheduler_layers), default=0),
                },
                "emitted": emitted,
            },
            "code_path": str(code_path), "code_sha256": sha256_file(code_path),
            "fidelity_path": str(fidelity_path),
            "fidelity_sha256": sha256_file(fidelity_path),
            "time_path": str(time_path), "time_sha256": sha256_file(time_path),
            "cir_fidelity": float(fidelity["cir_fidelity"]),
            "cir_duration_us": float(fidelity["cir_duration"]),
        }
        rows.append(row)
        _atomic_json(result_path, {
            "experiment_schema": SCHEMA_VERSION,
            "track": "zac_original_reproduction",
            "request_id": request["request_id"],
            "package_versions": package_versions,
            "rows": rows,
        })


WorkerRunner = Callable[[Sequence[str], Mapping[str, str], Path, Path, Path], None]


def _subprocess_worker(command: Sequence[str], environment: Mapping[str, str],
                       cwd: Path, stdout_path: Path, stderr_path: Path) -> None:
    with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
        subprocess.run(list(command), check=True, env=dict(environment),
                       cwd=str(cwd), stdout=stdout, stderr=stderr)


def reproduce_zac(hpca_directory: str | Path, architecture_path: str | Path,
                  zac_python: str | Path, compiler_root: str | Path,
                  output_directory: str | Path, *,
                  _runner: WorkerRunner = _subprocess_worker) -> Path:
    """Run or strictly reuse one complete, immutable original-ZAC attempt."""
    destination = Path(output_directory).resolve()
    request = _build_request(
        hpca_directory, architecture_path, zac_python, compiler_root)
    result_path = destination / RESULT_FILENAME
    evidence_path = destination / EVIDENCE_FILENAME
    if destination.exists() and any(destination.iterdir()):
        if result_path.is_file() and evidence_path.is_file():
            evidence = _validate_complete(destination)
            sealed_request = _read_object(
                destination / REQUEST_FILENAME, "ZAC reproduction request")
            if sealed_request != request:
                raise ValueError(
                    "requested ZAC reproduction differs from sealed reproduction")
            if evidence["request_id"] != request["request_id"]:
                raise ValueError("requested ZAC reproduction identity changed")
            return destination
        raise ValueError(
            "ZAC reproduction directory contains a partial attempt; use a fresh "
            f"unique output directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    request_path = destination / REQUEST_FILENAME
    _atomic_json(request_path, request)
    stdout_path = destination / "stdout.log"
    stderr_path = destination / "stderr.log"
    command = [
        str(request["zac_python_path"]), "-m",
        "experiments_v2.zac_reproduction", "--worker",
        "--request", str(request_path), "--output-directory", str(destination),
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    _runner(command, environment, Path(__file__).resolve().parents[2],
            stdout_path, stderr_path)
    if not stdout_path.is_file() or not stderr_path.is_file():
        raise ValueError("ZAC reproduction worker did not preserve stdout/stderr logs")
    result, artifacts = _validate_result(destination, request)
    evidence: Dict[str, Any] = {
        "experiment_schema": SCHEMA_VERSION,
        "track": "zac_original_reproduction",
        "status": "complete",
        "request_id": request["request_id"],
        "request_path": str(request_path),
        "request_sha256": sha256_file(request_path),
        "result_path": str(result_path),
        "result_sha256": sha256_file(result_path),
        "hpca_directory": request["hpca_directory"],
        "architecture_path": request["architecture_path"],
        "architecture_sha256": request["architecture_sha256"],
        "zac_python_path": request["zac_python_path"],
        "zac_python_sha256": request["zac_python_sha256"],
        "compiler_root": request["compiler_root"],
        "compiler_source_tree_sha256": request["compiler_source_tree_sha256"],
        "configuration": request["configuration"],
        "configuration_sha256": request["configuration_sha256"],
        "package_versions": result["package_versions"],
        "rows": len(result["rows"]),
        "artifacts": artifacts,
        "stdout_path": str(stdout_path), "stdout_sha256": sha256_file(stdout_path),
        "stderr_path": str(stderr_path), "stderr_sha256": sha256_file(stderr_path),
    }
    evidence["evidence_payload_sha256"] = stable_sha256(evidence)
    _atomic_json(evidence_path, evidence)
    _validate_complete(destination)
    return destination


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.worker:
        parser.error("this module command is an internal --worker entry point")
    _worker(args.request.resolve(), args.output_directory.resolve())


if __name__ == "__main__":
    main()


__all__ = [
    "EVIDENCE_FILENAME", "REQUEST_FILENAME", "RESULT_FILENAME", "ZAC_HPCA18",
    "ZAC_REPRODUCTION_CONFIG", "reproduce_zac",
    "validate_zac_reproduction_evidence",
]
