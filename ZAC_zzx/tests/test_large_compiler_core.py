"""End-to-end tests for the executable bounded-memory Large compiler."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.checkpoint import EventStreamWriter  # noqa: E402
from streaming.large_compiler import compile_large_streaming  # noqa: E402
from streaming.qasm_sqlite import build_layer_store  # noqa: E402


CHAIN_QASM = '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[20];
u3(0,0,0) q[4];
cz q[0],q[1];
cz q[1],q[2];
cz q[2],q[3];
cz q[0],q[3];
u1(0) q[4];
'''


class TestLargeStreamingCompiler(unittest.TestCase):
    def _workspace(self, directory: str) -> tuple[Path, Path]:
        base = Path(directory)
        qasm = base / "chain.qasm"
        store = base / "chain.sqlite"
        qasm.write_text(CHAIN_QASM, encoding="utf-8")
        build_layer_store(qasm, store)
        return base, store

    @staticmethod
    def _config(base: Path, method: str, *, stop: int | None = None) -> Path:
        source = {
            "M1": ROOT / "exp_setting" / "zac_m1_v2.json",
            "M2": ROOT / "exp_setting" / "iccad_m2_v2.json",
            "M3": ROOT / "exp_setting" / "ours_nl_v2.json",
            "M4": ROOT / "exp_setting" / "ours_lk_v2.json",
        }[method]
        value = json.loads(source.read_text(encoding="utf-8"))
        if stop is not None:
            value["large_runtime"] = {"max_layers_per_invocation": stop}
        path = base / f"{method.lower()}-config.json"
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path

    def test_toy_all_methods_are_strictly_valid_and_policy_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            base, store = self._workspace(directory)
            architecture = ROOT / "hardware_spec" / "toy_architecture.json"
            reports = {}
            for method in ("M1", "M2", "M3", "M4"):
                reports[method] = compile_large_streaming(
                    store,
                    architecture,
                    self._config(base, method),
                    method,
                    base / f"out-{method}",
                )
                validation = reports[method]["result"]["validation"]
                self.assertEqual(reports[method]["status"], "success")
                self.assertFalse(reports[method]["support_claim_eligible"])
                self.assertEqual(
                    reports[method]["implementation_status"],
                    "development_streaming_proxy_v1",
                )
                self.assertTrue(validation["ok"])
                self.assertEqual(validation["ghost_hits"], 0)
                self.assertEqual(validation["one_qubit_gates"], 2)
                self.assertEqual(validation["two_qubit_gates"], 4)

            # M1 eagerly returns; M2 can retain only into an adjacent CZ layer.
            self.assertGreater(
                reports["M1"]["compiler_stats"]["move_batches_generated"],
                reports["M2"]["compiler_stats"]["move_batches_generated"],
            )
            # q0 is reused at CZ layer 3.  It is outside NL's target-only view
            # at layer 0 but inside LK's L..L+3 window, so the two frozen ours
            # configs make a genuinely different bounded residency decision.
            self.assertGreater(
                reports["M4"]["compiler_stats"]["stay_decisions"],
                reports["M3"]["compiler_stats"]["stay_decisions"],
            )
            self.assertNotEqual(reports["M4"]["event_hash"], reports["M3"]["event_hash"])

    def test_checkpoint_resume_is_eventwise_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            base, store = self._workspace(directory)
            architecture = ROOT / "hardware_spec" / "toy_architecture.json"
            full_dir = base / "full"
            resumed_dir = base / "resumed"
            full = compile_large_streaming(
                store,
                architecture,
                self._config(base, "M1"),
                "M1",
                full_dir,
            )
            partial_config = self._config(base, "M1", stop=1)
            resumed = compile_large_streaming(
                store, architecture, partial_config, "M1", resumed_dir)
            invocations = 1
            while resumed["status"] == "checkpointed":
                resumed = compile_large_streaming(
                    store,
                    architecture,
                    partial_config,
                    "M1",
                    resumed_dir,
                    resume_from=resumed_dir / "checkpoint.json",
                )
                invocations += 1
                self.assertLess(invocations, 20)
            self.assertEqual(resumed["status"], "success")

            full_events = list(EventStreamWriter.read(full_dir / "canonical_trace.jsonl.gz"))
            resumed_events = list(EventStreamWriter.read(
                resumed_dir / "canonical_trace.jsonl.gz"))
            self.assertEqual(resumed_events, full_events)
            self.assertEqual(resumed["event_hash"], full["event_hash"])
            self.assertEqual(resumed["result"], full["result"])


if __name__ == "__main__":
    unittest.main()
