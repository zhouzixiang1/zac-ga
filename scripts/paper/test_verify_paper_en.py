"""Offline guards for the independent English manuscript and its build."""

from paper_paths import PAPER_ROOT, PROJECT_ROOT, artifact_path, tool_path, notes_path
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE_ROOT = PAPER_ROOT
SPEC = importlib.util.spec_from_file_location("english_verifier", tool_path(SOURCE_ROOT, "verify_paper_en.py"))
english = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SOURCE_ROOT))
try:
    SPEC.loader.exec_module(english)
finally:
    sys.path.remove(str(SOURCE_ROOT))


class EnglishPaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.root = self.project / "paper"
        (self.root / "sections").mkdir(parents=True)
        (self.root / "sections_en").mkdir()
        self.build = self.root / "build/paper_en"
        self.build.mkdir(parents=True)
        self.source_section = self.root / "sections/01_intro.tex"
        self.target_section = self.root / "sections_en/01_intro.tex"
        (self.root / "results_values_zh.tex").write_text(r"\newcommand{\Fidelity}{0.3}")
        main = (r"\input{results_values_zh}\begin{document}\input{sections/01_intro}"
                r"\clearpage\bibliography{references}\end{document}")
        (self.root / "paper_zh.tex").write_text(main)
        (self.root / "paper_en.tex").write_text(main.replace("sections/", "sections_en/"))
        floats = "\n".join(
            rf"\begin{{{kind}}}\label{{{kind}:{index}}}\end{{{kind}}}"
            for kind, count in (("figure", 4), ("table", 1), ("algorithm", 1)) for index in range(count))
        source = (r"中文\label{sec:intro}\cite{zac,iccad}\Fidelity\Fidelity" + floats
                  + r"\begin{equation}a+b=\text{存在}\label{eq:test}\end{equation}")
        self.source_section.write_text(source)
        self.target_section.write_text(source.replace("中文", "English").replace("存在", "exists"))

    def audit(self):
        errors = []
        result = english.audit_translation(self.root, errors)
        return result, errors

    def change_target(self, old, new):
        self.target_section.write_text(self.target_section.read_text().replace(old, new))

    def test_complete_correspondence_and_explicit_formula_translation_pass(self):
        result, errors = self.audit()
        self.assertFalse(errors)
        self.assertEqual(result["floats"]["en"], {"figure": 4, "table": 1, "algorithm": 1})

    def test_unused_retained_table_and_algorithm_do_not_inflate_active_float_count(self):
        for directory in ("sections", "sections_en"):
            (self.root / directory / "retired.tex").write_text(
                r"\begin{table}\label{tab:retired}\end{table}"
                r"\begin{algorithm}\label{alg:retired}\end{algorithm}")
        result, errors = self.audit()
        self.assertFalse(errors)
        self.assertEqual(result["floats"]["en"], {"figure": 4, "table": 1, "algorithm": 1})

    def test_nested_algorithm_input_is_counted_and_checked_in_both_languages(self):
        algorithm = r"\begin{algorithm}\label{algorithm:0}\end{algorithm}"
        for folder, section in (("sections", self.source_section), ("sections_en", self.target_section)):
            self.assertIn(algorithm, section.read_text())
            section.write_text(section.read_text().replace(algorithm, rf"\input{{{folder}/03_algorithm}}"))
            (self.root / folder / "03_algorithm.tex").write_text(algorithm)
        result, errors = self.audit()
        self.assertFalse(errors)
        self.assertEqual(result["floats"], {language: {"figure": 4, "table": 1, "algorithm": 1}
                                          for language in ("zh", "en")})
        self.assertIn("03_algorithm.tex", result["files"])
        (self.root / "sections_en/03_algorithm.tex").unlink()
        self.assertIn("english_section_file_set_mismatch", self.audit()[1])

    def test_repeated_nested_algorithm_input_counts_both_occurrences(self):
        algorithm = r"\begin{algorithm}\label{algorithm:0}\end{algorithm}"
        for folder, section in (("sections", self.source_section), ("sections_en", self.target_section)):
            nested = rf"\input{{{folder}/03_algorithm}}"
            section.write_text(section.read_text().replace(algorithm, nested + nested))
            (self.root / folder / "03_algorithm.tex").write_text(algorithm)
        result, errors = self.audit()
        self.assertEqual(result["floats"]["en"]["algorithm"], 2)
        self.assertEqual(result["floats"]["zh"]["algorithm"], 2)
        self.assertIn("english_requires_four_figures_one_table_one_algorithm", errors)

    def test_ga_publication_macro_mismatch_is_detected(self):
        bundle = self.root.parent / "ZAC_zzx/results/physical_ga_main_v1/paper_exports"
        bundle.mkdir(parents=True)
        (bundle / "ga_main_values.tex").write_text(r"\newcommand{\GAMainGain}{26.42}")
        self.source_section.write_text(self.source_section.read_text() + r"\GAMainGain")
        self.assertIn("translation_result_macros_equal:01_intro.tex", self.audit()[1])

    def test_english_paths_are_under_manuscript_build(self):
        self.assertEqual((self.root / english.BUILD_DIRECTORY).resolve(), self.build)
        self.assertEqual((self.root / english.OVERALL_FIGURE_PDF).resolve(),
                         self.root / "build/paper_en/figures/overall_framework.pdf")
        self.assertFalse((self.project / "build").exists())

    def test_shared_chinese_qa_is_read_only_from_manuscript_build(self):
        payload = {"status": "pass", "page_count": 9, "overall_figure_pdf": {"pages": 1},
                   "core_figures": {name: {"tikz": True, "includegraphics": False,
                                           "contains_han": False}
                                    for name in english.zh.CORE_FIGURES}}
        current = self.root / "build/paper_zh/final_paper_qa.json"
        old = self.project / "build/paper_zh/final_paper_qa.json"
        for path in (current, old):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        with patch.object(english.zh, "_source_build_manifest", return_value={}):
            errors = []
            english.audit_shared_figure(self.root, errors)
            self.assertFalse(errors)
            current.unlink()
            errors = []
            english.audit_shared_figure(self.root, errors)
            self.assertIn("shared_chinese_build_not_verified", errors)
        self.assertTrue(old.is_file())

    def test_comments_do_not_introduce_chinese_or_false_citations(self):
        self.target_section.write_text(self.target_section.read_text() + "\n% 中文 \\cite{fake} \\label{fake}\n")
        self.assertFalse(self.audit()[1])

    def test_changed_citation_fails(self):
        self.change_target("zac,iccad", "zac")
        self.assertIn("translation_citations_equal:01_intro.tex", self.audit()[1])

    def test_duplicate_label_fails(self):
        self.change_target(r"\label{sec:intro}", r"\label{sec:intro}\label{sec:intro}")
        self.assertIn("translation_labels_equal:01_intro.tex", self.audit()[1])

    def test_result_macro_occurrence_count_is_preserved(self):
        self.change_target(r"\Fidelity\Fidelity", r"\Fidelity")
        self.assertIn("translation_result_macros_equal:01_intro.tex", self.audit()[1])

    def test_missing_translated_section_fails(self):
        self.target_section.unlink()
        self.assertIn("english_section_file_set_mismatch", self.audit()[1])

    def test_extra_translated_section_fails(self):
        (self.root / "sections_en/extra.tex").write_text("extra")
        self.assertIn("english_section_file_set_mismatch", self.audit()[1])

    def test_english_entry_cannot_input_chinese_section(self):
        target = self.root / "paper_en.tex"
        target.write_text(target.read_text().replace("sections_en/", "sections/"))
        self.assertIn("translation_input_graph_equal:paper", self.audit()[1])

    def test_untranslated_visible_chinese_fails(self):
        self.change_target("English", "英文")
        self.assertIn("translation_no_chinese:01_intro.tex", self.audit()[1])

    def test_changed_mathematical_operator_fails(self):
        self.change_target("a+b", "a-b")
        result, errors = self.audit()
        self.assertIn("translation_display_equations_equal:01_intro.tex", errors)
        self.assertIn("eq:test", result["files"]["01_intro.tex"]["equation_differences"])

    def test_formula_text_not_on_whitelist_fails(self):
        self.change_target(r"\text{exists}", r"\text{available}")
        self.assertIn("translation_display_equations_equal:01_intro.tex", self.audit()[1])

    def test_formula_line_wrapping_does_not_change_math(self):
        self.change_target("a+b=", "a+\n b=\\\\[-1mm]")
        self.assertFalse(self.audit()[1])

    def test_missing_float_fails(self):
        self.change_target(r"\begin{table}\label{table:0}\end{table}", "")
        self.assertIn("english_requires_four_figures_one_table_one_algorithm", self.audit()[1])

    def test_missing_algorithm_fails_active_float_contract(self):
        self.change_target(r"\begin{algorithm}\label{algorithm:0}\end{algorithm}", "")
        self.assertIn("english_requires_four_figures_one_table_one_algorithm", self.audit()[1])

    def test_external_figure_must_use_the_same_verified_pdf(self):
        source = r"\includegraphics{build/paper_zh/figures/overall_framework.pdf}"
        target = r"\includegraphics{unverified.pdf}"
        self.source_section.write_text(self.source_section.read_text() + source)
        self.target_section.write_text(self.target_section.read_text() + target)
        self.assertIn("translation_external_figure_paths_equal:01_intro.tex", self.audit()[1])

    def test_failed_xelatex_does_not_keep_stale_or_partial_pdf(self):
        pdf = self.build / "paper_en.pdf"
        pdf.write_bytes(b"stale English output")

        def run(command, *, cwd):
            self.assertFalse(pdf.exists())
            self.assertIn(f"-outdir={self.build}", command)
            pdf.write_bytes(b"partial failed output")
            return subprocess.CompletedProcess(command, 1, "failed")

        with patch.object(english.zh, "_run", side_effect=run):
            result = english.compile_english(self.root, self.build)
        self.assertEqual(result["returncode"], 1)
        self.assertFalse(pdf.exists())

    def test_failed_prerequisite_does_not_reuse_old_english_pdf(self):
        pdf = self.build / "paper_en.pdf"
        pdf.write_bytes(b"stale English output")
        self.target_section.unlink()
        with patch.object(english, "compile_english") as compiler:
            result = english.verify(self.root, compile_pdf=True)
        compiler.assert_not_called()
        self.assertEqual(result["status"], "fail")
        self.assertFalse(pdf.exists())

    def test_read_only_check_cannot_create_english_source_manifest(self):
        errors = []
        english.source_binding(self.root, self.build, compile_pdf=False, before=None, errors=errors)
        self.assertIn("english_source_build_manifest_missing", errors)
        self.assertFalse((self.build / "source_build_manifest.json").exists())

    def test_reference_heading_accepts_actual_two_column_layout(self):
        page = "                               REFERENCES                                        [13] D. B. Tan, W.-H. Lin\n"
        text = "\f".join(["body page"] * 10 + [page])
        self.assertEqual(english.reference_heading_pages(text), [11])

    def test_reference_heading_accepts_small_cap_letter_spacing(self):
        for heading in ("REFERENCES", "R EFERENCES", "R E F E R E N C E S", "References"):
            with self.subTest(heading=heading):
                self.assertEqual(english.reference_heading_pages("    " + heading + "\n"), [1])

    def test_reference_heading_does_not_match_prose_or_cross_line_letters(self):
        for text in ("References explain the notation.\n", "See REFERENCES for details.\n",
                     "REFERENCES [13] and [14] describe this.\n", "REFE\nRENCES\n"):
            with self.subTest(text=text):
                self.assertEqual(english.reference_heading_pages(text), [])

    def test_references_on_penultimate_page_still_fail(self):
        (self.build / "paper_en.pdf").write_bytes(b"mocked PDF")
        (self.build / "paper_en.log").write_text("")
        extracted = "\f".join(["body"] * 9 + ["REFERENCES   [13] Author\n", "continued bibliography"])
        with patch.object(english, "audit_shared_figure", return_value={}), \
                patch.object(english, "source_binding", return_value={}), \
                patch.object(english.zh, "_pdf_pages", return_value=11), \
                patch.object(english.zh, "_run", return_value=subprocess.CompletedProcess([], 0, extracted)):
            result = english.verify(self.root, compile_pdf=False)
        self.assertEqual(result["references_pages"], [10])
        self.assertIn("english_references_not_on_last_page", result["errors"])


