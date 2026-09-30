"""Compact-manuscript structure, physical boundaries and evidence correspondence."""
from paper_paths import PAPER_ROOT, artifact_path, notes_path
import re
import unittest
import verify_paper_zh as verifier
from test_algorithm_structure import algorithm_checks, selection_checks

ROOT = PAPER_ROOT


def source(relative):
    return verifier._strip_revision_markup(artifact_path(ROOT, relative).read_text())


def subsections(relative):
    text = source(relative)
    headings = re.findall(r"\\subsection\{([^}]+)\}", text)
    bodies = re.split(r"\\subsection\{[^}]+\}", text)[1:]
    return dict(zip(headings, bodies))


def boundary_checks(text, patterns):
    """Check short scientific relations, independent of surrounding prose."""
    return {name: bool(re.search(pattern, text)) for name, pattern in patterns.items()}


CONTROL_BOUNDARIES = {
    "fixed_initial_mapping": r"固定[^。；\n]{0,8}初始映射",
    "different_samples": r"(?:采用|使用|来自|基于)[^。；\n]{0,12}不同样本",
    "increments_not_additive": r"(?:不(?:将|把)增量相加|增量(?:不能|不可|不应)相加)",
    "search_within_joint_space": r"联合空间内[^。；\n]{0,8}搜索(?:差异|选择|方式)",
}

AGGREGATION_BOUNDARIES = {
    "all_participating_configs_compile": r"各参与配置[^。；\n]{0,6}均完成编译",
    "every_atom_below_t2": r"各原子[^。；\n]{0,6}空闲时间低于\$T_2\$",
    "three_valid_seeds": r"三个种子[^。；\n]{0,6}均有效",
    "per_circuit_seed_median": r"逐电路[^。；\n]{0,6}取中位数",
    "fidelity_geometric_mean": r"保真度[^。；\n]{0,6}采用几何均值",
    "batches_latency_arithmetic_mean": r"批次和重排时延[^。；\n]{0,6}采用算术均值",
}

TIMING_BOUNDARIES = {
    "serial_timing": r"独立串行计时",
    "timing_seed_zero": r"(?:固定|使用)[^。；\n]{0,25}种子0",
    "three_repeats_after_warmup": r"预热后[^。；\n]{0,6}重复三次",
    "entire_init_subtracted_each_run": r"逐次[^。；\n]{0,6}扣除完整初始化阶段",
    "nested_timing_medians": r"(?:再)?取电路内及电路间中位数",
    "noninitialization_scope": r"非初始化编译耗时[^。\n]*\\GAMainPostInitialMedianSeconds",
    "main_timing_separate": r"主(?:质量研究|实验)[^。\n]*最多14进程并行编译",
    "main_includes_initialization": r"包含GA初始化的完整编译耗时",
    "budget_runs_once": r"每配置[^。；\n]{0,6}运行一次",
    "budget_seed_zero": r"(?:固定|使用)[^。；\n]{0,6}种子0",
    "budget_four_concurrent": r"允许[^。；\n]{0,3}四路并行",
    "budget_times_observed_schedule": r"(?:时间比|所报降幅)[^。；\n]{0,8}(?:对应|反映)[^。；\n]{0,6}运行安排",
}


def timing_boundary_checks(text):
    paragraphs = re.split(r"\n\s*\n", text)
    serial = "\n".join(p for p in paragraphs if r"\GAMainFixedLayoutCircuitN" in p)
    budget = "\n".join(p for p in paragraphs if r"\SensitivityFidelityN" in p)
    main = "\n".join(p for p in paragraphs if "包含GA初始化" in p)
    return {name: boundary_checks(budget if name.startswith("budget_") else main if name.startswith("main_") else serial,
                                 {name: pattern})[name]
            for name, pattern in TIMING_BOUNDARIES.items()}


