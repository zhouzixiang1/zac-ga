import importlib.util
from pathlib import Path
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / "experiments_v2/resume_default_initial_study.py"
SPEC = importlib.util.spec_from_file_location("resume_default_initial_study", PATH)
RESUME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESUME)


class ResumeDefaultInitialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.receipts = self.root / "quality/receipts"
        self.receipts.mkdir(parents=True)
        self.protocol = {"jobs": [{"job_id": "new", "origin": "fresh"},
                                   {"job_id": "old", "origin": "reused"}]}

    def test_selects_only_new_fresh_jobs(self):
        selected, unresolved = RESUME.select_unstarted(self.protocol, self.root)
        self.assertEqual([j["job_id"] for j in selected], ["new"])
        self.assertEqual(unresolved, [])

    def test_does_not_retry_any_terminal_outcome(self):
        for status in ("success", "timeout", "memory_limit", "runner_error", "recovered_success"):
            (self.receipts / "new.json").write_text('{"status":"' + status + '"}')
            self.assertEqual(RESUME.select_unstarted(self.protocol, self.root), ([], []))

    def test_preserves_unresolved_claim(self):
        claim = self.receipts / "new.started.json"
        claim.write_text("{}")
        self.assertEqual(RESUME.select_unstarted(self.protocol, self.root), ([], ["new"]))
        self.assertEqual(claim.read_text(), "{}")

    def test_rejects_unclaimed_output(self):
        (self.root / "quality/jobs/new").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "unclaimed output"):
            RESUME.select_unstarted(self.protocol, self.root)

    def test_state_is_operational_snapshot(self):
        RESUME.state(self.root, status="running", completed=0)
        RESUME.state(self.root, status="finished", completed=1)
        self.assertEqual(RESUME.BASE.read(self.root / "state.json")["completed"], 1)
        self.assertFalse((self.root / "state.json.tmp").exists())

    def test_rejects_external_run_directory(self):
        with self.assertRaises(ValueError):
            RESUME.validate_run_dir(self.root)


if __name__ == "__main__":
    unittest.main()
