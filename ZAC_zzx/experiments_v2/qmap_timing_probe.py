"""Output-equivalence probe for the timing-instrumented QMAP 3.2 build."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(REPO_ROOT / "experiments"))


def compile_probe(input_path: Path, architecture_path: Path,
                  config_path: Path) -> dict[str, Any]:
    from mqt.core import load
    from mqt.qmap.na.zoned import (RoutingAwareCompiler,
                                   ZonedNeutralAtomArchitecture)
    from qiskit import QuantumCircuit
    from spec_convert import convert

    config = json.loads(config_path.read_text(encoding="utf-8"))
    architecture_payload = json.loads(architecture_path.read_text(encoding="utf-8"))
    architecture = ZonedNeutralAtomArchitecture.from_json_string(
        json.dumps(convert(architecture_payload)))
    circuit = load(QuantumCircuit.from_qasm_file(str(input_path.resolve())))
    compiler = RoutingAwareCompiler(
        architecture,
        log_level="ERROR",
        deepening_factor=config["deepening_factor"],
        deepening_value=config["deepening_value"],
        lookahead_factor=config["lookahead_factor"],
        reuse_level=config["reuse_level"],
        max_nodes=config["max_nodes"],
    )
    native = compiler.compile(circuit)
    return {
        "input": input_path.name,
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "native_sha256": hashlib.sha256(native.encode()).hexdigest(),
        "native_bytes": len(native.encode()),
        "mqt_qmap_version": importlib.metadata.version("mqt.qmap"),
        "mqt_core_version": importlib.metadata.version("mqt.core"),
        "stats": compiler.stats(),
    }


def _run_interpreter(interpreter: Path, input_path: Path,
                     architecture_path: Path, config_path: Path) -> Mapping[str, Any]:
    environment = os.environ.copy()
    python_path = os.pathsep.join((str(PACKAGE_ROOT), str(REPO_ROOT / "experiments")))
    if environment.get("PYTHONPATH"):
        python_path += os.pathsep + environment["PYTHONPATH"]
    environment["PYTHONPATH"] = python_path
    output = subprocess.check_output([
        str(interpreter), "-m", "experiments_v2.qmap_timing_probe",
        "--input", str(input_path), "--architecture", str(architecture_path),
        "--config", str(config_path),
    ], text=True, env=environment)
    return json.loads(output)


def verify_interpreters(official_python: Path, timing_python: Path,
                        inputs: Sequence[Path], architecture_path: Path,
                        config_path: Path) -> dict[str, Any]:
    circuits = []
    for input_path in inputs:
        official = _run_interpreter(
            official_python, input_path, architecture_path, config_path)
        timing = _run_interpreter(
            timing_python, input_path, architecture_path, config_path)
        same = official["native_sha256"] == timing["native_sha256"]
        timing_stats = timing["stats"]
        required = {"initialPlacementTime", "layerPlacementTime",
                    "reuseAnalysisTime", "placementTime", "routingTime"}
        circuits.append({
            "circuit": input_path.stem,
            "same_native": same,
            "official": official,
            "timing": timing,
            "timing_fields_complete": required <= set(timing_stats),
            "transition_decision_us": (
                int(timing_stats.get("reuseAnalysisTime", 0)) +
                int(timing_stats.get("layerPlacementTime", 0))),
        })
    valid = all(row["same_native"] and row["timing_fields_complete"]
                for row in circuits)
    return {
        "protocol": "qmap-3.2-timing-instrumentation-v1",
        "valid": valid,
        "circuits": circuits,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--inputs", nargs="*", type=Path)
    parser.add_argument("--architecture", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--official-python", type=Path)
    parser.add_argument("--timing-python", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.official_python or args.timing_python:
        if not args.official_python or not args.timing_python or not args.inputs:
            parser.error("parity mode requires both interpreters and --inputs")
        result = verify_interpreters(
            args.official_python, args.timing_python, args.inputs,
            args.architecture, args.config)
    elif args.input:
        result = compile_probe(args.input, args.architecture, args.config)
    else:
        parser.error("provide --input or parity-mode arguments")
    payload = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0 if result.get("valid", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["compile_probe", "verify_interpreters"]
