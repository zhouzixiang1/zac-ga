from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.tuning import (  # noqa: E402
    DEFAULT_CANDIDATE, EXPONENTIAL_QUALITY_MODEL, TUNING_PROTOCOL_ID,
    TUNING_QUALITY_POLICY_ID, TuningTrial, build_tuning_split, candidate_id,
    circuit_family, generate_candidates, rank_candidates)


class TuningProtocolTests(unittest.TestCase):
    def test_family_normalization(self):
        self.assertEqual(circuit_family("alu-v2_31.qasm"), "alu")
        self.assertEqual(circuit_family("4mod5-bdd_287"), "4mod5")
        self.assertEqual(circuit_family("rd53_130.qasm"), "rd53")

    def test_real_qmap_split_has_expected_sizes_and_no_family_leakage(self):
        path = Path("/Users/zhouzixiang/Desktop/zac-schema2-artifacts/canonical/"
                    "qmap154/suite.manifest.json")
        if not path.is_file():
            self.skipTest("canonical QMAP154 suite is not available")
        suite = json.loads(path.read_text(encoding="utf-8"))
        split = build_tuning_split(suite)
        self.assertEqual(len(split["screen"]), 9)
        self.assertEqual(len(split["train"]), 18)
        self.assertEqual(len(split["validation"]), 9)
        self.assertEqual(len(split["holdout"]), 127)
        labels = {}
        for label in ("train", "validation", "holdout"):
            for circuit in split[label]:
                family = circuit_family(circuit)
                previous = labels.setdefault(family, label)
                self.assertEqual(previous, label)

    def test_candidate_sample_is_deterministic_and_contains_default(self):
        left = generate_candidates()
        right = generate_candidates()
        self.assertEqual(left, right)
        self.assertEqual(len(left), 18)
        self.assertEqual(left[0]["candidate_id"], candidate_id(DEFAULT_CANDIDATE))
        self.assertTrue(all(
            row["population_size"] * row["iterations"] *
            row["neighbor_sample_size"] <= 3456 for row in left))

    def test_shared_selector_rejects_one_sided_regression(self):
        default = candidate_id(DEFAULT_CANDIDATE)
        better = "better"
        trials = []
        for cid in (default, better):
            for method in ("M3", "M4"):
                value = -1.0
                if cid == better:
                    value += 0.1 if method == "M3" else -0.01
                trials.append(TuningTrial(
                    cid, "toy", method, 0, "success", True, 0, False,
                    value, 100 if cid == better else 200, 10.0, 1,
                    False, value))
        result = rank_candidates(
            trials, default_id=default, expected_circuits=["toy"],
            expected_seeds=[0])
        self.assertEqual(result["shared_selected"], default)
        self.assertEqual(result["independent_selected"]["M3"], better)
        self.assertEqual(result["independent_selected"]["M4"], default)

    def test_rank_uses_one_quality_model_for_whole_circuit_method_cohort(self):
        default = candidate_id(DEFAULT_CANDIDATE)
        candidate = "candidate"
        trials = []
        for cid in (default, candidate):
            for method in ("M3", "M4"):
                ood = cid == candidate and method == "M3"
                linear = None if ood else (-1.0 if cid == default else -0.9)
                exponential = (
                    -2.1 if cid == candidate and method == "M3" else -2.0)
                trials.append(TuningTrial(
                    candidate_id=cid, circuit="toy", method=method, seed=0,
                    status="success", verifier_ok=True, ghost_hits=0,
                    fallback=False, log_fidelity=linear,
                    transition_decision_ns=100, move_time_us=10.0,
                    move_batches=1, fidelity_ood=ood,
                    exponential_sensitivity_log_fidelity=exponential))

        result = rank_candidates(
            trials, default_id=default, expected_circuits=["toy"],
            expected_seeds=[0])

        self.assertEqual(result["protocol_id"], TUNING_PROTOCOL_ID)
        self.assertEqual(result["quality_policy"], TUNING_QUALITY_POLICY_ID)
        summaries = {row["candidate_id"]: row for row in result["summaries"]}
        self.assertAlmostEqual(
            summaries[candidate]["methods"]["M3"][
                "median_delta_log_fidelity"], -0.1)
        self.assertEqual(
            summaries[default]["methods"]["M3"]["quality_model_counts"][
                EXPONENTIAL_QUALITY_MODEL], 1)
        self.assertEqual(result["shared_selected"], default)


if __name__ == "__main__":
    unittest.main()
