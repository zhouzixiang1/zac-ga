"""Pure/read-only postprocessing checks; no compiler or experiment execution."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


path = Path(__file__).with_name("analyze_outputs.py")
spec = importlib.util.spec_from_file_location("postanalysis", path)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


class AnalysisTests(unittest.TestCase):
    def test_partial_arm_is_not_a_paired_comparison(self):
        def result(logf, batches):
            return {"status": "success", "validation": {"ok": True, "ghost_hits": 0},
                    "score": {"ood": False, "log_fidelity": logf, "move_batches": batches}}
        records = {("a", seed): {"accepted": seed != 2,
                   **{arm: result(-1., 10 + seed) for arm in analysis.ARMS}} for seed in (0, 1, 2)}
        records["a", 2]["h2"]["score"]["move_batches"] = 10000
        report = {"per_circuit": [{"circuit": "a"}]}
        analysis.add_common_seed_comparisons(report, {"seeds": [0, 1, 2]}, records)
        row = report["per_circuit"][0]
        self.assertEqual(row["common_valid_paired_seeds"], [0, 1])
        self.assertEqual(row["common_seed_comparisons"]["h2_vs_h0"]["move_batches_reduction_percent"], 0)
        self.assertTrue(row["common_seed_comparisons"]["h2_vs_h0"]["descriptive_only"])

    def test_linked_quality_has_no_wall_time(self):
        report = {"per_circuit": [{"arms": {"h0": {"mean_move_time_us": 10, "mean_selection_s": 2}}}],
                  "overall": {"arms": {"h0": {"mean_end_to_end_s": 15}},
                              "comparisons": {"h2_vs_h0": {"end_to_end_time_ratio": 1.1}}}}
        result = analysis.quality_only(report)
        self.assertNotIn("mean_selection_s", result["per_circuit"][0]["arms"]["h0"])
        self.assertEqual(result["per_circuit"][0]["arms"]["h0"]["mean_move_time_us"], 10)
        self.assertNotIn("end_to_end_time_ratio", result["overall"]["comparisons"]["h2_vs_h0"])

    def selection_fixture(self):
        root = path.parent / "jobs/ghz_n78_transpiled-s0"
        return analysis.read(root / "h2_selection.json"), analysis.read(root / "base_mapping.json")

    def test_actual_selection_valid(self):
        selection, base = self.selection_fixture()
        analysis.audit_selection(selection, base["mapping"], 2, 0, base["total_layers"])

    def test_wrong_candidate_mapping_rejected(self):
        selection, base = self.selection_fixture()
        selection["candidates"][1]["mapping"][0][2] += 1
        with self.assertRaisesRegex(ValueError, "candidate mapping"):
            analysis.audit_selection(selection, base["mapping"], 2, 0, base["total_layers"])

    def test_wrong_argmin_rejected(self):
        selection, base = self.selection_fixture()
        selection["selected_candidate"] = (selection["selected_candidate"] + 1) % 4
        with self.assertRaisesRegex(ValueError, "selected candidate"):
            analysis.audit_selection(selection, base["mapping"], 2, 0, base["total_layers"])

    def test_wrong_discount_rejected(self):
        selection, base = self.selection_fixture()
        selection["candidates"][0]["rows"][1]["weight"] = 1.
        with self.assertRaisesRegex(ValueError, "prefix weight"):
            analysis.audit_selection(selection, base["mapping"], 2, 0, base["total_layers"])

    def test_wrong_job_seed_rejected(self):
        protocol = analysis.read(path.parent / "protocol.json")
        real_read = analysis.read
        def altered_read(target):
            data = real_read(target)
            if str(target).endswith("jobs/ghz_n78_transpiled-s0/protocol.json"):
                data = deepcopy(data)
                data["dynamic_setting"]["seed"] = 91
            return data
        with patch.object(analysis, "read", side_effect=altered_read):
            with self.assertRaisesRegex(ValueError, "dynamic config mismatch"):
                analysis.audit_phase(path.parent, protocol, ["ghz_n78_transpiled"])


if __name__ == "__main__":
    unittest.main()
