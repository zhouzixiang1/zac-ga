"""Regressions for additive, cohort-aligned loss-component presentation."""
import copy
import json
from pathlib import Path
import unittest

import generate_loss_decomposition as decomposition
from paper_paths import artifact_path


class LossDecompositionTests(unittest.TestCase):
    def fixture(self):
        units, runs = [], []
        metadata = {"datasets": {}}
        for dataset in decomposition.DATASETS:
            # Component medians sum to -8, while the median-logF run sums to -7.
            components = ((-1., -5., -4.), (-4., -1., -2.), (-2., -2., -1.))
            digest = ("a" if dataset == "zac18" else "b") * 64
            for seed, values in enumerate(components):
                runs.append({"dataset": dataset, "canonical_sha256": digest, "seed": seed,
                             "status": "success", "fidelity_valid": True, "fidelity_ood": False,
                             "metrics": {**dict(zip(decomposition.FACTORS, values)),
                                         "log_fidelity": sum(values)}})
            baseline = dict(zip(decomposition.FACTORS, (-3., -3., -3.)), log_fidelity=-9.)
            units.append({"dataset": dataset, "canonical_sha256": digest, "member_N": 2,
                          "circuits": ["alias_a", "alias_b"], "M1": baseline, "M2": baseline,
                          "Default": {"log_fidelity": -7.}})
            metadata["datasets"][dataset] = {"common_canonical_N": 1, "methods": {
                "Default": {"log_fidelity_mean": -7.}, "M1": {"log_fidelity_mean": -9.},
                "M2": {"log_fidelity_mean": -9.}}}
        return units, runs, metadata

    def test_uses_one_real_median_run_not_component_medians(self):
        rows, provenance = decomposition.derive(*self.fixture())
        self.assertEqual([row["seed"] for row in provenance["selected_runs"]], [1, 1])
        self.assertEqual(rows[0]["transfer"], -1.)
        self.assertEqual(rows[0]["excitation"], 2.)
        self.assertEqual(rows[0]["coherence"], 1.)
        self.assertEqual(rows[0]["net"], 2.)

    def test_alias_members_count_once_and_duplicate_unit_is_rejected(self):
        units, runs, metadata = self.fixture()
        rows, provenance = decomposition.derive(units, runs, metadata)
        self.assertTrue(all(row["N"] == 1 for row in rows))
        self.assertEqual(len(provenance["selected_runs"]), 2)
        with self.assertRaisesRegex(ValueError, "Duplicate canonical"):
            decomposition.derive(units + [copy.deepcopy(units[0])], runs, metadata)

    def test_missing_seed_and_mismatched_median_are_rejected(self):
        units, runs, metadata = self.fixture()
        with self.assertRaisesRegex(ValueError, "exactly seeds"):
            decomposition.derive(units, runs[1:], metadata)
        units[0]["Default"]["log_fidelity"] = -6.
        with self.assertRaisesRegex(ValueError, "Median logF does not match"):
            decomposition.derive(units, runs, metadata)

    def test_factor_or_main_aggregate_mismatch_is_rejected(self):
        units, runs, metadata = self.fixture()
        broken = copy.deepcopy(runs)
        broken[1]["metrics"]["log_atom_transfer"] += .5
        with self.assertRaisesRegex(ValueError, "factors do not add"):
            decomposition.derive(units, broken, metadata)
        metadata["datasets"]["zac18"]["methods"]["Default"]["log_fidelity_mean"] += .1
        with self.assertRaisesRegex(ValueError, "main-table aggregate"):
            decomposition.derive(units, runs, metadata)

    def test_publication_outputs_recompute_and_match_main_table(self):
        outputs = decomposition.generate(decomposition.PAPER_ROOT, decomposition.PAPER_ROOT.parent)
        self.assertEqual(set(outputs), {
            artifact_path(decomposition.PAPER_ROOT, "figures/data/physical_ga_loss_decomposition.dat"),
            artifact_path(decomposition.PAPER_ROOT, "figures/data/physical_ga_loss_decomposition.provenance.json"),
        })
        for path, content in outputs.items():
            self.assertEqual(path.read_text(), content, str(path))
        provenance = json.loads(next(content for path, content in outputs.items() if path.suffix == ".json"))
        self.assertEqual(len(provenance["selected_runs"]), 135)
        self.assertEqual([row["N"] for row in provenance["rows"]], [16, 16, 119, 119])
        for check in provenance["aggregate_identity_checks"]:
            self.assertLess(check["component_sum_error"], 1e-12)
            self.assertLess(check["paired_mean_error"], 1e-12)


if __name__ == "__main__":
    unittest.main()
