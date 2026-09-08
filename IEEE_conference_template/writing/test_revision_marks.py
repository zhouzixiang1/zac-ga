"""Review markup is shared with standalone figures, not the English draft."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RevisionMarksTest(unittest.TestCase):
    def test_review_is_on_and_has_one_clean_preview_override(self):
        source = (ROOT / "revision_marks.tex").read_text()
        self.assertIn(r"\PaperShowRevisionstrue", source)
        self.assertIn(r"\ifdefined\PaperHideRevisions\PaperShowRevisionsfalse\fi", source)
        self.assertIn(r"\DeclareRobustCommand{\PaperRevision}", source)
        self.assertIn(r"\textcolor{PaperRevisionRed}{#1}\else#1", source)

    def test_main_and_standalone_use_same_review_setting(self):
        for name in ("paper_zh.tex", "figures/overall_framework_standalone.tex"):
            self.assertIn(r"\input{revision_marks}", (ROOT / name).read_text(), name)
        self.assertNotIn(r"\input{revision_marks}", (ROOT / "paper_en.tex").read_text())

    def test_changed_figure_label_is_marked_without_recoloring_atom_identity(self):
        source = (ROOT / "figures/overall_framework.tex").read_text()
        self.assertIn(r"\PaperRevision{Look-ahead}\\\PaperRevision{initialization}", source)
        self.assertNotIn(r"Look-ahead\\option", source)
        style = (ROOT / "figures/tikz_style.tex").read_text()
        self.assertNotIn("PaperRevisionRed", style)

    def test_revised_method_and_contributions_are_marked(self):
        method = (ROOT / "sections/03_method.tex").read_text()
        for opening in (r"GA-LK选择$J_{\rm init}$最小的可行布局", "单比特门按原子上的门依赖安排",
                        r"第~\ref{sec:horizon-evaluation}节比较"):
            self.assertIn(r"\PaperRevision{" + opening, method)
        contributions = (ROOT / "sections/02_background.tex").read_text().split(
            r"\label{sec:contributions}", 1)[1]
        self.assertEqual(contributions.count(r"\item \PaperRevision{"), 2)


if __name__ == "__main__":
    unittest.main()
