"""Regressions for the architecture and physical model in the merged introduction."""

from paper_paths import PAPER_ROOT, PROJECT_ROOT, artifact_path, tool_path, notes_path
from pathlib import Path
import re
import importlib.util
import unittest


ROOT = PAPER_ROOT
_spec = importlib.util.spec_from_file_location(
    "background_figure_checks", tool_path(ROOT, "background_figure_checks.py"))
_background_checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_background_checks)
scene_checks = _background_checks.checks


def visible_source(path):
    return re.sub(r"(?<!\\)%[^\n]*", "", path.read_text(encoding="utf-8"))


class ArchitectureBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.zh = visible_source(ROOT / "sections/01_introduction.tex")
        self.en = visible_source(ROOT / "sections_en/01_introduction.tex")
        self.intro_zh = self.zh
        self.intro_en = self.en
        self.figure = visible_source(ROOT / "figures/architecture_preliminaries.tex")

    def mutate(self, source, old, new, *, occurrences=1):
        """A negative test must change the intended current drawing token."""
        self.assertEqual(source.count(old), occurrences, old)
        changed = source.replace(old, new)
        self.assertNotEqual(changed, source)
        return changed

    def test_two_classes_keep_both_order_and_membership_examples(self):
        self.assertIn("两类约束", self.zh)
        self.assertNotIn("三类结构约束", self.zh)
        self.assertRegex(self.en, r"two (?:constraints|constraint classes|classes of constraints)")
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

    def test_aod_and_slm_are_introduced_together_before_formal_constraints(self):
        for source, terms in (
            (self.intro_zh, ("空间光调制器（SLM）", "声光偏转器（AOD）", "静态阱", "移动阱")),
            (self.intro_en, ("spatial light modulators (SLMs)", "acousto-optic deflectors (AODs)",
                       "static traps", "movable traps")),
        ):
            definition_paragraphs = [paragraph for paragraph in re.split(r"\n\s*\n", source)
                                     if terms[0].lower() in paragraph.lower()]
            self.assertEqual(len(definition_paragraphs), 1)
            for term in terms:
                self.assertIn(term.lower(), definition_paragraphs[0].lower())
            self.assertLess(source.lower().index(terms[0].lower()),
                            source.index(r"\label{eq:aod-relations}"))
        architecture = self.intro_en
        self.assertIn("SLM-to-AOD transfer", architecture)
        self.assertIn("AOD-to-SLM transfer", architecture)

    def test_wide_figure_has_consistent_bilingual_insertion(self):
        for source in (self.intro_zh, self.intro_en):
            match = re.search(
                r"\\begin\{figure\*\}.*?\\label\{fig:architecture-preliminaries\}"
                r"\s*\\end\{figure\*\}", source, re.S,
            )
            self.assertIsNotNone(match)
            self.assertIn(r"\resizebox{\textwidth}{!}", match.group())
        self.assertNotIn("atom in AOD", self.figure)

    def test_source_release_and_stationary_occupancy_are_distinct(self):
        self.assertIn("原子转入AOD时即释放源SLM阱", self.zh)
        self.assertIn("transferring an atom into an AOD frees its source SLM trap", self.en)
        self.assertIn("静止原子的阱位则不能通过交换批次次序释放", self.zh)
        self.assertIn("reordering batches cannot free a stationary atom's trap", self.en)
        for source, introduction in ((self.zh, self.intro_zh), (self.en, self.intro_en)):
            self.assertIn(r"fig:architecture-preliminaries}(c)", source)
            for token in (r"$q_0$", r"$q_2$", r"$s$"):
                self.assertIn(token, introduction)

    def test_constraint_panels_have_a_dashed_divider(self):
        self.assertRegex(self.figure, r"constraint divider/\.style=\{[^}]*dash pattern")
        self.assertIn(r"\draw[constraint divider]", self.figure)

    def test_motivation_keeps_interaction_layers_and_transport_stages_distinct(self):
        motivation = visible_source(artifact_path(ROOT, "figures/baseline_motivation.tex"))
        timeline, rest = motivation.split("(b) Inter-layer residency", 1)
        residency, transport = rest.split("(c) Storage-site dependence", 1)
        for gate in ("q,p", "u,v", "w,x", "q,r"):
            self.assertEqual(timeline.count(r"\mathrm{CZ}(" + gate + ")"), 1)
        self.assertIn("two intervening layers", timeline)
        for layer in (r"$L_{\ell}$", r"$L_{\ell+1}$", r"$L_{\ell+2}$", r"$L_{\ell+3}$"):
            self.assertEqual(timeline.count(layer), 1)
            self.assertEqual(residency.count(layer), 1)
        for token in (r"\miniDevice{\xx}{3.08}", r"\miniDevice{\xx}{.87}",
                      "Entanglement-resident", "Storage-mediated",
                      r"\foreach \xx in {7.22,8.53}", r"\draw[pulse]"):
            self.assertIn(token, residency)
        for label in ("Before return", "After return", "After transfer"):
            self.assertIn(label, transport)
        self.assertNotRegex(transport, r"L_\{\\ell(?:\+\d)?\}")
        self.assertNotIn(r"\input{figures/baseline_motivation}", self.zh)

    def test_current_scenes_pass_physical_checks(self):
        for name in ("architecture_preliminaries.tex", "baseline_motivation.tex"):
            results = scene_checks(name, visible_source(artifact_path(ROOT, Path("figures") / name)))
            self.assertTrue(results)
            self.assertEqual([key for key, value in results.items() if not value], [])

    def test_reversing_the_valid_column_order_is_rejected(self):
        broken = self.mutate(self.figure, "(v0t) at (8.97,5.73)", "(v0t) at (10.40,5.73)")
        self.assertFalse(scene_checks("architecture_preliminaries.tex", broken)[
            "aod_valid_preserves_both_relations"])

    def test_stationary_atom_cannot_be_moved_between_pickup_frames(self):
        broken = self.mutate(self.figure, r"\node[fixed] at (8.65,2.36)",
                             r"\node[fixed] at (8.75,2.36)", occurrences=2)
        self.assertFalse(scene_checks("architecture_preliminaries.tex", broken)[
            "pickup_shared_stationary_geometry"])

    def test_second_batch_cannot_reactivate_the_stationary_crossing(self):
        broken = self.mutate(self.figure, r"\draw[guide] (16.52,1.12)--(16.52,2.61)",
                             r"\draw[guide] (15.17,1.12)--(15.17,2.61)")
        self.assertFalse(scene_checks("architecture_preliminaries.tex", broken)[
            "pickup_ghost_and_serialized_beams_safe"])

    def test_initial_return_arrows_cannot_exchange_the_two_storage_choices(self):
        motivation = visible_source(artifact_path(ROOT, "figures/baseline_motivation.tex"))
        broken = self.mutate(motivation, "3.08/.50,.94/.20", "3.08/.20,.94/.50")
        self.assertFalse(scene_checks("baseline_motivation.tex", broken)[
            "storage_return_paths_match_states"])

    def test_blocked_transfer_cannot_precede_clearance(self):
        motivation = visible_source(artifact_path(ROOT, "figures/baseline_motivation.tex"))
        broken = self.mutate(motivation,
            "1/q/.56/.50/.56/.20,2/u/.18/.50/.94/.50",
            "1/u/.18/.50/.94/.50,2/q/.56/.50/.56/.20")
        self.assertFalse(scene_checks("baseline_motivation.tex", broken)[
            "storage_clearance_precedes_passage"])

    def test_direct_route_cannot_leave_q_on_u_path(self):
        motivation = visible_source(artifact_path(ROOT, "figures/baseline_motivation.tex"))
        broken = self.mutate(motivation, r"\def\storageClear{q/.56/.20,u/.18/.50}",
                             r"\def\storageClear{q/.56/.50,u/.18/.50}")
        result = scene_checks("baseline_motivation.tex", broken)
        self.assertFalse(result["storage_direct_move_is_clear"])
        self.assertFalse(result["storage_path_obstruction"])

    def test_drawn_final_state_cannot_ignore_replayed_data(self):
        motivation = visible_source(artifact_path(ROOT, "figures/baseline_motivation.tex"))
        broken = self.mutate(motivation, r"\storageAtoms{16.20}{3.08}{\storageFinal}",
                             r"\storageAtoms{16.20}{3.08}{\storageBlocked}")
        self.assertFalse(scene_checks("baseline_motivation.tex", broken)[
            "storage_states_drive_drawing"])

    def test_row_merging_cannot_be_replaced_by_preserved_distinct_rows(self):
        broken = self.mutate(self.figure, "(m1t) at (16.70,5.73)",
                             "(m1t) at (16.70,6.14)")
        self.assertFalse(scene_checks("architecture_preliminaries.tex", broken)[
            "aod_merging_invalid_without_path_crossing"])

    def test_return_arrow_must_reach_the_declared_storage_trap(self):
        motivation = visible_source(artifact_path(ROOT, "figures/baseline_motivation.tex"))
        broken = self.mutate(motivation, r"(12.04,\yy+\target)",
                             r"(12.42,\yy+\target)")
        self.assertFalse(scene_checks("baseline_motivation.tex", broken)[
            "storage_return_paths_match_states"])

    def test_drawn_move_endpoint_cannot_diverge_from_replay(self):
        motivation = visible_source(artifact_path(ROOT, "figures/baseline_motivation.tex"))
        broken = self.mutate(motivation, r"(#1+\sx,#2+\sy)--(#1+\tx,#2+\ty)",
                             r"(#1+\sx,#2+\sy)--(#1+\tx,#2+\sy)")
        self.assertFalse(scene_checks("baseline_motivation.tex", broken)[
            "storage_moves_drive_drawing"])


if __name__ == "__main__":
    unittest.main()
