"""No compiler runs: identity, resumption and quality-domain protection."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "experiments_v2/horizon_extension_v2.py"
SPEC = importlib.util.spec_from_file_location("horizon_v2_test_target", PATH)
v2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v2)


class HorizonV2Tests(unittest.TestCase):
    def job(self):
        return dict(dataset="zac18", circuit="test", horizon=1, seed=0, repetition=0, job_id="test-h1-s0")

    def temporary(self, root):
        v2.write_new(root / "protocol.json", {})
        return v2.digest(root / "protocol.json")

    def test_previous_helper_identity_and_missing_matrix(self):
        helper = v2.legacy()
        self.assertEqual(helper.REPO, v2.REPO)
        self.assertTrue((helper.ACCEPTED / "final_manifest.json").is_file())
        previous = v2.checked(v2.PREVIOUS / "protocol.json", v2.PREVIOUS_SHA)
        self.assertEqual(helper.missing_quality_jobs(previous["circuits"]), previous["quality_jobs"])
        self.assertEqual(len(previous["quality_jobs"]), 84)
        self.assertEqual(len(previous["reused_quality"]), 96)

    def test_smoke_is_deterministic_inside_existing_plan(self):
        old = v2.checked(v2.PREVIOUS / "protocol.json", v2.PREVIOUS_SHA)
        smoke = v2.choose_smoke(old["circuits"], old["quality_jobs"])
        self.assertEqual(len(smoke), 2)
        self.assertEqual(smoke, ["zac18-ghz_n23-h1-s0", "qmap154-4gt11_84-h1-s0"])
        self.assertTrue(set(smoke) <= {j["job_id"] for j in old["quality_jobs"]})

    def test_write_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            v2.write_new(path, {"first": True})
            with self.assertRaises(FileExistsError):
                v2.write_new(path, {"first": False})
            self.assertEqual(v2.read(path), {"first": True})

    def test_claim_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(v2, "ROOT", Path(directory)):
            self.temporary(v2.ROOT)
            job = self.job()
            self.assertIsNone(v2.receipt_state({}, job))
            v2.claim_job(job)
            self.assertEqual(v2.receipt_state({}, job)["status"], "interrupted_without_receipt")
            with self.assertRaises(FileExistsError):
                v2.claim_job(job)

    def test_failure_receipt_is_retained(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(v2, "ROOT", Path(directory)):
            sha = self.temporary(v2.ROOT)
            job = self.job()
            v2.write_new(v2.ROOT / "receipts/quality/test-h1-s0.json",
                         {**job, "status": "runner_error", "protocol_sha256": sha})
            self.assertEqual(v2.receipt_state({}, job)["status"], "runner_error")

    def test_success_requires_manifest(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(v2, "ROOT", Path(directory)):
            sha = self.temporary(v2.ROOT)
            v2.write_new(v2.ROOT / "receipts/quality/test-h1-s0.json",
                         {**self.job(), "status": "success", "protocol_sha256": sha})
            with self.assertRaisesRegex(ValueError, "lacks"):
                v2.receipt_state({}, self.job())

    def test_receipt_drift_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(v2, "ROOT", Path(directory)):
            sha = self.temporary(v2.ROOT)
            v2.write_new(v2.ROOT / "receipts/quality/test-h1-s0.json",
                         {**self.job(), "seed": 2, "status": "timeout", "protocol_sha256": sha})
            with self.assertRaisesRegex(ValueError, "identity"):
                v2.receipt_state({}, self.job())

    def test_quality_domain_and_failed_output_not_accepted(self):
        good = {"status": "success", "fidelity": .5, "ghost_hits": 0, "fidelity_ood": False}
        self.assertTrue(v2.record_valid(good))
        for change in ({"status": "verifier_fail"}, {"fidelity": None}, {"fidelity": 0},
                       {"fidelity": 1.01}, {"fidelity": float("nan")}, {"ghost_hits": 1}, {"fidelity_ood": True}):
            with self.subTest(change=change):
                self.assertFalse(v2.record_valid({**good, **change}))

    def test_more_than_two_workers_refused_before_freeze(self):
        with self.assertRaisesRegex(ValueError, "two"):
            v2.seal(3)

    def test_execution_lock_refuses_second_dispatcher(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(v2, "ROOT", Path(directory)):
            with v2.execution_lock(), self.assertRaisesRegex(RuntimeError, "dispatcher"):
                with v2.execution_lock():
                    self.fail("second dispatcher acquired the lock")

    def test_no_timing_execution_interface(self):
        with self.assertRaises(SystemExit):
            v2.main(["--timing"])

    def summary(self, pending=0):
        return {"new_status_counts": {}, "new_jobs_pending": pending,
                "complete_circuits": 0, "fidelity_ratio_vs_h8": {}}

    def test_systemic_failure_stops_dispatch_at_two_in_flight(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(v2, "ROOT", Path(directory)):
            self.temporary(v2.ROOT)
            jobs = [{**self.job(), "job_id": f"test-{i}"} for i in range(4)]
            protocol = {"quality_jobs": jobs, "quality_workers": 2,
                        "smoke_job_ids": [j["job_id"] for j in jobs]}
            helper = SimpleNamespace(bootstrap=lambda _: None,
                run_one=lambda p, j, phase: {**j, "status": "runner_error"})
            with patch.object(v2, "legacy", return_value=helper), patch.object(v2, "dependency_probe"), \
                    patch.object(v2, "summarize", return_value=self.summary(2)):
                v2.execute(protocol, smoke=True)
            self.assertEqual(len(list((v2.ROOT / "claims/quality").glob("*.json"))), 2)
            self.assertFalse((v2.ROOT / "claims/quality/test-2.json").exists())

    def test_successful_smoke_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(v2, "ROOT", Path(directory)):
            sha = self.temporary(v2.ROOT)
            job = self.job()
            path = v2.ROOT / "manifest.json"
            v2.write_new(path, {**job, "status": "success"})
            v2.write_new(v2.ROOT / "receipts/quality/test-h1-s0.json",
                         {**job, "status": "success", "protocol_sha256": sha,
                          "manifest": str(path), "manifest_sha256": v2.digest(path)})
            helper = SimpleNamespace(bootstrap=lambda _: None)
            protocol = {"quality_jobs": [job], "quality_workers": 2, "smoke_job_ids": [job["job_id"]]}
            with patch.object(v2, "legacy", return_value=helper), patch.object(v2, "dependency_probe"), \
                    patch.object(v2, "summarize", return_value=self.summary()):
                v2.execute(protocol, smoke=True)
            self.assertFalse((v2.ROOT / "claims").exists())


if __name__ == "__main__":
    unittest.main()
