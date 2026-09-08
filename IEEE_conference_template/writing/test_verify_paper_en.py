"""Offline guards for the independent English manuscript and its build."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("english_verifier", SOURCE_ROOT / "verify_paper_en.py")
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
            for kind, count in (("figure", 7), ("table", 4)) for index in range(count))
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
        self.assertEqual(result["floats"]["en"], {"figure": 7, "table": 4})

    def test_english_paths_are_under_manuscript_build(self):
        self.assertEqual((self.root / english.BUILD_DIRECTORY).resolve(), self.build)
        self.assertEqual((self.root / english.zh.OVERALL_FIGURE_PDF).resolve(),
                         self.root / "build/paper_zh/figures/overall_framework.pdf")
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
        self.change_target(r"\begin{table}\label{table:3}\end{table}", "")
        self.assertIn("english_requires_seven_figures_four_tables", self.audit()[1])

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
    def framework(self):
        return (SOURCE_ROOT / "figures/overall_framework.tex").read_text(encoding="utf-8")

    def test_current_framework_relationships_pass(self):
        checks = english.zh._figure_structure_checks("overall_framework.tex", self.framework())
        self.assertTrue(checks)
        self.assertTrue(all(checks.values()), checks)

    def test_unboxed_cz_is_rejected(self):
        text = self.framework().replace(r"\node[gate] at (#1,#3) {Z}",
                                        r"\node at (#1,#3) {Z}")
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["boxed_cz_macro"])

    def test_cz_without_control_dot_is_rejected(self):
        text = self.framework().replace(r"\fill[galkInk] (#1,#2) circle (.8pt);", "")
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["boxed_cz_macro"])

    def test_cz_without_connector_is_rejected(self):
        text = self.framework().replace(r"\draw[wire] (#1,#2)--(#1,#3);", "")
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["boxed_cz_macro"])

    def test_cz_target_label_is_not_double_controlled(self):
        text = self.framework().replace(r"\node[gate] at (#1,#3) {Z}",
                                        r"\node[gate] at (#1,#3) {CZ}")
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["boxed_cz_macro"])

    def test_v_gate_is_rejected(self):
        text = self.framework().replace("{U}", "{V}", 1)
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["boxed_u_no_v"])

    def test_both_five_atom_routing_examples_replay(self):
        for name in ("overall_framework.tex", "joint_ga.tex"):
            text = (SOURCE_ROOT / "figures" / name).read_text(encoding="utf-8")
            checks = english.zh._figure_structure_checks(name, text)
            with self.subTest(figure=name):
                self.assertTrue(all(checks.values()), checks)

    def test_routing_without_temporary_release_is_rejected(self):
        text = self.framework().replace("0/1/.56/1.70/.24/.52,", "")
        checks = english.zh._figure_structure_checks("overall_framework.tex", text)
        self.assertFalse(checks["routing_source_release"])
        self.assertFalse(checks["routing_temporary_storage"])

    def test_routing_row_merge_is_rejected(self):
        text = self.framework().replace("0/3/1.76/1.70/1.76/.52",
                                        "0/3/1.76/1.70/1.76/.84")
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["routing_aod_relations"])

    def test_routing_cannot_use_nontrap_coordinates(self):
        text = self.framework().replace("0/1/.56/1.70/.24/.52",
                                        "0/1/.56/1.70/.30/.52")
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["routing_trap_coordinates"])

    def test_routing_transfer_cannot_activate_stationary_q4(self):
        text = self.framework().replace("0/3/1.76/1.70/1.76/.52",
                                        "0/3/1.76/1.70/1.00/.20")
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["routing_ghost_safe"])

    def test_routing_path_cannot_pass_through_stationary_q0(self):
        text = self.framework().replace("1/2/1.44/1.70/.56/1.70",
                                        "1/2/1.44/1.70/.24/1.70")
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["routing_static_path_clear"])

    def test_joint_intermediate_snapshot_must_include_staged_q1(self):
        text = (SOURCE_ROOT / "figures/joint_ga.tex").read_text(encoding="utf-8")
        text = text.replace("\\def\\routeStaged{0/.36/1.91,1/.28/1.08",
                            "\\def\\routeStaged{0/.36/1.91,1/.58/1.91")
        self.assertFalse(english.zh._figure_structure_checks(
            "joint_ga.tex", text)["routing_staged_snapshot"])

    def test_initial_evaluation_cannot_be_bypassed(self):
        text = self.framework().replace(
            "(candidates.south)--(initialEval.north)",
            "(candidates.south)--(initialMap.north)")
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["initial_candidates_before_selection"])

    def test_future_estimation_cannot_start_at_prior_state(self):
        text = self.framework().replace("(stateAfter.south)--", "(stateBefore.south)--")
        self.assertFalse(english.zh._figure_structure_checks(
            "overall_framework.tex", text)["lookahead_starts_at_candidate_poststate"])

    def test_seven_english_vector_figure_sources_satisfy_semantic_contracts(self):
        self.assertEqual(len(english.zh.CORE_FIGURES), 7)
        self.assertIn("zair_output.tex", english.zh.CORE_FIGURES)
        for name in english.zh.CORE_FIGURES:
            text = (SOURCE_ROOT / "figures" / name).read_text(encoding="utf-8")
            active = english.zh._strip_tex_comments(text)
            with self.subTest(figure=name):
                self.assertIn(r"\begin{tikzpicture}", active)
                self.assertNotIn(r"\includegraphics", active)
                self.assertFalse(english.HAN.search(text))
                for marker in english.zh.CORE_FIGURE_PANEL_MARKERS[name]:
                    self.assertIn(marker, active)
                for markers in english.zh.CORE_FIGURE_SEMANTIC_MARKERS[name].values():
                    for marker in markers:
                        self.assertIn(marker, active)


if __name__ == "__main__":
    unittest.main()
