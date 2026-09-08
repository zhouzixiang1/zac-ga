from copy import deepcopy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from generate_initial_lookahead_values import load_verified, main, render_tex, validate_documents


class InitialLookaheadValuesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.protocol, cls.summary, cls.values = load_verified(Path(__file__).resolve().parents[2])

    def test_real_sealed_values_and_precision(self):
        tex = render_tex(self.values)
        for expected in (r"\InitStudyN}{9}", r"\InitCompleteN}{8}", r"\InitTimeouts}{1}",
                         r"\InitFidelityGain}{0.02047\%}", r"\InitBatchReduction}{0.00980\%}",
                         r"\InitVsSAGain}{0.03420\%}", r"\InitFidelityGainRounded}{0.02\%}",
                         r"\InitBatchReductionRounded}{0.01\%}", r"\InitVsSAGainRounded}{0.03\%}"):
            self.assertIn(expected, tex)

    def invoke(self, path, *, check=False):
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = main(["--paper-root", str(path)] + (["--check"] if check else []))
        return status, json.loads(stream.getvalue())

    def snapshot(self, path):
        return {str(file.relative_to(path)): (file.read_bytes(), file.stat().st_mtime_ns)
                for file in Path(path).rglob("*") if file.is_file()}

    def test_regenerated_files_pass_check_and_keep_full_precision_json(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            status, _ = self.invoke(path)
            self.assertEqual(status, 0)
            before = self.snapshot(path)
            with patch.object(Path, "write_text", side_effect=AssertionError("check attempted a write")), patch.object(
                    Path, "mkdir", side_effect=AssertionError("check attempted mkdir")):
                status, receipt = self.invoke(path, check=True)
            self.assertEqual(status, 0)
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(before, self.snapshot(path))
            payload = json.loads((path / "paper_initial_lookahead_values.json").read_text())
            self.assertEqual(payload["values"], self.values)
            self.assertEqual(payload["display_percent_values"]["InitFidelityGainRounded"], "0.02")
            export = json.loads((path / "paper_export_provenance.json").read_text())
            for filename, expected_hash in export["derived_file_sha256"].items():
                self.assertEqual(hashlib.sha256((path / filename).read_bytes()).hexdigest(), expected_hash)

    def test_stale_macro_fails_without_repair_or_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            self.invoke(path)
            tex = path / "initial_lookahead_values.tex"
            tex.write_text(tex.read_text().replace(r"\InitFidelityGainRounded}{0.02\%}",
                                                  r"\InitFidelityGainRounded}{2.00\%}"))
            before = self.snapshot(path)
            with patch.object(Path, "write_text", side_effect=AssertionError("check attempted repair")):
                status, receipt = self.invoke(path, check=True)
            self.assertNotEqual(status, 0)
            self.assertIn({"file": tex.name, "reason": "stale"}, receipt["stale_files"])
            self.assertEqual(before, self.snapshot(path))

    def test_stale_json_generator_hash_fails_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            self.invoke(path)
            data = path / "paper_initial_lookahead_values.json"
            payload = json.loads(data.read_text())
            payload["generator_sha256"] = "0" * 64
            data.write_text(json.dumps(payload))
            before = self.snapshot(path)
            status, receipt = self.invoke(path, check=True)
            self.assertNotEqual(status, 0)
            self.assertIn({"file": data.name, "reason": "stale"}, receipt["stale_files"])
            self.assertEqual(before, self.snapshot(path))

    def test_stale_json_full_precision_value_fails_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            self.invoke(path)
            data = path / "paper_initial_lookahead_values.json"
            payload = json.loads(data.read_text())
            payload["values"]["InitFidelityGain"] += .001
            data.write_text(json.dumps(payload))
            before = self.snapshot(path)
            status, _ = self.invoke(path, check=True)
            self.assertNotEqual(status, 0)
            self.assertEqual(before, self.snapshot(path))

    def test_missing_output_directory_is_not_created_by_check(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing"
            with patch.object(Path, "mkdir", side_effect=AssertionError("check attempted mkdir")):
                status, receipt = self.invoke(path, check=True)
            self.assertNotEqual(status, 0)
            self.assertEqual(len(receipt["stale_files"]), 2)
            self.assertFalse(path.exists())

    def test_missing_circuit_rejected(self):
        summary = deepcopy(self.summary)
        summary["per_circuit"].pop(3)
        with self.assertRaises(ValueError):
            validate_documents(self.protocol, summary)

    def test_timeout_cannot_disappear(self):
        summary = deepcopy(self.summary)
        summary["job_statuses"]["timeout"] = 0
        with self.assertRaises(ValueError):
            validate_documents(self.protocol, summary)

    def test_incomplete_circuit_cannot_enter_primary_mean(self):
        summary = deepcopy(self.summary)
        summary["per_circuit"][3]["complete"] = True
        with self.assertRaises(ValueError):
            validate_documents(self.protocol, summary)

    def test_wrong_aggregate_sign_rejected(self):
        summary = deepcopy(self.summary)
        summary["overall"]["comparisons"]["h2_vs_h0"]["move_batches_reduction_percent"] *= -1
        with self.assertRaises(ValueError):
            validate_documents(self.protocol, summary)

    def test_changed_control_rejected(self):
        protocol = deepcopy(self.protocol)
        protocol["dynamic_horizon"] = 2
        with self.assertRaises(ValueError):
            validate_documents(protocol, self.summary)

    def test_wrong_input_hash_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            from generate_initial_lookahead_values import STUDY_RELATIVE
            root = Path(temp) / STUDY_RELATIVE
            root.mkdir(parents=True)
            (root / "protocol.json").write_text("{}")
            with self.assertRaises(ValueError):
                load_verified(Path(temp))


if __name__ == "__main__":
    unittest.main()
