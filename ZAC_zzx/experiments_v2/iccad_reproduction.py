"""Reproduce the 15 overlapping ICCAD/QASMBench rows in two frozen envs.

The parent preprocessing phase must run under Qiskit 1.2.4.  It deliberately
retains ``id`` because this is an original-paper reproduction track.  The
separate Schema-2 canonical track removes ``id`` and must never consume these
files.  Compilation is delegated to the mqt.qmap 3.2.0 environment.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .contracts import SCHEMA_VERSION, sha256_file, stable_sha256


QASMBENCH: Sequence[Tuple[str, int]] = (
    ("ising", 42), ("ising", 98), ("qft", 18), ("qft", 29),
    ("bv", 30), ("bv", 70), ("wstate", 27), ("seca", 11),
    ("ghz", 40), ("ghz", 78), ("multiply", 13), ("cat", 22),
    ("cat", 35), ("swaptest", 25), ("knn", 31),
)

PREPROCESSING_SPEC: Mapping[str, object] = {
    "qiskit_version": "1.2.4",
    "basis_gates": ["cz", "id", "u1", "u2", "u3"],
    "optimization_level": 3,
    "seed_transpiler": 0,
    "removed_operations": ["measure", "barrier"],
    "retain_id": True,
    "two_qubit_layer_definition":
        "ASAP layers over two-qubit dependencies only; one-qubit gates ignored",
}

PAPER_ASTAR_PARAMETERS: Mapping[str, object] = {
    "alpha": 0.2,
    "beta": 0.2,
    "gamma": 5.0,
    "delta": 0.6,
    "maxNodes": 50_000_000,
    "routing_agnostic_fallback": False,
}

_EXPECTED_PACKAGES: Mapping[str, str] = {
    "mqt.qmap": "3.2.0",
    "mqt.core": "3.1.0",
}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _find_hpca(directory: Path, benchmark: str, qubits: int) -> Path:
    prefix = "swap_test" if benchmark == "swaptest" else benchmark
    matches = sorted(directory.glob(f"{prefix}_n{qubits}*.qasm"))
    if len(matches) != 1:
        raise ValueError(f"expected one {benchmark} n{qubits} input, found {matches}")
    return matches[0]


def _two_qubit_shape(circuit: object) -> Tuple[int, int, int]:
    """Return gate count, ASAP 2Q-layer count and max gates in one layer.

    This is the structural convention used by ICCAD Table I.  One-qubit gates
    do not delay a two-qubit layer; dependencies between two-qubit gates that
    touch the same logical qubit do.
    """
    number_qubits = int(getattr(circuit, "num_qubits"))
    last_layer = [-1] * number_qubits
    layer_population: Dict[int, int] = {}
    gates = 0
    for instruction in getattr(circuit, "data"):
        qubits = list(getattr(instruction, "qubits"))
        if len(qubits) != 2:
            continue
        indices = [int(circuit.find_bit(qubit).index) for qubit in qubits]
        layer = max(last_layer[index] for index in indices) + 1
        for index in indices:
            last_layer[index] = layer
        layer_population[layer] = layer_population.get(layer, 0) + 1
        gates += 1
    layers = max(layer_population, default=-1) + 1
    maximum = max(layer_population.values(), default=0)
    return gates, layers, maximum


def _validate_input_manifest(path: Path, *, verify_sources: bool = True
                             ) -> Mapping[str, Any]:
    if not path.is_file():
        raise ValueError(f"missing ICCAD reproduction input manifest: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("ICCAD reproduction input manifest must be a JSON object")
    required = {"experiment_schema", "track", "preprocessing", "records"}
    if set(payload) != required:
        raise ValueError(
            "ICCAD reproduction input manifest fields differ: "
            f"expected={sorted(required)}, actual={sorted(payload)}")
    if payload["experiment_schema"] != SCHEMA_VERSION:
        raise ValueError("ICCAD reproduction input manifest is not Schema 2")
    if payload["track"] != "iccad_original_reproduction_input":
        raise ValueError("wrong ICCAD reproduction input track")
    if payload["preprocessing"] != PREPROCESSING_SPEC:
        raise ValueError("ICCAD reproduction preprocessing configuration drifted")
    records = payload["records"]
    if not isinstance(records, list) or len(records) != len(QASMBENCH):
        raise ValueError("ICCAD reproduction requires exactly 15 input records")
    expected_keys = set(QASMBENCH)
    observed_keys: set[Tuple[str, int]] = set()
    expected_record_fields = {
        "benchmark", "qubits", "source_path", "source_sha256", "path",
        "preprocessed_sha256", "gates_1q", "gates_2q", "depth",
        "two_qubit_layers", "max_two_qubit_gates",
    }
    for record in records:
        if not isinstance(record, Mapping) or set(record) != expected_record_fields:
            raise ValueError("invalid ICCAD reproduction input record fields")
        key = (str(record["benchmark"]), int(record["qubits"]))
        if key in observed_keys:
            raise ValueError(f"duplicate ICCAD reproduction input: {key}")
        observed_keys.add(key)
        source = Path(str(record["source_path"]))
        preprocessed = Path(str(record["path"]))
        if not preprocessed.is_file() or sha256_file(preprocessed) != record["preprocessed_sha256"]:
            raise ValueError(f"preprocessed ICCAD input changed: {preprocessed}")
        if verify_sources and (
                not source.is_file() or sha256_file(source) != record["source_sha256"]):
            raise ValueError(f"source ICCAD input changed: {source}")
        if (int(record["qubits"]) <= 0 or int(record["gates_1q"]) < 0 or
                int(record["gates_2q"]) < 0 or int(record["depth"]) < 0 or
                int(record["two_qubit_layers"]) < 0 or
                int(record["max_two_qubit_gates"]) < 0):
            raise ValueError(f"invalid structural counts for ICCAD input {key}")
    if observed_keys != expected_keys:
        raise ValueError(
            f"wrong ICCAD reproduction input cohort: {sorted(observed_keys)}")
    return payload


def _assert_hpca_matches_records(hpca: Path,
                                 records: Sequence[Mapping[str, object]]) -> None:
    by_key = {(str(record["benchmark"]), int(record["qubits"])): record
              for record in records}
    for benchmark, qubits in QASMBENCH:
        requested = _find_hpca(hpca, benchmark, qubits).resolve()
        sealed = by_key[(benchmark, qubits)]
        if (requested != Path(str(sealed["source_path"])).resolve() or
                sha256_file(requested) != sealed["source_sha256"]):
            raise ValueError(
                "requested HPCA source differs from sealed ICCAD input: "
                f"{benchmark}_n{qubits}")


def _reuse_prepared_inputs(destination: Path, hpca: Path
                           ) -> List[Mapping[str, object]] | None:
    manifest = destination / "inputs.json"
    entries = list(destination.iterdir()) if destination.exists() else []
    if not entries:
        return None
    if not manifest.is_file():
        raise ValueError(
            "ICCAD preprocessing directory is non-empty but has no immutable "
            f"manifest: {destination}")
    payload = _validate_input_manifest(manifest)
    allowed = {"inputs.json"} | {
        Path(str(record["path"])).name for record in payload["records"]}
    unexpected = sorted(path.name for path in entries if path.name not in allowed)
    if unexpected:
        raise ValueError(
            f"untracked files in ICCAD preprocessing directory: {unexpected}")
    _assert_hpca_matches_records(hpca, payload["records"])
    return list(payload["records"])


def prepare_reproduction_inputs(hpca_directory: str | Path, output: str | Path
                                ) -> List[Mapping[str, object]]:
    import qiskit
    from qiskit import QuantumCircuit, qasm2, transpile

    if qiskit.__version__ != "1.2.4":
        raise RuntimeError(f"ICCAD reproduction preprocessing requires Qiskit 1.2.4, found {qiskit.__version__}")
    hpca = Path(hpca_directory).resolve()
    destination = Path(output).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    reused = _reuse_prepared_inputs(destination, hpca)
    if reused is not None:
        return reused
    records: List[Mapping[str, object]] = []
    for benchmark, qubits in QASMBENCH:
        source = _find_hpca(hpca, benchmark, qubits)
        circuit = QuantumCircuit.from_qasm_file(str(source))
        transpiled = transpile(
            circuit, basis_gates=["cz", "id", "u1", "u2", "u3"],
            optimization_level=3, seed_transpiler=0)
        stripped = QuantumCircuit(*transpiled.qregs, *transpiled.cregs)
        for instruction in transpiled.data:
            if instruction.operation.name not in {"measure", "barrier"}:
                stripped.append(instruction)
        target = destination / f"{benchmark}_n{qubits}.qasm"
        temporary = target.with_name(target.name + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            qasm2.dump(stripped, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        gates_2q, layers_2q, maximum_2q = _two_qubit_shape(stripped)
        records.append({
            "benchmark": benchmark, "qubits": qubits,
            "source_path": str(source.resolve()),
            "source_sha256": sha256_file(source),
            "path": str(target),
            "preprocessed_sha256": sha256_file(target),
            "gates_1q": sum(len(item.qubits) == 1 for item in stripped.data),
            "gates_2q": gates_2q,
            "depth": stripped.depth(),
            "two_qubit_layers": layers_2q,
            "max_two_qubit_gates": maximum_2q,
        })
    _atomic_json(destination / "inputs.json", {
        "experiment_schema": SCHEMA_VERSION,
        "track": "iccad_original_reproduction_input",
        "preprocessing": dict(PREPROCESSING_SPEC),
        "records": records,
    })
    _validate_input_manifest(destination / "inputs.json")
    return records


def _worker(input_manifest: Path, architecture_path: Path, output_path: Path,
            native_directory: Path) -> None:
    # Imports intentionally live here: the parent Qiskit environment does not
    # need MQT, and the worker MQT environment need not be Qiskit 1.2.4.
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "experiments"))
    from mqt.core import load
    from mqt.qmap.na.zoned import (
        RoutingAgnosticCompiler, RoutingAwareCompiler,
        ZonedNeutralAtomArchitecture,
    )
    from qiskit import QuantumCircuit
    from naviz_eval import NavizEvaluator
    from spec_convert import convert

    qmap_version = importlib.metadata.version("mqt.qmap")
    core_version = importlib.metadata.version("mqt.core")
    if (qmap_version, core_version) != ("3.2.0", "3.1.0"):
        raise RuntimeError(
            f"requires mqt.qmap==3.2.0,mqt-core==3.1.0; found {qmap_version},{core_version}")
    if output_path.exists():
        raise ValueError(f"refusing to overwrite ICCAD worker output: {output_path}")
    if native_directory.exists() and any(native_directory.iterdir()):
        raise ValueError(
            f"refusing non-empty ICCAD worker native directory: {native_directory}")
    input_payload = _validate_input_manifest(input_manifest)
    records = input_payload["records"]
    zac_architecture = json.loads(architecture_path.read_text(encoding="utf-8"))
    specification = convert(zac_architecture)
    architecture = ZonedNeutralAtomArchitecture.from_json_string(json.dumps(specification))
    evaluator = NavizEvaluator(specification)
    native_directory.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, object]] = []
    for record in records:
        if sha256_file(record["path"]) != record["preprocessed_sha256"]:
            raise ValueError(f"worker input changed before compile: {record['path']}")
        qiskit_circuit = QuantumCircuit.from_qasm_file(record["path"])
        observed_2q, observed_layers, observed_maximum = _two_qubit_shape(qiskit_circuit)
        observed_1q = sum(len(item.qubits) == 1 for item in qiskit_circuit.data)
        observed_structure = {
            "qubits": qiskit_circuit.num_qubits,
            "gates_1q": observed_1q,
            "gates_2q": observed_2q,
            "depth": qiskit_circuit.depth(),
            "two_qubit_layers": observed_layers,
            "max_two_qubit_gates": observed_maximum,
        }
        expected_structure = {key: int(record[key]) for key in observed_structure}
        if observed_structure != expected_structure:
            raise ValueError(
                f"worker input structure changed for {record['benchmark']}: "
                f"expected={expected_structure}, observed={observed_structure}")
        circuit = load(qiskit_circuit)
        for kind in ("agnostic", "astar"):
            if kind == "agnostic":
                compiler = RoutingAgnosticCompiler(architecture, log_level="ERROR")
            else:
                compiler = RoutingAwareCompiler(
                    architecture, log_level="ERROR", deepening_factor=0.6,
                    deepening_value=0.2, lookahead_factor=0.2,
                    reuse_level=5.0, max_nodes=50_000_000,
                )
            native = compiler.compile(circuit)
            stats = compiler.stats()
            native_path = native_directory / f"{record['benchmark']}_n{record['qubits']}.{kind}.na"
            native_temporary = native_path.with_name(native_path.name + ".tmp")
            with open(native_temporary, "w", encoding="utf-8") as handle:
                handle.write(native)  # keep every @+ u
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(native_temporary, native_path)
            evaluator.reset()
            movement = evaluator.evaluate(native)
            rows.append({
                "circuit": record["benchmark"], "qubits": record["qubits"],
                "config": kind,
                "input_sha256": record["preprocessed_sha256"],
                "input_manifest_sha256": sha256_file(input_manifest),
                "architecture_sha256": sha256_file(architecture_path),
                "gates_1q": record["gates_1q"],
                "two_qubit_gates": record["gates_2q"],
                "input_depth": record["depth"],
                "input_two_qubit_layers": record["two_qubit_layers"],
                "input_max_two_qubit_gates": record["max_two_qubit_gates"],
                "place_ms": float(stats.get("placementTime", 0)) / 1000.0,
                "route_ms": float(stats.get("routingTime", 0)) / 1000.0,
                "total_ms": float(stats.get("totalTime", 0)) / 1000.0,
                "steps": movement["rearrangement_steps"],
                "rearr_us": movement["rearrangement_duration"],
                "layers": movement["two_qubit_gate_layer"],
                "max_gates": movement["max_two_qubit_gates"],
                "native_path": str(native_path),
                "native_sha256": sha256_file(native_path),
                "mqt_qmap_version": qmap_version,
                "mqt_core_version": core_version,
                "astar_parameters": (dict(PAPER_ASTAR_PARAMETERS)
                                     if kind == "astar" else None),
            })
            _atomic_json(output_path, rows)


def evidence_manifest_path(result_path: str | Path) -> Path:
    path = Path(result_path)
    return path.with_name(path.stem + ".manifest.json")


def _validate_completed_reproduction(result_path: Path,
                                     evidence_path: Path) -> Mapping[str, Any]:
    if not result_path.is_file() or not evidence_path.is_file():
        raise ValueError("ICCAD reproduction result/evidence pair is incomplete")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    required = {
        "experiment_schema", "track", "status", "result_path",
        "result_sha256", "input_manifest_path", "input_manifest_sha256",
        "hpca_directory", "qmap_python_path", "qmap_python_sha256",
        "architecture_path", "architecture_sha256", "package_versions",
        "astar_parameters", "rows", "native_outputs",
        "evidence_payload_sha256",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != required:
        raise ValueError("invalid ICCAD reproduction evidence fields")
    if (evidence["experiment_schema"] != SCHEMA_VERSION or
            evidence["track"] != "iccad_original_reproduction" or
            evidence["status"] != "complete"):
        raise ValueError("invalid ICCAD reproduction evidence header")
    unhashed = {key: value for key, value in evidence.items()
                if key != "evidence_payload_sha256"}
    if stable_sha256(unhashed) != evidence["evidence_payload_sha256"]:
        raise ValueError("ICCAD reproduction evidence payload changed")
    if (Path(str(evidence["result_path"])).resolve() != result_path.resolve() or
            sha256_file(result_path) != evidence["result_sha256"]):
        raise ValueError("ICCAD reproduction result changed after completion")
    input_manifest = Path(str(evidence["input_manifest_path"]))
    if (not input_manifest.is_file() or
            sha256_file(input_manifest) != evidence["input_manifest_sha256"]):
        raise ValueError("ICCAD reproduction input manifest changed")
    input_payload = _validate_input_manifest(input_manifest)
    architecture = Path(str(evidence["architecture_path"]))
    if (not architecture.is_file() or
            sha256_file(architecture) != evidence["architecture_sha256"]):
        raise ValueError("ICCAD reproduction architecture changed")
    if evidence["package_versions"] != _EXPECTED_PACKAGES:
        raise ValueError("ICCAD reproduction package versions drifted")
    if evidence["astar_parameters"] != PAPER_ASTAR_PARAMETERS:
        raise ValueError("ICCAD reproduction A* parameters drifted")
    qmap_python = Path(str(evidence["qmap_python_path"]))
    if (not qmap_python.is_file() or
            sha256_file(qmap_python) != evidence["qmap_python_sha256"]):
        raise ValueError("ICCAD reproduction Python executable changed")
    rows = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or len(rows) != 30 or evidence["rows"] != 30:
        raise ValueError("ICCAD reproduction must contain 30 compiler rows")
    native_outputs = evidence["native_outputs"]
    if not isinstance(native_outputs, list) or len(native_outputs) != 30:
        raise ValueError("ICCAD reproduction evidence requires 30 native outputs")
    if any(not isinstance(item, Mapping) or set(item) != {"path", "sha256"}
           for item in native_outputs):
        raise ValueError("invalid ICCAD native-output evidence fields")
    expected_native = {str(Path(str(item["path"])).resolve()): str(item["sha256"])
                       for item in native_outputs}
    if len(expected_native) != 30:
        raise ValueError("duplicate native output in ICCAD evidence")
    input_hashes = {
        (str(record["benchmark"]), int(record["qubits"])):
            str(record["preprocessed_sha256"])
        for record in input_payload["records"]
    }
    observed_keys: set[Tuple[str, str, int]] = set()
    for row in rows:
        key = (str(row.get("config")), str(row.get("circuit")),
               int(row.get("qubits", -1)))
        if key in observed_keys:
            raise ValueError(f"duplicate ICCAD reproduction row: {key}")
        observed_keys.add(key)
        input_hash = input_hashes.get((key[1], key[2]))
        if (key[0] not in {"agnostic", "astar"} or input_hash is None or
                row.get("input_sha256") != input_hash or
                row.get("input_manifest_sha256") != evidence["input_manifest_sha256"] or
                row.get("architecture_sha256") != evidence["architecture_sha256"] or
                row.get("mqt_qmap_version") != _EXPECTED_PACKAGES["mqt.qmap"] or
                row.get("mqt_core_version") != _EXPECTED_PACKAGES["mqt.core"] or
                (key[0] == "astar" and
                 row.get("astar_parameters") != PAPER_ASTAR_PARAMETERS) or
                (key[0] == "agnostic" and row.get("astar_parameters") is not None)):
            raise ValueError(f"ICCAD row provenance drifted: {key}")
        native = Path(str(row["native_path"])).resolve()
        expected_hash = expected_native.get(str(native))
        if (expected_hash is None or not native.is_file() or
                sha256_file(native) != expected_hash or
                row.get("native_sha256") != expected_hash):
            raise ValueError(f"ICCAD native output changed: {native}")
    expected_rows = {(kind, benchmark, qubits)
                     for kind in ("agnostic", "astar")
                     for benchmark, qubits in QASMBENCH}
    if observed_keys != expected_rows:
        raise ValueError("ICCAD reproduction row cohort changed")
    return evidence


def _assert_request_matches_evidence(
        evidence: Mapping[str, Any], hpca_directory: str | Path,
        architecture_path: str | Path, qmap_python: str | Path) -> None:
    hpca = Path(hpca_directory).resolve()
    if hpca != Path(str(evidence["hpca_directory"])).resolve():
        raise ValueError("requested HPCA directory differs from sealed reproduction")
    input_payload = _validate_input_manifest(
        Path(str(evidence["input_manifest_path"])))
    _assert_hpca_matches_records(hpca, input_payload["records"])
    architecture = Path(architecture_path).resolve()
    if (architecture != Path(str(evidence["architecture_path"])).resolve() or
            not architecture.is_file() or
            sha256_file(architecture) != evidence["architecture_sha256"]):
        raise ValueError("requested architecture differs from sealed reproduction")
    requested_python = Path(qmap_python).absolute()
    if (requested_python != Path(str(evidence["qmap_python_path"])).absolute() or
            not requested_python.is_file() or
            sha256_file(requested_python) != evidence["qmap_python_sha256"]):
        raise ValueError("requested QMAP Python differs from sealed reproduction")


def validate_iccad_reproduction_evidence(result_path: str | Path
                                         ) -> Mapping[str, Any]:
    """Validate every immutable input, result and native-output hash."""
    result = Path(result_path).resolve()
    return _validate_completed_reproduction(result, evidence_manifest_path(result))


def reproduce_iccad(hpca_directory: str | Path, architecture_path: str | Path,
                    qmap_python: str | Path, output_directory: str | Path
                    ) -> Path:
    destination = Path(output_directory).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    preprocessed = destination / "inputs"
    output = destination / "qmap_table1_v32_schema2_reproduction.json"
    evidence_path = evidence_manifest_path(output)
    if output.exists() or evidence_path.exists():
        evidence = _validate_completed_reproduction(output, evidence_path)
        _assert_request_matches_evidence(
            evidence, hpca_directory, architecture_path, qmap_python)
        return output
    native_directory = destination / "native"
    if native_directory.exists() and any(native_directory.iterdir()):
        raise ValueError(
            "ICCAD reproduction has native outputs but no completed evidence; "
            "use a fresh output directory")
    prepare_reproduction_inputs(hpca_directory, preprocessed)
    input_manifest = preprocessed / "inputs.json"
    architecture = Path(architecture_path).resolve()
    if not architecture.is_file():
        raise ValueError(f"missing ICCAD architecture: {architecture}")
    qmap_executable = Path(qmap_python).absolute()
    if not qmap_executable.is_file():
        raise ValueError(f"missing QMAP Python executable: {qmap_executable}")
    command = [
        # Do not resolve a venv's ``bin/python`` symlink: invoking the real
        # framework binary would lose the virtual-environment prefix.
        str(qmap_executable), "-m", "experiments_v2.iccad_reproduction",
        "--worker", "--input-manifest", str(input_manifest),
        "--architecture", str(architecture),
        "--output", str(output), "--native-directory", str(native_directory),
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    subprocess.run(command, check=True, env=environment,
                   cwd=str(Path(__file__).resolve().parents[2]))
    rows = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or len(rows) != 30:
        raise ValueError("ICCAD reproduction worker did not produce 30 rows")
    versions = {
        "mqt.qmap": {str(row.get("mqt_qmap_version")) for row in rows},
        "mqt.core": {str(row.get("mqt_core_version")) for row in rows},
    }
    if versions != {key: {value} for key, value in _EXPECTED_PACKAGES.items()}:
        raise ValueError(f"ICCAD reproduction package evidence drifted: {versions}")
    native_outputs = sorted(({
        "path": str(Path(str(row["native_path"])).resolve()),
        "sha256": str(row["native_sha256"]),
    } for row in rows), key=lambda item: item["path"])
    evidence: Dict[str, Any] = {
        "experiment_schema": SCHEMA_VERSION,
        "track": "iccad_original_reproduction",
        "status": "complete",
        "result_path": str(output),
        "result_sha256": sha256_file(output),
        "input_manifest_path": str(input_manifest),
        "input_manifest_sha256": sha256_file(input_manifest),
        "hpca_directory": str(Path(hpca_directory).resolve()),
        "qmap_python_path": str(qmap_executable),
        "qmap_python_sha256": sha256_file(qmap_executable),
        "architecture_path": str(architecture),
        "architecture_sha256": sha256_file(architecture),
        "package_versions": dict(_EXPECTED_PACKAGES),
        "astar_parameters": dict(PAPER_ASTAR_PARAMETERS),
        "rows": len(rows),
        "native_outputs": native_outputs,
    }
    evidence["evidence_payload_sha256"] = stable_sha256(evidence)
    _atomic_json(evidence_path, evidence)
    _validate_completed_reproduction(output, evidence_path)
    return output


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--input-manifest", type=Path)
    parser.add_argument("--architecture", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--native-directory", type=Path)
    parser.add_argument("--hpca-directory", type=Path)
    parser.add_argument("--qmap-python", type=Path)
    parser.add_argument("--output-directory", type=Path)
    args = parser.parse_args(argv)
    if args.worker:
        if not all((args.input_manifest, args.output, args.native_directory)):
            parser.error("worker requires input-manifest, output, and native-directory")
        _worker(args.input_manifest, args.architecture, args.output, args.native_directory)
    else:
        if not all((args.hpca_directory, args.qmap_python, args.output_directory)):
            parser.error("requires hpca-directory, qmap-python, and output-directory")
        print(reproduce_iccad(args.hpca_directory, args.architecture,
                             args.qmap_python, args.output_directory))


if __name__ == "__main__":
    main()


__all__ = [
    "PAPER_ASTAR_PARAMETERS", "PREPROCESSING_SPEC", "QASMBENCH",
    "evidence_manifest_path", "prepare_reproduction_inputs", "reproduce_iccad",
    "validate_iccad_reproduction_evidence",
]
