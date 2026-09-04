from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.qmap_legacy_regression import (  # noqa: E402
    METHOD_LABELS,
    check_current,
    collect_current_report,
    freeze_reference,
)


class TestQmapLegacyRegression(unittest.TestCase):
    def _fixtures(self, directory: Path):
        header = ["电路"]
        for metric in ("保真度F", "Move批次", "Move时间(ms)", "算法时间(s)"):
            header.extend(f"{metric} | {label}" for label in METHOD_LABELS.values())
        values = [header]
        attempts = []
        for index in range(154):
            circuit = f"c{index:03d}"
            row = [circuit]
            row.extend((0.4, 0.5, 0.55, 0.6 if index < 126 else None))
            row.extend((12, 11, 10, 9))
            row.extend((1.2, 1.1, 1.0, 0.9))
            row.extend((4.0, 1.0, 2.0, 3.0))
            values.append(row)
            for method in METHOD_LABELS:
                attempts.append({
                    "circuit": circuit,
                    "method": method,
                    "input_sha256": f"hash-{index:03d}",
                    "config_sha256": f"config-{method}",
                })
        rows_path = directory / "rows.json"
        aggregate_path = directory / "aggregate.json"
        rows_path.write_text(json.dumps({"QMAP": {"values": values}}))
        aggregate_path.write_text(json.dumps({"attempt_index": attempts}))
        return rows_path, aggregate_path

    def _manifest(self, root: Path, *, circuit: str, input_sha256: str,
                  fidelity: float, config: str = "config-current",
                  commit: str = "a" * 40, backend: str = "native",
                  ghost_hits: int = 0, verifier_ok: bool = True,
                  ledger_ok: bool = True, suffix: str = "one") -> Path:
        directory = root / circuit / suffix
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "manifest.json"
        path.write_text(json.dumps({
            "dataset": "qmap154",
            "circuit": circuit,
            "method": "M4",
            "seed": 0,
            "repetition": 0,
            "status": "success",
            "git_commit": commit,
            "config_sha256": config,
            "backend": backend,
            "native_abi_version": 8,
            "input_sha256": input_sha256,
            "fidelity": fidelity,
            "log_fidelity": math.log(fidelity),
            "ghost_hits": ghost_hits,
            "verifier_ok": verifier_ok,
            "expected_gate_ledger_sha256": "g" * 64,
            "observed_gate_ledger_sha256": (
                "g" * 64 if ledger_ok else "x" * 64),
            "canonical_input_layer_ledger_sha256": "l" * 64,
            "observed_transition_layer_ledger_sha256": (
                "l" * 64 if ledger_ok else "y" * 64),
        }), encoding="utf-8")
        return path

    def test_exact_cohort_must_strictly_exceed_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            rows_path, aggregate_path = self._fixtures(Path(temporary))
            reference = freeze_reference(rows_path, aggregate_path)
            self.assertEqual(126, reference["cohort_n"])
            self.assertAlmostEqual(
                0.6,
                reference["targets"]["fidelity_geometric_mean"]["M4"],
            )
            current = {"circuit_rows": [
                {
                    "circuit": row["circuit"],
                    "input_sha256": row["input_sha256"],
                    "methods": {"M4": {
                        "status": "success",
                        "fidelity": row["methods"]["M4"]["fidelity"] * 1.01,
                    }},
                }
                for row in reference["circuits"]
            ]}
            result = check_current(reference, current)
            self.assertTrue(result["passed"])
            self.assertEqual("pass", result["status"])
            self.assertAlmostEqual(
                1.01, result["completed_current_vs_legacy_m4_gm_ratio"])

    def test_partial_cohort_reports_headroom_but_never_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            rows_path, aggregate_path = self._fixtures(Path(temporary))
            reference = freeze_reference(rows_path, aggregate_path)
            first = reference["circuits"][0]
            current = {"circuit_rows": [{
                "circuit": first["circuit"],
                "input_sha256": first["input_sha256"],
                "methods": {"M4": {"status": "success", "fidelity": 0.66}},
            }]}
            result = check_current(reference, current)
            self.assertFalse(result["passed"])
            self.assertEqual("partial", result["status"])
            self.assertEqual(1, result["completed_n"])
            self.assertEqual(125, result["missing_n"])
            self.assertLess(
                result["minimum_remaining_current_vs_legacy_gm_ratio_to_pass"],
                1.0,
            )

    def test_best_and_latest_collection_expose_mixed_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rows_path, aggregate_path = self._fixtures(directory)
            reference = freeze_reference(rows_path, aggregate_path)
            first, second = reference["circuits"][:2]
            old = self._manifest(
                directory / "root-a", circuit=first["circuit"],
                input_sha256=first["input_sha256"], fidelity=0.70,
                config="config-a", suffix="old")
            new = self._manifest(
                directory / "root-b", circuit=first["circuit"],
                input_sha256=first["input_sha256"], fidelity=0.61,
                config="config-b", suffix="new")
            self._manifest(
                directory / "root-b", circuit=second["circuit"],
                input_sha256=second["input_sha256"], fidelity=0.62,
                config="config-b")
            os.utime(old, ns=(1_000_000_000, 1_000_000_000))
            os.utime(new, ns=(2_000_000_000, 2_000_000_000))

            best = collect_current_report(
                reference, [directory / "root-a", directory / "root-b"],
                selection="best")
            latest = collect_current_report(
                reference, [directory / "root-a", directory / "root-b"],
                selection="latest")
            self.assertEqual(
                0.70, best["circuit_rows"][0]["methods"]["M4"]["fidelity"])
            self.assertEqual(
                0.61, latest["circuit_rows"][0]["methods"]["M4"]["fidelity"])
            self.assertTrue(best["mixed_identity"])
            self.assertIn("config_sha256", best["mixed_identity_fields"])
            self.assertFalse(best["formal_eligible"])

    def test_formal_collection_passes_only_one_valid_native_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rows_path, aggregate_path = self._fixtures(directory)
            reference = freeze_reference(rows_path, aggregate_path)
            root = directory / "attempts"
            for row in reference["circuits"]:
                self._manifest(
                    root, circuit=row["circuit"],
                    input_sha256=row["input_sha256"], fidelity=0.61)
            current = collect_current_report(
                reference, [root], selection="best", formal=True)
            self.assertTrue(current["formal_eligible"])
            self.assertFalse(current["mixed_identity"])
            result = check_current(reference, current)
            self.assertTrue(result["passed"])

            first = reference["circuits"][0]
            self._manifest(
                root, circuit=first["circuit"],
                input_sha256=first["input_sha256"], fidelity=0.62,
                ghost_hits=1, suffix="one")
            invalid = collect_current_report(
                reference, [root], selection="best", formal=True)
            self.assertFalse(invalid["formal_eligible"])
            self.assertEqual(1, len(invalid["invalid_success_rows"]))
            rejected = check_current(reference, invalid)
            self.assertFalse(rejected["passed"])
            self.assertFalse(rejected["formal_eligible"])


if __name__ == "__main__":
    unittest.main()
