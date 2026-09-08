import unittest

from audit_paper_revisions import compare, marked_content, visible_scope


class ReviewAuditTest(unittest.TestCase):
    def test_nested_math_is_red_and_unchanged_source_stays_unmarked(self):
        text, mask = marked_content(r"旧文\PaperRevision{新文$H_{\rm init}=2$}结尾")
        self.assertEqual(text, r"旧文新文$H_{\rm init}=2$结尾")
        self.assertEqual(mask[:2], [False, False])
        self.assertTrue(all(mask[2:-2]))
        self.assertEqual(mask[-2:], [False, False])

    def test_unmarked_changes_fail_but_red_replacement_passes(self):
        self.assertTrue(compare("旧文内容", "新文内容")["unmarked_changes"])
        self.assertFalse(compare("旧文内容", r"\PaperRevision{新文内容}")["unmarked_changes"])

    def test_removals_are_kept_in_audit(self):
        result = compare("方法不适用于本节。", r"\PaperRevision{方法适用于本节。}")
        self.assertFalse(result["unmarked_changes"])
        self.assertEqual(result["edits"][0]["before"], "不")

    def test_multiline_tikz_uses_separate_color_groups(self):
        result = compare(r"{Look-ahead\\option}",
                         r"{\PaperRevision{Look-ahead}\\\PaperRevision{initialization}}")
        self.assertFalse(result["unmarked_changes"])

    def test_preamble_and_comments_are_not_visible_revisions(self):
        old = visible_scope(r"preamble\begin{document}文本", "paper_zh.tex")
        new = visible_scope(r"new preamble\begin{document}文本% comment", "paper_zh.tex")
        self.assertEqual(compare(old, new)["edits"], [])

    def test_unbalanced_argument_fails(self):
        with self.assertRaisesRegex(ValueError, "unbalanced"):
            marked_content(r"\PaperRevision{not closed")

    def test_tikz_option_syntax_does_not_hide_unmarked_label_text(self):
        name = "figures/plot.tex"
        old = visible_scope(r"\begin{tikzpicture}\end{tikzpicture}", name)
        red = visible_scope(r"\begin{tikzpicture}nodes near coords={\PaperRevision{8.9}},\end{tikzpicture}", name)
        black = visible_scope(r"\begin{tikzpicture}nodes near coords={8.9},\end{tikzpicture}", name)
        self.assertFalse(compare(old, red)["unmarked_changes"])
        self.assertTrue(compare(old, black)["unmarked_changes"])

    def test_alias_setup_is_not_visible_but_table_content_is(self):
        name = "sections/05_circuit_table.tex"
        source = r"\ExplSyntaxOn alias definition\ExplSyntaxOff\begin{table}body\end{table}"
        self.assertEqual(visible_scope(source, name), r"\begin{table}body\end{table}")


if __name__ == "__main__":
    unittest.main()