METHOD_BOUNDARIES = {
    "single_layer_participants": r"单门层[^。；\n]{0,20}(?:已在纠缠区的参与原子|区内参与原子)",
    "distance_constructs_candidates": r"距离(?:用于构造候选|(?:限制|缩小)候选(?:构造|范围|存储阱的范围))",
    "execution_selects_candidates": r"(?:按物理执行损失比较|最终选择依据其可执行批次)",
    "transit_no_residency_gene": r"(?:临时)?中转不增加驻留基因",
    "candidate_poststate": r"(?:候选的|从各自的)\$\\mathbf\{s\}_\{\\ell\+1\}\$",
    "reference_by_current_loss": r"以该组当前损失最小者为参照",
    "quarter_admission": r"增加量[^。；\n]{0,6}不超过加权未来节省的四分之一",
    "small_space_enumeration": r"小(?:规模)?(?:联合)?空间(?:直接)?枚举",
    "ga_above_threshold": r"超过阈值时(?:使用|以|采用)GA",
    "same_routing_for_evaluation_and_output": r"候选评价与最终指令共用路由(?:和时序)?规则",
}


def illustration_checks(text, language):
    paragraph = "\n".join(p for p in re.split(r"\n\s*\n", text)
                          if r"\ArgumentExcitationFidelity" in p)
    common = {name: re.escape(expression) for name, expression in {
        "stay_loss": r"-r\log f_{\rm exc}",
        "roundtrip_loss": r"-4\log f_{\rm tran}",
        "excitation_parameter": r"f_{\rm exc}=\ArgumentExcitationFidelity",
        "transfer_parameter": r"f_{\rm tran}=\ArgumentTransferFidelity",
    }.items()}
    language_patterns = {
        "zh": {
            "two_factor_scope": r"仅计转移与受激",
            "one_layer_favors_staying": r"\$r=1\$[^。；\n]{0,8}驻留损失较低",
            "two_layers_favor_return": r"\$r=2\$[^。；\n]{0,8}往返损失较低",
            "complete_objective_is_broader": r"实际放置[^。；\n]{0,8}占位、路由与相干",
        },
        "en": {
            "two_factor_scope": r"only transfer and excitation",
            "one_layer_favors_staying": r"staying costs less for \$r=1\$",
            "two_layers_favor_return": r"returning costs less for \$r=2\$",
            "complete_objective_is_broader": r"Actual placement[^.\n]{0,25}occupancy, routing, and decoherence",
        },
    }
    return boundary_checks(paragraph, common | language_patterns[language])


