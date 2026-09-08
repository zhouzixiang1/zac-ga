"""Regressions for the two-class architecture explanation and Fig. 1."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def visible_source(path):
    return re.sub(r"(?<!\\)%[^\n]*", "", path.read_text(encoding="utf-8"))


class ArchitectureBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.zh = visible_source(ROOT / "sections/02_background.tex")
        self.en = visible_source(ROOT / "sections_en/02_background.tex")
        self.figure = visible_source(ROOT / "figures/architecture_preliminaries.tex")

    def test_two_classes_keep_both_order_and_membership_examples(self):
        self.assertIn("两类约束", self.zh)
        self.assertNotIn("三类结构约束", self.zh)
        self.assertIn("two constraint classes", self.en)
        self.assertNotIn("Three structural constraints", self.en)
        for label in ("Row/column relation constraint", "Unintended-intersection constraint",
                      "Order reversal", "Row merging"):
            self.assertIn(label, self.figure)
        self.assertNotIn("(d)", self.figure)

    def test_relation_equation_preserves_all_three_relations_on_both_axes(self):
        for source in (self.zh, self.en):
            self.assertIn(r"\star\in\{<,=,>\}", source)
            self.assertIn(r"\label{eq:aod-relations}", source)
            for axis in "xy":
                self.assertIn(
                    rf"{axis}_i\star {axis}_j&\ \Longleftrightarrow\ {axis}'_i\star {axis}'_j",
                    source,
                )
        self.assertRegex(self.figure, r"y_0<y_1\\;\\(?:longrightarrow|to)\\;y'_0=y'_1")

    def test_aod_definition_is_in_background_not_introduction(self):
        self.assertIn("Acousto-Optic Deflector, AOD", self.zh)
        self.assertIn("acousto-optic deflector (AOD)", self.en)
        for directory in ("sections", "sections_en"):
            intro = visible_source(ROOT / directory / "01_introduction.tex")
            self.assertNotIn("AOD", intro)

    def test_wide_figure_has_consistent_bilingual_insertion(self):
        for source in (self.zh, self.en):
            match = re.search(
                r"\\begin\{figure\*\}.*?\\label\{fig:architecture-preliminaries\}"
                r"\s*\\end\{figure\*\}", source, re.S,
            )
            self.assertIsNotNone(match)
            self.assertIn(r"\resizebox{\textwidth}{!}", match.group())
        self.assertNotIn("atom in AOD", self.figure)

    def test_source_release_and_stationary_occupancy_are_distinct(self):
        self.assertIn("源阱的释放时刻", self.zh)
        self.assertIn("source-trap release", self.en)
        for source in (self.zh, self.en):
            self.assertIn(r"fig:architecture-preliminaries}(c)", source)
            for token in (r"$q_0$", r"$q_2$", r"$s$"):
                self.assertIn(token, source)
        self.assertIn("腾空", self.zh)
        self.assertIn("vacated", self.en)
        self.assertIn("对调这两批的次序不能释放", self.zh)
        self.assertIn("Reordering these two batches cannot release", self.en)
        self.assertNotIn("$q_a$", self.zh)
        self.assertNotIn("$q_b$", self.zh)

    def test_constraint_panels_have_a_dashed_divider(self):
        self.assertRegex(self.figure, r"constraint divider/\.style=\{[^}]*dash pattern")
        self.assertIn(r"\draw[constraint divider]", self.figure)

    def test_motivation_layer_labels_share_the_top_alignment(self):
        motivation = visible_source(ROOT / "figures/baseline_motivation.tex")
        labels = re.findall(
            r"\\node\[anchor=(south|north)\] at \(\\xx\+\.56,([\d.]+)\) \{\\lab\}",
            motivation,
        )
        self.assertEqual(len(labels), 3)
        self.assertEqual(len(set(labels)), 1)
        self.assertEqual(labels[0][0], "south")
        self.assertGreater(float(labels[0][1]), 4.38)


if __name__ == "__main__":
    unittest.main()
