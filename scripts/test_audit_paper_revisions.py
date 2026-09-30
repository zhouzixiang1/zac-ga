import unittest
from pathlib import Path
import tempfile

from audit_paper_revisions import compare, marked_content, visible_scope, apply_figure_policy, audit, artifact_path


class ReviewAuditTest(unittest.TestCase):
    def test_retired_table_is_recorded_as_removal_with_preserved_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, baseline = (Path(temporary) / name for name in ("paper", "baseline"))
            for tree in (root, baseline):
                tree.mkdir()
                (tree / "paper_zh.tex").write_text("unchanged")
            relative = Path("sections/05_circuit_table.tex")
            old = baseline / relative
            old.parent.mkdir()
            content = r"\begin{table}\caption{Archived cases}old values\end{table}"
            old.write_text(content)
            archived = artifact_path(root, relative)
            archived.parent.mkdir(parents=True)
            archived.write_text(content)
            result = audit(root, baseline)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(len(result["files"]), 1)
            item = result["files"][0]
            self.assertTrue(item["retired_from_manuscript"])
            self.assertEqual(item["archived_path"], str(archived))
            self.assertEqual(item["archived_sha256"], item["before_sha256"])
            self.assertEqual(item["edits"][0]["classification"], "deletion")

    def test_normal_colour_exemption_is_explicit_and_limited_to_figures(self):
        for name, enabled, exempt in (("figures/x.tex", True, True),
                                      ("figures/x.tex", False, False),
                                      ("sections/03_method.tex", True, False)):
            result = apply_figure_policy(compare("old", "new"), name, enabled)
            self.assertEqual(bool(result["unmarked_changes"]), not exempt)
            self.assertEqual(bool(result["normal_colour_figure_changes"]), exempt)
            self.assertEqual(result["revision_colour_required"], not exempt)

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

    def test_geometry_changes_are_separate_from_visible_labels(self):
        a = visible_scope(r"\begin{tikzpicture}\node at (0,0) {Old};\end{tikzpicture}", "figures/x.tex")
        b = visible_scope(r"\begin{tikzpicture}\node at (2,3) {Old};\end{tikzpicture}", "figures/x.tex")
        self.assertFalse(compare(a, b)["edits"])
        self.assertTrue(compare(a, b.replace("Old", "New"))["unmarked_changes"])

    def test_new_numeric_macro_is_visible_even_without_literal_digits(self):
        self.assertTrue(compare(r"\OldValue", r"\NewValue")["unmarked_changes"])

    def test_unchanged_long_block_move_and_deletion_are_recorded(self):
        before = "第一段内容保留且移动。第二段内容保持原样。"
        result = compare(before, "第二段内容保持原样。第一段内容保留且移动。")
        self.assertFalse(result["unmarked_changes"])
        self.assertIn("move", {edit["classification"] for edit in result["edits"]})
        self.assertIn("deletion", {edit["classification"] for edit in result["edits"]})

    def test_alias_setup_is_not_visible_but_table_content_is(self):
        name = "sections/05_circuit_table.tex"
        source = r"\ExplSyntaxOn alias definition\ExplSyntaxOff\begin{table}body\end{table}"
        self.assertEqual(visible_scope(source, name), "body")

    def test_moved_float_driver_does_not_hide_unmarked_cells(self):
        name = "sections/05_evaluation.tex"
        table = (r"\begin{table*}[!t]\caption{\PaperRevision{参数}}"
                 r"\PaperTableSetup\begin{tabular*}{\textwidth}{@{\extracolsep{\fill}}rr@{}}"
                 r"\PaperRevision{次数} & \PaperRevision{32}\\\end{tabular*}"
                 r"\PaperTableNote[\textwidth]{\PaperRevision{说明}}\end{table*}")
        self.assertFalse(compare('', visible_scope(table, name))['unmarked_changes'])
        self.assertTrue(compare('', visible_scope(table.replace(r'\PaperRevision{32}', '64'), name))['unmarked_changes'])


if __name__ == "__main__":
    unittest.main()
