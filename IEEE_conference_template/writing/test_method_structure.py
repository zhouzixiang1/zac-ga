"""Structure and terminology regressions for the chronological method revision."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import verify_paper_zh as verifier


class MethodStructureTests(unittest.TestCase):
    def test_overview_defines_compilation_task_and_explains_pipeline(self):
        method = (ROOT / "sections/03_method.tex").read_text()
        overview = method.split(r"\subsection{门调度}", 1)[0]
        self.assertRegex(overview, r"输入(?:为)?逻辑电路")
        for wording in ("输出为", "可执行指令序列",
                        "结合遗传算法与多层前瞻", "五个阶段",
                        "匹配补全回迁存储位", "路由规则既用于候选评价"):
            self.assertIn(wording, overview)
        self.assertIn(r"\ref{fig:overall-framework}(b)--(f)", overview)
        self.assertNotIn("遗传搜索联合确定门位、驻留和回迁位置", overview)
        self.assertIn("框内仅列指令类型示意", overview)

    def test_horizon_explanation_links_to_existing_quantitative_comparison(self):
        method = (ROOT / "sections/03_method.tex").read_text()
        overview = method.split(r"\subsection{门调度}", 1)[0]
        evaluation = (ROOT / "sections/05_evaluation.tex").read_text()
        self.assertNotIn("例如，$H=8$", overview)
        self.assertIn(r"\ref{sec:horizon-evaluation}", overview)
        self.assertIn(r"\label{sec:horizon-evaluation}", evaluation)
        for macro in (r"\HorizonExtZeroFidelityRatio", r"\HorizonExtOneFidelityRatio"):
            self.assertIn(macro, evaluation)
        sensitivity = (ROOT / "sections/05_sensitivity_table.tex").read_text()
        for word in ("Zero", "One", "Two", "Four", "Eight"):
            for metric in ("FidelityRatio", "BatchRatio"):
                self.assertIn(r"\HorizonExt" + word + metric, sensitivity)
        self.assertIn("按电路计算后取几何均值", sensitivity)
        self.assertIn("中位数", sensitivity)
        self.assertIn("共同集合", sensitivity)
        self.assertNotIn("编译时间比", sensitivity)

    def test_chinese_interaction_and_decision_terms_are_explicit(self):
        paths = [ROOT / "paper_zh.tex", *sorted((ROOT / "sections").glob("*.tex"))]
        for path in paths:
            source = path.read_text()
            for wording in ("下一交互伙伴", "下一伙伴", "未来伙伴", "交互伙伴",
                            "联合优化三类决策"):
                self.assertNotIn(wording, source, str(path))

    def test_chinese_keeps_only_two_substantive_contributions(self):
        source = (ROOT / "sections/02_background.tex").read_text()
        contributions = source.split(r"\label{sec:contributions}", 1)[1]
        self.assertEqual(contributions.count(r"\item"), 2)
        self.assertIn("保真度损失最小化", contributions)
        self.assertIn("多层执行代价", contributions)
        self.assertNotIn("将可执行重排的构造纳入", contributions)

    def test_initialization_is_a_method_stage_with_ablation_evidence(self):
        method = (ROOT / "sections/03_method.tex").read_text()
        evaluation = (ROOT / "sections/05_evaluation.tex").read_text()
        self.assertIn(r"GA-LK选择$J_{\rm init}$最小的可行布局", method)
        self.assertNotIn("默认启用初始化前瞻", method)
        self.assertNotIn("默认初始化采用", evaluation)
        self.assertIn("至多四个候选", evaluation)
        self.assertIn("编码评价预算为32", evaluation)
        self.assertIn("消融实验评估", method)
        self.assertNotIn("初始化前瞻为可选环节", method)
        self.assertIn(r"\ref{sec:init-evaluation}", method)
        self.assertIn(r"\label{sec:init-evaluation}", evaluation)
        self.assertIn("前瞻初始化布局", evaluation)

    def test_one_qubit_locations_follow_execution_mapping(self):
        method = (ROOT / "sections/03_method.tex").read_text()
        self.assertIn("根据该时刻的原子映射", method)
        self.assertIn("也可能位于纠缠区", method)
        self.assertNotIn("单比特门在存储区执行，不占用纠缠位", method)

    def test_current_cost_increments_are_explicitly_defined(self):
        source = (ROOT / "sections/03_method.tex").read_text()
        self.assertIn("本次转换增加的阱转移次数和空闲受激次数", source)
        self.assertNotIn(r"\mathcal D_\ell", source)

    def test_chronological_sections_and_no_implementation_appendix(self):
        source = (ROOT / "sections/03_method.tex").read_text()
        self.assertEqual(re.findall(r"\\subsection\{([^}]+)\}", source), [
            "总体框架", "门调度", "前瞻感知的初始化布局", "层内联合优化",
            "路由调度", "指令生成与输出"])
        self.assertEqual(len(re.findall(r"\\subsubsection\{", source)), 5)
        self.assertLessEqual(source.count(r"\begin{equation}"), 7)

    def test_legacy_event_codes_are_distinct_from_zair_or_plain_english(self):
        pattern = verifier.FORBIDDEN_PDF_TONE_PATTERNS["implementation_event_code"]
        for token in ("LOAD", "MOVE", "STORE", "STAY", "RETURN", "RESEAT"):
            self.assertIsNotNone(pattern.search(token))
        for text in ("move {row_id, row_y_begin, row_y_end}",
                     "Return to storage", "return selected candidate"):
            self.assertIsNone(pattern.search(text))

    def test_both_languages_keep_initial_prefix_and_zero_horizon_rules(self):
        for directory in ("sections", "sections_en"):
            method = (ROOT / directory / "03_method.tex").read_text()
            self.assertIn(r"\label{eq:initial-lookahead}", method)
            self.assertIn(r"\input{figures/zair_output}", method)
        self.assertIn(r"H_{\rm init}=0", (ROOT / "sections/05_evaluation.tex").read_text())
        self.assertIn(r"当$H=0$时不启用", (ROOT / "sections/03_method.tex").read_text())
        self.assertIn("disabled at zero horizon", (ROOT / "sections_en/03_method.tex").read_text())

    def test_lookahead_limits_have_plain_definition_and_actual_counts(self):
        method = (ROOT / "sections/03_method.tex").read_text()
        overview = method.split(r"\subsection{门调度}", 1)[0]
        self.assertIn("前瞻层数上限$H$", overview)
        self.assertIn("不包含当前层", overview)
        self.assertIn(r"0\le h_\ell\le H", method)
        self.assertIn(r"0\le h_{\rm init}\le H_{\rm init}", method)
        self.assertIn("首层及后续两层", (ROOT / "sections/05_evaluation.tex").read_text())
        self.assertIn(r"实际预测层数$h_\ell=0$，约定未来层求和项和末端项均为零", method)
        for path in (ROOT / "sections").glob("*.tex"):
            source = path.read_text()
            self.assertNotIn("视界", source, str(path))
            self.assertNotIn(r"H_{\max}", source, str(path))
            self.assertNotIn(r"H_{\rm cap}", source, str(path))

    def test_full_configurations_and_single_traps_are_distinguished(self):
        for relative in ("sections/03_method.tex", "figures/overall_framework.tex",
                         "figures/joint_ga.tex", "figures/physical_lookahead.tex"):
            source = (ROOT / relative).read_text()
            self.assertIn(r"\mathbf{s}_{\ell+1}", source, relative)
            self.assertNotIn(r"s_{\ell+1}", source, relative)
            self.assertNotIn(r"s_\ell", source, relative)
        self.assertIn("$s_1$", (ROOT / "sections/02_background.tex").read_text())
        joint = (ROOT / "figures/joint_ga.tex").read_text()
        self.assertIn("After staging", joint)
        self.assertNotIn(r"\pi^{\rm sto}", joint)

    def test_named_budget_columns_preserve_solver_limits(self):
        source = (ROOT / "sections/03_method.tex").read_text()
        for header in (r"评价\\预算", r"种群\\规模", r"迭代\\代数", r"分配数\\$K$"):
            self.assertIn(header, source)
        self.assertNotIn("$(E,P,I,K)$", source)
        self.assertNotIn(r"N_{\rm bnd}", source)
        for row in ("默认配置 & 512 & 576 & 8 & 6 & 4 & 8",
                    r"边界数$>512$ & 64 & 192 & $\le6$ & $\le4$ & $\le2$ & $\le4$",
                    r"量子比特数$\le16$} & 16 & 32 & $\le4$ & $\le1$ & 1 & --",
                    r"边界数$\ge5000$ & -- & -- & -- & -- & -- & $\le2$"):
            self.assertIn(row, source)

    def test_one_use_parameters_do_not_add_unexplained_symbols(self):
        method = (ROOT / "sections/03_method.tex").read_text()
        self.assertIn("预计未来节省的四分之一", method)
        self.assertNotIn(r"\eta", method)
        self.assertIn("正负上标表示两原子的放置方向", method)
        contributions = (ROOT / "sections/02_background.tex").read_text().split(
            r"\label{sec:contributions}", 1)[1]
        self.assertNotIn("$K$", contributions)
        table = (ROOT / "sections/05_circuit_table.tex").read_text()
        self.assertIn("$n/N_2$", table)
        self.assertNotIn("g_2", table)
        sensitivity = (ROOT / "sections/05_sensitivity_table.tex").read_text()
        self.assertIn("保真度比 & 重排批次比", sensitivity)

    def test_result_subsections_follow_the_two_contributions(self):
        evaluation = (ROOT / "sections/05_evaluation.tex").read_text()
        headings = re.findall(r"\\subsection\{([^}]+)\}", evaluation)
        self.assertEqual(headings, ["实验设置", "总体编译质量", "联合优化中的遗传搜索",
                                    "多层前瞻", "参数选择与编译开销"])
        sections = dict(zip(headings, re.split(r"\\subsection\{[^}]+\}", evaluation)[1:]))
        genetic, future = sections["联合优化中的遗传搜索"], sections["多层前瞻"]
        self.assertIn(r"\AblationGARatio", genetic)
        self.assertIn(r"\AblationGAN", genetic)
        for shared in ("联合决策变量", "物理约束", "目标函数", "评价预算"):
            self.assertIn(shared, genetic)
        self.assertIn(r"\AblationHRatio", future)
        self.assertIn(r"\AblationHN", future)
        self.assertIn(r"\label{sec:init-evaluation}", future)
        self.assertIn(r"\InitExtFidelityGainRounded", future)
        self.assertNotIn(r"\AblationHRatio", genetic)
        self.assertNotIn(r"\InitExtFidelityGainRounded", sections["总体编译质量"])

    def test_model_example_is_limited_to_one_atom_and_two_factors(self):
        method = (ROOT / "sections/03_method.tex").read_text()
        example = method.split(r"\subsubsection{驻留与回迁存储位}", 1)[1].split(
            r"\subsubsection{当前损失与多层前瞻}", 1)[0]
        for token in ("模型算例", "仅比较该原子的转移与受激因子",
                      r"f_{\rm exc}^{r}", r"f_{\rm tran}^{4}",
                      r"\ArgumentStayOneFactor", r"\ArgumentStayTwoFactor",
                      r"\ArgumentRoundtripFactor", "相干损失及其他原子"):
            self.assertIn(token, example)
        self.assertRegex(method, r"模型分量算例.{0,12}不是完整候选得分")
        self.assertNotIn(r"J_{\rm stay}", example)
        self.assertNotIn(r"J_{\rm roundtrip}", example)

    def test_experiment_protocol_retains_distinct_sampling_and_timeout_scopes(self):
        protocol = (ROOT / "writing/experiment_protocol.md").read_text()
        evaluation = (ROOT / "sections/05_evaluation.tex").read_text()
        self.assertRegex(evaluation, r"代码仓库.{0,8}实验协议")
        for token in ("基线的主结果不是三种子结果", "完整 36 电路均值",
                      "共 5 个任务超时", "不单独分离其中每个组成项",
                      "不是联合决策与顺序决策的对照"):
            self.assertIn(token, protocol)
        sensitivity = (ROOT / "sections/05_sensitivity_table.tex").read_text()
        self.assertIn(r"\caption{前瞻层数上限与编译质量}", sensitivity)
        self.assertNotIn("单因素", sensitivity)
        for macro in (r"\HorizonExtTargetN", r"\HorizonExtCompleteN", r"\HorizonExtSeedN"):
            self.assertIn(macro, sensitivity)
        self.assertIn(r"\input{horizon_extension_values}", (ROOT / "paper_zh.tex").read_text())

    def test_conclusion_is_single_section_without_early_balancing(self):
        for directory in ("sections", "sections_en"):
            ending = "\n".join((ROOT / directory / name).read_text() for name in
                               ("06_related_work.tex", "07_conclusion.tex"))
            self.assertEqual(ending.count(r"\section{"), 1)
            self.assertNotIn(r"\balance", ending)
            self.assertIn(r"\label{sec:discussion}", ending)
            self.assertIn(r"\label{sec:conclusion}", ending)

    def test_no_reference_number_specific_page_break(self):
        for entry in ("paper_zh.tex", "paper_en.tex"):
            self.assertNotIn(r"\IEEEtriggeratref", (ROOT / entry).read_text())


if __name__ == "__main__":
    unittest.main()
