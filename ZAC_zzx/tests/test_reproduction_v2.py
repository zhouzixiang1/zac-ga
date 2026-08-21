"""Formal original-paper reproduction evidence tests."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.contracts import sha256_file, stable_sha256  # noqa: E402
from experiments_v2.iccad_reproduction import (  # noqa: E402
    PAPER_ASTAR_PARAMETERS, PREPROCESSING_SPEC, QASMBENCH,
    _two_qubit_shape, evidence_manifest_path, reproduce_iccad,
    validate_iccad_reproduction_evidence,
)
from experiments_v2.reproduction import (assess_iccad_reproduction,  # noqa: E402
                                         assess_zac_reproduction)
from experiments_v2.zac_reproduction import (  # noqa: E402
    RESULT_FILENAME as ZAC_RESULT_FILENAME, ZAC_HPCA18, reproduce_zac,
    validate_zac_reproduction_evidence,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _refresh_evidence_hash(evidence_path: Path, result_path: Path) -> None:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["result_sha256"] = sha256_file(result_path)
    evidence.pop("evidence_payload_sha256", None)
    evidence["evidence_payload_sha256"] = stable_sha256(evidence)
    _write_json(evidence_path, evidence)


class ReproductionFixture:
    def __init__(self, root: Path):
        self.root = root
        self.truth = root / "qmap_table1.csv"
        self.result = root / "qmap_table1_v32_schema2_reproduction.json"
        self.evidence = evidence_manifest_path(self.result)
        self.input_manifest = root / "inputs" / "inputs.json"
        self.architecture = root / "architecture.json"
        self.architecture.write_text("{}\n", encoding="utf-8")
        self.hpca_directory = root / "sources"
        self.qmap_python = root / "qmap-python"
        self.qmap_python.write_text("fake frozen interpreter\n", encoding="utf-8")

        truth_rows = []
        input_records = []
        rows = []
        native_outputs = []
        for index, (benchmark, qubits) in enumerate(QASMBENCH, start=1):
            gates_2q = index + 2
            layers = index
            maximum = min(gates_2q, 2)
            source = root / "sources" / f"{benchmark}_n{qubits}.qasm"
            preprocessed = root / "inputs" / f"{benchmark}_n{qubits}.qasm"
            source.parent.mkdir(parents=True, exist_ok=True)
            preprocessed.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"source {benchmark} {qubits}\n", encoding="utf-8")
            preprocessed.write_text(
                f"preprocessed {benchmark} {qubits}\n", encoding="utf-8")
            input_records.append({
                "benchmark": benchmark,
                "qubits": qubits,
                "source_path": str(source),
                "source_sha256": sha256_file(source),
                "path": str(preprocessed),
                "preprocessed_sha256": sha256_file(preprocessed),
                "gates_1q": index,
                "gates_2q": gates_2q,
                "depth": layers + 2,
                "two_qubit_layers": layers,
                "max_two_qubit_gates": maximum,
            })
            truth_rows.append({
                "circuit": benchmark,
                "qubits": qubits,
                "two_qubit_gates": gates_2q,
                "layers": layers,
                "max_gates_in_layer": maximum,
                "ra_steps": index + 10,
                "ra_rearr_ms": float(index + 20),
                "rw_steps": index + 5,
                "rw_rearr_ms": float(index + 15),
                "source": "qasmbench",
            })

        _write_json(self.input_manifest, {
            "experiment_schema": 2,
            "track": "iccad_original_reproduction_input",
            "preprocessing": dict(PREPROCESSING_SPEC),
            "records": input_records,
        })
        input_manifest_hash = sha256_file(self.input_manifest)
        architecture_hash = sha256_file(self.architecture)
        by_key = {(record["benchmark"], record["qubits"]): record
                  for record in input_records}
        truth_by_key = {(row["circuit"], row["qubits"]): row
                        for row in truth_rows}
        for config, prefix in (("agnostic", "ra"), ("astar", "rw")):
            for benchmark, qubits in QASMBENCH:
                record = by_key[(benchmark, qubits)]
                truth = truth_by_key[(benchmark, qubits)]
                native = root / "native" / f"{benchmark}_n{qubits}.{config}.na"
                native.parent.mkdir(parents=True, exist_ok=True)
                native.write_text(f"native {benchmark} {qubits} {config}\n",
                                  encoding="utf-8")
                native_hash = sha256_file(native)
                native_outputs.append({"path": str(native), "sha256": native_hash})
                rows.append({
                    "circuit": benchmark,
                    "qubits": qubits,
                    "config": config,
                    "input_sha256": record["preprocessed_sha256"],
                    "input_manifest_sha256": input_manifest_hash,
                    "architecture_sha256": architecture_hash,
                    "gates_1q": record["gates_1q"],
                    "two_qubit_gates": record["gates_2q"],
                    "input_depth": record["depth"],
                    "input_two_qubit_layers": record["two_qubit_layers"],
                    "input_max_two_qubit_gates": record["max_two_qubit_gates"],
                    "place_ms": 1.0,
                    "route_ms": 2.0,
                    "total_ms": 3.0,
                    "steps": truth[f"{prefix}_steps"],
                    "rearr_us": truth[f"{prefix}_rearr_ms"] * 1000.0,
                    "layers": record["two_qubit_layers"],
                    "max_gates": record["max_two_qubit_gates"],
                    "native_path": str(native),
                    "native_sha256": native_hash,
                    "mqt_qmap_version": "3.2.0",
                    "mqt_core_version": "3.1.0",
                    "astar_parameters": (dict(PAPER_ASTAR_PARAMETERS)
                                         if config == "astar" else None),
                })
        _write_json(self.result, rows)
        with open(self.truth, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(truth_rows[0]))
            writer.writeheader()
            writer.writerows(truth_rows)
        evidence = {
            "experiment_schema": 2,
            "track": "iccad_original_reproduction",
            "status": "complete",
            "result_path": str(self.result),
            "result_sha256": sha256_file(self.result),
            "input_manifest_path": str(self.input_manifest),
            "input_manifest_sha256": input_manifest_hash,
            "hpca_directory": str(self.hpca_directory),
            "qmap_python_path": str(self.qmap_python),
            "qmap_python_sha256": sha256_file(self.qmap_python),
            "architecture_path": str(self.architecture),
            "architecture_sha256": architecture_hash,
            "package_versions": {"mqt.qmap": "3.2.0", "mqt.core": "3.1.0"},
            "astar_parameters": dict(PAPER_ASTAR_PARAMETERS),
            "rows": 30,
            "native_outputs": sorted(native_outputs, key=lambda item: item["path"]),
        }
        evidence["evidence_payload_sha256"] = stable_sha256(evidence)
        _write_json(self.evidence, evidence)


class TestICCADEvidence(unittest.TestCase):
    def test_table_layer_definition_ignores_one_qubit_gates(self):
        from qiskit import QuantumCircuit

        circuit = QuantumCircuit(4)
        circuit.cz(0, 1)
        circuit.h(0)
        circuit.cz(2, 3)  # parallel with the first CZ in the 2Q-only view
        circuit.cz(1, 2)  # depends on both preceding CZs
        self.assertEqual(_two_qubit_shape(circuit), (3, 2, 2))

    def test_strict_structure_and_evidence_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReproductionFixture(Path(directory))
            evidence = validate_iccad_reproduction_evidence(fixture.result)
            self.assertEqual(evidence["rows"], 30)
            report = assess_iccad_reproduction(fixture.truth, fixture.result)
            self.assertTrue(report["passed"])
            self.assertEqual(report["routing_aware"]["exact_structure"], 15)
            self.assertEqual(report["routing_agnostic_appendix"]["exact_structure"],
                             15)

    def test_one_structural_mismatch_fails_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReproductionFixture(Path(directory))
            rows = json.loads(fixture.result.read_text(encoding="utf-8"))
            next(row for row in rows if row["config"] == "astar")["layers"] += 1
            _write_json(fixture.result, rows)
            _refresh_evidence_hash(fixture.evidence, fixture.result)
            report = assess_iccad_reproduction(fixture.truth, fixture.result)
            self.assertFalse(report["passed"])
            self.assertEqual(report["routing_aware"]["exact_structure"], 14)

    def test_changed_preprocessed_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReproductionFixture(Path(directory))
            input_payload = json.loads(
                fixture.input_manifest.read_text(encoding="utf-8"))
            target = Path(input_payload["records"][0]["path"])
            target.write_text(target.read_text(encoding="utf-8") + "changed\n",
                              encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "preprocessed ICCAD input changed"):
                validate_iccad_reproduction_evidence(fixture.result)

    def test_partial_result_is_never_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            result = destination / "qmap_table1_v32_schema2_reproduction.json"
            _write_json(result, [])
            with self.assertRaisesRegex(ValueError, "result/evidence pair is incomplete"):
                reproduce_iccad(destination / "hpca", destination / "arch.json",
                                sys.executable, destination)

    def test_completed_result_cannot_be_reused_for_a_different_request(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReproductionFixture(Path(directory))
            with self.assertRaisesRegex(ValueError, "HPCA directory differs"):
                reproduce_iccad(fixture.root / "different-hpca",
                                fixture.architecture, fixture.qmap_python,
                                fixture.root)


class TestZACEvidence(unittest.TestCase):
    def test_hashes_results_without_claiming_missing_structure_truth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            truth = root / "zac.csv"
            truth.write_text(
                "circuit,fidelity,duration_us\n"
                "toy,0.9,100.0\n", encoding="utf-8")
            fidelity = root / "fidelity"
            _write_json(fidelity / "toy_fidelity.json", {
                "cir_fidelity": 0.9,
                "cir_duration": 100.0,
            })
            report = assess_zac_reproduction(truth, fidelity)
            self.assertTrue(report["passed"])
            self.assertEqual(report["evidence"]["structure_gate"], "not_asserted")
            self.assertFalse(report["evidence"]["paper_structure_truth_available"])
            self.assertEqual(len(report["rows"][0]["fidelity_artifact_sha256"]), 64)


class FreshZACFixture:
    def __init__(self, root: Path):
        self.root = root
        self.hpca = root / "hpca"
        self.hpca.mkdir()
        for circuit in ZAC_HPCA18:
            (self.hpca / f"{circuit}.qasm").write_text(
                f"OPENQASM 2.0; // {circuit}\n", encoding="utf-8")
        self.architecture = root / "architecture.json"
        self.architecture.write_text("{}\n", encoding="utf-8")
        self.python = root / "python"
        self.python.write_text("fake Qiskit 1.2.4 interpreter\n", encoding="utf-8")
        self.compiler = root / "ZAC"
        (self.compiler / "zac").mkdir(parents=True)
        (self.compiler / "zac" / "zac.py").write_text(
            "# original ZAC fixture\n", encoding="utf-8")
        (self.compiler / "run.py").write_text(
            "# original entry point fixture\n", encoding="utf-8")
        self.output = root / "fresh-output"

    @staticmethod
    def runner(command, environment, cwd, stdout_path, stderr_path):
        del environment, cwd
        request_path = Path(command[command.index("--request") + 1])
        destination = Path(command[command.index("--output-directory") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        stdout_path.write_text("fresh original ZAC run\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        rows = []
        for index, source in enumerate(request["sources"]):
            circuit = source["circuit"]
            code = destination / "code" / f"{circuit}_code.json"
            fidelity = destination / "fidelity" / f"{circuit}_fidelity.json"
            timing = destination / "time" / f"{circuit}_time.json"
            value = 0.90 - index / 1000.0
            duration = 100.0 + index
            _write_json(code, {"name": circuit, "instructions": []})
            _write_json(fidelity, {
                "cir_fidelity": value, "cir_duration": duration})
            _write_json(timing, {"total": 1.0})
            rows.append({
                "circuit": circuit,
                "source_path": source["path"],
                "source_sha256": source["sha256"],
                "architecture_sha256": request["architecture_sha256"],
                "configuration_sha256": request["configuration_sha256"],
                "compiler_source_tree_sha256":
                    request["compiler_source_tree_sha256"],
                "structure": {
                    "source": {"qubits": 2, "gates_1q": 1,
                               "gates_2q": 1, "depth": 2},
                    "resynthesized": {"qubits": 2, "gates_1q": 1,
                                      "gates_2q": 1, "depth": 2},
                    "scheduler": {"two_qubit_layers": 1,
                                  "max_two_qubit_gates": 1},
                    "emitted": {"gates_1q": 1, "gates_2q": 1,
                                "instructions": 3},
                },
                "code_path": str(code.resolve()),
                "code_sha256": sha256_file(code),
                "fidelity_path": str(fidelity.resolve()),
                "fidelity_sha256": sha256_file(fidelity),
                "time_path": str(timing.resolve()),
                "time_sha256": sha256_file(timing),
                "cir_fidelity": value,
                "cir_duration_us": duration,
            })
        _write_json(destination / ZAC_RESULT_FILENAME, {
            "experiment_schema": 2,
            "track": "zac_original_reproduction",
            "request_id": request["request_id"],
            "package_versions": {
                "python": "3.10.0", "qiskit": "1.2.4",
                "numpy": "1.26.0", "scipy": "1.12.0",
            },
            "rows": rows,
        })

    def run(self) -> Path:
        return reproduce_zac(
            self.hpca, self.architecture, self.python, self.compiler,
            self.output, _runner=self.runner)

    def truth(self) -> Path:
        path = self.root / "zac.csv"
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=[
                    "circuit", "fidelity", "duration_us", "qubits",
                    "gates_1q", "gates_2q", "depth",
                    "two_qubit_layers", "max_two_qubit_gates",
                    "structure_source",
                ])
            writer.writeheader()
            for index, circuit in enumerate(ZAC_HPCA18):
                writer.writerow({
                    "circuit": circuit,
                    "fidelity": 0.90 - index / 1000.0,
                    "duration_us": 100.0 + index,
                    "qubits": 2,
                    "gates_1q": 1,
                    "gates_2q": 1,
                    "depth": 2,
                    "two_qubit_layers": 1,
                    "max_two_qubit_gates": 1,
                    "structure_source": "test_original_artifact_qiskit1.2.4",
                })
        return path


class TestFreshZACExecutionEvidence(unittest.TestCase):
    def test_fresh_run_is_sealed_and_assessed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FreshZACFixture(Path(directory))
            output = fixture.run()
            evidence = validate_zac_reproduction_evidence(output)
            self.assertEqual(evidence["rows"], 18)
            self.assertEqual(evidence["package_versions"]["qiskit"], "1.2.4")
            report = assess_zac_reproduction(
                fixture.truth(), output, require_evidence=True)
            self.assertTrue(report["passed"])
            self.assertIsNotNone(report["rows"][0]["structure_observed"])
            self.assertEqual(report["evidence"]["structure_gate"],
                             "artifact_reference_exact")

    def test_formal_run_rejects_structure_reference_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FreshZACFixture(Path(directory))
            output = fixture.run()
            truth = fixture.truth()
            with truth.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["gates_1q"] = "2"
            with truth.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            report = assess_zac_reproduction(
                truth, output, require_evidence=True)
            self.assertFalse(report["passed"])
            self.assertFalse(report["rows"][0]["structure_exact"])

    def test_partial_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FreshZACFixture(Path(directory))
            fixture.output.mkdir()
            (fixture.output / "partial.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "partial attempt"):
                fixture.run()

    def test_completed_run_rejects_a_different_request(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FreshZACFixture(Path(directory))
            fixture.run()
            different = fixture.root / "different-architecture.json"
            different.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs from sealed"):
                reproduce_zac(
                    fixture.hpca, different, fixture.python, fixture.compiler,
                    fixture.output, _runner=fixture.runner)

    def test_completed_artifact_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FreshZACFixture(Path(directory))
            fixture.run()
            result = json.loads(
                (fixture.output / ZAC_RESULT_FILENAME).read_text(encoding="utf-8"))
            code = Path(result["rows"][0]["code_path"])
            code.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "code artifact changed"):
                fixture.run()


if __name__ == "__main__":
    unittest.main()