class CurrentFigureContractsTests(unittest.TestCase):
    def setUp(self):
        # The shared semantic modules are imported lazily during each audit.
        # Keep this class runnable alone, independently of discovery order.
        import_path = patch.object(sys, "path", [str(SOURCE_ROOT), *sys.path])
        import_path.start()
        self.addCleanup(import_path.stop)

    def source(self, name):
        return (artifact_path(SOURCE_ROOT, Path("figures") / name)).read_text(encoding="utf-8")

    def framework(self):
        return self.source("overall_framework.tex")

    def scheduled_gate(self, gate, qubit):
        """Locate the intended gate without freezing its display coordinates."""
        source = self.framework()
        wires = re.search(r"\\foreach\s+\\qq/\\yy\s+in\s*\{([^{}]+)\}", source)
        self.assertIsNotNone(wires)
        ypos = dict(row.split("/") for row in wires[1].split(","))[str(qubit)]
        spans = re.findall(r"\\draw\[wire\]\s*\(([\d.]+),\\yy\)--\(([\d.]+),\\yy\)", source)
        self.assertEqual(len(spans), 2)
        lo, hi = map(float, spans[1])
        matches = [match for match in re.finditer(
            r"\\node\[gate\]\s+at\s*\(([\d.]+),([\d.]+)\)\s*"
            + r"\{\\PaperRevision\{" + re.escape(gate) + r"\}\};", source)
            if lo <= float(match[1]) <= hi and float(match[2]) == float(ypos)]
        self.assertTrue(matches)
        return matches[-1]

    def move_framework_node_horizontally(self, name, target):
        source = self.framework()
        pattern = r"\(" + re.escape(name) + r"\)\s*at\s*\(([\d.]+),([\d.]+)\)"
        node = re.search(pattern, source)
        other = re.search(r"\(" + re.escape(target) + r"\)\s*at\s*\(([\d.]+),([\d.]+)\)", source)
        self.assertIsNotNone(node)
        self.assertIsNotNone(other)
        return self.changed_checks("overall_framework.tex", node[0],
            f"({name}) at ({other[1]},{node[2]})")

    def changed_checks(self, name, old, new):
        text = self.source(name)
        self.assertIn(old, text)
        return english.zh._figure_structure_checks(name, text.replace(old, new))

    def test_current_framework_relationships_pass(self):
        checks = english.zh._figure_structure_checks("overall_framework.tex", self.framework())
        self.assertTrue(checks)
        self.assertTrue(all(checks.values()), checks)

    def test_archived_framework_retains_its_physical_relationships(self):
        checks = english.zh._figure_structure_checks(
            "overall_framework_precompact.tex", self.source("overall_framework_precompact.tex"))
        self.assertTrue(checks)
        self.assertTrue(all(checks.values()), checks)

    def test_cz_requires_a_second_symmetric_dot(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            r"\fill[galkInk] (#1,#3) circle (1.15pt);", "")["symmetric_cz_macro"])

    def test_cz_without_control_dot_is_rejected(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            r"\fill[galkInk] (#1,#2) circle (1.15pt);", "")["symmetric_cz_macro"])

    def test_cz_without_connector_is_rejected(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            r"\draw[wire] (#1,#2)--(#1,#3);", "")["symmetric_cz_macro"])

    def test_cz_cannot_silently_become_a_cnot(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            r"\fill[galkInk] (#1,#3) circle (1.15pt);",
            r"\node[gate] at (#1,#3) {$\oplus$};")["symmetric_cz_macro"])

    def test_rebased_single_qubit_notation_is_consistent(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            "{$U_3$}", "{V}")["rebased_cz_u3_gate_set"])

    def test_scheduling_cannot_delete_a_gate(self):
        gate = self.scheduled_gate("$U_3$", 3)
        self.assertFalse(self.changed_checks("overall_framework.tex",
            gate[0], "")["same_gate_multiset"])

    def test_scheduling_cannot_cross_a_qubits_2q_predecessor(self):
        gate, earlier = self.scheduled_gate("$U_3$", 3), self.scheduled_gate("$U_3$", 4)
        self.assertFalse(self.changed_checks("overall_framework.tex",
            gate[0], gate[0].replace(f"({gate[1]},{gate[2]})",
                f"({earlier[1]},{gate[2]})"))["per_atom_dependency_order"])

    def test_q4_without_2q_predecessor_belongs_in_first_1q_layer(self):
        gate, later = self.scheduled_gate("$U_3$", 4), self.scheduled_gate("$U_3$", 3)
        self.assertFalse(self.changed_checks("overall_framework.tex",
            gate[0], gate[0].replace(f"({gate[1]},{gate[2]})",
                f"({later[1]},{gate[2]})"))["scheduled_gate_layers"])

    def test_framework_cannot_skip_initial_placement(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            "schedule/initial,initial/joint", "schedule/joint")["six_stage_compilation_order"])

    def test_framework_requires_connected_stage_flow(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            r"(\from.east |- flowRow)--(\to.west |- flowRow)",
            r"(\from.east |- flowRow)--(output.west |- flowRow)")[
                "six_stage_compilation_order"])

    def test_framework_joint_detail_cannot_move_into_routing(self):
        self.assertFalse(self.move_framework_node_horizontally("currentEval", "routing")[
                "framework_joint_owns_candidate_evaluation"])

    def test_initialization_cannot_bypass_early_layer_evaluation(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            "(initialCandidates.south)--(initialEval.north)",
            "(initialCandidates.south)--(initialMap.north)")[
                "initialization_selects_starting_state"])

    def test_generic_loop_prediction_follows_the_candidate_poststate(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            r"\widehat{\mathbf{s}}_{\ell+2}", r"\widehat{\mathbf{s}}_{\ell+1}")[
                "general_layer_does_not_claim_terminal_future"])

    def test_framework_prediction_score_must_return_to_joint_search(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            "--(jointSearch.west)", "--(selectedPlan.west)")[
                "candidate_evaluation_and_score_feedback"])

    def test_execution_feedback_cannot_bypass_state_update(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            "(stateUpdate.south)--", "(routeBatches.south)--")[
                "execution_updates_next_layer_state"])

    def test_execution_feedback_does_not_reinitialize_each_layer(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            "--(joint.west |- flowRow)", "--(initial.west |- flowRow)")[
                "execution_updates_next_layer_state"])

    def test_framework_zair_cannot_drop_the_final_single_qubit_layer(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            ",2.52/1qGate", "")["zair_stream_preserves_execution_order"])

    def test_framework_states_conserve_atom_identities(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            "4/1.46/.24}", "3/1.46/.24}")["generic_candidate_conserves_five_atoms"])

    def test_generic_candidate_cannot_merge_atoms_into_one_trap(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            "4/1.46/1.01", "4/1.14/1.01")["generic_candidate_conserves_five_atoms"])

    def test_framework_draws_the_verified_final_state(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            r"\stateCard{11.15}{\candidateAfter}", r"\stateCard{11.15}{\candidateBefore}")[
                "generic_candidate_states_drive_drawing"])

    def test_initial_layouts_keep_atom_and_site_uniqueness(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            r"\def\initPermTwo{0/0,2/1,4/2,1/3,3/4}",
            r"\def\initPermTwo{0/0,2/1,4/2,1/3,3/3}")[
                "initial_layouts_are_permutations_on_shared_sites"])

    def test_current_loss_must_join_the_same_score_as_prediction(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            "(currentLoss.south)--", "(stateBefore.south)--")[
                "candidate_evaluation_and_score_feedback"])

    def test_joint_five_atom_routing_example_replays(self):
        checks = english.zh._figure_structure_checks("joint_ga.tex", self.source("joint_ga.tex"))
        self.assertTrue(all(checks.values()), checks)

    def test_routing_without_temporary_release_is_rejected(self):
        checks = self.changed_checks("joint_ga.tex", "0/1/.58/1.91/.28/1.08,", "")
        self.assertFalse(checks["routing_source_release"])
        self.assertFalse(checks["routing_temporary_storage"])

    def test_routing_row_merge_is_rejected(self):
        self.assertFalse(self.changed_checks("joint_ga.tex",
            "0/3/1.24/1.91/1.33/1.08", "0/3/1.24/1.91/1.33/.70")["routing_aod_relations"])

    def test_routing_cannot_use_nontrap_coordinates(self):
        self.assertFalse(self.changed_checks("joint_ga.tex",
            "0/1/.58/1.91/.28/1.08", "0/1/.58/1.91/.30/1.08")["routing_trap_coordinates"])

    def test_routing_transfer_cannot_activate_stationary_q4(self):
        self.assertFalse(self.changed_checks("joint_ga.tex",
            "0/3/1.24/1.91/1.33/1.08", "0/3/1.24/1.91/.63/.70")["routing_ghost_safe"])

    def test_routing_path_cannot_pass_through_stationary_q0(self):
        self.assertFalse(self.changed_checks("joint_ga.tex",
            "1/2/1.02/1.91/.58/1.91", "1/2/1.02/1.91/.36/1.91")["routing_static_path_clear"])

    def test_joint_intermediate_snapshot_must_include_staged_q1(self):
        self.assertFalse(self.changed_checks("joint_ga.tex",
            r"\def\routeStaged{0/.36/1.91,1/.28/1.08",
            r"\def\routeStaged{0/.36/1.91,1/.58/1.91")["routing_staged_snapshot"])

    def test_future_estimation_cannot_start_at_prior_state(self):
        self.assertFalse(self.changed_checks("overall_framework.tex",
            "(stateAfter)--", "(stateBefore)--")["lookahead_starts_at_candidate_poststate"])

    def test_future_estimation_anchor_must_belong_to_the_final_snapshot(self):
        self.assertFalse(self.move_framework_node_horizontally("stateAfter", "stateBefore")[
                "lookahead_starts_at_candidate_poststate"])

    def test_four_english_vector_figure_sources_satisfy_semantic_contracts(self):
        self.assertEqual(len(english.zh.CORE_FIGURES), 4)
        self.assertNotIn("zair_output.tex", english.zh.CORE_FIGURES)
        for name in english.zh.CORE_FIGURES:
            text = (artifact_path(SOURCE_ROOT, Path("figures") / name)).read_text(encoding="utf-8")
            active = english.zh._strip_tex_comments(text)
            with self.subTest(figure=name):
                self.assertIn(r"\begin{tikzpicture}", active)
                self.assertNotIn(r"\includegraphics", active)
                self.assertFalse(english.HAN.search(text))
                for marker in english.zh.CORE_FIGURE_PANEL_MARKERS[name]:
                    self.assertIn(marker, active)
                for markers in english.zh.CORE_FIGURE_SEMANTIC_MARKERS[name].values():
                    for marker in markers:
                        self.assertTrue(english.zh._figure_has_marker(name, active, marker),
                                        (name, marker))

    def test_joint_staging_label_allows_named_equivalent_but_cannot_vanish(self):
        name, marker = "joint_ga.tex", "temporary storage"
        for phrase in (marker, r"$q_1$ is staged", r"Stage $q_1$"):
            self.assertTrue(english.zh._figure_has_marker(name, phrase, marker))
        for phrase in ("", r"$q_0$ is staged", r"Stage $q_0$", "Direct swap blocked"):
            self.assertFalse(english.zh._figure_has_marker(name, phrase, marker))


if __name__ == "__main__":
    unittest.main()
