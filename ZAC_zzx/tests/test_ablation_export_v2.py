"""Verified delivery tests for the isolated Schema-2 ablation track."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.ablation_export import export_ablation_report  # noqa: E402
from experiments_v2.ablation_statistics import aggregate_ablation  # noqa: E402
from tests.test_ablation_v2 import _success_run  # noqa: E402


class TestAblationExport(unittest.TestCase):
    def test_diagnostic_tables_workbook_and_figures_are_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for circuit in ("c0", "c1"):
                for variant in ("h0", "decay_phase_coloring"):
                    for seed in range(5):
                        run = _success_run(circuit, variant, seed)
                        path = root / f"{run.run_id}.json"
                        run.write(path)
                        paths.append(path)
            report = aggregate_ablation(
                paths, dataset="zac18", frozen_circuits=["c0", "c1"],
                experiment_id="frozen-ablation",
                expected_variants=("h0", "decay_phase_coloring"),
                bootstrap_iterations=100)
            output = root / "delivery"
            manifest = export_ablation_report(report, output)
            self.assertTrue(manifest["diagnostic_only"])
            self.assertFalse(manifest["eligible_for_main_claim_gate"])
            self.assertTrue(manifest["xlsx_rendered"])
            self.assertTrue((output / "ablation.xlsx").read_bytes().startswith(b"PK"))
            for name in (
                "variant_summary.csv", "circuit_ablation.csv",
                "seed_attempts.csv", "comparisons.csv", "report.md",
                "tables.tex", "workbook_contract.json", "export_manifest.json",
            ):
                self.assertTrue((output / name).is_file(), name)
            for name in (
                "ablation_four_metric.svg", "ablation_ecdf.pdf",
                "ablation_paired_effects.png", "figure_qa.json",
            ):
                self.assertTrue((output / "figures" / name).is_file(), name)
            markdown = (output / "report.md").read_text(encoding="utf-8")
            self.assertIn("Exploratory diagnostic only", markdown)
            with open(output / "comparisons.csv", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row["holm_p_value"] for row in rows))
            workbook_qa = json.loads(
                (output / "workbook_qa" / "workbook_qa.json").read_text())
            self.assertEqual(workbook_qa["formula_errors"], [])

    def test_non_ablation_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Schema-2 ablation"):
                export_ablation_report(
                    {"experiment_schema": 2, "run_kind": "main"}, directory)


if __name__ == "__main__":
    unittest.main()
