"""Read-only evidence and classification tests; never launch the compiler."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / "experiments_v2/horizon_extension_v2_continuation.py"
SPEC = importlib.util.spec_from_file_location("horizon_cont_test", PATH)
continuation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(continuation)


class HorizonContinuationTests(unittest.TestCase):
    def fixture(self):
        manifest = {"status": "success", "verifier_ok": True, "ghost_hits": 0,
                    "fidelity": None, "log_fidelity": None, "fidelity_ood": True}
        scorer = {"result": {"ood": True, "fidelity": None, "log_fidelity": None,
                   "components": {"coherence_linear": {"fidelity": None, "log_fidelity": None}},
                   "warnings": ["linear coherence model out of domain for atoms: 0"]}}
        return manifest, scorer

    def test_explicit_original_ood_is_not_numerical_success(self):
        m, s = self.fixture()
        self.assertEqual(continuation.classification(m, s), "model_out_of_domain")
        self.assertIsNone(m["fidelity"])

    def test_physics_gate_not_relaxed_for_ood(self):
        for update in ({"verifier_ok": False}, {"ghost_hits": 1}):
            m, s = self.fixture(); m.update(update)
            with self.assertRaisesRegex(ValueError, "physical"):
                continuation.classification(m, s)

    def test_fake_or_unexplained_ood_rejected(self):
        for field, value in (("warnings", []), ("fidelity", .1), ("ood", False), ("log_fidelity", -1)):
            m, s = self.fixture(); s["result"][field] = value
            with self.assertRaises(ValueError):
                continuation.classification(m, s)

    def test_in_domain_requires_positive_finite_fidelity(self):
        m, s = self.fixture()
        m.update(fidelity=.2, fidelity_ood=False)
        s["result"].update(fidelity=.2, ood=False)
        self.assertEqual(continuation.classification(m, s), "valid_fidelity")
        for value in (0, -1, 1.1, float("nan")):
            m["fidelity"] = s["result"]["fidelity"] = value
            with self.assertRaises(ValueError):
                continuation.classification(m, s)

    def test_raw_execution_failure_stays_failure(self):
        m, s = self.fixture(); m["status"] = "verifier_fail"
        self.assertEqual(continuation.classification(m, s), "execution_failure")

    def test_sealed_base_and_prior_23_are_untouched(self):
        module = continuation.base_module()
        base = module.read(continuation.BASE / "protocol.json")
        receipts = list((continuation.BASE / "receipts/quality").glob("*.json"))
        self.assertEqual(len(receipts), 23)
        self.assertEqual(sum(module.read(p)["status"] == "runner_error" for p in receipts), 2)
        self.assertEqual(len(base["quality_jobs"])-len(receipts), 61)

    def test_actual_two_ood_outputs_pass_unchanged_physics_rule(self):
        module = continuation.base_module()
        base = module.read(continuation.BASE / "protocol.json")
        for job_id in ("qmap154-clip_206-h1-s0", "qmap154-hwb8_113-h1-s2"):
            job = next(j for j in base["quality_jobs"] if j["job_id"] == job_id)
            path = next((continuation.BASE / "runs/quality" / job_id).glob("*/manifest.json"))
            m, scorer, kind = continuation.check_manifest(module, base, job, path)
            self.assertEqual(kind, "model_out_of_domain")
            self.assertTrue(m["verifier_ok"])
            self.assertEqual(m["ghost_hits"], 0)


if __name__ == "__main__":
    unittest.main()
