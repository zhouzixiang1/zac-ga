"""Analysis-unit, failure-accounting and fixed-sample regressions."""
from copy import deepcopy
import math
import unittest

from experiments_v2.initial_lookahead_batch import (ARMS, CIRCUITS, PROTOCOL_ID,
                                                  summarize_records, valid_result)


def result(logf=-1, batches=10, elapsed=1):
    return {"status": "success", "validation": {"ok": True, "ghost_hits": 0},
            "selected_candidate": 0, "sa_initialization_ns": int(1e8), "selection_ns": 0,
            "compile_with_selected_mapping_ns": int(elapsed * 1e9 - 1e8),
            "end_to_end_ns": int(elapsed * 1e9),
            "score": {"ood": False, "log_fidelity": logf,
                      "move_batches": batches, "move_time_us": batches * 100}}


class InitialLookaheadBatchTests(unittest.TestCase):
    def fixture(self):
        protocol = {"protocol_id": PROTOCOL_ID, "seeds": [0, 1, 2],
                    "circuits": [{"circuit": "small"}, {"circuit": "large"}]}
        records = {(circuit, seed): {"accepted": True, "status": "success",
                    **{arm: result() for arm in ARMS}}
                   for circuit in ("small", "large") for seed in (0, 1, 2)}
        return protocol, records

    def test_nine_family_inventory_has_no_duplicate(self):
        self.assertEqual(len(CIRCUITS), 9)
        self.assertEqual(len({circuit for _, circuit, _ in CIRCUITS}), 9)
        self.assertEqual({dataset for dataset, _, _ in CIRCUITS}, {"zac18", "qmap154"})

    def test_seed_repetitions_are_not_counted_as_circuits(self):
        protocol, records = self.fixture()
        for seed in (0, 1, 2):
            records["small", seed]["h2"] = result(logf=-1 + seed * .1, batches=5)
            records["large", seed]["h2"] = result(logf=-1.2, batches=8)
        report = summarize_records(protocol, records)
        comparison = report["overall"]["comparisons"]["h2_vs_h0"]
        self.assertEqual(report["overall"]["complete_circuits"], 2)
        self.assertEqual(comparison["fidelity_win_tie_loss"], {"wins": 1, "ties": 0, "losses": 1})
        self.assertAlmostEqual(comparison["fidelity_relative_gain_percent"], math.expm1(-.05) * 100)
        self.assertAlmostEqual(comparison["move_batches_reduction_percent"], 35)
        self.assertAlmostEqual(report["per_circuit"][0]["arms"]["h2"]["mean_log_fidelity"], -.9)

    def test_timeout_keeps_every_planned_circuit_without_partial_seed_primary(self):
        protocol, records = self.fixture()
        records["small", 2] = {"status": "timeout", "accepted": False,
                              "sa_reference": result()}
        report = summarize_records(protocol, records)
        self.assertEqual(len(report["per_circuit"]), 2)
        self.assertEqual(report["overall"]["complete_circuits"], 1)
        self.assertFalse(report["per_circuit"][0]["complete"])
        self.assertEqual(report["per_circuit"][0]["arms"]["sa_reference"]["completed_runs"], 3)
        self.assertEqual(report["per_circuit"][0]["arms"]["h2"]["completed_runs"], 2)

    def test_ood_or_bad_physics_is_not_silent_valid_result(self):
        for bad in ("ood", "ghost", "not_finite"):
            sample = result()
            if bad == "ood":
                sample["score"]["ood"] = True
            elif bad == "ghost":
                sample["validation"]["ghost_hits"] = 1
            else:
                sample["score"]["log_fidelity"] = float("nan")
            self.assertFalse(valid_result(sample))
            protocol, records = self.fixture()
            records["small", 0]["h2"] = sample
            report = summarize_records(protocol, records)
            self.assertFalse(report["per_circuit"][0]["complete"])
            self.assertEqual(report["overall"]["complete_circuits"], 1)

    def test_no_complete_circuit_does_not_invent_an_average(self):
        protocol, _ = self.fixture()
        report = summarize_records(protocol, {})
        self.assertEqual(report["overall"]["arms"], {})
        self.assertEqual(report["overall"]["complete_circuits"], 0)
        self.assertEqual(len(report["per_circuit"]), 2)

    def test_source_unstable_job_is_not_accepted(self):
        protocol, records = self.fixture()
        records["small", 0]["accepted"] = False
        report = summarize_records(protocol, records)
        self.assertEqual(report["overall"]["complete_circuits"], 1)
        self.assertFalse(report["per_circuit"][0]["complete"])


if __name__ == "__main__":
    unittest.main()
