from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments_v2.initial_lookahead_extension import (DYNAMIC_REPORT, DYNAMIC_TABLE,
    EXPECTED_NEW, PILOT, cohort_definition, protocol_payload, run_job)
from experiments_v2.initial_lookahead_runner import logical_trace_receipt, source_snapshot


class InitialLookaheadExtensionTests(unittest.TestCase):
    def test_exact_missing_cohort_retains_prior_exclusions_and_timeout(self):
        with DYNAMIC_TABLE.open() as stream:
            rows = list(csv.DictReader(stream))
        cohort = cohort_definition(rows, json.loads(DYNAMIC_REPORT.read_text()),
                                   json.loads((PILOT / "protocol.json").read_text()))
        self.assertEqual(set(cohort["new"]), EXPECTED_NEW)
        self.assertEqual(len(cohort["target"]), 39)
        self.assertEqual(len(cohort["reused"]), 7)
        self.assertIn("ising_n42", cohort["reused"])
        self.assertEqual(cohort["pilot_outside_target"], ["cm82a_208", "ex1_226"])
        self.assertEqual(len(cohort["excluded"]), 9)

    def test_source_snapshot_includes_untracked_module_and_test(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("ZAC_zzx/experiments_v2/untracked.py", "ZAC_zzx/tests/untracked_test.py"):
                file = root / name; file.parent.mkdir(parents=True, exist_ok=True); file.write_text("x=1\n")
            responses = [SimpleNamespace(stdout=b""), SimpleNamespace(stdout="?? untracked.py\n"),
                         SimpleNamespace(stdout="abcd\n")]
            with patch("experiments_v2.initial_lookahead_runner.REPO", root), patch(
                    "experiments_v2.initial_lookahead_runner.subprocess.run", side_effect=responses):
                snapshot = source_snapshot()
            self.assertEqual(len(snapshot["source_files"]), 2)
            self.assertTrue(snapshot["untracked_python_included"])

    def test_invalid_worker_count_fails_before_execution(self):
        with self.assertRaises(ValueError):
            protocol_payload(4)

    def test_logical_order_check_catches_same_counts_wrong_partner(self):
        with tempfile.TemporaryDirectory() as temp:
            qasm = Path(temp) / "input.qasm"
            qasm.write_text("u3(0,0,0) q[0];\ncz q[0],q[1];\n")
            events = [SimpleNamespace(kind="one_qubit_gate", atoms=[0]),
                      SimpleNamespace(kind="two_qubit_gate", gate_pairs=[(0, 1)])]
            self.assertTrue(logical_trace_receipt(qasm, 3, events, [[(0, 1)]])["ok"])
            events[1].gate_pairs = [(0, 2)]
            with self.assertRaisesRegex(ValueError, "operation-sequence"):
                logical_trace_receipt(qasm, 3, events, [[(0, 2)]])

    def settings(self):
        return {"config_path": "config.json", "architecture_path": "arch.json", "workers": 3,
                "timeout_per_paired_job_s": 600, "max_rss_per_job_bytes": 3 * 1024**3,
                "environment": {"OMP_NUM_THREADS": "1"}}

    def test_parallel_workers_are_isolated_subprocesses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "receipts").mkdir()
            barrier = threading.Barrier(3); calls = []
            class FinishedProcess:
                def __init__(self, command, **kwargs):
                    calls.append((command, kwargs)); barrier.wait(timeout=3)
                def poll(self): return 0
            jobs = [{"job_id": f"c-s{s}", "circuit": "c", "seed": s} for s in (0, 1, 2)]
            with patch("experiments_v2.initial_lookahead_extension.subprocess.Popen", FinishedProcess):
                with ThreadPoolExecutor(max_workers=3) as pool:
                    outcomes = list(pool.map(lambda job: run_job(root, self.settings(), job,
                        {"c": "input.qasm"}, threading.Event()), jobs))
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(value["status"] == "success" for value in outcomes))
            self.assertTrue(all(kwargs["start_new_session"] and kwargs["env"]["OMP_NUM_THREADS"] == "1"
                                for _, kwargs in calls))
            self.assertEqual(len(list((root / "receipts").glob("*.json"))), 3)

    def test_deadline_kills_only_owned_process_and_records_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "receipts").mkdir()
            process = SimpleNamespace(pid=1234, returncode=-9, poll=lambda: None, wait=lambda: -9)
            with patch("experiments_v2.initial_lookahead_extension.subprocess.Popen", return_value=process), patch(
                    "experiments_v2.initial_lookahead_extension.resident_bytes", return_value=0), patch(
                    "experiments_v2.initial_lookahead_extension.time.monotonic", side_effect=[0, 601, 602]), patch(
                    "experiments_v2.initial_lookahead_extension.os.killpg") as kill:
                receipt = run_job(root, self.settings(), {"circuit": "c", "seed": 0, "job_id": "c-s0"},
                                  {"c": "input.qasm"}, threading.Event())
            self.assertEqual(receipt["status"], "timeout")
            self.assertEqual(kill.call_args.args[0], 1234)

    def test_memory_limit_is_retained_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "receipts").mkdir()
            process = SimpleNamespace(pid=1234, returncode=-9, poll=lambda: None, wait=lambda: -9)
            with patch("experiments_v2.initial_lookahead_extension.subprocess.Popen", return_value=process) as launch, patch(
                    "experiments_v2.initial_lookahead_extension.resident_bytes", return_value=4 * 1024**3), patch(
                    "experiments_v2.initial_lookahead_extension.os.killpg"):
                receipt = run_job(root, self.settings(), {"circuit": "c", "seed": 0, "job_id": "c-s0"},
                                  {"c": "input.qasm"}, threading.Event())
            self.assertEqual(receipt["status"], "memory_limit")
            launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
