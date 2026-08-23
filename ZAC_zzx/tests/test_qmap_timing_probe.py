from __future__ import annotations

import sys
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.qmap_timing_probe import verify_interpreters  # noqa: E402


class QmapTimingProbeTests(unittest.TestCase):
    @mock.patch("experiments_v2.qmap_timing_probe._run_interpreter")
    def test_parity_requires_identical_native_and_timing_fields(self, run):
        official = {"native_sha256": "a", "stats": {}, "mqt_qmap_version": "3.2.0"}
        timing = {
            "native_sha256": "a", "mqt_qmap_version": "timing",
            "stats": {"initialPlacementTime": 1, "layerPlacementTime": 2,
                      "reuseAnalysisTime": 3, "placementTime": 4,
                      "routingTime": 5}}
        run.side_effect = [official, timing]
        result = verify_interpreters(
            Path("official"), Path("timing"), [Path("toy.qasm")],
            Path("arch.json"), Path("config.json"))
        self.assertTrue(result["valid"])
        self.assertEqual(result["circuits"][0]["transition_decision_us"], 5)

    @mock.patch("experiments_v2.qmap_timing_probe._run_interpreter")
    def test_parity_fails_on_output_drift(self, run):
        run.side_effect = [
            {"native_sha256": "a", "stats": {}},
            {"native_sha256": "b", "stats": {
                "initialPlacementTime": 1, "layerPlacementTime": 2,
                "reuseAnalysisTime": 3, "placementTime": 4, "routingTime": 5}},
        ]
        result = verify_interpreters(
            Path("official"), Path("timing"), [Path("toy.qasm")],
            Path("arch.json"), Path("config.json"))
        self.assertFalse(result["valid"])

    def test_frozen_timing_wheel_matches_parity_lock_and_installed_binaries(self):
        worktree = ROOT.parent
        python = (worktree.parent / "artifacts" / "native-ga-v1" / "build" /
                  "qmap-timing-venv" / "bin" / "python")
        self.assertTrue(python.is_file(), "frozen QMAP timing venv is missing")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        completed = subprocess.run(
            [str(python), "-c",
             "import json; from experiments_v2.qmap_timing_provenance "
             "import validate_qmap_timing_freeze; "
             "print(json.dumps(validate_qmap_timing_freeze(), sort_keys=True))"],
            cwd=worktree, env=environment, text=True,
            capture_output=True, check=True, timeout=30)
        evidence = json.loads(completed.stdout)
        self.assertEqual(
            evidence["wheel_sha256"],
            "bc7ff59115e66ad4993bab58a3abe1413173e1c3ca2ccffa2435859be0c53937")
        self.assertEqual(len(evidence["parity_circuits"]), 6)
        self.assertTrue(evidence["installed_binary_sha256"])


if __name__ == "__main__":
    unittest.main()
