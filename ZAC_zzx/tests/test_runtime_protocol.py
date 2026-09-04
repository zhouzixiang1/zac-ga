from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.runtime_benchmark import (  # noqa: E402
    StageTiming, build_balanced_schedule, canonical_two_qubit_layer_ledger,
    ordered_two_qubit_layer_ledger, summarize_stage_timing,
    validate_balanced_schedule)
from experiments_v2.protocol import FORMAL_TIMING_REPETITIONS  # noqa: E402


class RuntimeProtocolTests(unittest.TestCase):
    def test_canonical_layer_ledger_is_ordered_and_deterministic(self):
        from qiskit import QuantumCircuit

        circuit = QuantumCircuit(4)
        circuit.cz(0, 1)
        circuit.cz(2, 3)
        circuit.x(0)
        circuit.cz(1, 2)
        first = canonical_two_qubit_layer_ledger(circuit)
        second = canonical_two_qubit_layer_ledger(circuit)
        self.assertEqual(first, second)
        self.assertEqual(first["layers"], [[(0, 1), (2, 3)], [(1, 2)]])
        self.assertEqual(first["gates_2q"], 3)
        self.assertEqual(first["transitions"], 1)

    def test_observed_ledger_preserves_compiler_layer_boundaries(self):
        from qiskit import QuantumCircuit

        circuit = QuantumCircuit(4)
        circuit.cz(0, 1)
        circuit.cz(2, 3)
        circuit.cz(0, 2)
        canonical = canonical_two_qubit_layer_ledger(circuit)
        observed = ordered_two_qubit_layer_ledger(
            4, [[(0, 1)], [(2, 3)], [(0, 2)]])
        self.assertNotEqual(
            observed["layer_ledger_sha256"],
            canonical["layer_ledger_sha256"])
        self.assertEqual(observed["transitions"], 2)

    def test_schedule_is_deterministic_and_balanced(self):
        left = build_balanced_schedule(["a", "b"])
        right = build_balanced_schedule(["a", "b"])
        self.assertEqual(left, right)
        jobs = left["jobs"]
        self.assertEqual(len(jobs), 2 * 4 * FORMAL_TIMING_REPETITIONS)
        self.assertEqual({row["method"] for row in jobs}, {"M1", "M2", "M3", "M4"})

        first_repetition_orders = {}
        for circuit in ("a", "b"):
            first_repetition_orders[circuit] = tuple(
                row["method"] for row in jobs
                if row["circuit"] == circuit and row["repetition"] == 0)
        self.assertNotEqual(
            first_repetition_orders["a"], first_repetition_orders["b"])
        for circuit in ("a", "b"):
            for repetition in range(FORMAL_TIMING_REPETITIONS):
                methods = [
                    row["method"] for row in jobs
                    if row["circuit"] == circuit
                    and row["repetition"] == repetition
                ]
                self.assertEqual(set(methods), {"M1", "M2", "M3", "M4"})

    def test_schedule_tampering_fails_even_when_resealed(self):
        schedule = build_balanced_schedule(["a", "b"])
        schedule["jobs"][0]["method"] = (
            "M1" if schedule["jobs"][0]["method"] != "M1" else "M2")
        import hashlib
        import json
        core = {key: value for key, value in schedule.items()
                if key != "sha256"}
        schedule["sha256"] = hashlib.sha256(json.dumps(
            core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "deterministic frozen"):
            validate_balanced_schedule(
                schedule, ["a", "b"])

    def test_summary_only_computes_strict_ratio_for_equal_ledgers(self):
        rows = []
        for method, elapsed in (("M1", 80), ("M2", 100), ("M3", 50), ("M4", 40)):
            for repetition in range(FORMAL_TIMING_REPETITIONS):
                rows.append(StageTiming(
                    "a", method, repetition, "success", "same", 3,
                    elapsed + repetition, elapsed, 1, 10, 20, 200))
        result = summarize_stage_timing(rows)
        self.assertEqual(result["strict_valid"], 1)
        row = result["circuits"][0]
        self.assertAlmostEqual(row["M3"]["speedup_vs_m2"], 101 / 51)
        self.assertAlmostEqual(row["M4"]["speedup_vs_m2"], 101 / 41)
        summary = result["paired_speedup_vs_m2"]["M4"]
        self.assertEqual(summary["n"], 1)
        self.assertAlmostEqual(
            summary["geometric_mean_speedup_vs_m2"], 101 / 41)
        self.assertAlmostEqual(summary["bootstrap95_low"], 101 / 41)

    def test_ledger_mismatch_is_not_strict(self):
        rows = [
            StageTiming("a", method, 0, "success", method, 1, 1, 1, 0, 0, 0, 1)
            for method in ("M1", "M2", "M3", "M4")]
        result = summarize_stage_timing(rows)
        self.assertEqual(result["strict_valid"], 0)

    def test_one_successful_method_is_not_a_strict_timing_cohort(self):
        rows = [
            StageTiming("a", "M2", repetition, "success", "same", 1,
                        10, 8, 2, 3, 4, 20)
            for repetition in range(FORMAL_TIMING_REPETITIONS)
        ]
        result = summarize_stage_timing(rows)
        self.assertEqual(result["strict_valid"], 0)

    def test_duplicate_repetition_fails_closed(self):
        rows = [
            StageTiming("a", "M2", 0, "success", "same", 1,
                        10, 8, 2, 3, 4, 20),
            StageTiming("a", "M2", 0, "success", "same", 1,
                        11, 9, 2, 3, 4, 20),
        ]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            summarize_stage_timing(rows)


if __name__ == "__main__":
    unittest.main()
