"""End-to-end tests for the formal M1/M3/M4 Large composition layer."""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.formal_large_zac_compiler import (  # noqa: E402
    compile_formal_large_zac,
)
from streaming.qasm_sqlite import build_layer_store  # noqa: E402


QASM = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[6];
u3(0.1,0.2,0.3) q[5];
cz q[0],q[1];
u1(0.4) q[0];
cz q[2],q[3];
u2(0.5,0.6) q[3];
cz q[1],q[4];
u3(0.7,0.8,0.9) q[4];
cz q[0],q[5];
"""


def _setting(name: str) -> dict:
    value = json.loads((ROOT / "exp_setting" / name).read_text())
    return dict(value["zac_setting"][0])


def _jsonl(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="ascii") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _decompressed(path: Path) -> bytes:
    with gzip.open(path, "rb") as handle:
        return handle.read()


class TestFormalLargeZacCompiler(unittest.TestCase):
    def setUp(self):
        self.git_state = patch(
            "streaming.formal_large_zac_compiler._git_state",
            return_value=("a" * 40, False),
        )
        self.git_state.start()
        self.addCleanup(self.git_state.stop)

    def fixture(self, directory: str) -> tuple[Path, Path]:
        base = Path(directory)
        source = base / "input.qasm"
        store = base / "layers.sqlite"
        architecture = base / "architecture.json"
        source.write_text(QASM, encoding="utf-8")
        build_layer_store(source, store)
        spec = json.loads(
            (ROOT / "hardware_spec" / "toy_architecture.json").read_text())
        spec["operation_duration"] = {
            "rydberg": 0.36,
            "1qGate": 52.0,
            "atom_transfer": 15.0,
        }
        architecture.write_text(
            json.dumps(spec, sort_keys=True), encoding="utf-8")
        return store, architecture

    def test_all_formal_zac_methods_compile_validate_and_score(self):
        cases = (
            ("M1", None),
            ("M3", _setting("ours_nl_v2.json")),
            ("M4", _setting("ours_lk_v2.json")),
        )
        with tempfile.TemporaryDirectory() as directory:
            store, architecture = self.fixture(directory)
            for method, setting in cases:
                with self.subTest(method=method):
                    output = Path(directory) / method
                    result = compile_formal_large_zac(
                        method=method,
                        layer_store_path=store,
                        architecture_path=architecture,
                        output_directory=output,
                        setting=setting,
                        progress_every=100,
                    )
                    self.assertTrue(result.full_circuit)
                    self.assertTrue(result.support_claim_eligible)
                    self.assertEqual(result.status, "success")
                    self.assertEqual(result.stages_completed, result.stages_total)
                    self.assertEqual(result.validation["one_qubit_gates"], 4)
                    self.assertEqual(result.validation["two_qubit_gates"], 4)
                    self.assertEqual(result.validation["ghost_hits"], 0)
                    self.assertEqual(result.fidelity["counts"]["one_qubit_gates"], 4)
                    self.assertEqual(result.fidelity["counts"]["two_qubit_gates"], 4)
                    self.assertEqual(len(_jsonl(output / "native.jsonl.gz")),
                                     result.native_instructions)
                    self.assertEqual(len(_jsonl(output / "trace.jsonl.gz")),
                                     result.canonical_events)
                    manifest = json.loads((output / "manifest.json").read_text())
                    self.assertEqual(manifest["event_order"], "dependency")
                    self.assertEqual(
                        manifest["result"]["canonical_chain_sha256"],
                        result.canonical_chain_sha256,
                    )

    def test_prefix_is_valid_but_never_support_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            store, architecture = self.fixture(directory)
            output = Path(directory) / "prefix"
            result = compile_formal_large_zac(
                method="M4",
                layer_store_path=store,
                architecture_path=architecture,
                output_directory=output,
                setting=_setting("ours_lk_v2.json"),
                max_stages=1,
            )
            self.assertFalse(result.full_circuit)
            self.assertFalse(result.support_claim_eligible)
            self.assertEqual(result.stages_completed, 1)
            self.assertEqual(result.validation["ghost_hits"], 0)

    def test_failure_never_publishes_the_final_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            store, architecture = self.fixture(directory)
            bad = json.loads(architecture.read_text())
            bad["operation_duration"]["1qGate"] = 0.625
            architecture.write_text(json.dumps(bad), encoding="utf-8")
            output = Path(directory) / "failed"
            with self.assertRaisesRegex(ValueError, "frozen fidelity model"):
                compile_formal_large_zac(
                    method="M1",
                    layer_store_path=store,
                    architecture_path=architecture,
                    output_directory=output,
                )
            self.assertFalse(output.exists())
            self.assertEqual(
                list(Path(directory).glob(".failed.attempt-*")), [])
            self.assertFalse((Path(directory) / ".failed.inprogress").exists())

    def test_interrupted_resume_is_event_exact_to_continuous_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store, architecture = self.fixture(directory)
            setting = _setting("ours_lk_v2.json")
            continuous = Path(directory) / "continuous"
            resumed = Path(directory) / "resumed"
            reference = compile_formal_large_zac(
                method="M4",
                layer_store_path=store,
                architecture_path=architecture,
                output_directory=continuous,
                setting=setting,
                checkpoint_every_stages=1,
            )
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                compile_formal_large_zac(
                    method="M4",
                    layer_store_path=store,
                    architecture_path=architecture,
                    output_directory=resumed,
                    setting=setting,
                    checkpoint_every_stages=1,
                    interrupt_after_stages=2,
                )
            self.assertFalse(resumed.exists())
            self.assertTrue(
                (Path(directory) / ".resumed.inprogress" / "checkpoint.json").is_file())
            recovered = compile_formal_large_zac(
                method="M4",
                layer_store_path=store,
                architecture_path=architecture,
                output_directory=resumed,
                setting=setting,
                checkpoint_every_stages=1,
                resume=True,
            )
            self.assertEqual(recovered.native_chain_sha256,
                             reference.native_chain_sha256)
            self.assertEqual(recovered.canonical_chain_sha256,
                             reference.canonical_chain_sha256)
            self.assertEqual(recovered.fidelity, reference.fidelity)
            self.assertEqual(recovered.validation, reference.validation)
            for filename in (
                "native.jsonl.gz", "trace.jsonl.gz", "decisions.jsonl.gz",
                "route.jsonl.gz",
            ):
                self.assertEqual(
                    _decompressed(continuous / filename),
                    _decompressed(resumed / filename),
                    filename,
                )

    def test_full_run_rejects_dirty_or_untracked_code(self):
        with tempfile.TemporaryDirectory() as directory:
            store, architecture = self.fixture(directory)
            output = Path(directory) / "dirty"
            with patch(
                "streaming.formal_large_zac_compiler._git_state",
                return_value=("b" * 40, True),
            ):
                with self.assertRaisesRegex(RuntimeError, "clean Git worktree"):
                    compile_formal_large_zac(
                        method="M1",
                        layer_store_path=store,
                        architecture_path=architecture,
                        output_directory=output,
                    )
            self.assertFalse(output.exists())
            self.assertFalse((Path(directory) / ".dirty.inprogress").exists())


if __name__ == "__main__":
    unittest.main()
