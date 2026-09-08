"""Source-level guards for the explanatory model and measured result panels."""

import csv
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import verify_paper_zh as verifier


class ArgumentFigureTests(unittest.TestCase):
    def test_negative_flushend_split_is_a_layout_failure(self):
        pattern = verifier.LOG_FAILURE_PATTERNS["invalid_column_balance"]
        self.assertRegex("- LAST -\nSplit: -90.01546pt\n", pattern)
        self.assertNotRegex("- LAST -\nSplit: 385.66444pt\n", pattern)

    def source(self, name):
        return (ROOT / "figures" / name).read_text(encoding="utf-8")

    def checks(self, name, text=None):
        return verifier._figure_structure_checks(name, self.source(name) if text is None else text)

    def test_updated_figures_pass_all_relationship_checks(self):
        for name in ("joint_ga.tex", "physical_lookahead.tex", "experimental_summary.tex"):
            checks = self.checks(name)
            self.assertTrue(checks, name)
            self.assertTrue(all(checks.values()), (name, checks))

    def test_joint_keeps_every_existing_routing_check(self):
        expected = {"routing_five_atom_conservation", "routing_trap_coordinates",
                    "routing_source_release", "routing_aod_relations", "routing_ghost_safe",
                    "routing_static_path_clear", "routing_temporary_storage",
                    "routing_staged_snapshot", "routing_states_drive_drawing"}
        self.assertTrue(expected <= self.checks("joint_ga.tex").keys())

    def test_gate_encoding_must_match_the_depicted_sites_and_direction(self):
        original = self.source("joint_ga.tex")
        for old, new in ((r"T_1^{+}", r"T_4^{+}"),
                         (r"T_2^{+}", r"T_2^{-}"),
                         ("{$(q_0,q_2)$}", "{$(q_2,q_0)$}")):
            with self.subTest(change=new):
                self.assertIn(old, original)
                self.assertFalse(self.checks("joint_ga.tex", original.replace(old, new))[
                    "encoding_uses_depicted_gate_sites"])

    def test_residency_bit_must_agree_with_q3_storage_assignment(self):
        original = self.source("joint_ga.tex")
        changed = original.replace(r"2/{$b_{\ell,1}$}/{1}", r"2/{$b_{\ell,1}$}/{0}")
        self.assertNotEqual(original, changed)
        self.assertFalse(self.checks("joint_ga.tex", changed)[
            "return_gene_matches_storage_assignment"])

    def test_two_factor_plot_cannot_claim_full_fidelity(self):
        changed = self.source("physical_lookahead.tex").replace(
            "Transfer and excitation factors only", "Full candidate fidelity")
        self.assertFalse(self.checks("physical_lookahead.tex", changed)[
            "single_atom_component_scope"])

    def test_excluded_coherence_scope_cannot_be_removed(self):
        changed = self.source("physical_lookahead.tex").replace(
            "Coherence and other atoms excluded", "All atom losses included")
        self.assertFalse(self.checks("physical_lookahead.tex", changed)[
            "single_atom_component_scope"])

    def test_roundtrip_requires_four_atom_transfers(self):
        changed = self.source("physical_lookahead.tex").replace(
            r"f_{\rm tran}^{\,4}", r"f_{\rm tran}^{\,2}")
        self.assertFalse(self.checks("physical_lookahead.tex", changed)[
            "roundtrip_four_transfers"])

    def test_model_bar_heights_cannot_be_hardcoded(self):
        changed = self.source("physical_lookahead.tex").replace(
            r"\ArgumentStayTwoLoss", "5.006260")
        self.assertFalse(self.checks("physical_lookahead.tex", changed)[
            "model_losses_drive_bars"])

    def test_prediction_must_start_from_candidate_poststate_and_update_in_order(self):
        original = self.source("physical_lookahead.tex")
        for old, new in ((r"\mathbf{s}_{\ell+1}", r"\mathbf{s}_\ell"),
                         ("(postCandidate.east)--(firstFuture.west)",
                          "(priorState.east)--(firstFuture.west)"),
                         ("(firstFuture.east)--(secondFuture.west)",
                          "(postCandidate.east)--(secondFuture.west)")):
            with self.subTest(change=new):
                self.assertIn(old, original)
                self.assertFalse(self.checks("physical_lookahead.tex", original.replace(old, new))[
                    "lookahead_starts_at_candidate_poststate"])

    def test_second_future_increment_has_decay_weight(self):
        changed = self.source("physical_lookahead.tex").replace(
            r"\alpha\rho\widehat C_{\ell,2}", r"\alpha\widehat C_{\ell,2}")
        self.assertFalse(self.checks("physical_lookahead.tex", changed)[
            "future_increments_are_decayed"])

    def test_results_keep_baseline_specific_aggregate_macros(self):
        changed = self.source("experimental_summary.tex").replace(
            r"100*(\DefaultQMAPMFourF/\DefaultQMAPMTwoF-1)",
            r"100*(\DefaultQMAPMFourF/\DefaultQMAPMOneF-1)")
        self.assertFalse(self.checks("experimental_summary.tex", changed)[
            "aggregate_and_control_values_are_derived"])

    def test_new_default_values_cannot_use_historical_cohort(self):
        original = self.source("experimental_summary.tex")
        changed = original.replace(r"\DefaultZACStrictN", r"\FigSixZACN")
        self.assertFalse(self.checks("experimental_summary.tex", changed)["default_cohort_labels_are_marked"])
        changed = original.replace(r"100*(\DefaultZACMFourF/\DefaultZACMOneF-1)",
                                   r"100*(\ZACMFourF/\ZACMOneF-1)")
        self.assertFalse(self.checks("experimental_summary.tex", changed)["aggregate_and_control_values_are_derived"])

    def test_default_bar_review_marks_do_not_enter_numeric_macros(self):
        original = self.source("experimental_summary.tex")
        changed = original.replace(r"\PaperRevision{\pgfmathprintnumber{\pgfplotspointmeta}}",
                                   r"\pgfmathprintnumber{\pgfplotspointmeta}")
        self.assertFalse(self.checks("experimental_summary.tex", changed)["default_bar_values_are_marked"])
        definitions = re.findall(r"\\edef[^\n]+", original)
        self.assertTrue(definitions)
        self.assertFalse(any("PaperRevision" in definition for definition in definitions))

    def test_main_table_and_representatives_use_new_separate_macros(self):
        table = (ROOT / "sections/05_main_table.tex").read_text()
        self.assertIn(r"\begin{PaperRevisionBlock}", table)
        for dataset in ("ZAC", "QMAP"):
            self.assertIn(rf"\Default{dataset}StrictN", table)
            for method in ("MOne", "MTwo", "MFour"):
                for field in ("F", "B", "T", "V"):
                    self.assertIn(rf"\Default{dataset}{method}{field}", table)
                    self.assertNotIn(rf"\{dataset}{method}{field}", table)
        cases = (ROOT / "sections/05_circuit_table.tex").read_text()
        self.assertIn(r"\DefaultRepresentativeCircuitRows", cases)
        self.assertNotIn(r"\RepresentativeCircuitRows", cases)
        self.assertIn(r"\tl_replace_all:Nnn \l_tmpa_tl {\_} {_}", cases)
        self.assertIn("{wstate_n27}{Wstate-27}", cases)

    def test_control_plot_cannot_replace_the_registered_ratio(self):
        changed = self.source("experimental_summary.tex").replace(
            r"(2,\ArgumentLookaheadGain)", "(2,30)")
        self.assertFalse(self.checks("experimental_summary.tex", changed)[
            "aggregate_and_control_macros_drive_plots"])

    def test_qft_component_sign_cannot_be_inverted_or_hidden(self):
        original = self.source("experimental_summary.tex")
        for replacement in (r"(2,-\MechanismQFTExcitationGain)",
                            r"(2,{abs(\MechanismQFTExcitationGain)})", "(2,0)"):
            with self.subTest(replacement=replacement):
                changed = original.replace(r"(2,\MechanismQFTExcitationGain)", replacement)
                self.assertFalse(self.checks("experimental_summary.tex", changed)[
                    "qft_signed_components_drive_plots"])

    def test_qft_axis_must_show_negative_component(self):
        original = self.source("experimental_summary.tex")
        changed = re.sub(r"ymin=-[\d.]+", "ymin=0", original)
        self.assertNotEqual(original, changed)
        self.assertFalse(self.checks("experimental_summary.tex", changed)[
            "qft_axis_preserves_negative_component"])

    def test_comment_only_macros_cannot_satisfy_plot_contract(self):
        changed = self.source("experimental_summary.tex").replace(
            r"(2,\ArgumentLookaheadGain)", "(2,0)")
        changed += "\n% (2,\\ArgumentLookaheadGain)\n"
        self.assertFalse(self.checks("experimental_summary.tex", changed)[
            "aggregate_and_control_macros_drive_plots"])

    def test_model_and_measured_macros_have_distinct_verified_sources(self):
        source = ROOT.parent / "ZAC_zzx/results/paper_zh_v2/fig6_mechanism.csv"
        with source.open(newline="", encoding="utf-8") as stream:
            rows = [row for row in csv.DictReader(stream)
                    if row["dataset"] == "zac18" and row["circuit"] == "qft_n18_transpiled"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["baseline_method"], row["strict_paired"]), ("M1", "True"))
        metadata = json.loads((ROOT / "paper_argument_values.json").read_text())
        macros, duplicates, malformed = verifier._parse_result_macros(
            (ROOT / "method_argument_values.tex").read_text())
        self.assertFalse(duplicates or malformed)
        self.assertEqual(metadata["macros"], macros)
        self.assertEqual(metadata["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(metadata["kind"], "derived_presentation_not_new_experiment")
        self.assertEqual(metadata["baseline"], "ZAC")
        self.assertEqual(metadata["roundtrip_transfers"], 4)
        self.assertIn("not full objective", metadata["illustration"])
        for macro, field in {
                "MechanismQFTBaseTransfers": "Bstar_transfers",
                "MechanismQFTGATransfers": "M4_transfers",
                "MechanismQFTTransferGain": "M4_minus_Bstar_log_atom_transfer",
                "MechanismQFTExcitationGain": "M4_minus_Bstar_log_idle_excitation",
                "MechanismQFTCoherenceGain": "M4_minus_Bstar_log_coherence_linear"}.items():
            self.assertEqual(Decimal(macros[macro]), Decimal(row[field]))
        with localcontext() as context:
            context.prec = 40
            exc, tran = Decimal("0.9975"), Decimal("0.999")
            for macro, expected in {
                    "ArgumentStayOneLoss": -1000 * exc.ln(),
                    "ArgumentStayTwoLoss": -2000 * exc.ln(),
                    "ArgumentRoundtripLoss": -4000 * tran.ln()}.items():
                self.assertLessEqual(abs(Decimal(macros[macro]) - expected), Decimal("0.0000005"))


class ArgumentValuesAuditTests(unittest.TestCase):
    def test_live_generated_values_pass_read_only_check(self):
        paths = [ROOT / "method_argument_values.tex", ROOT / "paper_argument_values.json"]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        errors = []
        audit = verifier._audit_argument_values(ROOT, errors=errors)
        self.assertEqual(audit["status"], "pass")
        self.assertFalse(errors)
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths])

    def test_checker_is_invoked_with_check_and_failed_report_is_rejected(self):
        for code, payload in ((1, {"status": "fail", "read_only": True, "stale_files": ["macro"]}),
                              (0, {"status": "pass", "read_only": False, "stale_files": []}),
                              (0, {"status": "pass", "read_only": True, "stale_files": ["json"]}),
                              (0, ["pass"])):
            with self.subTest(payload=payload), patch.object(verifier, "_run", return_value=
                    subprocess.CompletedProcess([], code, json.dumps(payload))) as run:
                errors = []
                audit = verifier._audit_argument_values(ROOT, errors=errors)
                self.assertEqual(run.call_args.args[0][-1], "--check")
                self.assertEqual(audit["status"], "fail")
                self.assertIn("argument_values_not_current", errors)

    def test_missing_checker_fails_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(verifier, "_run") as run:
            errors = []
            audit = verifier._audit_argument_values(Path(directory), errors=errors)
            self.assertEqual(audit["status"], "fail")
            self.assertIn("argument_values_checker_missing", errors)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
