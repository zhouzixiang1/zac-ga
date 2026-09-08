"""Publication integration tests; no experiments, compiler or real trace reads."""
import importlib.util
import csv
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("default_publication", Path(__file__).with_name("verify_default_initial_publication.py"))
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.paper = self.root / "IEEE_conference_template"
        self.paper.mkdir()
        self.bundle = self.root / module.BUNDLE_RELATIVE
        self.bundle.mkdir(parents=True)
        for name in module.BUNDLE_FILES:
            (self.bundle / name).write_text("fixture")
        self.values = {"publication_status": "independently_verified_complete_amended_quality_matrix",
                       "provenance": {"new_default": {"complete": True, "verified_files": [{}],
                                      "actual_status_counts": {"success": 1}, "execution_conditions": {"complete": True}}}}
        (self.bundle / "default_initial_values.json").write_text(json.dumps(self.values))
        self.main = "\n".join(r"\input{../" + str(module.BUNDLE_RELATIVE / name) + "}"
                              for name in ("default_initial_values.tex", "representative_cases.tex"))
        (self.paper / "paper_zh.tex").write_text(self.main)
        self.checked = subprocess.CompletedProcess([], 0, json.dumps({"status": "pass", "read_only": True, "different_exports": []}), "")

    def test_publication_runs_full_source_check_with_explicit_amendment(self):
        with patch.object(module.subprocess, "run", return_value=self.checked) as run:
            report = module.audit(self.paper)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(len(report["files"]), 6)
        self.assertIn("--check", run.call_args.args[0])
        self.assertIn(str(self.root / module.AMENDMENT_RELATIVE), run.call_args.args[0])
        self.assertTrue(report["does_not_replace_old_accepted_audit"])

    def test_wrong_or_missing_macro_input_is_rejected_before_check(self):
        (self.paper / "paper_zh.tex").write_text(self.main.replace("default_initial_values.tex", "copied_values.tex"))
        with patch.object(module.subprocess, "run") as run:
            self.assertEqual(module.audit(self.paper)["status"], "fail")
            run.assert_not_called()

    def test_duplicate_input_is_rejected(self):
        (self.paper / "paper_zh.tex").write_text(self.main + "\n" + self.main)
        self.assertEqual(module.audit(self.paper)["status"], "fail")

    def test_any_bundle_or_source_drift_report_blocks_publication(self):
        failed = subprocess.CompletedProcess([], 1, json.dumps({"status": "fail", "read_only": True, "different_exports": ["main_rows.csv"]}), "")
        with patch.object(module.subprocess, "run", return_value=failed):
            self.assertEqual(module.audit(self.paper)["status"], "fail")

    def test_partial_execution_closure_cannot_be_published(self):
        self.values["provenance"]["new_default"]["execution_conditions"]["complete"] = False
        (self.bundle / "default_initial_values.json").write_text(json.dumps(self.values))
        with patch.object(module.subprocess, "run", return_value=self.checked):
            self.assertEqual(module.audit(self.paper)["status"], "fail")

    def test_changes_during_check_are_not_accepted(self):
        def change(*args, **kwargs):
            (self.bundle / "default_initial_values.tex").write_text("changed")
            return self.checked
        with patch.object(module.subprocess, "run", side_effect=change):
            self.assertEqual(module.audit(self.paper)["status"], "fail")

    def qft_fixture(self):
        (self.paper / "figures").mkdir()
        (self.paper / "figures/experimental_summary.tex").write_text(r"\MechanismQFTTransferGain")
        accepted = self.root / "ZAC_zzx/results/paper_zh_v2/fig6_mechanism.csv"
        accepted.parent.mkdir(parents=True)
        old = {"dataset": "zac18", "circuit": "qft_n18_transpiled", "canonical_sha256": "canonical",
               "baseline_method": "M1", "strict_paired": "True"}
        new = {"dataset": "zac18", "circuits": '["qft_n18_transpiled"]',
               "canonical_sha256": "canonical", "baseline_method": "M1"}
        for field in module.QFT_FIELDS:
            for a, b, value in (("Bstar_", "baseline_", "648.0" if field == "transfers" else "-0.12345678901234567"),
                                ("M4_", "default_", "402.0" if field == "transfers" else "-0.02345678901234567"),
                                ("M4_minus_Bstar_", "delta_", "-246.0" if field == "transfers" else "0.10000000000000001")):
                old[a + field] = new[b + field] = value
        macros = {"MechanismQFTBaseTransfers": "648", "MechanismQFTGATransfers": "402"}
        for component in ("Transfer", "Excitation", "Coherence"):
            macros["MechanismQFT" + component + "Gain"] = "0.10000000000000001"
            macros["MechanismQFT" + component + "GainRounded"] = "0.1000"
        self.values["macros"] = {"Default" + key: value if key.endswith("Rounded") else format(float(value), ".15g")
                                 for key, value in macros.items()}
        (self.paper / "method_argument_values.tex").write_text("\n".join(
            "\\newcommand{\\" + key + "}{" + value + "}" for key, value in macros.items()))
        def write_csv(path, row):
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
        write_csv(accepted, old)
        write_csv(self.bundle / "mechanism.csv", new)
        return old, new, write_csv

    def test_legacy_qft_requires_exact_raw_equivalence_before_precision_conversion(self):
        self.qft_fixture()
        report = module.qft_legacy_alias_audit(self.paper, self.bundle, self.values)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["exact_raw_fields"], 15)
        self.assertEqual(len(report["equivalent_macros"]), 8)
        self.assertFalse(report["numeric_tolerance_used"])

    def test_sub_rendering_precision_mechanism_change_rejects_legacy_panel(self):
        _, new, write_csv = self.qft_fixture()
        new["delta_log_atom_transfer"] = "0.10000000000000002"
        write_csv(self.bundle / "mechanism.csv", new)
        with self.assertRaisesRegex(ValueError, "mechanism differs"):
            module.qft_legacy_alias_audit(self.paper, self.bundle, self.values)

    def test_legacy_qft_transfer_count_change_is_rejected(self):
        _, new, write_csv = self.qft_fixture()
        new["default_transfers"] = "403.0"
        write_csv(self.bundle / "mechanism.csv", new)
        with self.assertRaisesRegex(ValueError, "mechanism differs"):
            module.qft_legacy_alias_audit(self.paper, self.bundle, self.values)

    def test_default_qft_macro_cannot_drift_despite_matching_raw_rows(self):
        self.qft_fixture()
        self.values["macros"]["DefaultMechanismQFTTransferGain"] = "0.2"
        with self.assertRaisesRegex(ValueError, "does not serialize"):
            module.qft_legacy_alias_audit(self.paper, self.bundle, self.values)

    def test_legacy_qft_macro_cannot_drift_despite_matching_raw_rows(self):
        self.qft_fixture()
        macro = self.paper / "method_argument_values.tex"
        macro.write_text(macro.read_text().replace("{648}", "{650}"))
        with self.assertRaisesRegex(ValueError, "not bound"):
            module.qft_legacy_alias_audit(self.paper, self.bundle, self.values)


if __name__ == "__main__":
    unittest.main()
