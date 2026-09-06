"""Offline guards for the independent English manuscript and its build."""

import importlib.util
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
        self.build = self.project / "build/paper_en"
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
            for kind, count in (("figure", 6), ("table", 4)) for index in range(count))
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
        self.assertEqual(result["floats"]["en"], {"figure": 6, "table": 4})

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
        self.assertIn("english_requires_six_figures_four_tables", self.audit()[1])

    def test_external_figure_must_use_the_same_verified_pdf(self):
        source = r"\includegraphics{../build/paper_zh/figures/overall_framework.pdf}"
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


if __name__ == "__main__":
    unittest.main()
