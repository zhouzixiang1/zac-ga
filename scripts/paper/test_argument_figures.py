"""Source-level guards for the explanatory model and measured result panels."""

from paper_paths import PAPER_ROOT, PROJECT_ROOT, artifact_path, tool_path, notes_path
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

ROOT = PAPER_ROOT
sys.path.insert(0, str(ROOT))
import verify_paper_zh as verifier
import result_figure_checks as result_checks


class ArgumentFigureTests(unittest.TestCase):
    def test_printed_table_order_excludes_prose_and_catches_overtaking(self):
        self.assertEqual(verifier._table_caption_order(
            "TABLE I\ncaption\nresults in TABLE IV are discussed\nTABLE II\nTABLE III\nTABLE IV\n"),
            ["I", "II", "III", "IV"])
        self.assertNotEqual(verifier._table_caption_order(
            "TABLE I\nTABLE IV\nTABLE II\nTABLE III\n"), ["I", "II", "III", "IV"])
        self.assertEqual(verifier._table_caption_order(
            "TABLE I\nbody\n\fTABLE II\ncaption\nTABLE III\nTABLE IV\n"),
            ["I", "II", "III", "IV"])

    def test_negative_flushend_split_is_a_layout_failure(self):
        pattern = verifier.LOG_FAILURE_PATTERNS["invalid_column_balance"]
        self.assertRegex("- LAST -\nSplit: -90.01546pt\n", pattern)
        self.assertNotRegex("- LAST -\nSplit: 385.66444pt\n", pattern)

    def source(self, name):
        return (artifact_path(ROOT, Path("figures") / name)).read_text(encoding="utf-8")

    def checks(self, name, text=None):
        return verifier._figure_structure_checks(name, self.source(name) if text is None else text)

    def distribution_evidence(self):
        source = self.source("fidelity_distribution_supplement.tex")
        paths = re.findall(r"\{(figures/data/[^{}]+\.dat)\}", source)
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], paths[1])
        evidence = result_checks._read_distribution(paths[0])
        self.assertIsNotNone(evidence)
        return evidence

    def changed_distribution_option(self, key, value):
        original = self.source("fidelity_distribution_supplement.tex")
        changed, count = re.subn(rf"(?<![A-Za-z]){key}={result_checks._NUMBER}(?=,|\])",
                                f"{key}={value}", original)
        self.assertEqual(count, 1)
        self.assertNotEqual(changed, original)
        return self.checks("fidelity_distribution_supplement.tex", changed)

    def test_updated_figures_pass_all_relationship_checks(self):
        for name in ("baseline_motivation.tex", "joint_ga.tex", "physical_lookahead.tex", "experimental_summary.tex", "loss_decomposition_supplement.tex", "fidelity_distribution_supplement.tex"):
            checks = self.checks(name)
            self.assertTrue(checks, name)
            self.assertTrue(all(checks.values()), (name, checks))

    def test_storage_transport_actually_changes_both_final_atoms(self):
        original = self.source("baseline_motivation.tex")
        for new in (r"\storageFinal{q/.56/.50,u/.18/.50}",
                    r"\storageFinal{q/.56/.20,u/.18/.50}"):
            changed = original.replace(r"\storageFinal{q/.56/.20,u/.94/.50}", new)
            self.assertNotEqual(changed, original)
            self.assertFalse(self.checks("baseline_motivation.tex", changed)[
                "storage_common_sources_and_targets"])

    def test_blocking_atom_must_leave_before_u_passes(self):
        original = self.source("baseline_motivation.tex")
        changed = original.replace(
            "1/q/.56/.50/.56/.20,2/u/.18/.50/.94/.50",
            "1/u/.18/.50/.94/.50,2/q/.56/.50/.56/.20")
        self.assertNotEqual(changed, original)
        self.assertFalse(self.checks("baseline_motivation.tex", changed)[
            "storage_clearance_precedes_passage"])

    def test_clear_storage_choice_cannot_add_a_second_q_move(self):
        original = self.source("baseline_motivation.tex")
        changed = original.replace(r"\storageDirectMoves{1/u/.18/.50/.94/.50}",
            r"\storageDirectMoves{1/q/.56/.20/.56/.50,2/u/.18/.50/.94/.50}")
        self.assertNotEqual(changed, original)
        self.assertFalse(self.checks("baseline_motivation.tex", changed)[
            "storage_direct_move_is_clear"])

    def test_new_state_and_path_data_must_drive_storage_drawing(self):
        original = self.source("baseline_motivation.tex")
        for old, new, key in (
            (r"\storageAtoms{16.20}{3.08}{\storageFinal}",
             r"\storageAtoms{16.20}{3.08}{\storageBlocked}", "storage_states_drive_drawing"),
            (r"\storageMoves{16.20}{3.08}{\storageDetourMoves}",
             r"\storageMoves{16.20}{3.08}{\storageDirectMoves}", "storage_moves_drive_drawing")):
            self.assertIn(old, original)
            self.assertFalse(self.checks("baseline_motivation.tex", original.replace(old, new))[key])

    def test_transport_stages_do_not_invent_gate_layers(self):
        original = self.source("baseline_motivation.tex")
        changed = original.replace("16.20/After transfer", r"16.20/$L_{\ell+2}$")
        self.assertNotEqual(changed, original)
        self.assertFalse(self.checks("baseline_motivation.tex", changed)[
            "storage_stages_not_extra_gate_layers"])

    def test_joint_keeps_every_existing_routing_check(self):
        expected = {"routing_five_atom_conservation", "routing_trap_coordinates",
                    "routing_source_release", "routing_aod_relations", "routing_ghost_safe",
                    "routing_static_path_clear", "routing_temporary_storage",
                    "routing_staged_snapshot", "routing_states_drive_drawing"}
        self.assertTrue(expected <= self.checks("joint_ga.tex").keys())

    def test_method_relationships_do_not_require_subfigure_labels(self):
        for name in ("overall_framework.tex", "joint_ga.tex", "physical_lookahead.tex"):
            text = re.sub(r"\([abc]\)\s*", "", self.source(name))
            with self.subTest(figure=name):
                checks = self.checks(name, text)
                self.assertTrue(checks)
                self.assertTrue(all(checks.values()), checks)

    def changed_checks(self, name, old, new):
        original = self.source(name)
        self.assertIn(old, original)
        return self.checks(name, original.replace(old, new))

    def test_gate_encoding_must_match_the_depicted_sites_and_direction(self):
        for old, new in ((r"T_1^{+}", r"T_4^{+}"),
                         (r"T_2^{+}", r"T_2^{-}"),
                         (r"$(q_0,q_2)$, $(q_1,q_4)$", r"$(q_2,q_0)$, $(q_1,q_4)$")):
            with self.subTest(change=new):
                self.assertFalse(self.changed_checks("joint_ga.tex", old, new)[
                    "encoding_uses_depicted_gate_sites"])

    def test_residency_bit_must_agree_with_q3_storage_assignment(self):
        self.assertFalse(self.changed_checks("joint_ga.tex",
            r"2/{$b_{\ell,1}$}/{1}", r"2/{$b_{\ell,1}$}/{0}")[
                "return_gene_matches_storage_assignment"])
        self.assertFalse(self.changed_checks("joint_ga.tex",
            r"a_\ell:q_3\mapsto s_1", r"a_\ell:q_3\mapsto s_3")[
                "return_gene_matches_storage_assignment"])

    def test_candidate_includes_a_separate_storage_assignment(self):
        self.assertFalse(self.changed_checks("joint_ga.tex",
            r"Candidate $(\mathbf{x}_\ell,a_\ell)$", r"Candidate $\mathbf{x}_\ell$")[
                "candidate_includes_separate_storage_assignment"])

    def test_execution_endpoint_cannot_omit_the_current_gates(self):
        self.assertFalse(self.changed_checks("joint_ga.tex", "2Q + 1Q", "Transport only")[
            "ordered_execution_includes_current_gates"])

    def test_candidate_genes_must_drive_the_visible_cells_and_headers(self):
        for old, new in ((r"{\PaperRevision{\value}}", r"{\PaperRevision{0}}"),
                         (r"{\PaperRevision{\head}}", r"{\PaperRevision{gene}}")):
            with self.subTest(change=new):
                self.assertFalse(self.changed_checks("joint_ga.tex", old, new)[
                    "candidate_genes_drive_visible_cells"])

    def test_comment_only_candidate_gene_cannot_satisfy_encoding(self):
        original = self.source("joint_ga.tex")
        gene = r"0/{$z_{\ell,1}$}/{$T_1^{+}$}"
        self.assertIn(gene, original)
        changed = original.replace(gene, r"0/{$z_{\ell,1}$}/{$T_4^{+}$}") + "\n% " + gene
        self.assertFalse(self.checks("joint_ga.tex", changed)["encoding_uses_depicted_gate_sites"])

    def test_return_label_must_identify_the_assigned_storage_trap(self):
        self.assertFalse(self.changed_checks("joint_ga.tex",
            r"q_3@s_1", r"q_3@s_3")["return_gene_matches_storage_assignment"])

    def test_execution_endpoint_is_the_candidate_poststate(self):
        self.assertFalse(self.changed_checks("joint_ga.tex",
            r"\mathbf{s}_{\ell+1}", r"\mathbf{s}_\ell")[
                "ordered_execution_includes_current_gates"])

    def test_two_factor_plot_cannot_claim_full_fidelity(self):
        self.assertFalse(self.changed_checks("physical_lookahead.tex",
            "Transfer and excitation loss", "Full candidate fidelity")[
                "single_atom_component_scope"])

    def test_two_factor_scene_cannot_silently_add_other_atoms(self):
        self.assertFalse(self.changed_checks("physical_lookahead.tex",
            r"\node[occupied] at (.79,#3) {};",
            r"\node[occupied] at (.79,#3) {};\node[occupied] at (1.2,#3) {};")[
                "single_atom_component_scope"])

    def test_roundtrip_requires_four_atom_transfers(self):
        self.assertFalse(self.changed_checks("physical_lookahead.tex",
            r"f_{\rm tran}^{\,4}", r"f_{\rm tran}^{\,2}")["roundtrip_four_transfers"])
        self.assertFalse(self.changed_checks("physical_lookahead.tex",
            r"at (3.62,6.67) {\PaperRevision{2}}", r"at (3.62,6.67) {\PaperRevision{1}}")[
                "roundtrip_four_transfers"])

    def test_inactive_gap_is_not_the_immediate_next_layer(self):
        self.assertFalse(self.changed_checks("physical_lookahead.tex",
            r"L_{\ell+r+1}", r"L_{\ell+1}")["reuse_after_r_inactive_layers"])

    def test_roundtrip_spatial_scene_must_visit_storage_between_uses(self):
        self.assertFalse(self.changed_checks("physical_lookahead.tex",
            r"\device{4.12}{6.09}{.15}", r"\device{4.12}{6.09}{.535}")[
                "reuse_after_r_inactive_layers"])

    def test_model_bar_heights_cannot_be_hardcoded(self):
        self.assertFalse(self.changed_checks("physical_lookahead.tex",
            r"\ArgumentStayTwoLoss", "5.006260")["model_losses_drive_bars"])

    def test_loss_axis_cannot_start_above_zero(self):
        self.assertFalse(self.changed_checks("physical_lookahead.tex",
            "{0,2,4,6}", "{2,4,6}")["negative_log_axis_starts_at_zero"])

    def test_prediction_must_start_from_candidate_poststate_and_update_in_order(self):
        for old, new in ((r"\mathbf{s}_{\ell+1}", r"\mathbf{s}_\ell"),
                         ("(postCandidate.east)--(firstFuture.west)",
                          "(priorState.east)--(firstFuture.west)"),
                         ("(firstFuture.east)--(secondFuture.west)",
                          "(postCandidate.east)--(secondFuture.west)")):
            with self.subTest(change=new):
                self.assertFalse(self.changed_checks("physical_lookahead.tex", old, new)[
                    "lookahead_starts_at_candidate_poststate"])

    def test_second_future_increment_has_decay_weight(self):
        self.assertFalse(self.changed_checks("physical_lookahead.tex",
            r"\alpha\rho\widehat C_{\ell,2}", r"\alpha\widehat C_{\ell,2}")[
                "future_increments_are_decayed"])

    def test_each_forecast_updates_configuration_and_clock(self):
        original = self.source("physical_lookahead.tex")
        changed = original.replace(r"update state\\and clock", "reuse starting state", 1)
        self.assertNotEqual(original, changed)
        self.assertFalse(self.checks("physical_lookahead.tex", changed)[
            "prediction_updates_configuration_and_clock"])

    def test_candidate_objective_retains_current_loss_and_terminal_estimate(self):
        for old, new in ((r"C_\ell^{\rm cur}", "0"), (r"\Phi_\ell", "0"),
                         (r"\alpha\rho^{h_\ell-1}", "1"),
                         ("(firstFuture.south)--", "(firstFuture.east)--")):
            with self.subTest(change=old):
                self.assertFalse(self.changed_checks("physical_lookahead.tex", old, new)[
                    "current_and_future_losses_share_candidate_objective"])

    def test_endpoint_must_follow_the_whole_window(self):
        self.assertFalse(self.changed_checks("physical_lookahead.tex",
            "(laterFuture.south)--(endpoint.north)", "(secondFuture.south)--(endpoint.north)")[
                "endpoint_follows_entire_prediction_window"])

    def test_horizon_keeps_all_circuits_and_separate_metric_means(self):
        for old, new in (("{0,...,9}", "{0,...,8}"),
                         ("y=f_mean", "y=b_mean"),
                         ("y=b#1", "y=f#1")):
            with self.subTest(change=old):
                self.assertFalse(self.changed_checks("experimental_summary.tex", old, new)[
                    "horizon_pairs_all_circuits_and_each_geometric_mean"])

    def test_horizon_spacing_uses_numeric_h_not_point_index(self):
        self.assertFalse(self.changed_checks("experimental_summary.tex",
            "x=horizon", r"x expr=\coordindex")[
                "horizon_uses_actual_numeric_configuration_spacing"])

    def test_horizon_cannot_crop_ratios_or_move_h8_reference(self):
        for old, new in (("ymin=.18", "ymin=.3"), ("ymax=1.73", "ymax=1.5"),
                         ("{(0,1) (8,1)}", "{(0,.9) (8,.9)}")):
            with self.subTest(change=old):
                self.assertFalse(self.changed_checks("experimental_summary.tex", old, new)[
                    "horizon_preserves_all_ratios_and_h8_reference"])

    def test_horizon_mean_must_remain_visually_distinct_from_circuits(self):
        self.assertFalse(self.changed_checks("experimental_summary.tex",
            "line width=1.3pt", "line width=.4pt")[
                "horizon_distinguishes_individual_and_mean_trends"])

    def test_horizon_clipping_is_safe_when_all_ratios_are_inside_axes(self):
        original = self.source("experimental_summary.tex")
        self.assertIn("clip=true", original)
        for source in (original, original.replace("clip=true", "clip=false")):
            self.assertTrue(self.checks("experimental_summary.tex", source)[
                "horizon_preserves_all_ratios_and_h8_reference"])

    def test_decomposition_net_cannot_be_replaced_by_a_component(self):
        for old, new in (("x=net,y expr=", "x=transfer,y expr="),
                         (r"{#1}{net}", r"{#1}{coherence}"),
                         (r"zerofill]{\LossNet}", r"zerofill]{\LossTransfer}")):
            with self.subTest(change=old):
                self.assertFalse(self.changed_checks("loss_decomposition_supplement.tex", old, new)[
                    "decomposition_net_marks_use_net_values"])

    def test_decomposition_must_show_negative_and_full_positive_stack(self):
        for old, new in (("xmin=-.8", "xmin=0"), ("xmax=1.0", "xmax=.8"),
                         ("clip=false", "clip=true")):
            with self.subTest(change=old):
                self.assertFalse(self.changed_checks("loss_decomposition_supplement.tex", old, new)[
                    "decomposition_preserves_negative_and_positive_extents"])

    def test_decomposition_preserves_component_stacking_and_row_order(self):
        for old, new in ((r"\LossTransfer+\LossCoherence", r"\LossTransfer-\LossCoherence"),
                         (r"(axis cs:\LossTransfer,\LossLow) rectangle (axis cs:\LossPositive,\LossHigh)",
                          r"(axis cs:0,\LossLow) rectangle (axis cs:\LossCoherence,\LossHigh)"),
                         (r"(axis cs:\LossExcitation,\LossLow) rectangle (axis cs:0,\LossHigh)",
                          r"(axis cs:0,\LossLow) rectangle (axis cs:\LossTransfer,\LossHigh)")):
            with self.subTest(change=old):
                self.assertFalse(self.changed_checks("loss_decomposition_supplement.tex", old, new)[
                    "decomposition_stacks_signed_physical_components"])
        self.assertFalse(self.changed_checks("loss_decomposition_supplement.tex", "{4-#1}", "{#1+1}")[
            "decomposition_components_bind_to_ordered_data_rows"])

    def test_distribution_cannot_use_historical_cohort(self):
        macro = re.search(r"\\(?:Default|GAMain)QMAPStrictN", self.source("fidelity_distribution_supplement.tex"))
        self.assertIsNotNone(macro)
        self.assertFalse(self.changed_checks("fidelity_distribution_supplement.tex",
            macro.group(), r"\FigSixQMAPN")[
                "distribution_uses_current_common_circuit_cohort"])

    def test_distribution_must_pair_same_rank_against_both_baselines(self):
        for old, new in (("x=rank,y=dlog_vs_iccad", "x=member_N,y=dlog_vs_iccad"),
                         ("y=dlog_vs_iccad", "y=dlog_vs_zac"),
                         ("ascending $\\Delta\\ln F$ vs. ZAC", "ascending independently")):
            with self.subTest(change=old):
                self.assertFalse(self.changed_checks("fidelity_distribution_supplement.tex", old, new)[
                    "distribution_pairs_baselines_on_shared_circuit_rank"])

    def test_distribution_review_marks_do_not_enter_numeric_transforms(self):
        from result_figure_checks import checks as raw_checks
        original = self.source("fidelity_distribution_supplement.tex")
        definitions = re.findall(r"y coord (?:inv )?trafo/\.code=\{(.*?)\n", original)
        self.assertEqual(len(definitions), 2)
        self.assertFalse(any("PaperRevision" in code for code in definitions))
        self.assertIn(r"\PaperRevision", original)
        changed = original.replace("sign(#1)*(abs(#1)", "sign(#1)*(abs(\\PaperRevision{#1})")
        self.assertNotEqual(changed, original)
        # The central presentation checker deliberately strips revision marks;
        # numeric PGF expressions must additionally be checked before stripping.
        self.assertFalse(raw_checks("fidelity_distribution_supplement.tex", changed)[
            "distribution_uses_explicit_continuous_symmetric_log_scale"])

    def test_main_table_and_representatives_use_new_separate_macros(self):
        table = (ROOT / "sections/05_main_table.tex").read_text()
        self.assertNotIn(r"\begin{PaperRevisionBlock}", table)
        prefixes = set(re.findall(r"\\(Default|GAMain)(?:ZAC|QMAP)", table))
        self.assertEqual(len(prefixes), 1, "main table must use one result bundle")
        prefix = prefixes.pop()
        for dataset in ("ZAC", "QMAP"):
            self.assertIn(rf"\{prefix}{dataset}StrictN", table)
            for method in ("MOne", "MTwo", "MFour"):
                for field in ("F", "B", "T", "V"):
                    self.assertIn(rf"\{prefix}{dataset}{method}{field}", table)
                    self.assertNotIn(rf"\{dataset}{method}{field}", table)
        cases = artifact_path(ROOT, "sections/05_circuit_table.tex").read_text()
        self.assertIn(rf"\{prefix}RepresentativeCircuitRows", cases)
        self.assertEqual(set(re.findall(r"\\(Default|GAMain)RepresentativeCircuitRows", cases)), {prefix})
        self.assertNotIn(r"\RepresentativeCircuitRows", cases)
        self.assertIn(r"\tl_replace_all:Nnn \l_tmpa_tl {\_} {_}", cases)

    def test_distribution_cannot_replace_data_with_comment_only_reference(self):
        original = self.source("fidelity_distribution_supplement.tex")
        changed = original.replace("y=dlog_vs_iccad", "y=member_N")
        changed += "\n% table[x=rank,y=dlog_vs_iccad,col sep=space]\n"
        self.assertFalse(self.checks("fidelity_distribution_supplement.tex", changed)[
            "distribution_pairs_baselines_on_shared_circuit_rank"])

    def test_distribution_cannot_add_an_independent_comparison_axis(self):
        original = self.source("fidelity_distribution_supplement.tex")
        changed = original + r"\begin{axis}[name=anotherAxis]\end{axis}"
        self.assertFalse(self.checks("fidelity_distribution_supplement.tex", changed)[
            "distribution_has_one_evidence_axis"])

    def test_distribution_retains_all_selected_cohort_rank_positions(self):
        count = self.distribution_evidence()["count"]
        for key, value in (("xmax", count - .5), ("xmin", 1.5)):
            with self.subTest(change=key):
                self.assertFalse(self.changed_distribution_option(key, value)[
                    "distribution_uses_current_common_circuit_cohort"])

    def test_distribution_cannot_clip_either_signed_tail_or_filter_points(self):
        evidence = self.distribution_evidence()
        for key, value in (("ymin", min(0, evidence["minimum"]) + 1e-7),
                           ("ymax", max(0, evidence["maximum"]) - 1e-7)):
            with self.subTest(change=key):
                self.assertFalse(self.changed_distribution_option(key, value)[
                    "distribution_preserves_all_signed_values_and_extremes"])
        for old, new in (("clip=false", "clip=true"),
                         ("clip=false", "clip=false,restrict y to domain=-.1:1"),
                         ("clip=false", "clip=false,each nth point=2")):
            with self.subTest(change=old):
                self.assertFalse(self.changed_checks("fidelity_distribution_supplement.tex", old, new)[
                    "distribution_preserves_all_signed_values_and_extremes"])

    def test_distribution_point_series_cannot_be_connected_or_merged(self):
        for old, new in (("only marks", "sharp plot"),
                         ("mark=triangle", "mark=*")):
            with self.subTest(change=old):
                self.assertFalse(self.changed_checks("fidelity_distribution_supplement.tex", old, new)[
                    "distribution_points_have_distinct_markers_without_lines"])

    def test_distribution_symmetric_log_keeps_sign_zero_and_linear_core(self):
        for old, new in (("sign(#1)*", "abs(#1)*"),
                         ("? abs(#1)/0.01", "? abs(#1)/0.1"),
                         ("y coord inv trafo", "obsolete inverse"),
                         ("Linear scale within", "No linear region within")):
            with self.subTest(change=old):
                self.assertFalse(self.changed_checks("fidelity_distribution_supplement.tex", old, new)[
                    "distribution_uses_explicit_continuous_symmetric_log_scale"])
        original = self.source("fidelity_distribution_supplement.tex")
        for key in ("ytick", "yticklabels"):
            pattern = rf"(?<![A-Za-z]){key}=\{{[^{{}}]*\}}"
            changed, count = re.subn(pattern, f"{key}={{.01,.1,1}}", original)
            self.assertEqual(count, 1)
            self.assertNotEqual(changed, original)
            self.assertFalse(self.checks("fidelity_distribution_supplement.tex", changed)[
                "distribution_uses_explicit_continuous_symmetric_log_scale"])

    def test_distribution_zero_line_cannot_move_or_be_removed(self):
        original = self.source("fidelity_distribution_supplement.tex")
        changed, count = re.subn(
            rf"(\(axis cs:{result_checks._NUMBER},)0(\)--\(axis cs:{result_checks._NUMBER},)0(\))",
            r"\g<1>.1\g<2>.1\3", original)
        self.assertEqual(count, 1)
        self.assertNotEqual(changed, original)
        self.assertFalse(self.checks("fidelity_distribution_supplement.tex", changed)[
                "distribution_has_true_zero_line_and_signed_direction"])

    def test_distribution_baseline_legend_cannot_be_ambiguous(self):
        self.assertFalse(self.changed_checks("fidelity_distribution_supplement.tex",
            "vs. ICCAD/QMAP", "vs. baseline")["distribution_identifies_each_baseline"])

    def test_distribution_supports_both_bundles_with_nonhistorical_count_and_extremes(self):
        # Hypothetical bounds exercise the source guard only. No GA experiment
        # data or plotting artifact is invented or written by this test.
        evidence = {"count": 137, "minimum": -1.5, "maximum": 250.0}
        for path, macro in result_checks._DISTRIBUTIONS.items():
            with self.subTest(path=path):
                source = self.source("fidelity_distribution_supplement.tex")
                source, count = re.subn(r"figures/data/[^{}]+\.dat", lambda _: path, source)
                self.assertEqual(count, 2)
                source, count = re.subn(r"\\(?:Default|GAMain)QMAPStrictN", lambda _: macro, source)
                self.assertEqual(count, 1)
                for key, value in (("xmin", ".5"), ("xmax", "137.5"),
                                   ("ymin", "-2"), ("ymax", "3e2")):
                    source, count = re.subn(rf"(?<![A-Za-z]){key}={result_checks._NUMBER}(?=,|\])",
                                           f"{key}={value}", source)
                    self.assertEqual(count, 1)
                for key, value in (("xtick", "1,50,100,137"),
                                   ("ytick", "-1,-.01,0,.01,1,100"),
                                   ("yticklabels", "$-1$,$-0.01$,$0$,$0.01$,$1$,$100$")):
                    source, count = re.subn(rf"(?<![A-Za-z]){key}=\{{[^{{}}]*\}}",
                                           f"{key}={{{value}}}", source)
                    self.assertEqual(count, 1)
                source, count = re.subn(
                    rf"\(axis cs:{result_checks._NUMBER},0\)--\(axis cs:{result_checks._NUMBER},0\)",
                    "(axis cs:.5,0)--(axis cs:137.5,0)", source)
                self.assertEqual(count, 1)
                with patch.object(result_checks, "_read_distribution", return_value=evidence) as read:
                    checks = result_checks.checks("fidelity_distribution_supplement.tex", source)
                    self.assertTrue(all(checks.values()), checks)
                    read.assert_called_once_with(path)
                    clipped = source.replace("ymax=3e2", "ymax=200")
                    self.assertNotEqual(clipped, source)
                    self.assertFalse(result_checks.checks("fidelity_distribution_supplement.tex", clipped)[
                        "distribution_preserves_all_signed_values_and_extremes"])
                    other_macro = (r"\GAMainQMAPStrictN" if "Default" in macro else r"\DefaultQMAPStrictN")
                    mislabeled = source.replace(macro, other_macro)
                    self.assertNotEqual(mislabeled, source)
                    self.assertFalse(result_checks.checks("fidelity_distribution_supplement.tex", mislabeled)[
                        "distribution_uses_current_common_circuit_cohort"])

    def test_distribution_missing_selected_data_has_no_historical_fallback(self):
        with patch.object(result_checks, "_read_distribution", return_value=None):
            checks = result_checks.checks("fidelity_distribution_supplement.tex", self.source("fidelity_distribution_supplement.tex"))
        for key in ("distribution_data_matches_selected_frozen_summary",
                    "distribution_uses_current_common_circuit_cohort",
                    "distribution_preserves_all_signed_values_and_extremes"):
            self.assertFalse(checks[key], key)

    def test_distribution_reader_validates_count_rank_and_both_extrema_without_writes(self):
        relative = "figures/data/default_initial_qmap_distribution.dat"
        data_path = artifact_path(ROOT, relative)
        provenance_path = artifact_path(ROOT, Path(relative).with_suffix(".provenance.json"))
        original_data = data_path.read_text()
        original_summary = json.loads(provenance_path.read_text())
        lines = original_data.splitlines()
        duplicate_rank = lines.copy()
        duplicate_rank[2] = "1 " + duplicate_rank[2].split(" ", 1)[1]
        wrong_bounds = json.loads(json.dumps(original_summary))
        wrong_bounds["comparisons"]["dlog_vs_iccad"]["maximum"] += 1
        for label, data, summary in (
                ("missing point", "\n".join(lines[:-1]) + "\n", original_summary),
                ("duplicate rank", "\n".join(duplicate_rank) + "\n", original_summary),
                ("ICCAD extremum mismatch", original_data, wrong_bounds)):
            contents = {data_path: data, provenance_path: json.dumps(summary)}
            def read(path, *args, **kwargs):
                return contents[path]
            with self.subTest(change=label), patch.object(Path, "read_text", read), patch.object(
                    Path, "write_text", side_effect=AssertionError("checker attempted to write")):
                self.assertIsNone(result_checks._read_distribution(relative))

    def test_distribution_reader_accepts_the_new_filename_with_the_same_schema(self):
        old_relative = "figures/data/default_initial_qmap_distribution.dat"
        old_path = artifact_path(ROOT, old_relative)
        new_relative = "figures/data/physical_ga_main_qmap_distribution.dat"
        new_path = artifact_path(ROOT, new_relative)
        # Reuse existing bytes in memory to test the parser's second path;
        # this does not represent or create results for the new experiment.
        contents = {new_path: old_path.read_text(),
                    artifact_path(ROOT, Path(new_relative).with_suffix(".provenance.json")): artifact_path(ROOT, Path(old_relative).with_suffix(".provenance.json")).read_text()}
        expected = result_checks._read_distribution("figures/data/default_initial_qmap_distribution.dat")
        with patch.object(Path, "read_text", lambda path, *args, **kwargs: contents[path]), patch.object(
                Path, "write_text", side_effect=AssertionError("checker attempted to write")):
            self.assertEqual(result_checks._read_distribution(new_relative), expected)

    def test_zair_operations_are_owned_by_rearrangement_instructions(self):
        self.assertFalse(self.changed_checks("zair_output.tex",
            "'insts':", "'substeps':")[
                "zair_rearrangement_contains_expanded_operations"])

    def test_zair_fields_cannot_lose_gate_locations_or_move_endpoint(self):
        for field in (r"'locs': \textcolor{galkData}{list[qloc]}, ",
                      r"'row\_y\_end': \textcolor{galkData}{list[float]}, "):
            self.assertFalse(self.changed_checks("zair_output.tex", field, "")[
                "zair_fields_belong_to_named_interfaces"])

    def test_zair_record_format_passes_without_tree_or_panel_contracts(self):
        checks = self.checks("zair_output.tex")
        self.assertTrue(checks)
        self.assertTrue(all(checks.values()), checks)
        self.assertNotIn("zair_high_level_types_are_parallel_branches", checks)
        self.assertNotIn("zair_type_branches_are_not_execution_arrows", checks)

    def test_zair_preserves_each_instruction_and_operation_kind(self):
        for kind in ("init", "1qGate", "rydberg", "rearrangeJob",
                     "activate", "deactivate", "move"):
            with self.subTest(kind=kind):
                self.assertFalse(self.changed_checks("zair_output.tex",
                    f"<{kind}>", "<unknown>")["zair_instruction_types_are_named"])

    def test_zair_locations_are_qloc_records_not_scalar_atom_ids(self):
        for field in (r"init\_locs", "locs", r"begin\_locs", r"end\_locs"):
            with self.subTest(field=field):
                self.assertFalse(self.changed_checks("zair_output.tex",
                    "'" + field + r"': \textcolor{galkData}{list[qloc]}",
                    "'" + field + r"': \textcolor{galkData}{list[int]}")[
                        "zair_locations_retain_qubit_and_trap_records"])

    def test_zair_id_arrays_cannot_be_coordinate_arrays(self):
        self.assertFalse(self.changed_checks("zair_output.tex",
            r"'row\_id': \textcolor{galkData}{list[int]}",
            r"'row\_id': \textcolor{galkData}{list[float]}")[
                "zair_aod_ids_and_coordinates_have_correct_types"])

    def test_zair_coordinates_cannot_be_discrete_ids(self):
        self.assertFalse(self.changed_checks("zair_output.tex",
            r"'col\_x\_end': \textcolor{galkData}{list[float]}",
            r"'col\_x\_end': \textcolor{galkData}{list[int]}")[
                "zair_aod_ids_and_coordinates_have_correct_types"])

    def test_zair_gate_and_atom_lists_keep_their_distinct_types(self):
        for old, new in ((r"'gates': \textcolor{galkData}{list[gate]}",
                         r"'gates': \textcolor{galkData}{list[qloc]}"),
                        (r"'aod\_qubits': \textcolor{galkData}{list[int]}",
                         r"'aod\_qubits': \textcolor{galkData}{list[qloc]}")):
            self.assertFalse(self.changed_checks("zair_output.tex", old, new)[
                "zair_scalar_and_gate_types_match_output"])

    def test_zair_operation_alternatives_are_not_an_execution_order(self):
        changed = self.changed_checks("zair_output.tex",
            "list[activate | move | deactivate]", "list[deactivate | activate | move]")
        self.assertTrue(all(changed.values()), changed)

    def test_zair_operation_alternatives_cannot_omit_or_duplicate_a_kind(self):
        for replacement in ("list[activate | move]", "list[activate | move | move]"):
            self.assertFalse(self.changed_checks("zair_output.tex",
                "list[activate | move | deactivate]", replacement)[
                    "zair_rearrangement_contains_expanded_operations"])

    def test_zair_missing_field_separator_is_not_valid_format_notation(self):
        self.assertFalse(self.changed_checks("zair_output.tex",
            r"list[float]}, 'col\_id'", r"list[float]} 'col\_id'")[
                "zair_format_uses_record_productions"])

    def test_zair_comment_only_fields_do_not_satisfy_the_format(self):
        original = self.source("zair_output.tex")
        field = r"'locs': \textcolor{galkData}{list[qloc]}, "
        self.assertIn(field, original)
        changed = original.replace(field, "") + "\n% " + field + "\n"
        self.assertFalse(self.checks("zair_output.tex", changed)[
            "zair_fields_belong_to_named_interfaces"])

    def test_zair_format_is_retained_as_supplement_after_body_compression(self):
        method = (ROOT / "sections/03_method.tex").read_text()
        self.assertNotIn(r"\input{figures/zair_output}", method)
        attribution = re.compile(r"ZAIR[^。；\n]{0,12}\\cite\{lin2025zac\}")
        self.assertRegex(method, attribution)
        # A ZAC citation elsewhere in scheduling does not attribute ZAIR.
        match = attribution.search(method)
        without_attribution = method[:match.start()] + match[0].replace(
            r"\cite{lin2025zac}", "") + method[match.end():]
        self.assertNotRegex(without_attribution, attribution)
        self.assertIn("执行时的原子映射", method)
        self.assertTrue(all(self.checks("zair_output.tex").values()))

    def test_zair_direct_checker_accepts_marked_and_unmarked_format(self):
        from result_figure_checks import checks
        original = self.source("zair_output.tex")
        marked = checks("zair_output.tex", original)
        unmarked = checks("zair_output.tex", verifier._strip_tex_comments(original))
        self.assertTrue(all(marked.values()), marked)
        self.assertEqual(marked, unmarked)

    def test_model_and_measured_macros_have_distinct_verified_sources(self):
        source = ROOT.parent / "ZAC_zzx/results/paper_zh_v2/fig6_mechanism.csv"
        with source.open(newline="", encoding="utf-8") as stream:
            rows = [row for row in csv.DictReader(stream)
                    if row["dataset"] == "zac18" and row["circuit"] == "qft_n18_transpiled"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["baseline_method"], row["strict_paired"]), ("M1", "True"))
        metadata = json.loads((artifact_path(ROOT, "paper_argument_values.json")).read_text())
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
        paths = [ROOT / "method_argument_values.tex", artifact_path(ROOT, "paper_argument_values.json")]
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
