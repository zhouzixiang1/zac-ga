"""Main-loop algorithm and its separately stated selection rules."""
import re
import unittest

from paper_paths import PAPER_ROOT
import verify_paper_zh as verifier

POSTSTATE = r"\mathbf{s}_{\ell+1}"
CURRENT_LOSS = r"C_\ell^{\rm cur}"
SCORE = r"J_\ell"


def has(text, *patterns):
    return all(re.search(pattern, text, re.I | re.S) for pattern in patterns)


def algorithm_checks(text):
    text = verifier._strip_tex_comments(text)
    steps = list(re.finditer(r"\\State\b(.*?)(?=\\(?:State|For|EndFor|While|EndWhile|If|ElsIf|Else|EndIf|PaperAlgorithmPhase)\b|\\end\{algorithmic\}|$)", text, re.S))
    step_with = lambda token: next((step for step in steps if token in step[1]), None)
    loop = re.search(r"\\While(.*?)\\EndWhile", text, re.S)
    candidates = re.search(r"\\For(.*?)\\EndFor", text, re.S)
    route, current, future = (step_with(token) for token in (
        r"\label{alg:route}", CURRENT_LOSS, r"\label{alg:predict}"))
    rollout = next((step for step in steps if has(step[1], r"多层前瞻|Look-ahead", re.escape(POSTSTATE))), None)
    selection = re.search(r"\\PaperAlgorithmPhase\{II\}", text)
    refinement = text[loop.end():selection.start()] if loop and selection else ""
    commit = text[selection.end():] if selection else ""
    matching = text[loop.start():candidates.start()] if loop and candidates else ""
    return {
        "matching_then_complete_candidates": bool(loop and candidates
            and loop.start() < candidates.start() < candidates.end() < loop.end()
            and has(matching, r"匹配|match", re.escape("$K$"))
            and has(candidates[1], re.escape(r"\mathbf x_\ell"), re.escape(r"a_\ell"))),
        "route_current_then_future": bool(candidates and route and current and rollout and future
            and candidates.start() < route.start() < current.start() < rollout.start() < future.start() < candidates.end()
            and POSTSTATE in current[1] and POSTSTATE in rollout[1] and SCORE in future[1]),
        "current_and_prediction_feasibility_separate": bool(candidates and route and future
            and not has(candidates[1].split(r"\State", 1)[0], r"当前可执行|current-executable")
            and has(route[1], r"不可行则跳过|skip if infeasible")
            and has(future[1], r"预测可行性|prediction feasibility")),
        "reevaluate_outside_search": has(refinement, r"局部|local", r"重新评价|reevaluat",
            re.escape(r"\ref{alg:route}"), re.escape(r"\ref{alg:predict}")),
        "commit_current_layer": has(commit, r"提交当前层批次|Commit current batches",
            r"当前层批次|current batches", r"\\Return", re.escape(POSTSTATE)),
    }


def selection_checks(method):
    """Selection remains reproducible after its branches leave the guide box."""
    section = re.split(r"\\subsection\{(?:遗传搜索与初始布局|Genetic Search and Initial Placement)\}", method)[-1]
    return {
        "explicit_current_failure": has(section,
            r"不存在当前可执行候选[^。\n]*报告失败|absence of any current-executable candidate reports failure"),
        "prediction_failure_uses_current_cost": has(section,
            r"所有未来预测均不可行[^。\n]*按当前可执行代价选择|all predictions are infeasible[^.\n]*executable current cost"),
        "same_group_reference": has(section,
            r"违反这些建议最少的一组[^。]*以该组当前损失最小者为参照[^。]*仅在同组内|Only the group with the fewest violations enters admission and ranking[^.]*minimum-current-loss candidate as the reference"),
        "quarter_guard_and_reference": has(section,
            r"不超过加权未来节省的四分之一|not exceed one quarter of weighted future savings",
            r"\$J_\\ell\$不升|\$J_\\ell\$ must not increase", r"参照|reference", r"同分|tie"),
    }


