"""Schema-2 contracts, atomic runner, statistics and canonicalisation tests."""

from __future__ import annotations

import gzip
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

from experiments_v2.canonicalize import (  # noqa: E402
    canonicalize_circuit, canonicalize_large_circuit_streaming,
    canonicalize_suite)
from experiments_v2.contracts import (  # noqa: E402
    load_run_manifest,
    repository_snapshot,
)
from experiments_v2.runner import AttemptSpec, run_attempt  # noqa: E402
from experiments_v2.method_driver import _validated_m2_config  # noqa: E402
from experiments_v2.statistics import (  # noqa: E402
    holm_adjust, stratified_bootstrap_log_ratio, wilson_interval)


class TestProvenance(unittest.TestCase):
    def test_unversioned_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = repository_snapshot(Path(directory))
            self.assertEqual(snapshot["commit"], "unknown")
            self.assertEqual(snapshot["branch"], "unknown")
            self.assertTrue(snapshot["dirty"])

    def test_repository_snapshot_records_live_commit_and_branch(self):
        snapshot = repository_snapshot(REPO)
        self.assertRegex(snapshot["commit"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(snapshot["branch"], "unknown")
        self.assertEqual(Path(snapshot["root"]).resolve(), REPO.resolve())


class TestCanonicalisation(unittest.TestCase):
    def test_canonical_manifest_and_hash_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "toy.qasm"
            source.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\n'
                'creg c[2];\nh q[0];\ncx q[0],q[1];\nbarrier q;\n'
                'measure q[0] -> c[0];\n', encoding="utf-8")
            first = canonicalize_circuit(source, base / "a.qasm")
            second = canonicalize_circuit(source, base / "b.qasm")
            self.assertEqual(first.canonical_sha256, second.canonical_sha256)
            self.assertEqual((first.qubits, first.gates_2q), (2, 1))
            self.assertEqual(first.removed_operations, {"barrier": 1, "measure": 1})
            self.assertIn("cz q[0],q[1]", (base / "a.qasm").read_text())

    def test_reset_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "bad.qasm"
            source.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\nreset q[0];\n',
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported operation"):
                canonicalize_circuit(source, base / "bad-out.qasm")

    def test_large_stream_expansion_is_deterministic_and_records_resets(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "bwt_n37.qasm"
            source.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\n'
                'creg c[3];\nreset q[2];\nh q[0];\ncx q[0],q[1];\n'
                'ccx q[0],q[1],q[2];\nbarrier q;\nmeasure q -> c;\n',
                encoding="utf-8")
            kwargs = {
                "upstream_commit":
                    "357b942396d5c2b7cbc1c229c585a6ef5ccaebac",
            }
            first = canonicalize_large_circuit_streaming(
                source, base / "a.qasm", **kwargs)
            second = canonicalize_large_circuit_streaming(
                source, base / "b.qasm", **kwargs)
            self.assertEqual(first.canonical_sha256, second.canonical_sha256)
            self.assertEqual(first.canonical_profile,
                             "large_qasmbench_expand_only")
            self.assertEqual((first.gates_1q, first.gates_2q), (24, 7))
            self.assertEqual(first.removed_operations, {
                "barrier": 1, "classical_register": 1,
                "measure": 1, "reset": 1,
            })
            text = (base / "a.qasm").read_text(encoding="utf-8")
            self.assertNotIn("reset", text)
            self.assertNotIn("cx ", text)
            self.assertNotIn("ccx ", text)

    def test_large_stream_accepts_and_normalizes_qasmbench_register_names(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "multiplier_n400.qasm"
            source.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q0[3];\n'
                'creg c0[1];\nx q0[2];\ncx q0[2],q0[1];\n'
                'ccx q0[2],q0[1],q0[0];\n',
                encoding="utf-8",
            )
            manifest = canonicalize_large_circuit_streaming(
                source,
                base / "canonical.qasm",
                upstream_commit=(
                    "357b942396d5c2b7cbc1c229c585a6ef5ccaebac"
                ),
            )
            self.assertEqual(manifest.qubits, 3)
            text = (base / "canonical.qasm").read_text(encoding="utf-8")
            self.assertIn("qreg q[3];", text)
            self.assertNotIn("q0[", text)

    def test_canonicalize_suite_failure_leaves_no_partial_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            good = base / "a.qasm"
            bad = base / "b.qasm"
            good.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\nx q[0];\n',
                encoding="utf-8",
            )
            bad.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ny q[0];\n',
                encoding="utf-8",
            )
            destination = base / "suite"
            with self.assertRaisesRegex(ValueError, "unsupported Large QASM"):
                canonicalize_suite(
                    [good, bad],
                    destination,
                    canonical_profile="large_qasmbench_expand_only",
                    upstream_commit=(
                        "357b942396d5c2b7cbc1c229c585a6ef5ccaebac"
                    ),
                )
            self.assertFalse(destination.exists())
            self.assertEqual(list(base.glob(".suite.staging-*")), [])


