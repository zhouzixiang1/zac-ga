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
                writer.writerow(["qubits"])
                writer.writerows([[value] for value in bounds])
        self.tex = r"""\input{results_values_zh}
\begin{abstract}
两个基准集分别包含11--98和3--16量子比特。
相对ZAC分别提高\PaperPercentGain{\ZACMFourF}{\ZACMOneF}和
\PaperPercentGain{\QMAPMFourF}{\QMAPMOneF}；相对ICCAD分别提高
\PaperPercentGain{\ZACMFourF}{\ZACMTwoF}和\PaperPercentGain{\QMAPMFourF}{\QMAPMTwoF}。
\end{abstract}
"""
        (self.paper / "paper_zh.tex").write_text(self.tex, encoding="utf-8")
        self.pdf = self.project / "build/paper_zh/paper_zh.pdf"
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(b"test fixture; extraction is mocked")
        self.evaluation = r"""多层评价共用$(\alpha,\rho,\epsilon)=(0.5,0.7,0.05)$。
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
        self.pdf_text = ("Title\nAbstract—基准电路范围11–98与3–16。"
                         "相对ZAC提高9.21%和26.11%；相对ICCAD提高10.76%和25.98%。"
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
        old = self.pdf_text.replace("26.11%", "25.90%").replace("25.98%", "25.90%")
        self.assert_failed(self.run_audit(old), "rendered_abstract_percentages")

    def test_correct_numbers_in_wrong_order_fail(self):
        swapped = self.pdf_text.replace("26.11%", "REPLACEMENT").replace("25.98%", "26.11%")
        self.assert_failed(self.run_audit(swapped.replace("REPLACEMENT", "25.98%")),
                           "rendered_abstract_percentages")

    def test_abstract_stops_at_index_terms(self):
        report = self.run_audit(self.pdf_text + " 123.45% 678.90%")
        self.assertEqual(report["status"], "pass")

    def test_wrong_zac_qubit_range_fails_even_when_tex_is_correct(self):
        self.assert_failed(self.run_audit(self.pdf_text.replace("11–98", "14–98")),
                           "zac18_abstract_qubit_range")

    def test_missing_qmap_range_in_source_fails_even_when_pdf_has_it(self):
        (self.paper / "paper_zh.tex").write_text(self.tex.replace("3--16", "若干"), encoding="utf-8")
        self.assert_failed(self.run_audit(), "qmap154_abstract_qubit_range")

    def test_wrong_legacy_ablation_parameters_fail(self):
        self.write_evaluation(self.evaluation.replace("(0.5,0.7,0.05)", "(0.1,0.6,0.05)"))
        self.assert_failed(self.run_audit(), "stated_ablation_parameters")

    def test_missing_ablation_parameters_fail(self):
        self.write_evaluation("受控比较采用共享设置。")
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


if __name__ == "__main__":
    unittest.main()
