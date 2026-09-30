"""Current redline and clean-output contract; scientific semantics are separate."""
from paper_paths import PAPER_ROOT, PROJECT_ROOT, artifact_path, tool_path, notes_path
from pathlib import Path
import hashlib
import sys
import unittest

ROOT = PAPER_ROOT
sys.path.insert(0, str(ROOT))
import verify_paper_zh as verifier

class RevisionMarksTest(unittest.TestCase):
    def test_clean_default_with_explicit_review_opt_in(self):
        source = (ROOT / 'revision_marks.tex').read_text()
        self.assertIn(r'\newif\ifPaperShowRevisions' + '\n' + r'\PaperShowRevisionsfalse', source)
        self.assertIn(r'\ifdefined\PaperShowReviewMarks\PaperShowRevisionstrue\fi', source)
        self.assertIn(r'\ifdefined\PaperHideRevisions\PaperShowRevisionsfalse\fi', source)
        # Retain the same TeX grouping so Chinese punctuation spacing does not
        # change when the clean variant inherits the surrounding text colour.
        self.assertIn(r'\textcolor{PaperRevisionRed}{#1}\else\textcolor{.}{#1}', source)

    def test_main_and_standalone_share_the_switch(self):
        for name in ('paper_zh.tex', 'paper_en.tex', 'figures/overall_framework_standalone.tex'):
            self.assertIn(r'\input{revision_marks}', (ROOT / name).read_text())

    def test_nested_formula_unwrap_preserves_all_content(self):
        sample = r'\PaperRevision{新文$H_{\rm init}=2$和\PaperRevision{末端$\Phi_\ell$}}'
        self.assertEqual(verifier._strip_revision_markup(sample), r'新文$H_{\rm init}=2$和末端$\Phi_\ell$')
        with self.assertRaises(ValueError): verifier._strip_revision_markup(r'\PaperRevision{broken')

    def test_approved_abstract_and_keywords_survive_compression(self):
        for language in ("zh", "en"):
            errors = []
            checks = verifier._audit_protected_text(ROOT, language, errors)
            self.assertEqual(checks, {"abstract": True, "IEEEkeywords": True})
            self.assertFalse(errors)

    def test_semantic_colours_are_not_revision_colours(self):
        style = (ROOT / 'figures/tikz_style.tex').read_text()
        self.assertNotIn('PaperRevisionRed', style)
        for semantic in ('galkStorage', 'galkEntangle', 'galkInk', 'galkBad'):
            self.assertIn(r'\definecolor{' + semantic + '}', style)
        for name in verifier.CORE_FIGURES:
            source = (ROOT / 'figures' / name).read_text()
            self.assertNotIn(r'\begin{PaperRevisionBlock}', source)

    def test_all_figures_disable_review_colour_locally(self):
        for name in verifier.CORE_FIGURES:
            source = verifier._strip_tex_comments((ROOT / 'figures' / name).read_text())
            if r'\PaperRevision' in source:
                self.assertRegex(source, r"\\begingroup\s*\\PaperShowRevisionsfalse")
                self.assertTrue(source.rstrip().endswith(r'\endgroup'))
            self.assertNotIn(r'\PaperShowRevisionstrue', source)
        # Accepted prose defaults to clean; explicit review remains available.
        self.assertIn(r'\ifdefined\PaperShowReviewMarks\PaperShowRevisionstrue\fi', (ROOT / 'revision_marks.tex').read_text())

    def test_numeric_sources_do_not_contain_revision_wrappers(self):
        for name in ('results_values_zh.tex', 'method_argument_values.tex', 'horizon_extension_values.tex'):
            self.assertNotIn(r'\PaperRevision', (ROOT / name).read_text())

    def test_clean_build_has_distinct_main_and_figure_targets(self):
        source = (tool_path(ROOT, "verify_paper_zh.py")).read_text()
        self.assertIn('choices=("review", "clean")', source)
        self.assertIn('overall_framework_clean.tex', source)
        self.assertIn('paper_clean_driver.tex', source)
        self.assertIn(r'\def\PaperOverallFigurePath{', source)
        self.assertIn('paper-clean:', (ROOT.parent / 'Makefile').read_text())

    def test_two_contributions_survive_the_redline(self):
        source = verifier._strip_revision_markup(
            (ROOT / 'sections/01_introduction.tex').read_text()
        ).split(r'\label{sec:contributions}', 1)[1]
        self.assertNotIn(r'\begin{itemize}', source)
        self.assertRegex(source, r'联合(?:优化|选择)[^。；\n]{0,30}门位[^。；\n]{0,15}驻留[^。；\n]{0,15}回迁存储位')
        self.assertRegex(source, r'候选[^。；\n]{0,12}执行后[^。；\n]{0,8}配置')
        self.assertRegex(source, r'(?:预测|评价)[^。；\n]{0,20}多层物理损失')

if __name__ == '__main__': unittest.main()
