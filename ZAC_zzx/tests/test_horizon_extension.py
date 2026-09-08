"""Independent extension tests: no compiler executions and no frozen edits."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[1] / "experiments_v2/horizon_extension.py"
SPEC = importlib.util.spec_from_file_location("horizon_extension_test_target", PATH)
extension = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extension)


def template():
    return {"ablation_variant": "paper_h8_ga", "controls": {"lookahead_horizon": 8},
        "base_config": {"zac_setting": [{"lookahead_horizon": {"max_horizon": 8,
            "rho": .7, "epsilon": .05}, "seed": 0, "dir": "old/", "budget": 576}]}}


def circuits():
    return [{"dataset": dataset, "circuit": f"{dataset}_{i}",
             "canonical": {"gates_1q": i + 1, "gates_2q": 3}}
            for dataset in ("zac18", "qmap154") for i in range(6)]


class HorizonExtensionTests(unittest.TestCase):
    def test_wrapper_does_not_mutate_source(self):
        old = template(); original = deepcopy(old)
        new = extension.make_wrapper(old, 1, 2)
        self.assertEqual(old, original)
        self.assertEqual(new["controls"]["lookahead_horizon"], 1)
        self.assertEqual(extension.effective(new)["seed"], 2)
        self.assertEqual(extension.effective(new)["budget"], 576)
        self.assertEqual(extension.effective(new)["lookahead_horizon"]["rho"], .7)

    def test_wrapper_rejects_unregistered_values(self):
        for h, seed in [(3, 0), (16, 0), (1, 5), (-1, 0)]:
            with self.assertRaises(ValueError):
                extension.make_wrapper(template(), h, seed)

    def test_only_output_directory_is_ignored(self):
        a = template(); b = deepcopy(a)
        extension.effective(b)["dir"] = "another/"
        self.assertEqual(extension.comparable_setting(a), extension.comparable_setting(b))
        extension.effective(b)["budget"] = 32
        self.assertNotEqual(extension.comparable_setting(a), extension.comparable_setting(b))

    def test_quality_matrix_has_exactly_84_missing_jobs(self):
        jobs = extension.missing_quality_jobs(circuits())
        self.assertEqual(len(jobs), 84)
        self.assertEqual(len({j["job_id"] for j in jobs}), 84)
        self.assertEqual(sum(j["horizon"] == 1 for j in jobs), 36)
        self.assertEqual(sum(j["horizon"] == 2 for j in jobs), 24)
        self.assertEqual(sum(j["horizon"] == 4 for j in jobs), 24)
        self.assertFalse(any(j["horizon"] in (0, 8) for j in jobs))
        self.assertFalse(any(j["horizon"] in (2, 4) and j["seed"] == 0 for j in jobs))

    def test_quality_order_reproducible(self):
        self.assertEqual(extension.missing_quality_jobs(circuits()),
                         extension.missing_quality_jobs(circuits()))

    def test_timing_separate_108_plus_six_warmups(self):
        jobs, warmups = extension.timing_jobs(circuits())
        self.assertEqual(len(jobs), 108)
        self.assertEqual(len(warmups), 6)
        self.assertEqual(len({j["job_id"] for j in jobs}), 108)
        self.assertTrue(all(j["seed"] == 0 for j in jobs))
        self.assertEqual({j["repetition"] for j in jobs}, {0, 1, 2})
        self.assertEqual({j["horizon"] for j in warmups}, {2, 4, 8})

    def test_timing_refuses_without_authorization_before_writing(self):
        with patch.object(extension, "write") as write:
            with self.assertRaisesRegex(ValueError, "authorization"):
                extension.execute_timing({}, False)
            write.assert_not_called()

    def test_write_is_exclusive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            extension.write(path, {"old": True})
            with self.assertRaises(FileExistsError):
                extension.write(path, {"old": False})
            self.assertEqual(json.loads(path.read_text()), {"old": True})

    def test_checked_json_detects_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            extension.write(path, {"value": 1})
            good = extension.digest(path)
            self.assertEqual(extension.checked_json(path, good), {"value": 1})
            with self.assertRaises(ValueError):
                extension.checked_json(path, "0" * 64)

    def records(self):
        return [{"dataset": "zac18", "circuit": "test", "horizon": h,
            "seed": seed, "status": "success", "fidelity": .5 if h == 8 else .25}
            for h in extension.HORIZONS for seed in extension.SEEDS]

    def test_summary_uses_matched_complete_three_seed_blocks(self):
        rows = self.records(); c = [{"dataset": "zac18", "circuit": "test"}]
        summary = extension.aggregate_quality(rows, c, (0, 1, 2))
        self.assertEqual(summary["complete_circuits"], 1)
        self.assertEqual(summary["fidelity_ratio_vs_h8"]["1"], .5)
        self.assertEqual(summary["fidelity_ratio_vs_h8"]["8"], 1.)

    def test_summary_retains_missing_and_model_domain_outcomes(self):
        rows = self.records(); rows[0]["fidelity"] = None
        summary = extension.aggregate_quality(rows, [{"dataset": "zac18", "circuit": "test"}], (0, 1, 2))
        self.assertEqual(summary["complete_circuits"], 0)
        self.assertEqual(summary["excluded"][0]["incomplete_horizons"], [0])
        self.assertIsNone(summary["fidelity_ratio_vs_h8"]["8"])

    def test_summary_rejects_duplicate_runs(self):
        rows = self.records()
        with self.assertRaises(ValueError):
            extension.aggregate_quality(rows + [rows[0]], [], (0, 1, 2))

    def test_visible_window_is_recorded_without_completed_depth_claim(self):
        manifest = {"dataset": "zac18", "circuit": "test", "status": "success",
                    "forecast_summary": {"visible_depth_counts": {"8": 5}}}
        row = extension.quality_record(manifest, horizon=8, seed=0, source="existing", source_sha256="hash")
        self.assertEqual(row["forecast_summary"]["visible_depth_counts"], {"8": 5})
        self.assertNotIn("completed_depth", row)
        self.assertTrue(row["old_timing_not_reused_as_new_measurement"])


if __name__ == "__main__":
    unittest.main()
