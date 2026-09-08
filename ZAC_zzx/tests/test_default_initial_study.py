"""Protocol and evidence invariants without compilation or experiment writes."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / "experiments_v2/default_initial_study.py"
SPEC = importlib.util.spec_from_file_location("default_initial_study", PATH)
STUDY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STUDY)


class DefaultInitialStudyTests(unittest.TestCase):
    def test_fixed_policy(self):
        self.assertEqual(STUDY.FIXED, {"horizon": 2, "candidates": 4, "rho": .7, "rollout_evaluations": 32})

    def test_plan_deduplicates_only_exact_canonical_input(self):
        p = STUDY.build_plan()
        self.assertEqual(len(p["jobs"]), 507)
        self.assertEqual(sum(len(j["labels"]) for j in p["jobs"]), 516)
        self.assertEqual(len({(j["canonical_sha256"], j["seed"]) for j in p["jobs"]}), 507)
        self.assertEqual(sum(j["origin"] == "reused" for j in p["jobs"]), 118)
        self.assertEqual(sum(j["origin"] == "fresh" for j in p["jobs"]), 389)
        self.assertEqual(len(p["old_timeouts_preserved"]), 5)
        self.assertEqual(len(set(p["smoke_job_ids"])), 4)

    def test_dynamic_controls_cannot_be_excluded_from_identity(self):
        a = {"name": "x", "seed": 0, "init_strategy": "legacy", "max_unique_evaluations": 576}
        b = {**a, "name": "y", "seed": 1, "init_strategy": "physical_prefix"}
        self.assertEqual(STUDY.computational_setting(a), STUDY.computational_setting(b))
        b["max_unique_evaluations"] = 575
        self.assertNotEqual(STUDY.computational_setting(a), STUDY.computational_setting(b))

    def test_immutable_write_and_pin_drift(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "record.json"
            STUDY.write_new(p, {"x": 1})
            ref = STUDY.pin(p)
            self.assertEqual(STUDY.checked(ref), {"x": 1})
            with self.assertRaises(FileExistsError):
                STUDY.write_new(p, {"x": 2})
            ref["sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                STUDY.checked(ref)

    def test_score_must_match_original_and_preserves_ood(self):
        a = {"fidelity": None, "log_fidelity": None, "move_batches": 4,
             "move_time_us": 10., "ood": True, "counts": {"transfers": 2}}
        STUDY.compare_score(a, deepcopy(a))
        for key, value in (("fidelity", 0.1), ("move_batches", 3), ("ood", False)):
            b = {**a, key: value}
            with self.assertRaises(ValueError):
                STUDY.compare_score(a, b)


if __name__ == "__main__":
    unittest.main()