class TestAtomicRunner(unittest.TestCase):
    def _files(self, base: Path):
        for name in ("input", "config", "architecture", "model"):
            (base / f"{name}.json").write_text('{}\n', encoding="utf-8")

    def _spec(self, base: Path, command, timeout=2.0):
        self._files(base)
        return AttemptSpec(
            dataset="toy", circuit="c0", method="M3", seed=0, repetition=0,
            command=command, output_root=base / "runs", repo_root=REPO,
            input_path=base / "input.json", config_path=base / "config.json",
            architecture_path=base / "architecture.json", model_path=base / "model.json",
            qubits=1, expected_gates_1q=0, expected_gates_2q=0,
            expected_gate_ledger_sha256="a" * 64,
            timeout_seconds=timeout, minimum_free_bytes=0,
        )

    def test_success_uses_only_unique_attempt_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            stale = base / "runs" / "old" / "success.txt"
            stale.parent.mkdir(parents=True)
            stale.write_text("must not be read", encoding="utf-8")
            command = [sys.executable, "-c",
                       "import json,os,pathlib; p=pathlib.Path(os.environ['ZAC_RUN_DIR']); (p/'new.txt').write_text('ok'); (p/'compiler_timing.json').write_text(json.dumps({'compiler_time_ns':1}))"]
            manifest = run_attempt(
                self._spec(base, command), verifier=lambda _: {"ok": True},
                scorer=lambda _: {
                    "log_fidelity": 0.0, "fidelity": 1.0,
                    "fidelity_components": {
                        "log_one_qubit_gate": 0.0,
                        "log_two_qubit_gate": 0.0,
                        "log_idle_excitation": 0.0,
                        "log_atom_transfer": 0.0,
                        "log_coherence_linear": 0.0,
                    },
                    "move_batches": 0, "move_time_us": 0.0,
                    "duration_us": 0.0, "qubits": 1,
                    "expected_gates_1q": 0, "expected_gates_2q": 0,
                    "observed_gates_1q": 0, "observed_gates_2q": 0,
                    "expected_gate_ledger_sha256": "a" * 64,
                    "observed_gate_ledger_sha256": "a" * 64,
                    "ghost_hits": 0,
                })
            self.assertEqual(manifest.status, "success")
            artifact = Path(manifest.artifact_dir)
            self.assertTrue((artifact / "new.txt").is_file())
            self.assertFalse((artifact / "success.txt").exists())
            loaded = load_run_manifest(artifact / "manifest.json")
            self.assertEqual(loaded.run_id, manifest.run_id)
            self.assertFalse(any(path.name.endswith(".tmp") for path in (base / "runs").iterdir()))

    def test_terminal_attempt_archives_large_replay_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            payloads = {
                "trace.zair.json": '{"instructions":[]}\n',
                "trace.na": "atom (0, 0) atom0\n",
                "trace.na.raw": "atom (0, 0) atom0\n",
                "compiler_stats.json": '{"python_version":"test"}\n',
            }
            script = (
                "import json,os,pathlib;"
                "p=pathlib.Path(os.environ['ZAC_RUN_DIR']);"
                f"payloads={payloads!r};"
                "[(p/name).write_text(value) for name,value in payloads.items()];"
                "(p/'compiler_timing.json').write_text("
                "json.dumps({'compiler_time_ns':1}))"
            )
            manifest = run_attempt(
                self._spec(base, [sys.executable, "-c", script]),
                verifier=lambda _: {"ok": True},
                scorer=lambda _: {
                    "log_fidelity": 0.0, "fidelity": 1.0,
                    "fidelity_components": {
                        "log_one_qubit_gate": 0.0,
                        "log_two_qubit_gate": 0.0,
                        "log_idle_excitation": 0.0,
                        "log_atom_transfer": 0.0,
                        "log_coherence_linear": 0.0,
                    },
                    "move_batches": 0, "move_time_us": 0.0,
                    "duration_us": 0.0, "qubits": 1,
                    "expected_gates_1q": 0, "expected_gates_2q": 0,
                    "observed_gates_1q": 0, "observed_gates_2q": 0,
                    "expected_gate_ledger_sha256": "a" * 64,
                    "observed_gate_ledger_sha256": "a" * 64,
                    "ghost_hits": 0,
                })
            artifact = Path(manifest.artifact_dir)
            self.assertEqual(manifest.status, "success")
            for name, expected in payloads.items():
                self.assertFalse((artifact / name).exists())
                archived = artifact / f"{name}.gz"
                self.assertTrue(archived.is_file())
                with gzip.open(archived, "rt", encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), expected)

    def test_timeout_kills_process_group_and_never_promotes_stale_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            command = [sys.executable, "-c",
                       "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); time.sleep(30)"]
            manifest = run_attempt(
                self._spec(base, command, timeout=0.2),
                verifier=lambda _: {"ok": True}, scorer=lambda _: {})
            self.assertEqual(manifest.status, "timeout")
            self.assertLess(manifest.end_to_end_time_ns / 1e9, 5.0)

    def test_formal_attempt_requires_verifier_and_scorer(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with self.assertRaisesRegex(ValueError, "requires both verifier and scorer"):
                run_attempt(self._spec(base, [sys.executable, "-c", "pass"]))

    def test_incomplete_success_is_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest = run_attempt(
                self._spec(base, [sys.executable, "-c", "pass"]),
                verifier=lambda _: {"ok": True}, scorer=lambda _: {})
            self.assertEqual(manifest.status, "scorer_error")


class TestFrozenMethodConfigs(unittest.TestCase):
    def test_m2_config_must_match_every_frozen_parameter(self):
        path = ROOT / "exp_setting" / "iccad_m2_v2.json"
        self.assertEqual(_validated_m2_config(path)["max_nodes"], 50_000_000)
        with tempfile.TemporaryDirectory() as directory:
            altered = json.loads(path.read_text())
            altered["fallback"] = True
            candidate = Path(directory) / "m2.json"
            candidate.write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError, "configuration mismatch"):
                _validated_m2_config(candidate)


class TestStatistics(unittest.TestCase):
    def test_wilson_and_holm(self):
        low, high = wilson_interval(95, 100)
        self.assertLess(low, .95)
        self.assertGreater(high, .95)
        adjusted = holm_adjust({"a": .01, "b": .04})
        self.assertAlmostEqual(adjusted["a"], .02)
        self.assertAlmostEqual(adjusted["b"], .04)

    def test_stratified_bootstrap_is_reproducible(self):
        diffs = {"a": math.log(1.02), "b": math.log(1.04), "c": math.log(.99)}
        strata = {"a": "small", "b": "small", "c": "large"}
        first = stratified_bootstrap_log_ratio(diffs, strata, iterations=1000, seed=7)
        second = stratified_bootstrap_log_ratio(diffs, strata, iterations=1000, seed=7)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
