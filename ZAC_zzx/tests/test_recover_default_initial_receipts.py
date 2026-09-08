"""Recovery refuses evidence drift and never fabricates observed exit/timing."""
from copy import deepcopy
import gzip
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "experiments_v2/recover_default_initial_receipts.py"
SPEC = importlib.util.spec_from_file_location("recover_default_initial_receipts", PATH)
RECOVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVER)


class RecoveryGuardsTests(unittest.TestCase):
    def test_recovery_lock_excludes_concurrent_recoverers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with RECOVER.recovery_lock(root):
                with self.assertRaises(BlockingIOError):
                    with RECOVER.recovery_lock(root):
                        self.fail("a second recovery writer acquired the lock")

    def test_live_worker_blocks_exact_target_only(self):
        target = RECOVER.JOB_IDS[0]
        lines = (f"999998 /python -B /repo/default_initial_study.py --worker {target} --phase quality\n"
                 "999997 /python -B /repo/default_initial_study.py --worker another-s0\n"
                 f"999996 /python /repo/recover_default_initial_receipts.py --label {target}\n")
        self.assertEqual([row["pid"] for row in RECOVER.live_target_processes(lines)], [999998])
        with patch.object(RECOVER.subprocess, "run") as run:
            run.return_value.stdout = lines
            with self.assertRaisesRegex(ValueError, "still alive"):
                RECOVER.assert_no_workers()

    def test_existing_terminal_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "quality/receipts").mkdir(parents=True)
            terminal = root / "quality/receipts" / (RECOVER.JOB_IDS[0] + ".json")
            RECOVER.write_new(terminal, {"status": "success"})
            original = terminal.read_bytes()
            with self.assertRaisesRegex(ValueError, "already exists"):
                RECOVER.assert_missing_terminals(root)
            with self.assertRaises(FileExistsError):
                RECOVER.write_new(terminal, {"status": "recovered_success"})
            self.assertEqual(terminal.read_bytes(), original)

    def test_observed_exit_and_timing_remain_unknown(self):
        row = {"job_id": RECOVER.JOB_IDS[0], "result": {"path": "result", "sha256": "a" * 64},
               "started_receipt": {"path": "started", "sha256": "b" * 64},
               "quality_classification": "valid_fidelity"}
        receipt = RECOVER.make_receipt(row, {"path": "audit", "sha256": "c" * 64})
        self.assertEqual(receipt["status"], "recovered_success")
        for key in ("started_at", "returncode", "elapsed_s", "ended_at", "peak_observed_rss_bytes"):
            self.assertIsNone(receipt[key])
        self.assertTrue(receipt["recovery"]["supervision_gap"])
        self.assertFalse(receipt["recovery"]["returncode_observed"])
        self.assertFalse(receipt["recovery"]["timing_eligible"])
        self.assertEqual(receipt["recovery"]["exit_reason"], "unknown")

    def test_pin_drift_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            RECOVER.write_new(path, {"value": 1})
            reference = RECOVER.pin(path)
            reference["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "hash changed"):
                RECOVER.verify_pin(reference)


class FrozenRecoveryReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protocol, cls.source_checks = RECOVER.load_protocol()
        try:
            cls.replayers = RECOVER.load_replayers(cls.protocol)
        except ValueError as error:
            raise unittest.SkipTest(str(error))
        cls.architecture = RECOVER.read_json(cls.protocol["architecture"]["path"])
        cls.job = next(j for j in cls.protocol["jobs"] if j["job_id"] == RECOVER.JOB_IDS[-1])
        root = RECOVER.ROOT / "quality/jobs" / cls.job["job_id"]
        cls.result = RECOVER.read_json(root / "result.json")
        cls.trace = json.loads(gzip.decompress((root / "native_trace.json.gz").read_bytes()))

    def replay(self, result):
        return RECOVER.validate_quality(self.protocol, self.job, result, self.trace,
                                        self.replayers, self.architecture)

    def recovery_scope_snapshot(self):
        # Other independently authorized circuit workers may run concurrently;
        # compare only this recovery's four identities and recovery artifacts.
        paths = list((RECOVER.ROOT / "recovery").glob("*"))
        for job_id in RECOVER.JOB_IDS:
            paths += list((RECOVER.ROOT / "quality/jobs" / job_id).glob("*"))
            paths += list((RECOVER.ROOT / "quality/receipts").glob(job_id + ".*"))
        return {str(path): RECOVER.digest(path) for path in paths if path.is_file()}

    def test_complete_four_job_readonly_audit(self):
        before = self.recovery_scope_snapshot()
        report = RECOVER.audit()
        self.assertEqual(tuple(row["job_id"] for row in report["jobs"]), RECOVER.JOB_IDS)
        self.assertEqual(report["source_checks"]["frozen_source_file_count"], 113)
        for row in report["jobs"]:
            self.assertEqual(set(row["checks"]), RECOVER.CHECK_NAMES)
            self.assertTrue(all(value is True for value in row["checks"].values()))
        self.assertEqual(self.recovery_scope_snapshot(), before)

    def test_direct_cli_defaults_to_readonly(self):
        before = self.recovery_scope_snapshot()
        completed = subprocess.run([sys.executable, "-B", str(PATH)], cwd=RECOVER.REPO,
                                   capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(completed.stdout)["audit_type"], "interrupted_worker_quality_recovery")
        self.assertEqual(self.recovery_scope_snapshot(), before)

    def test_score_tampering_rejected(self):
        result = deepcopy(self.result)
        result["score"]["fidelity"] *= 1.01
        with self.assertRaisesRegex(ValueError, "score_exact"):
            self.replay(result)

    def test_recorded_logical_hash_tampering_rejected(self):
        result = deepcopy(self.result)
        result["logical_validation"]["compiled_layer_ledger_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "logical_exact"):
            self.replay(result)

    def test_physical_or_selected_mapping_tampering_rejected(self):
        for field in ("physics", "mapping"):
            result = deepcopy(self.result)
            if field == "physics":
                result["validation"]["ghost_hits"] = 1
            else:
                result["selected_mapping_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "physics_exact|selected_mapping_sha"):
                self.replay(result)

    def test_stdout_result_pin_tampering_rejected(self):
        original_read = RECOVER.read_json
        def altered_read(path):
            value = original_read(path)
            if str(path).endswith(self.job["job_id"] + ".log"):
                value["result"]["sha256"] = "0" * 64
            return value
        with patch.object(RECOVER, "read_json", side_effect=altered_read):
            with self.assertRaisesRegex(ValueError, "stdout result"):
                RECOVER.audit_job(RECOVER.ROOT, self.protocol, self.job, self.replayers, self.architecture)


if __name__ == "__main__":
    unittest.main()