class MethodStructureTests(unittest.TestCase):
    def test_four_method_sections_follow_candidate_evaluation_search_execution(self):
        sections = subsections("sections/03_method.tex")
        self.assertEqual(list(sections), ["联合放置建模", "跨层物理损失评价", "遗传搜索与初始布局", "物理约束下的编译输出"])
        text = source("sections/03_method.tex")
        self.assertEqual(text.count(r"\input{sections/03_algorithm}"), 1)
        self.assertNotIn(r"\subsubsection", text)
        model_labels = ("eq:aod-relations", "eq:fidelity")
        method_labels = ("eq:chromosome", "eq:current-cost", "eq:lookahead-cost",
                         "eq:initial-lookahead", "eq:rearrangement-metrics")
        active_sections = [path.name for path in verifier._active_manuscript_files(ROOT)
                           if path.parent == ROOT / "sections"]
        for folder in ("sections", "sections_en"):
            introduction = source(f"{folder}/01_introduction.tex")
            method = source(f"{folder}/03_method.tex")
            equations = [r"\label{" + key + "}" for key in method_labels]
            self.assertEqual(sorted(method.index(key) for key in equations),
                             [method.index(key) for key in equations])
            self.assertEqual(len(re.findall(r"\\begin\{equation\}", method)), 5)
            for key in model_labels:
                label = r"\label{" + key + "}"
                self.assertIn(label, introduction)
                self.assertNotIn(label, method)
            self.assertLess(introduction.index(r"\label{eq:aod-relations}"),
                            introduction.index(r"\label{eq:fidelity}"))
            self.assertIn(r"\eqref{eq:fidelity}", method)
            algorithm = r"\input{" + folder + "/03_algorithm}"
            self.assertEqual(method.count(algorithm), 1)
            self.assertLess(method.index(r"\section{"), method.index(algorithm))
            self.assertLess(method.index(algorithm), method.index(r"\subsection{"))
            manuscript = "\n".join(source(f"{folder}/{name}") for name in active_sections)
            self.assertEqual(len(re.findall(r"\\begin\{equation\}", manuscript)), 7)
            for key in model_labels + method_labels:
                self.assertEqual(manuscript.count(r"\label{" + key + "}"), 1)

    def test_compact_float_contract_and_retired_visuals_are_explicit(self):
        text = "\n".join(path.read_text() for path in verifier._active_manuscript_files(ROOT))
        self.assertEqual(len(re.findall(r"\\begin\{figure\*?\}", text)), 4)
        self.assertEqual(len(re.findall(r"\\begin\{table\*?\}", text)), 1)
        self.assertEqual(len(re.findall(r"\\begin\{algorithm\*?\}", text)), 1)
        self.assertEqual(text.count(r"\label{alg:solver}"), 1)
        for label in ("architecture-preliminaries", "overall-framework", "joint-ga", "experimental-summary"):
            self.assertEqual(text.count(r"\label{fig:" + label + "}"), 1)
        self.assertEqual(text.count(r"\label{tab:main-results}"), 1)
        for name in ("baseline_motivation", "physical_lookahead", "zair_output"):
            self.assertNotRegex(text, r"\\input\{figures/" + name + r"(?:\.tex)?\}")
            self.assertTrue(artifact_path(ROOT, f"figures/{name}.tex").is_file())
        for language in ("sections", "sections_en"):
            self.assertTrue(artifact_path(ROOT, f"{language}/05_circuit_table.tex").is_file())
            self.assertTrue(artifact_path(ROOT, f"{language}/algorithm_precompact.tex").is_file())

    def test_task_input_output_and_joint_decisions_are_defined(self):
        text = source("sections/03_method.tex")
        for token in ("逻辑电路", "阱阵列", "原生门", "输运约束", "ZAIR", "门位", "驻留", "回迁存储位"):
            self.assertIn(token, text)
        self.assertIn(r"\ref{fig:overall-framework}", text)
        self.assertIn(r"\mathbf{s}_\ell", text)
        self.assertIn(r"\mathbf{s}_{\ell+1}", text)
        self.assertNotIn(r"s_{\ell+1}", text)

    def test_gate_scheduling_preserves_dependencies_capacity_and_single_qubit_addresses(self):
        text = source("sections/03_method.tex")
        for token in ("前驱均已完成", "同层不共享原子", "全局Rydberg脉冲", "CZ门", "超出门位容量时拆层",
                      "边着色", "单比特门", "无此类前驱时置于首层之前", "执行时的原子映射", "存储区或纠缠区"):
            self.assertIn(token, text)
        self.assertNotRegex(text, r"Rydberg脉冲[^。；\n]*单比特门")
        self.assertIn("单比特旋转由可寻址Raman光束执行", source("sections/01_introduction.tex"))

    def test_complete_candidate_retains_encoding_and_distinct_matching(self):
        text = source("sections/03_method.tex")
        for token in ("0为保留", "1为回迁", "单门层", "区内换位", "每个回迁原子最多保留六个位置",
                      "前$K$个", "每个匹配将回迁原子分配至互不重复的存储阱", "组成完整候选", r"$(\mathbf x_\ell,a_\ell)$"):
            self.assertIn(token, text)
        for name in ("single_layer_participants", "distance_constructs_candidates",
                     "execution_selects_candidates", "transit_no_residency_gene"):
            self.assertRegex(text, METHOD_BOUNDARIES[name])
        self.assertRegex(text, r"其(?:代价|开销)[^。\n]{0,12}计入[^。\n]{0,15}候选")

    def test_current_cost_uses_execution_increments_and_accumulated_clock(self):
        text = source("sections/03_method.tex")
        for token in (r"\Delta N_{\rm tran}", r"\Delta N_{\rm exc}", r"t_q^{-}", r"t_q^{+}", "累计空闲时间", "省略固定门保真度项", "累计值作差"):
            self.assertIn(token, text)
        self.assertIn(r"\log\frac{1-t_q^{-}/T_2}{1-t_q^{+}/T_2}", text)

    def test_prediction_continues_each_candidates_poststate_and_only_scores_future(self):
        text = source("sections/03_method.tex")
        self.assertRegex(text, METHOD_BOUNDARIES["candidate_poststate"])
        for token in ("逐层更新位置、占位和时间", "后续层继承该层执行后的配置",
                      "贪心选择门位", "尚未分配的合法门位", "有限中转和逐原子入区"):
            self.assertIn(token, text)
        self.assertRegex(text, r"(?:逐层(?:调用算法|优化)|以门层为优化单元|传入下一层优化)")
        checks = algorithm_checks(source("sections/03_algorithm.tex"))
        for name in ("route_current_then_future", "commit_current_layer"):
            self.assertTrue(checks[name], name)

    def test_prediction_cutoff_and_terminal_atom_scope_are_retained(self):
        text = source("sections/03_method.tex")
        for token in (r"0\le h_\ell\le H", "后续双比特层数上限", "后续层用尽", "规模相关的层数预算", r"\rho^{d-1}<\epsilon",
                      "$h_\ell=0$且未来层与末端项均取零", "未参与该层门操作的原子", "可行回迁损失"):
            self.assertIn(token, text)
        for term in (r"\alpha\rho^{d-1}\widehat C_{\ell,d}", r"\alpha\rho^{h_\ell-1}\Phi_\ell"):
            self.assertIn(term, text)

    def test_reference_and_quarter_admission_are_not_replaced_with_simple_minimum(self):
        text = source("sections/03_method.tex")
        for name in ("reference_by_current_loss", "quarter_admission"):
            self.assertRegex(text, METHOD_BOUNDARIES[name])
        for token in ("容量与强制回迁", "$H=0$时不启用驻留优先", "$J_\ell$不升",
                      "未来层及末端项", "批次数、重排时延、距离和固定编码次序", "没有更优者保留参照", "所有未来预测均不可行", "当前可执行代价"):
            self.assertIn(token, text)

    def test_search_precedes_final_reference_and_admission(self):
        text = source("sections/03_method.tex")
        for name in ("small_space_enumeration", "ga_above_threshold"):
            self.assertRegex(text, METHOD_BOUNDARIES[name])
        for pattern in (r"全驻留、全回迁[及和]物理贪心", r"执行与预测均可行分配的最小\$J_\\ell\$",
                        r"(?:相同编码不重复评价|编码缓存避免重复评价)", r"评价数或代数达到上限"):
            self.assertRegex(text, pattern)
        self.assertIn(r"\ref{alg:solver}", text)
        # The guide algorithm carries the main loop; prose gives final selection.
        checks = algorithm_checks(source("sections/03_algorithm.tex"))
        for name in ("current_and_prediction_feasibility_separate", "reevaluate_outside_search"):
            self.assertTrue(checks[name], name)
        self.assertTrue(all(selection_checks(text).values()))
        self.assertLess(text.index("有限局部调整并重新评价"), text.index("以该组当前损失最小者为参照"))
        self.assertLess(text.index(r"\label{eq:lookahead-cost}"), text.index("交叉组合父代"))

    def test_initial_ga_keeps_mapping_prefix_and_budget_boundaries(self):
        text = subsections("sections/03_method.tex")["遗传搜索与初始布局"]
        for token in ("数量等于原子数", "固定阱集合上的排列", "首层通过门位匹配及可行性修复", "关闭内层前瞻", "临时配置",
                      r"0\le h_{\rm init}\le H_{\rm init}", "单比特门及其时间", "仅到达真实末层时计入末端回迁", r"$H_{\rm init}=0$仍搜索首层损失",
                      "三个种子与扰动", "种子可读取全电路交互", "适应度仅评价早期前缀", "顺序交叉（OX）", "交换、插入、反转变异", "保留精英",
                      "唯一映射评价数、候选生成数、代数或停滞", "无可行映射则报告失败", "外层映射和内层编码分别设置预算"):
            self.assertIn(token, text)
        self.assertIn(r"\ref{sec:experimental-setup}", text)
        self.assertNotIn("模拟退火", text)

    def test_routing_and_output_preserve_constraints_and_real_metrics(self):
        text = subsections("sections/03_method.tex")["物理约束下的编译输出"]
        self.assertRegex(text, METHOD_BOUNDARIES["same_routing_for_evaluation_and_output"])
        for token in ("行列关系", "拾取、输运、释放时覆盖静止原子", "DSATUR", "源阱释放、目标占用和AOD容量",
                      "不满足整体约束时拆批", "单条轨迹扫过静止原子", "源阱释放后才可复用", "拾取、输运、释放及必要内部移动的总时长", "各批顺序执行",
                      "完整序列的门操作、占位和AOD约束", "从中统计模型保真度、批次和时延"):
            self.assertIn(token, text)
        self.assertIn(r"T_\ell^{\rm rr}=\sum_{b=1}^{B_\ell}T_{\ell,b}^{\rm batch}", text)

    def test_rephrased_method_boundaries_cannot_be_deleted(self):
        text = source("sections/03_method.tex")
        for name, pattern in METHOD_BOUNDARIES.items():
            with self.subTest(boundary=name):
                match = re.search(pattern, text)
                self.assertIsNotNone(match)
                changed = text[:match.start()] + text[match.end():]
                self.assertFalse(boundary_checks(changed, METHOD_BOUNDARIES)[name])

    def test_quarter_admission_cannot_reverse_or_relax_the_bound(self):
        text = source("sections/03_method.tex")
        for old, new in (("不超过加权未来节省", "不少于加权未来节省"),
                         ("加权未来节省的四分之一", "加权未来节省的二分之一")):
            self.assertIn(old, text)
            self.assertFalse(boundary_checks(text.replace(old, new), METHOD_BOUNDARIES)["quarter_admission"])

    def test_inline_two_factor_example_retains_scope_and_physics(self):
        for language, folder in (("zh", "sections"), ("en", "sections_en")):
            text = source(f"{folder}/01_introduction.tex")
            checks = illustration_checks(text, language)
            self.assertTrue(all(checks.values()), (language, checks))
            for old, new, key in ((r"-4\log f_{\rm tran}", r"-2\log f_{\rm tran}", "roundtrip_loss"),
                                  (r"-r\log f_{\rm exc}", r"-2r\log f_{\rm exc}", "stay_loss"),
                                  ("$r=2$", "$r=1$", "two_layers_favor_return"),
                                  (r"\ArgumentTransferFidelity", "1", "transfer_parameter")):
                with self.subTest(language=language, boundary=key):
                    self.assertIn(old, text)
                    self.assertFalse(illustration_checks(text.replace(old, new), language)[key])
            for key, old in (("two_factor_scope", "仅计转移与受激" if language == "zh" else "only transfer and excitation"),
                             ("complete_objective_is_broader", "占位、路由与相干" if language == "zh" else "occupancy, routing, and decoherence")):
                self.assertIn(old, text)
                self.assertFalse(illustration_checks(text.replace(old, ""), language)[key])

    def test_only_two_contributions_are_in_introduction(self):
        core_relations = {
            "sections": (
                r"联合(?:优化|选择)[^。；\n]{0,30}门位[^。；\n]{0,15}驻留[^。；\n]{0,15}回迁存储位",
                r"候选[^。；\n]{0,12}执行后[^。；\n]{0,8}配置",
                r"(?:预测|评价)[^。；\n]{0,20}多层物理损失",
            ),
            "sections_en": (
                r"jointly (?:optimizes|selects)[^.;\n]{0,30}gate (?:locations|sites)[^.;\n]{0,30}residency[^.;\n]{0,30}return storage sites",
                r"candidate[^.;\n]{0,35}(?:post-execution configuration|configuration after execution)",
                r"(?:predicts|evaluates)[^.;\n]{0,35}(?:multi-layer physical losses|physical losses across (?:subsequent|multiple) layers)",
            ),
        }
        for language, patterns in core_relations.items():
            text = source(f"{language}/01_introduction.tex").split(r"\label{sec:contributions}", 1)[1]
            text = re.sub(r"\\begin\{figure\*?\}.*?\\end\{figure\*?\}", "", text, flags=re.S)
            self.assertNotIn(r"\begin{itemize}", text)
            paragraphs = [paragraph for paragraph in re.split(r"\n\s*\n", text.strip()) if paragraph.strip()]
            self.assertEqual(len(paragraphs), 1)
            for pattern in patterns:
                self.assertRegex(paragraphs[0], pattern)
            self.assertNotIn(r"\label{sec:contributions}", source(f"{language}/02_background.tex"))

    def test_four_results_sections_follow_gain_design_and_cost(self):
        self.assertEqual(list(subsections("sections/05_evaluation.tex")), ["比较设置", "总体收益与物理来源", "前瞻与搜索的作用", "计算预算"])
        self.assertEqual(list(subsections("sections_en/05_evaluation.tex")), ["Comparison Setup", "Overall Gains and Physical Sources", "Effects of Look-Ahead and Search", "Computational Budget"])

    def test_main_results_and_controls_keep_separate_sources(self):
        parts = subsections("sections/05_evaluation.tex")
        overall, controls, runtime = (parts[key] for key in ("总体收益与物理来源", "前瞻与搜索的作用", "计算预算"))
        self.assertIn(r"\input{sections/05_main_table}", overall)
        self.assertNotIn(r"\input{sections/05_circuit_table}", overall)
        for macro in (r"\AblationGARatio", r"\AblationGAN", r"\AblationHRatio", r"\AblationHN", r"\HorizonExtCompleteN",
                      r"\PhysicalInitSearchCompleteN", r"\PhysicalInitGaVsHZeroGainPercent", r"\PhysicalInitGaVsRandomGainPercent"):
            self.assertIn(macro, controls)
        checks = boundary_checks(controls, CONTROL_BOUNDARIES)
        self.assertTrue(all(checks.values()), checks)
        self.assertIn(r"\SensitivityFidelityN", runtime)
        for obsolete in (r"\Default", r"\PhysicalInitGaVsSa", r"\StrictMFourTime"):
            self.assertNotIn(obsolete, source("sections/05_evaluation.tex"))

    def test_cohort_aggregation_and_runtime_remain_explicit(self):
        parts = subsections("sections/05_evaluation.tex")
        aggregation = boundary_checks(parts["比较设置"], AGGREGATION_BOUNDARIES)
        timing = timing_boundary_checks(parts["计算预算"])
        self.assertTrue(all(aggregation.values()), aggregation)
        self.assertTrue(all(timing.values()), timing)

    def test_evidence_boundaries_allow_equivalent_phrasing(self):
        parts = subsections("sections/05_evaluation.tex")
        controls = parts["前瞻与搜索的作用"].replace("来自不同样本", "采用不同样本").replace(
            "其增量不能相加", "不将增量相加").replace("搜索选择", "搜索差异")
        self.assertTrue(all(boundary_checks(controls, CONTROL_BOUNDARIES).values()))
        timing = parts["计算预算"].replace(
            "主质量研究的", "主实验中的")
        self.assertTrue(all(timing_boundary_checks(timing).values()))

    def test_tone_check_allows_named_controlled_conclusions(self):
        examples = {
            "meta_supports_effect": (
                "受控比较支持前瞻配置和联合空间内遗传搜索的作用。",
                "两项独立对照支持近期前瞻与遗传搜索的作用。"),
            "meta_provides_evidence": (
                "对照为固定联合空间内的搜索差异提供证据。",
                "结果为前瞻减少后续输运提供直接证据。"),
        }
        for name, sentences in examples.items():
            pattern = verifier.FORBIDDEN_PDF_TONE_PATTERNS[name]
            for sentence in sentences:
                with self.subTest(rule=name, sentence=sentence):
                    self.assertNotRegex(sentence, pattern)
                    self.assertNotRegex("\n".join(sentence), pattern)

    def test_tone_check_still_catches_object_free_templates(self):
        examples = {"meta_supports_effect": "结果支持该方法的作用。",
                    "meta_provides_evidence": "这些对照为该方法提供了有力证据。"}
        for name, sentence in examples.items():
            pattern = verifier.FORBIDDEN_PDF_TONE_PATTERNS[name]
            self.assertRegex(sentence, pattern)
            self.assertRegex("\n".join(sentence), pattern)

    def test_control_boundaries_cannot_be_deleted(self):
        text = subsections("sections/05_evaluation.tex")["前瞻与搜索的作用"]
        for name, pattern in CONTROL_BOUNDARIES.items():
            with self.subTest(boundary=name):
                match = re.search(pattern, text)
                self.assertIsNotNone(match)
                changed = text[:match.start()] + text[match.end():]
                self.assertFalse(boundary_checks(changed, CONTROL_BOUNDARIES)[name])

    def test_aggregation_boundaries_cannot_be_deleted(self):
        text = subsections("sections/05_evaluation.tex")["比较设置"]
        for name, pattern in AGGREGATION_BOUNDARIES.items():
            with self.subTest(boundary=name):
                match = re.search(pattern, text)
                self.assertIsNotNone(match)
                changed = text[:match.start()] + text[match.end():]
                self.assertFalse(boundary_checks(changed, AGGREGATION_BOUNDARIES)[name])

    def test_timing_boundaries_cannot_be_deleted_or_borrowed_from_other_study(self):
        text = subsections("sections/05_evaluation.tex")["计算预算"]
        paragraphs = re.split(r"\n\s*\n", text)
        for name, pattern in TIMING_BOUNDARIES.items():
            with self.subTest(boundary=name):
                macro = r"\SensitivityFidelityN" if name.startswith("budget_") else "包含GA初始化" if name.startswith("main_") else r"\GAMainFixedLayoutCircuitN"
                paragraph = next(p for p in paragraphs if macro in p)
                match = re.search(pattern, paragraph)
                self.assertIsNotNone(match)
                replacement = paragraph[:match.start()] + paragraph[match.end():]
                self.assertFalse(timing_boundary_checks(text.replace(paragraph, replacement))[name])

    def test_horizon_figure_retains_all_circuits_and_normalization(self):
        evaluation = source("sections/05_evaluation.tex")
        caption = source("sections/05_overview_floats.tex")
        self.assertEqual(evaluation.count(r"\input{sections/05_overview_floats}"), 1)
        self.assertRegex(caption, r"\\begin\{figure\}\[[!htbp]+\]")
        self.assertNotIn(r"\begin{figure*}", caption)
        for token in (r"\HorizonExtSeedN", "中位数", "$H=8$", "归一化", "保真度比", "批次比", "全部电路", "几何均值", "实际深度", "剩余层数", "规模预算"):
            self.assertIn(token, caption)
        self.assertIn(r"\HorizonExtCompleteN", caption)
        self.assertNotIn(r"\ref{tab:sensitivity}", evaluation)

    def test_protocol_retains_full_source_and_budget_detail(self):
        text = notes_path(ROOT, "experiment_protocol.md").read_text()
        for path in ("paper_zh_v2/", "horizon_extension_v2/", "physical_ga_initial_v1/"):
            self.assertIn(path, text)
        for row in ("| ≤512 | 不限 | 512 | 576 | 8 | 6 | 4 | 8 |", "| 513–4999 | 不限 | 64 | 192 | 6 | 4 | 2 | 4 |",
                    "| ≥5000 | >16 | 64 | 192 | 6 | 4 | 2 | 2 |", "| ≥5000 | ≤16 | 16 | 32 | 4 | 1 | 1 | 2 |"):
            self.assertIn(row, text)

    def test_single_conclusion_and_separate_references(self):
        for language in ("sections", "sections_en"):
            ending = "\n".join(source(f"{language}/{name}") for name in ("06_related_work.tex", "07_conclusion.tex"))
            self.assertEqual(ending.count(r"\section{"), 1)
            self.assertNotIn(r"\balance", ending)
            for label in ("sec:discussion", "sec:conclusion"):
                self.assertIn(r"\label{" + label + "}", ending)
        for entry in ("paper_zh.tex", "paper_en.tex"):
            self.assertNotIn(r"\IEEEtriggeratref", source(entry))
            self.assertNotRegex(source(entry), r"\\input\{sections(?:_en)?/02_background(?:\.tex)?\}")
        for language in ("sections", "sections_en"):
            active_sections = "\n".join(source(f"{language}/{name}") for name in (
                "01_introduction.tex", "02_background.tex", "03_method.tex",
                "05_evaluation.tex", "06_related_work.tex", "07_conclusion.tex"))
            self.assertEqual(active_sections.count(r"\section{"), 4)


if __name__ == "__main__":
    unittest.main()
