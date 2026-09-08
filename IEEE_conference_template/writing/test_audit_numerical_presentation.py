"""Regressions for errors actually found in the Chinese paper's presentation."""

import csv
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import audit_numerical_presentation as audit_module


class NumericalPresentationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.paper = self.project / "paper"
        self.evidence = self.project / "ZAC_zzx/results/paper_zh_v2"
        (self.paper / "sections").mkdir(parents=True)
        self.evidence.mkdir(parents=True)
        self.macros = {
            "ZACMFourF": "2.957394e-01", "ZACMOneF": "2.708061e-01",
            "ZACMTwoF": "2.670025e-01", "QMAPMFourF": "5.628677e-03",
            "QMAPMOneF": "4.463285e-03", "QMAPMTwoF": "4.467989e-03",
            "ZACMOneB": "94.56", "ZACMTwoB": "83.72", "ZACMFourB": "78.61",
            "QMAPMOneB": "753.33", "QMAPMTwoB": "746.80", "QMAPMFourB": "558.76",
            "ZACMOneT": "10.830", "ZACMTwoT": "10.004", "ZACMFourT": "9.920",
        }
        (self.paper / "results_values_zh.tex").write_text("\n".join(
            rf"\newcommand{{\{key}}}{{{value}}}" for key, value in self.macros.items()
        ), encoding="utf-8")
        (self.evidence / "paper_values.json").write_text(
            json.dumps({"macros": self.macros}), encoding="utf-8"
        )
        for dataset, bounds in (("zac18", (11, 98)), ("qmap154", (3, 16))):
            with (self.evidence / f"{dataset}.csv").open("w", newline="") as file:
                writer = csv.writer(file)
                count, unique = (18, 18) if dataset == "zac18" else (154, 151)
                writer.writerow(["qubits", "canonical_sha256"])
                writer.writerows([[bounds[index % 2], f"{dataset}-{index % unique}"]
                                  for index in range(count)])
        self.tex = r"""\input{results_values_zh}
\begin{abstract}
两个基准集分别包含11--98和3--16量子比特。
在各方法共同有效的电路上，相较现有编译方法，基准集的模型保真度几何均值最高提高
\PaperPercentGain{\QMAPMFourF}{\QMAPMOneF}，平均重排批次最多减少
\PaperPercentReduction{\QMAPMFourB}{\QMAPMOneB}。
\end{abstract}
"""
        (self.paper / "paper_zh.tex").write_text(self.tex, encoding="utf-8")
        self.pdf = self.paper / "build/paper_zh/paper_zh.pdf"
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(b"test fixture; extraction is mocked")
        self.inventory = ("实验使用ZAC的18个基准电路（ZAC18）和MQT QMAP的154个示例输入（QMAP154），"
                          "后者规范化后包含151个不同电路。\n")
        self.evaluation = self.inventory + r"""多层评价共用$(\alpha,\rho,\epsilon)=(0.5,0.7,0.05)$。
按规范化电路内容排序选取。
"""
        self.write_evaluation(self.evaluation)
        (self.paper / "sections/05_main_table.tex").write_text(
            r"\ZACMOneT & \ZACMTwoT & \textbf{\ZACMFourT}", encoding="utf-8"
        )
        self.config_root = self.project / audit_module.CONFIG_RELATIVE
        self.config_root.mkdir(parents=True)
        for name in [f"{variant}-seed{seed}.json" for variant in ("h0", "h8")
                     for seed in range(3)] + ["greedy-seed0.json"]:
            horizon = 0 if name.startswith("h0") else 8
            payload = {"base_config": {"zac_setting": [{
                "alpha_lookahead": 0.5,
                "lookahead_horizon": {"rho": 0.7, "epsilon": 0.05, "max_horizon": horizon},
            }]}}
            (self.config_root / name).write_text(json.dumps(payload), encoding="utf-8")
        self.pdf_text = ("Title\nAbstract—两个基准集范围11–98与3–16。"
                         "在各方法共同有效的电路上，相较现有编译方法，"
                         "基准集的模型保真度几何均值最高提高26.11%，"
                         "平均重排批次最多减少25.83%。"
                         "\nIndex Terms—neutral atoms\n正文不应参与校验：25.90%。")

    def write_evaluation(self, text):
        (self.paper / "sections/05_evaluation.tex").write_text(text, encoding="utf-8")

    def run_audit(self, pdf_text=None):
        result = subprocess.CompletedProcess([], 0, stdout=pdf_text or self.pdf_text, stderr="")
        with patch.object(audit_module.subprocess, "run", return_value=result):
            return audit_module.audit(self.paper, self.evidence, self.project,
                                      pdf_path=self.pdf)

    def assert_failed(self, report, check):
        self.assertEqual(report["status"], "fail")
        self.assertIn(check, report["errors"])

    def test_valid_rendered_numbers_and_source_evidence_pass(self):
        self.assertEqual(self.run_audit()["status"], "pass")

    def test_audit_reads_explicit_build_pdf(self):
        result = subprocess.CompletedProcess([], 0, stdout=self.pdf_text, stderr="")
        with patch.object(audit_module.subprocess, "run", return_value=result) as run:
            report = audit_module.audit(self.paper, self.evidence, self.project,
                                        pdf_path=self.pdf)
        self.assertEqual(report["status"], "pass")
        self.assertIn(str(self.pdf.resolve()), run.call_args.args[0])

    def test_missing_build_pdf_does_not_fall_back_to_source_pdf(self):
        self.pdf.unlink()
        (self.paper / "paper_zh.pdf").write_bytes(b"stale source-directory PDF")
        report = self.run_audit()
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("Missing rendered paper" in error for error in report["errors"]))

    def test_old_pgf_fixed_point_percentages_fail(self):
        old = self.pdf_text.replace("26.11%", "25.90%")
        self.assert_failed(self.run_audit(old), "rendered_abstract_percentages")

    def test_correct_numbers_in_wrong_order_fail(self):
        swapped = self.pdf_text.replace("26.11%", "REPLACEMENT").replace("25.83%", "26.11%")
        self.assert_failed(self.run_audit(swapped.replace("REPLACEMENT", "25.83%")),
                           "rendered_abstract_percentages")

    def test_nonmaximum_fidelity_summary_fails(self):
        (self.paper / "paper_zh.tex").write_text(
            self.tex.replace(r"{\QMAPMOneF}", r"{\QMAPMTwoF}"), encoding="utf-8"
        )
        self.assert_failed(self.run_audit(self.pdf_text.replace("26.11%", "25.98%")),
                           "abstract_summary_maxima")

    def test_nonmaximum_batch_summary_fails(self):
        (self.paper / "paper_zh.tex").write_text(
            self.tex.replace(r"{\QMAPMFourB}{\QMAPMOneB}",
                             r"{\ZACMFourB}{\ZACMOneB}"), encoding="utf-8"
        )
        self.assert_failed(self.run_audit(self.pdf_text.replace("25.83%", "16.87%")),
                           "abstract_summary_maxima")

    def test_maxima_are_computed_from_all_frozen_candidates(self):
        # Make the unselected ZAC summary better than the chosen QMAP summary.
        # Both copies agree, so only checking consistency would miss this error.
        file = self.paper / "results_values_zh.tex"
        file.write_text(file.read_text().replace("2.957394e-01", "9e-01"), encoding="utf-8")
        changed = dict(self.macros, ZACMFourF="9e-01")
        (self.evidence / "paper_values.json").write_text(
            json.dumps({"macros": changed}), encoding="utf-8"
        )
        self.assert_failed(self.run_audit(), "abstract_summary_maxima")

    def test_negative_batch_reduction_in_pdf_fails(self):
        self.assert_failed(self.run_audit(self.pdf_text.replace("25.83%", "-25.83%")),
                           "rendered_abstract_percentages")

    def test_batch_gain_operator_instead_of_reduction_fails(self):
        (self.paper / "paper_zh.tex").write_text(
            self.tex.replace("PaperPercentReduction", "PaperPercentGain"), encoding="utf-8"
        )
        self.assert_failed(self.run_audit(), "abstract_two_summary_metrics")

    def test_literal_percentage_instead_of_derived_macro_fails(self):
        (self.paper / "paper_zh.tex").write_text(
            self.tex.replace(r"\PaperPercentGain{\QMAPMFourF}{\QMAPMOneF}", r"26.11\%"),
            encoding="utf-8"
        )
        self.assert_failed(self.run_audit(), "abstract_no_literal_percentages")

    def test_unqualified_maxima_in_source_fail(self):
        (self.paper / "paper_zh.tex").write_text(
            self.tex.replace("基准集的模型保真度", "模型保真度"), encoding="utf-8"
        )
        self.assert_failed(self.run_audit(), "abstract_summary_scope")

    def test_unqualified_maxima_in_pdf_fail(self):
        self.assert_failed(self.run_audit(self.pdf_text.replace("基准集的模型保真度", "模型保真度")),
                           "rendered_abstract_summary_scope")

    def test_explicit_summary_clause_also_passes(self):
        (self.paper / "paper_zh.tex").write_text(
            self.tex.replace("基准集的模型保真度", "基准集汇总结果中，模型保真度"),
            encoding="utf-8"
        )
        report = self.run_audit(self.pdf_text.replace(
            "基准集的模型保真度", "基准集汇总结果中，模型保真度"
        ))
        self.assertEqual(report["status"], "pass")

    def test_missing_statistical_metric_words_fail(self):
        for word in ("几何均值", "平均", "最高", "最多"):
            with self.subTest(word=word):
                self.assert_failed(self.run_audit(self.pdf_text.replace(word, "")),
                                   "rendered_abstract_summary_scope")

    def test_reduction_described_as_increase_fails(self):
        self.assert_failed(self.run_audit(self.pdf_text.replace("最多减少", "最多增加")),
                           "rendered_abstract_summary_scope")

    def test_abstract_stops_at_index_terms(self):
        report = self.run_audit(self.pdf_text + " 123.45% 678.90%")
        self.assertEqual(report["status"], "pass")

    def test_wrong_zac_qubit_range_fails_even_when_tex_is_correct(self):
        self.assert_failed(self.run_audit(self.pdf_text.replace("11–98", "14–98")),
                           "zac18_abstract_qubit_range")

    def test_abstract_may_omit_inventory_ranges_in_both_source_and_pdf(self):
        (self.paper / "paper_zh.tex").write_text(
            self.tex.replace("两个基准集分别包含11--98和3--16量子比特。", ""), encoding="utf-8"
        )
        self.assertEqual(self.run_audit(self.pdf_text.replace(
            "两个基准集范围11–98与3–16。", ""))["status"], "pass")

    def test_stale_qmap_range_in_pdf_fails_when_source_omits_it(self):
        (self.paper / "paper_zh.tex").write_text(self.tex.replace("3--16", "若干"), encoding="utf-8")
        self.assert_failed(self.run_audit(), "qmap154_abstract_qubit_range")

    def test_matching_but_incorrect_abstract_ranges_fail(self):
        (self.paper / "paper_zh.tex").write_text(
            self.tex.replace("11--98", "14--98"), encoding="utf-8"
        )
        self.assert_failed(self.run_audit(self.pdf_text.replace("11–98", "14–98")),
                           "zac18_abstract_qubit_range")

    def test_wrong_evaluation_input_counts_fail(self):
        for old, new, dataset in (("18个", "19个", "zac18"),
                                  ("154个", "155个", "qmap154")):
            with self.subTest(dataset=dataset):
                self.write_evaluation(self.evaluation.replace(old, new))
                self.assert_failed(self.run_audit(), f"{dataset}_evaluation_input_count")

    def test_benchmark_name_alone_does_not_replace_input_count(self):
        self.write_evaluation(self.evaluation.replace("18个基准电路", "基准电路"))
        self.assert_failed(self.run_audit(), "zac18_evaluation_input_count")

    def test_missing_experimental_inventory_fails(self):
        self.write_evaluation(self.evaluation.replace(self.inventory, ""))
        self.assert_failed(self.run_audit(), "zac18_evaluation_input_count")
        self.assert_failed(self.run_audit(), "qmap154_evaluation_input_count")

    def test_wrong_normalized_circuit_count_fails(self):
        self.write_evaluation(self.evaluation.replace("151个不同电路", "154个不同电路"))
        self.assert_failed(self.run_audit(), "qmap154_evaluation_unique_count")

    def test_wrong_legacy_ablation_parameters_fail(self):
        self.write_evaluation(self.evaluation.replace("(0.5,0.7,0.05)", "(0.1,0.6,0.05)"))
        self.assert_failed(self.run_audit(), "stated_ablation_parameters")

    def test_missing_ablation_parameters_fail(self):
        self.write_evaluation("受控比较采用共享设置。")
        self.assert_failed(self.run_audit(), "stated_ablation_parameters")

    def test_named_ablation_parameters_pass(self):
        self.write_evaluation(
            self.inventory
            + r"未来权重、层距衰减因子和停止阈值分别取$\alpha=0.5$、$\rho=0.7$和$\epsilon=0.05$。")
        self.assertEqual(self.run_audit()["status"], "pass")

    def test_wrong_or_incomplete_named_ablation_parameters_fail(self):
        valid = r"未来权重$\alpha=0.5$、层距衰减因子$\rho=0.7$和停止阈值$\epsilon=0.05$。"
        for invalid in (valid.replace("0.5", "0.1"), valid.replace("0.7", "0.6"),
                        valid.replace("0.05", "0.1"), valid.replace(r"\epsilon=0.05", "阈值")):
            with self.subTest(source=invalid):
                self.write_evaluation(invalid)
                self.assert_failed(self.run_audit(), "stated_ablation_parameters")

    def test_conflicting_named_declaration_after_valid_tuple_fails(self):
        self.write_evaluation(self.evaluation + r"另取$\alpha=0.1$、$\rho=0.7$和$\epsilon=0.05$。")
        self.assert_failed(self.run_audit(), "stated_ablation_parameters")

    def test_real_configuration_disagreement_fails(self):
        file = self.config_root / "h0-seed1.json"
        payload = json.loads(file.read_text())
        payload["base_config"]["zac_setting"][0]["alpha_lookahead"] = 0.1
        file.write_text(json.dumps(payload), encoding="utf-8")
        self.assert_failed(self.run_audit(), "shared_ablation_configuration")

    def test_filename_instead_of_content_selection_fails(self):
        self.write_evaluation(self.evaluation.replace("电路内容", "电路名"))
        self.assert_failed(self.run_audit(), "selection_uses_content_not_name")

    def test_wrong_best_time_bold_fails(self):
        (self.paper / "sections/05_main_table.tex").write_text(
            r"\ZACMOneT & \textbf{\ZACMTwoT} & \ZACMFourT", encoding="utf-8"
        )
        self.assert_failed(self.run_audit(), "zac18_rearrangement_time_emphasis")

    def test_stale_between_baselines_claim_fails(self):
        self.write_evaluation(self.evaluation + "\n\nZAC18重排时延介于两个基线之间。")
        self.assert_failed(self.run_audit(), "no_stale_between_baselines_claim")

    def test_changed_paper_macro_without_evidence_update_fails(self):
        file = self.paper / "results_values_zh.tex"
        file.write_text(file.read_text().replace("5.628677e-03", "5.7e-03"), encoding="utf-8")
        self.assert_failed(self.run_audit(), "macro_evidence_consistency")

    def test_missing_evidence_file_fails_closed(self):
        (self.evidence / "qmap154.csv").unlink()
        report = self.run_audit()
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("input_or_extraction_error" in error for error in report["errors"]))


