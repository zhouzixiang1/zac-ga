import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "experiments_v2/expand_default_initial_study.py"
SPEC = importlib.util.spec_from_file_location("expand_default_initial", PATH)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class ExpansionTests(unittest.TestCase):
    def test_reverse_subset_and_skip_claims(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            receipts = root / "quality/receipts"
            receipts.mkdir(parents=True)
            (receipts / "b.started.json").write_text("{}")
            (receipts / "a.json").write_text("{}")
            protocol = {"jobs": [{"job_id": j, "origin": "fresh"} for j in "abcde"]}
            rows = MOD.select_reverse(protocol, {"selected_jobs": list("abcd")}, root)
            self.assertEqual([j["job_id"] for j in rows], ["d", "c"])

    def test_rejects_unregistered_jobs(self):
        with self.assertRaises(ValueError):
            MOD.select_reverse({"jobs": []}, {"selected_jobs": ["unknown"]}, Path("/not-used"))

    def test_memory_pressure_parser(self):
        with patch.object(MOD.subprocess, "check_output", return_value="System-wide memory free percentage: 58%"):
            self.assertEqual(MOD.pressure_free_percent(), 58)

    def test_unknown_memory_does_not_admit_work(self):
        with patch.object(MOD.subprocess, "check_output", return_value="unavailable"):
            with self.assertRaises(ValueError):
                MOD.pressure_free_percent()

    def test_transient_incomplete_receipt_is_read_again(self):
        with patch.object(MOD.BASE, "read", side_effect=[MOD.json.JSONDecodeError("partial", "{", 1), {"status": "success"}]), patch.object(MOD.time, "sleep"):
            self.assertEqual(MOD.read_complete("unused"), {"status": "success"})

    def test_invalid_receipt_fails_closed(self):
        with patch.object(MOD.BASE, "read", side_effect=MOD.json.JSONDecodeError("partial", "{", 1)), patch.object(MOD.time, "sleep"):
            with self.assertRaises(MOD.json.JSONDecodeError):
                MOD.read_complete("unused")


if __name__ == "__main__":
    unittest.main()