class AlgorithmStructureTests(unittest.TestCase):
    def source(self, folder):
        return (PAPER_ROOT / folder / "03_algorithm.tex").read_text()

    def test_bilingual_algorithm_inputs_and_control_flow(self):
        for folder in ("sections", "sections_en"):
            method = (PAPER_ROOT / folder / "03_method.tex").read_text()
            self.assertEqual(method.count(r"\input{" + folder + "/03_algorithm}"), 1)
            self.assertIn(r"\ref{alg:solver}", method)
            text = self.source(folder)
            self.assertEqual(text.count(r"\label{alg:solver}"), 1)
            checks = algorithm_checks(text)
            self.assertTrue(all(checks.values()), (folder, checks))
            self.assertTrue(all(selection_checks(method).values()))

    def test_fitness_step_uses_the_full_method_definition(self):
        patterns = {
            "sections": r"适应度[^。；\n]*执行与预测均可行[^。；\n]*最小\$J_\\ell\$",
            "sections_en": (r"fitness[^.\n]*(?:smallest|minimum) \$J_\\ell\$[^.\n]*"
                            r"(?:assignments feasible for both execution and prediction|execution- and prediction-feasible assignments)"),
        }
        for folder, pattern in patterns.items():
            self.assertRegex(self.source(folder), r"适应度|fitness")
            method = (PAPER_ROOT / folder / "03_method.tex").read_text()
            self.assertRegex(method, pattern)
            wrong = method.replace("执行与预测均可行", "仅执行可行").replace(
                "feasible for both execution and prediction", "feasible for execution only").replace(
                "execution- and prediction-feasible", "execution-feasible")
            self.assertNotRegex(wrong, pattern)

    def test_failure_guard_and_commit_cannot_change_meaning(self):
        mutations = {
            "sections": (("不存在当前可执行候选", "不存在预测可行候选", "explicit_current_failure"),
                         ("所有未来预测", "所有当前执行", "prediction_failure_uses_current_cost"),
                         ("四分之一", "二分之一", "quarter_guard_and_reference"),
                         ("不升", "上升", "quarter_guard_and_reference"),
                         ("仅在同组内", "跨组", "same_group_reference")),
            "sections_en": (("any current-executable", "any prediction-feasible", "explicit_current_failure"),
                            ("all predictions", "all current plans", "prediction_failure_uses_current_cost"),
                            ("one quarter", "one half", "quarter_guard_and_reference"),
                            ("must not increase", "may increase", "quarter_guard_and_reference"),
                            ("Only the group with the fewest violations", "All groups", "same_group_reference")),
        }
        for folder, cases in mutations.items():
            text = (PAPER_ROOT / folder / "03_method.tex").read_text()
            for old, new, key in cases:
                with self.subTest(folder=folder, boundary=key):
                    self.assertIn(old, text)
                    self.assertFalse(selection_checks(text.replace(old, new))[key])
            algorithm = self.source(folder)
            old, new = (("提交当前层批次", "提交全部预测层批次") if folder == "sections" else
                        ("Commit current batches", "Commit all predicted batches"))
            self.assertIn(old, algorithm)
            self.assertFalse(algorithm_checks(algorithm.replace(old, new))["commit_current_layer"])

    def test_post_search_reevaluation_cannot_move_inside_loop(self):
        for folder in ("sections", "sections_en"):
            text = self.source(folder)
            match = re.search(r"(?<=\\EndWhile)(.*?)(?=\\PaperAlgorithmPhase|\\If)", text, re.S)
            self.assertIsNotNone(match)
            changed = text[:match.start()] + text[match.end():]
            changed = changed.replace(r"\EndWhile", match[0] + "\n" + r"\EndWhile")
            self.assertFalse(algorithm_checks(changed)["reevaluate_outside_search"])


if __name__ == "__main__":
    unittest.main()