class DefaultNumericalPresentationTest(unittest.TestCase):
    """The new quality bundle must not bypass legacy evidence or PDF checks."""

    write_evaluation = NumericalPresentationTest.write_evaluation
    run_audit = NumericalPresentationTest.run_audit
    assert_failed = NumericalPresentationTest.assert_failed

    def setUp(self):
        NumericalPresentationTest.setUp(self)
        self.default_root = self.project / audit_module.DEFAULT_EXPORT_RELATIVE
        self.default_root.mkdir(parents=True)
        self.default_input = "../ZAC_zzx/results/default_initial_v1/paper_exports/default_initial_values.tex"
        self.tex = self.tex.replace(r"\input{results_values_zh}",
                                    r"\input{results_values_zh}" + "\n" + rf"\input{{{self.default_input}}}")
        self.tex = self.tex.replace(r"\PaperPercentGain{\QMAPMFourF}{\QMAPMOneF}",
                                    r"\DefaultMaxDatasetFidelityGain\%")
        self.tex = self.tex.replace(r"\PaperPercentReduction{\QMAPMFourB}{\QMAPMOneB}",
                                    r"\DefaultMaxDatasetBatchReduction\%")
        (self.paper / "paper_zh.tex").write_text(self.tex, encoding="utf-8")
        self.pdf_text = self.pdf_text.replace("26.11%", "26.24%").replace("25.83%", "25.85%")
        self.default_macros = {}
        cells = {
            "ZACMOne": ("2.952236e-01", "103.56", "11.786", "18/18"),
            "ZACMTwo": ("2.884872e-01", "92.88", "11.039", "18/18"),
            "ZACMFour": ("3.215046e-01", "83.69", "10.606", "16/18"),
            "QMAPMOne": ("4.463285e-03", "753.33", "72.983", "133/154"),
            "QMAPMTwo": ("4.467989e-03", "746.80", "72.518", "154/154"),
            "QMAPMFour": ("5.634649e-03", "558.58", "57.246", "148/154"),
        }
        for prefix, values in cells.items():
            self.default_macros.update({"Default" + prefix + field: value
                                        for field, value in zip(("F", "B", "T", "V"), values)})
        self.default_macros.update({
            "DefaultZACStrictN": "16", "DefaultZACStrictFileN": "16",
            "DefaultQMAPStrictN": "120", "DefaultQMAPStrictFileN": "122",
            "DefaultMaxDatasetFidelityGain": "26.24", "DefaultMaxDatasetBatchReduction": "25.85",
            "DefaultMechanismQFTBaseTransfers": "648",
        })
        self.default_data = {"macros": self.default_macros, "datasets": {
            "zac18": {"comparisons": {
                "M1": {"fidelity_gain_percent": 8.902047117078848,
                       "mean_batch_reduction_percent": 19.191309595654793},
                "M2": {"fidelity_gain_percent": 11.445005454201434,
                       "mean_batch_reduction_percent": 9.892328398384931}}},
            "qmap154": {"comparisons": {
                "M1": {"fidelity_gain_percent": 26.244434810158324,
                       "mean_batch_reduction_percent": 25.850949678646884},
                "M2": {"fidelity_gain_percent": 26.11153456234389,
                       "mean_batch_reduction_percent": 25.203088734154612}}},
        }}
        self.write_default_bundle()
        self.table = "\n".join(
            r"\textbf{\DefaultZACMFourT}" if name == "DefaultZACMFourT" else "\\" + name
            for name in self.default_macros if name[-1:] in "FBTV"
        )
        (self.paper / "sections/05_main_table.tex").write_text(self.table, encoding="utf-8")

    def write_default_macros(self, macros):
        (self.default_root / "default_initial_values.tex").write_text("\n".join(
            rf"\newcommand{{\{key}}}{{{value}}}" for key, value in macros.items()
        ), encoding="utf-8")

    def write_default_bundle(self):
        self.write_default_macros(self.default_macros)
        (self.default_root / "default_initial_values.json").write_text(
            json.dumps(self.default_data), encoding="utf-8")

    def test_declared_default_bundle_and_direct_maxima_pass(self):
        report = self.run_audit()
        self.assertEqual(report["status"], "pass", report["errors"])
        self.assertEqual(report["quality_study"], "default_initial_v1")

    def test_default_ratio_expressions_remain_supported(self):
        source = self.tex.replace(r"\DefaultMaxDatasetFidelityGain\%",
                                  r"\PaperPercentGain{\DefaultQMAPMFourF}{\DefaultQMAPMOneF}")
        source = source.replace(r"\DefaultMaxDatasetBatchReduction\%",
                                r"\PaperPercentReduction{\DefaultQMAPMFourB}{\DefaultQMAPMOneB}")
        (self.paper / "paper_zh.tex").write_text(source, encoding="utf-8")
        self.assertEqual(self.run_audit()["status"], "pass")

    def test_default_ratio_expression_must_select_the_actual_maximum(self):
        source = self.tex.replace(r"\DefaultMaxDatasetFidelityGain\%",
                                  r"\PaperPercentGain{\DefaultQMAPMFourF}{\DefaultQMAPMTwoF}")
        (self.paper / "paper_zh.tex").write_text(source, encoding="utf-8")
        self.assert_failed(self.run_audit(self.pdf_text.replace("26.24%", "26.11%")),
                           "abstract_summary_maxima")

    def test_direct_maxima_do_not_divide_displayed_underflow_fidelities(self):
        # Finite log-fidelity comparisons can remain meaningful when both
        # displayed geometric means underflow; the direct macro uses JSON gain.
        for name in ("DefaultQMAPMFourF", "DefaultQMAPMOneF", "DefaultQMAPMTwoF"):
            self.default_macros[name] = "0.000000e+00"
        self.write_default_bundle()
        self.assertEqual(self.run_audit()["status"], "pass")

    def test_default_input_must_be_unique_and_resolve_to_the_declared_bundle(self):
        original = rf"\input{{{self.default_input}}}"
        for replacement in ("", original + "\n" + original,
                            r"\input{build/copied/default_initial_values.tex}"):
            with self.subTest(replacement=replacement):
                (self.paper / "paper_zh.tex").write_text(
                    self.tex.replace(original, replacement), encoding="utf-8")
                self.assert_failed(self.run_audit(), "default_result_input")

    def test_default_input_cannot_resolve_through_a_symlink_to_a_different_bundle(self):
        path = self.default_root / "default_initial_values.tex"
        foreign = self.paper / "copy.tex"
        path.rename(foreign)
        path.symlink_to(foreign)
        self.assert_failed(self.run_audit(), "default_result_input")

    def test_every_new_macro_is_checked_including_coverage_and_mechanism(self):
        for name in ("DefaultQMAPMOneF", "DefaultQMAPMFourB", "DefaultQMAPMTwoT",
                     "DefaultQMAPMFourV", "DefaultZACStrictN", "DefaultMechanismQFTBaseTransfers"):
            with self.subTest(name=name):
                self.write_default_macros(dict(self.default_macros, **{name: "999"}))
                self.assert_failed(self.run_audit(), "default_macro_evidence_consistency")

    def test_missing_macro_fails_even_if_both_macro_copies_omit_it(self):
        del self.default_macros["DefaultQMAPMFourV"]
        self.write_default_bundle()
        self.assert_failed(self.run_audit(), "default_macro_evidence_consistency")

    def test_duplicate_macro_definition_is_rejected(self):
        path = self.default_root / "default_initial_values.tex"
        path.write_text(path.read_text() + "\n" + r"\renewcommand{\DefaultZACMFourF}{0.9}", encoding="utf-8")
        report = self.run_audit()
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("Duplicate result macro definitions" in error for error in report["errors"]))

    def test_direct_maximum_is_checked_against_all_json_candidates(self):
        for field in ("fidelity_gain_percent", "mean_batch_reduction_percent"):
            original = self.default_data["datasets"]["zac18"]["comparisons"]["M2"][field]
            with self.subTest(field=field):
                self.default_data["datasets"]["zac18"]["comparisons"]["M2"][field] = 90
                self.write_default_bundle()
                self.assert_failed(self.run_audit(), "abstract_summary_maxima")
            self.default_data["datasets"]["zac18"]["comparisons"]["M2"][field] = original

    def test_agreeing_wrong_maximum_macro_and_pdf_do_not_override_candidates(self):
        self.default_macros["DefaultMaxDatasetFidelityGain"] = "26.11"
        self.write_default_bundle()
        self.assert_failed(self.run_audit(self.pdf_text.replace("26.24%", "26.11%")),
                           "abstract_summary_maxima")

    def test_missing_comparison_cannot_be_silently_excluded_from_maximum(self):
        del self.default_data["datasets"]["zac18"]["comparisons"]["M2"]
        self.write_default_bundle()
        report = self.run_audit()
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("both baseline comparisons" in error for error in report["errors"]))

    def test_null_fidelity_candidate_is_not_silently_excluded(self):
        self.default_data["datasets"]["zac18"]["comparisons"]["M2"]["fidelity_gain_percent"] = None
        self.write_default_bundle()
        self.assertEqual(self.run_audit()["status"], "fail")

    def test_direct_summary_requires_percent_sign_and_metric_order(self):
        for source in (self.tex.replace(r"\DefaultMaxDatasetFidelityGain\%", r"\DefaultMaxDatasetFidelityGain"),
                       self.tex.replace("DefaultMaxDatasetFidelityGain", "DefaultMaxDatasetBatchReduction")):
            with self.subTest(source=source):
                (self.paper / "paper_zh.tex").write_text(source, encoding="utf-8")
                self.assert_failed(self.run_audit(), "abstract_two_summary_metrics")

    def test_default_branch_rejects_stale_or_swapped_pdf_percentages(self):
        for old in (self.pdf_text.replace("26.24%", "26.11%"),
                    self.pdf_text.replace("25.85%", "-25.85%"),
                    self.pdf_text.replace("26.24%", "25.85%").replace("减少25.85%", "减少26.24%")):
            with self.subTest(pdf=old):
                self.assert_failed(self.run_audit(old), "rendered_abstract_percentages")

    def test_default_branch_retains_scope_checks(self):
        self.assert_failed(self.run_audit(self.pdf_text.replace("几何均值", "")),
                           "rendered_abstract_summary_scope")

    def test_default_main_table_cannot_mix_old_quality_macros(self):
        (self.paper / "sections/05_main_table.tex").write_text(
            self.table.replace(r"\DefaultQMAPMFourF", r"\QMAPMFourF"), encoding="utf-8")
        self.assert_failed(self.run_audit(), "default_main_table_macros")

    def test_default_zac_time_must_mark_the_actual_new_minimum(self):
        (self.paper / "sections/05_main_table.tex").write_text(
            self.table.replace(r"\textbf{\DefaultZACMFourT}", r"\DefaultZACMFourT")
            .replace(r"\DefaultZACMTwoT", r"\textbf{\DefaultZACMTwoT}"), encoding="utf-8")
        self.assert_failed(self.run_audit(), "zac18_rearrangement_time_emphasis")

    def test_default_branch_still_checks_accepted_macro_evidence(self):
        path = self.paper / "results_values_zh.tex"
        path.write_text(path.read_text().replace("5.628677e-03", "0.9"), encoding="utf-8")
        self.assert_failed(self.run_audit(), "macro_evidence_consistency")

    def test_default_branch_still_checks_accepted_inventory_and_ablation_parameters(self):
        self.write_evaluation(self.evaluation.replace("154个", "155个")
                              .replace("(0.5,0.7,0.05)", "(0.1,0.7,0.05)"))
        report = self.run_audit()
        self.assert_failed(report, "qmap154_evaluation_input_count")
        self.assert_failed(report, "stated_ablation_parameters")


if __name__ == "__main__":
    unittest.main()
