"""Original-paper reproduction gate, kept separate from unified scoring."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Dict, List, Mapping

from .contracts import sha256_file
from .iccad_reproduction import (PAPER_ASTAR_PARAMETERS,
                                 validate_iccad_reproduction_evidence)
from .zac_reproduction import (RESULT_FILENAME as ZAC_RESULT_FILENAME,
                               validate_zac_reproduction_evidence)


def _relative_error(actual: float, expected: float) -> float:
    return abs(actual - expected) / abs(expected) if expected else abs(actual - expected)


def assess_zac_reproduction(truth_csv: str | Path, fidelity_directory: str | Path,
                            *, fidelity_tolerance: float = 0.005,
                            duration_relative_tolerance: float = 0.03,
                            require_evidence: bool = False,
                            ) -> Mapping[str, object]:
    truth_path = Path(truth_csv).resolve()
    with open(truth_path, newline="", encoding="utf-8") as handle:
        truth_rows = list(csv.DictReader(handle))
    truth = {row["circuit"]: row for row in truth_rows}
    if len(truth) != len(truth_rows):
        raise ValueError("duplicate circuit in ZAC paper truth")
    structure_fields = {
        "qubits", "gates_1q", "gates_2q", "depth",
        "two_qubit_layers", "max_two_qubit_gates", "structure_source",
    }
    structure_available = all(
        structure_fields.issubset(row) and
        all(str(row.get(field, "")).strip() for field in structure_fields)
        for row in truth_rows)
    partial_structure = any(
        any(str(row.get(field, "")).strip() for field in structure_fields)
        for row in truth_rows)
    if partial_structure and not structure_available:
        raise ValueError("ZAC structure reference must be complete for every row")
    if require_evidence and not structure_available:
        raise ValueError(
            "formal ZAC reproduction requires a complete frozen structure reference")
    supplied = Path(fidelity_directory).resolve()
    sealed = (supplied.is_file() or
              (supplied / ZAC_RESULT_FILENAME).is_file())
    run_evidence: Mapping[str, object] | None = None
    structures: Dict[str, Mapping[str, object]] = {}
    if sealed:
        run_evidence = validate_zac_reproduction_evidence(supplied)
        directory = supplied if supplied.is_dir() else supplied.parent
        result_path = directory / ZAC_RESULT_FILENAME
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        result_rows = payload["rows"]
        available = {str(row["circuit"]): Path(str(row["fidelity_path"]))
                     for row in result_rows}
        structures = {str(row["circuit"]): row["structure"]
                      for row in result_rows}
    else:
        if require_evidence:
            raise ValueError(
                "formal ZAC reproduction requires a sealed fresh-run evidence "
                f"directory, not legacy fidelity files: {supplied}")
        directory = supplied
        available = {path.name.removesuffix("_fidelity.json"): path
                     for path in directory.glob("*_fidelity.json")}
    rows: List[Dict[str, object]] = []
    for circuit, expected in sorted(truth.items()):
        path = available.get(circuit) or available.get(circuit + "_transpiled")
        if path is None:
            rows.append({"circuit": circuit, "passed": False, "error": "missing"})
            continue
        with open(path, encoding="utf-8") as handle:
            actual = json.load(handle)
        fidelity_actual = float(actual["cir_fidelity"])
        duration_actual = float(actual["cir_duration"])
        if (not math.isfinite(fidelity_actual) or not 0.0 <= fidelity_actual <= 1.0 or
                not math.isfinite(duration_actual) or duration_actual < 0.0):
            raise ValueError(f"invalid ZAC reproduction values: {path}")
        fidelity_error = fidelity_actual - float(expected["fidelity"])
        duration_error = (duration_actual /
                          float(expected["duration_us"]) - 1.0)
        passed = (abs(fidelity_error) <= fidelity_tolerance and
                  abs(duration_error) <= duration_relative_tolerance)
        expected_structure: Mapping[str, object] | None = None
        structure_exact: bool | None = None
        if structure_available:
            observed = structures.get(circuit)
            expected_structure = {
                "resynthesized": {
                    "qubits": int(expected["qubits"]),
                    "gates_1q": int(expected["gates_1q"]),
                    "gates_2q": int(expected["gates_2q"]),
                    "depth": int(expected["depth"]),
                },
                "scheduler": {
                    "two_qubit_layers": int(expected["two_qubit_layers"]),
                    "max_two_qubit_gates": int(expected["max_two_qubit_gates"]),
                },
            }
            structure_exact = bool(
                isinstance(observed, Mapping) and
                observed.get("resynthesized") ==
                expected_structure["resynthesized"] and
                observed.get("scheduler") == expected_structure["scheduler"] and
                isinstance(observed.get("emitted"), Mapping) and
                int(observed["emitted"].get("gates_1q", -1)) ==
                int(expected["gates_1q"]) and
                int(observed["emitted"].get("gates_2q", -1)) ==
                int(expected["gates_2q"])
            )
            passed = passed and structure_exact
        rows.append({
            "circuit": circuit, "fidelity_actual": actual["cir_fidelity"],
            "fidelity_paper": float(expected["fidelity"]),
            "fidelity_absolute_error": fidelity_error,
            "duration_actual_us": actual["cir_duration"],
            "duration_paper_us": float(expected["duration_us"]),
            "duration_relative_error": duration_error, "passed": passed,
            "fidelity_artifact_path": str(path.resolve()),
            "fidelity_artifact_sha256": sha256_file(path),
            "structure_observed": structures.get(circuit),
            "structure_expected": expected_structure,
            "structure_exact": structure_exact,
        })
    return {
        "track": "baseline_reproduction",
        "method": "M1_ZAC_original",
        "criteria": {"fidelity_absolute": fidelity_tolerance,
                     "duration_relative": duration_relative_tolerance},
        "evidence": {
            "paper_truth_path": str(truth_path),
            "paper_truth_sha256": sha256_file(truth_path),
            "fidelity_directory": str(directory.resolve()),
            "fresh_rerun_evidence": run_evidence,
            "paper_structure_truth_available": False,
            "artifact_structure_reference_available": structure_available,
            "structure_gate": ("artifact_reference_exact" if structure_available
                               else "not_asserted"),
            "structure_reference_sources": sorted({
                row["structure_source"] for row in truth_rows
                if row.get("structure_source")}),
            "note": (
                "Fidelity and duration are checked against the paper-derived "
                "table. Structural quantities are checked exactly against the "
                "tracked original-artifact/Qiskit-1.2.4 reference and are not "
                "mislabelled as paper-published structure."
            ),
        },
        "passed": len(rows) == len(truth) and all(row["passed"] for row in rows),
        "passed_n": sum(bool(row["passed"]) for row in rows),
        "total_n": len(truth), "rows": rows,
    }


def assess_iccad_reproduction(truth_csv: str | Path, result_json: str | Path,
                              *, aggregate_relative_tolerance: float = 0.02,
                              minimum_exact_steps: int = 13) -> Mapping[str, object]:
    truth_path = Path(truth_csv).resolve()
    result_path = Path(result_json).resolve()
    evidence = validate_iccad_reproduction_evidence(result_path)
    with open(truth_path, newline="", encoding="utf-8") as handle:
        truth_rows = [row for row in csv.DictReader(handle)
                      if row.get("source") == "qasmbench"]
    truth = {(row["circuit"], int(row["qubits"])): row for row in truth_rows}
    if len(truth) != 15 or len(truth) != len(truth_rows):
        raise ValueError("ICCAD reproduction truth must contain 15 unique QASMBench rows")
    required_truth = {
        "circuit", "qubits", "two_qubit_gates", "layers",
        "max_gates_in_layer", "ra_steps", "ra_rearr_ms", "rw_steps",
        "rw_rearr_ms", "source",
    }
    if any(not required_truth.issubset(row) for row in truth_rows):
        raise ValueError("ICCAD truth is missing structural or movement columns")
    with open(result_path, encoding="utf-8") as handle:
        results = json.load(handle)
    if not isinstance(results, list) or len(results) != 30:
        raise ValueError("ICCAD reproduction requires exactly 30 result rows")
    observed_result_keys = [
        (str(row.get("config")), str(row.get("circuit")),
         int(row.get("qubits", -1))) for row in results
    ]
    expected_result_keys = {
        (config, circuit, qubits)
        for config in ("agnostic", "astar") for circuit, qubits in truth
    }
    if (len(set(observed_result_keys)) != len(observed_result_keys) or
            set(observed_result_keys) != expected_result_keys):
        raise ValueError("ICCAD result cohort has missing, extra, or duplicate rows")
    rows: List[Dict[str, object]] = []
    summaries: Dict[str, object] = {}
    for config, prefix in (("agnostic", "ra"), ("astar", "rw")):
        subset = [row for row in results if row.get("config") == config and
                  (row.get("circuit"), int(row.get("qubits", -1))) in truth]
        actual_steps = expected_steps = 0.0
        actual_move = expected_move = 0.0
        exact = 0
        exact_structure = 0
        for actual in sorted(subset, key=lambda row: (row["circuit"], row["qubits"])):
            key = (actual["circuit"], int(actual["qubits"]))
            expected = truth[key]
            paper_steps = int(expected[f"{prefix}_steps"])
            paper_move = float(expected[f"{prefix}_rearr_ms"])
            observed_steps = int(actual["steps"])
            observed_move = (float(actual["rearr_us"]) / 1000.0
                             if "rearr_us" in actual else float(actual["rearr_ms"]))
            expected_structure = {
                "qubits": int(expected["qubits"]),
                "two_qubit_gates": int(expected["two_qubit_gates"]),
                "layers": int(expected["layers"]),
                "max_gates": int(expected["max_gates_in_layer"]),
            }
            observed_structure = {
                "qubits": int(actual["qubits"]),
                "two_qubit_gates": int(actual["two_qubit_gates"]),
                "layers": int(actual["layers"]),
                "max_gates": int(actual["max_gates"]),
            }
            input_structure = {
                "qubits": int(actual["qubits"]),
                "two_qubit_gates": int(actual["two_qubit_gates"]),
                "layers": int(actual["input_two_qubit_layers"]),
                "max_gates": int(actual["input_max_two_qubit_gates"]),
            }
            structure_matches = (
                observed_structure == expected_structure and
                input_structure == expected_structure
            )
            exact_structure += structure_matches
            if (actual.get("mqt_qmap_version") != "3.2.0" or
                    actual.get("mqt_core_version") != "3.1.0"):
                raise ValueError(f"ICCAD package version drift in row {key}")
            if config == "astar" and actual.get("astar_parameters") != PAPER_ASTAR_PARAMETERS:
                raise ValueError(f"ICCAD A* parameter drift in row {key}")
            for field in ("steps", "rearr_us", "layers", "max_gates"):
                if not math.isfinite(float(actual[field])) or float(actual[field]) < 0:
                    raise ValueError(f"invalid ICCAD {field} in row {key}")
            exact += observed_steps == paper_steps
            actual_steps += observed_steps
            expected_steps += paper_steps
            actual_move += observed_move
            expected_move += paper_move
            rows.append({
                "config": config, "circuit": key[0], "qubits": key[1],
                "steps_actual": observed_steps, "steps_paper": paper_steps,
                "steps_exact": observed_steps == paper_steps,
                "move_actual_ms": observed_move, "move_paper_ms": paper_move,
                "move_relative_error": observed_move / paper_move - 1.0,
                "structure_actual": observed_structure,
                "structure_input": input_structure,
                "structure_paper": expected_structure,
                "structure_exact": structure_matches,
                "input_sha256": actual["input_sha256"],
                "native_sha256": actual["native_sha256"],
            })
        step_error = _relative_error(actual_steps, expected_steps)
        move_error = _relative_error(actual_move, expected_move)
        summaries[config] = {
            "rows": len(subset), "exact_steps": exact,
            "exact_structure": exact_structure,
            "aggregate_steps_actual": actual_steps,
            "aggregate_steps_paper": expected_steps,
            "aggregate_steps_relative_error": step_error,
            "aggregate_move_actual_ms": actual_move,
            "aggregate_move_paper_ms": expected_move,
            "aggregate_move_relative_error": move_error,
            "passed": (len(subset) == 15 and exact_structure == 15 and
                       exact >= minimum_exact_steps and
                       step_error <= aggregate_relative_tolerance and
                       move_error <= aggregate_relative_tolerance),
        }
    # M2 is the routing-aware A* result.  Agnostic is retained as an appendix audit.
    return {
        "track": "baseline_reproduction",
        "method": "M2_ICCAD_QMAP_3.2",
        "criteria": {"aggregate_relative": aggregate_relative_tolerance,
                     "minimum_exact_steps": minimum_exact_steps,
                     "structure": "15/15 exact for both input and native output"},
        "evidence": {
            "paper_truth_path": str(truth_path),
            "paper_truth_sha256": sha256_file(truth_path),
            "result_path": str(result_path),
            "result_sha256": sha256_file(result_path),
            "run_manifest": evidence,
        },
        "passed": bool(summaries.get("astar", {}).get("passed", False)),
        "routing_aware": summaries.get("astar"),
        "routing_agnostic_appendix": summaries.get("agnostic"),
        "rows": rows,
    }


def reproduce_baselines(zac_truth: str | Path, zac_results: str | Path,
                        iccad_truth: str | Path, iccad_results: str | Path
                        ) -> Mapping[str, object]:
    zac = assess_zac_reproduction(
        zac_truth, zac_results, require_evidence=True)
    iccad = assess_iccad_reproduction(iccad_truth, iccad_results)
    return {
        "experiment_schema": 2,
        "track": "baseline_reproduction",
        "eligible_for_main_experiment": bool(zac["passed"] and iccad["passed"]),
        "zac": zac, "iccad": iccad,
        "note": "Paper values are reproduction references only; final tables use reruns and unified scoring.",
    }


__all__ = ["assess_iccad_reproduction", "assess_zac_reproduction",
           "reproduce_baselines"]
