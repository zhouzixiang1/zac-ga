"""Delivery-format tests for frozen Schema-2 aggregate reports."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.cli import build_parser  # noqa: E402
from experiments_v2.export import _build_tables, export_experiment_report  # noqa: E402
from experiments_v2.figures import render_report_figures  # noqa: E402
from experiments_v2.workbook_renderer import render_workbook  # noqa: E402


def _status_counts(success: int = 1) -> dict[str, int]:
    return {
        "success": success, "timeout": 0, "oom": 0, "compiler_error": 0,
        "verifier_fail": 0, "scorer_error": 0, "missing": 0, "duplicate": 0,
    }


def sample_report(*, claim_passed: bool = False) -> dict:
    methods = ("M1", "M2", "M3", "M4")
    coverage_methods = {
        method: {
            "valid": 1, "N": 1, "rate": 1.0,
            "wilson95": [0.2065, 1.0], "status": "complete",
            "meets_dataset_threshold": True,
            "status_counts": _status_counts(),
        }
        for method in methods
    }
    quality_methods = {
        method: {
            "valid": 1, "N": 1, "status": "complete",
            "status_counts": _status_counts(1 if method in ("M1", "M2") else 5),
            "invalid_reasons": {},
        }
        for method in methods
    }
    comparison = {
        "n": 1, "ratio": 1.03, "ci95_low": 1.01, "ci95_high": 1.05,
        "simultaneous95_low": 1.005, "simultaneous95_high": 1.055,
        "p_value": 0.01, "holm_p_value": 0.02, "rank_biserial": 1.0,
        "wins": 1, "ties": 0, "losses": 0,
        "point_gain_at_least_2pct": True,
        "simultaneous_lower_above_1": True,
        "holm_significant_0.05": True, "stratum_consistent": True,
        "strata": {"le32": {"n": 1, "ratio": 1.03}},
    }
    move_comparison = {
        "n": 1, "ratio": float("inf"), "ci95_low": 0.94,
        "ci95_high": 1.01, "simultaneous95_low": 0.93,
        "simultaneous95_high": 1.02, "p_value": 0.5,
        "rank_biserial": 0.0, "wins": 0, "ties": 1, "losses": 0,
    }
    timing_methods = {
        method: {
            "valid": 1, "N": 1, "status": "complete", "PAR2_seconds": 1.25,
            "status_counts": _status_counts(5),
            "circuits": {
                "toy": {
                    "protocol_complete": True, "attempts": 5, "successful": 5,
                    "runtime_observed": True,
                    "median_success_seconds": 1.2, "PAR2_seconds": 1.25,
                },
            },
        }
        for method in methods
    }
    return {
        "experiment_schema": 2, "experiment_id": "frozen-v2",
        "dataset": "zac_demo", "run_kinds": ["coverage", "main", "timing"],
        "frozen_suite": {
            "source": "caller-supplied frozen suite", "explicit": True,
            "sha256": "a" * 64, "circuits": ["toy"], "N": 1,
        },
        "integrity": {"passed": True, "errors": []},
        "coverage": {
            "available": True, "valid": 1, "N": 1, "status": "pass",
            "dataset_threshold": 1.0, "methods": coverage_methods,
            "gate": {"passed": True},
        },
        "main": {
            "available": True, "valid": 1, "N": 1,
            "status": "pass" if claim_passed else "fail",
            "paired_cohort": ["toy"], "methods": quality_methods,
            "paired_method_summary": {
                method: {
                    "valid": 1, "N": 1,
                    "fidelity_geometric_mean": 0.904837418,
                    "move_batches_median": 2,
                    "move_time_us_median": 40.0,
                    "quality_compiler_seconds_median": 1.0,
                }
                for method in methods
            },
            "stratum_by_circuit": {"toy": "le32"},
            "stratum_method_summary": {
                "le32": {
                    method: {
                        "n": 1,
                        "fidelity_geometric_mean": 0.904837418,
                        "move_batches_median": 2,
                        "move_time_us_median": 40.0,
                        "quality_compiler_seconds_median": 1.0,
                    }
                    for method in methods
                },
            },
            "per_circuit_quality": [
                {
                    "circuit": "toy", "method": method, "paired": True,
                    "protocol": "valid", "log_fidelity": -0.1,
                    "fidelity": 0.904837418, "move_batches": 2,
                    "move_time_us": 40.0, "quality_compiler_seconds": 1.0,
                }
                for method in methods
            ],
            "fidelity": {
                "Bstar_method_by_circuit": {"toy": "M2"},
                "comparisons": {
                    "M4_vs_M3": comparison,
                    "M4_vs_Bstar": {**comparison, "ratio": 1.025},
                },
                "gate": {"passed": claim_passed},
            },
            "move": {
                "metric_best_method_by_circuit": {
                    "move_batches": {"toy": "M1"},
                    "move_time_us": {"toy": "M2"},
                },
                "metrics": {
                    "move_batches": {
                        "vs_metric_best_baseline": move_comparison,
                        "vs_M3": {**move_comparison, "ratio": 0.98},
                    },
                },
                "gate": {"passed": True},
            },
            "gate": {"passed": claim_passed},
        },
        "timing": {
            "available": True, "valid": 1, "N": 1, "status": "pass",
            "methods": timing_methods, "gate": {"passed": True},
        },
        "claim_gate": {
            "passed": claim_passed,
            "status": "pass" if claim_passed else "fail",
            "missing_sections": [],
        },
        "attempt_index": [{
            "run_id": "toy-M4-s0", "run_kind": "main", "circuit": "toy",
            "method": "M4", "seed": 0, "repetition": 0,
            "status": "success", "git_commit": "1" * 40,
            "input_sha256": "2" * 64, "config_sha256": "3" * 64,
            "fidelity_ood": False, "log_fidelity": -0.1,
            "fidelity": 0.904837418, "move_batches": 2,
            "move_time_us": 40.0, "compiler_time_seconds": 1.0,
            "artifact_dir": "/tmp/toy", "error": None,
        }],
    }


class TestExperimentExport(unittest.TestCase):
    def test_text_exports_are_strict_traceable_and_non_imputing(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "delivery"
            manifest = export_experiment_report(sample_report(), output)
            self.assertFalse(manifest["claim_gate_passed"])
            self.assertTrue(manifest["no_imputation"])
            self.assertFalse(manifest["xlsx_pending_artifact_render"])
            self.assertTrue(manifest["xlsx_rendered"])
            for name in (
                "report.json", "report.md", "tables.tex", "summary.csv",
                "coverage.csv", "method_quality.csv", "quality_strata.csv",
                "fidelity.csv", "move.csv", "timing.csv",
                "cohort.csv", "quality_circuits.csv", "attempts.csv",
                "plot_data.csv", "workbook_contract.json",
                "report.xlsx", "export_manifest.json",
            ):
                self.assertTrue((output / name).is_file(), name)
            self.assertTrue((output / "report.xlsx").read_bytes().startswith(b"PK"))
            for name in (
                "coverage_status.svg", "four_method_four_metric.pdf",
                "paired_ecdf.svg", "paired_pareto.pdf",
                "preregistered_strata.svg", "figure_qa.json",
            ):
                self.assertTrue((output / "figures" / name).is_file(), name)
            self.assertIn(
                "<text", (output / "figures" / "four_method_four_metric.svg").read_text())
            strict_json = json.loads((output / "report.json").read_text())
            ratio = strict_json["main"]["move"]["metrics"]["move_batches"][
                "vs_metric_best_baseline"]["ratio"]
            self.assertEqual(ratio, "Infinity")
            markdown = (output / "report.md").read_text()
            self.assertIn("diagnostic only", markdown)
            self.assertIn("not imputed", markdown)
            latex = (output / "tables.tex").read_text()
            self.assertIn(r"zac\_demo", latex)
            self.assertIn("failed-or-incomplete", latex)
            with open(output / "plot_data.csv", newline="", encoding="utf-8") as handle:
                plot_rows = list(csv.DictReader(handle))
            self.assertTrue(any(row["section"] == "fidelity" for row in plot_rows))
            self.assertTrue(any(row["section"] == "move" for row in plot_rows))
            self.assertTrue(any(row["section"] == "ecdf" for row in plot_rows))
            self.assertTrue(any(row["section"] == "pareto" for row in plot_rows))
            with open(output / "attempts.csv", newline="", encoding="utf-8") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 1)
            exported = json.loads((output / "export_manifest.json").read_text())
            self.assertTrue(all(len(row["sha256"]) == 64 for row in exported["files"]))

    def test_workbook_contract_renders_verified_xlsx(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "delivery"
            manifest = export_experiment_report(
                sample_report(claim_passed=True), output)
            contract = json.loads((output / "workbook_contract.json").read_text())
            self.assertTrue(contract["source_contract"])
            self.assertEqual(contract["rendered_xlsx"], "report.xlsx")
            self.assertIn("artifact tool", contract["render_policy"])
            self.assertEqual(
                [sheet["sheet_name"] for sheet in contract["sheets"]],
                ["Overview", "Coverage", "Quality Methods", "Quality Strata",
                 "Circuit Quality", "Fidelity",
                 "Fidelity Strata", "Move", "Timing", "Cohort",
                 "Timing Circuits", "Attempts", "Plot Data"],
            )
            coverage = next(
                sheet for sheet in contract["sheets"]
                if sheet["sheet_name"] == "Coverage")
            self.assertEqual(coverage["rows"][0]["method"], "M1")
            self.assertEqual(coverage["freeze_panes"], "A2")
            self.assertTrue((output / "report.xlsx").is_file())
            qa = json.loads((output / "workbook_qa" / "workbook_qa.json").read_text())
            self.assertEqual(qa["formula_errors"], [])
            self.assertEqual(len(qa["previews"]), 13)
            self.assertFalse(manifest["xlsx_pending_artifact_render"])
            render_workbook(
                output / "workbook_contract.json", output / "report-copy.xlsx",
                qa_directory=output / "workbook-copy-qa")
            self.assertEqual(
                (output / "report.xlsx").read_bytes(),
                (output / "report-copy.xlsx").read_bytes())
            second_figures = output / "figures-copy"
            render_report_figures(
                sample_report(claim_passed=True),
                _build_tables(sample_report(claim_passed=True)), second_figures)
            for name in ("four_method_four_metric.svg", "paired_ecdf.pdf"):
                self.assertEqual(
                    (output / "figures" / name).read_bytes(),
                    (second_figures / name).read_bytes())

    def test_cli_exposes_delivery_directory(self):
        parsed = build_parser().parse_args([
            "aggregate", "--plan", "plan.json", "--dataset", "zac18",
            "--output-dir", "delivery",
        ])
        self.assertEqual(parsed.output_dir, Path("delivery"))
        ablation = build_parser().parse_args([
            "aggregate-ablation", "--plan", "plan.json", "--dataset", "zac18",
            "--output-dir", "ablation-delivery",
        ])
        self.assertEqual(ablation.output_dir, Path("ablation-delivery"))

    def test_non_schema2_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "experiment_schema=2"):
                export_experiment_report(
                    {"experiment_schema": 1, "dataset": "legacy"}, directory)


if __name__ == "__main__":
    unittest.main()
