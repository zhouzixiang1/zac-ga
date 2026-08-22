"""Exact multi-cut resume tests for the formal Large ZAC state machine."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.formal_zac_checkpoint import (  # noqa: E402
    CheckpointCadence,
    FormalZacCheckpointIdentity,
    FormalZacCheckpointManager,
    RollingInstructionHash,
)
from streaming.formal_zac_placement import FormalZacPlacementStream  # noqa: E402
from streaming.qasm_sqlite import LayerStore, build_layer_store  # noqa: E402
from streaming.zac_route_transition import ZACRouteTransitionDriver  # noqa: E402
from zac.ds.architecture import Architecture  # noqa: E402


def _setting(method: str):
    filename = "ours_nl_v2.json" if method == "M3" else "ours_lk_v2.json"
    value = json.loads((ROOT / "exp_setting" / filename).read_text())
    return dict(value["zac_setting"][0])


def _hash_json(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class TestFormalZacCheckpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = json.loads(
            (ROOT / "hardware_spec" / "toy_architecture.json").read_text())
        spec["operation_duration"] = {
            "rydberg": 0.36,
            "1qGate": 52.0,
            "atom_transfer": 15.0,
        }
        cls.architecture = Architecture(spec)
        cls.architecture.preprocessing()
        cls.architecture_sha = _hash_json(spec)
        cls.initial = [(0, 0, q) for q in range(8)]

    def make_store(self, directory: str) -> tuple[LayerStore, str]:
        qasm = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[8];
u3(0.1,0.2,0.3) q[7];
cz q[0],q[1];
u1(0.4) q[0];
cz q[2],q[3];
u2(0.5,0.6) q[3];
cz q[1],q[4];
cz q[3],q[5];
u3(0.7,0.8,0.9) q[4];
cz q[0],q[6];
cz q[2],q[7];
u1(0.2) q[6];
cz q[4],q[5];
cz q[6],q[7];
"""
        source = Path(directory) / "input.qasm"
        database = Path(directory) / "layers.sqlite"
        source.write_text(qasm, encoding="utf-8")
        metadata = build_layer_store(source, database)
        return LayerStore(database), str(metadata["statement_sha256"])

    def identity(self, method: str, input_sha: str):
        setting = {} if method == "M1" else _setting(method)
        return FormalZacCheckpointIdentity(
            git_commit="a" * 40,
            input_sha256=input_sha,
            config_sha256=_hash_json({"method": method, "setting": setting}),
            architecture_sha256=self.architecture_sha,
        )

    def new_components(self, method: str, store: LayerStore):
        setting = None if method == "M1" else _setting(method)
        placement = FormalZacPlacementStream(
            method=method,
            architecture=self.architecture,
            initial_mapping=self.initial,
            store=store,
            max_gates_per_stage=1,
            setting=setting,
        )
        route = ZACRouteTransitionDriver(
            self.architecture,
            self.initial,
            initial_one_qubit_gates=placement.leading_one_qubit_gates,
            placer_kind="zac" if method == "M1" else "resident",
        )
        output_hash = RollingInstructionHash()
        output_hash.update_many(route.initial_instructions)
        return placement, route, output_hash, setting

    @staticmethod
    def route_one(placement, route, output_hash):
        row = placement.next_placement()
        result = route.route_layer(
            row.layer,
            row.b_l,
            row.g_l,
            row.b_l_plus_1,
            row.gates,
            row.gate_ids,
            row.parent_one_qubit_gates,
        )
        output_hash.update_many(result.instructions)
        return tuple(deepcopy(result.instructions))

    def baseline(self, method: str, store: LayerStore):
        placement, route, output_hash, _ = self.new_components(method, store)
        chunks = []
        while placement.next_layer < placement.stage_count:
            chunks.append(self.route_one(placement, route, output_hash))
        return chunks, output_hash.state_dict(), route.state_dict()

    def test_m1_m3_m4_multiple_cut_resumes_are_dictionary_exact(self):
        for method in ("M1", "M3", "M4"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                store, input_sha = self.make_store(directory)
                self.addCleanup(store.close)
                baseline_chunks, baseline_hash, baseline_route = self.baseline(
                    method, store)
                stage_count = len(baseline_chunks)
                self.assertGreaterEqual(stage_count, 6)
                cuts = (0, 2, stage_count // 2, stage_count - 1, stage_count)
                identity = self.identity(method, input_sha)

                for cut in cuts:
                    with self.subTest(method=method, cut=cut):
                        placement, route, output_hash, setting = \
                            self.new_components(method, store)
                        for _ in range(cut):
                            self.route_one(placement, route, output_hash)
                        checkpoint = Path(directory) / f"{method}-{cut}.checkpoint"
                        summary = FormalZacCheckpointManager.save(
                            checkpoint,
                            identity=identity,
                            placement=placement,
                            route=route,
                            output_hash=output_hash,
                            counters={"core_runtime_ns": 123 + cut},
                        )
                        self.assertEqual(summary["next_physical_stage"], cut)
                        self.assertEqual(
                            summary["current_boundary_sha256"],
                            _hash_json(placement._resident_boundary),
                        )
                        resume = FormalZacCheckpointManager.restore(
                            checkpoint,
                            expected_identity=identity,
                            architecture=self.architecture,
                            initial_mapping=self.initial,
                            store=store,
                            max_gates_per_stage=1,
                            setting=setting,
                        )
                        self.assertEqual(resume.next_layer, cut)
                        self.assertEqual(
                            resume.counters, {"core_runtime_ns": 123 + cut})
                        expected_cache = {"M1": 3, "M3": 2, "M4": 4}[method]
                        self.assertEqual(
                            resume.placement.provider.max_cached_stages,
                            expected_cache,
                        )
                        self.assertEqual(
                            resume.placement.provider.cached_stages, ())

                        future_chunks = []
                        while resume.next_layer < resume.placement.stage_count:
                            future_chunks.append(self.route_one(
                                resume.placement, resume.route,
                                resume.output_hash))
                            self.assertLessEqual(
                                len(resume.placement.provider.cached_stages),
                                expected_cache,
                            )
                        self.assertEqual(future_chunks, baseline_chunks[cut:])
                        self.assertEqual(
                            resume.output_hash.state_dict(), baseline_hash)
                        self.assertEqual(
                            resume.route.state_dict(), baseline_route)

    def test_identity_and_envelope_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store, input_sha = self.make_store(directory)
            self.addCleanup(store.close)
            placement, route, output_hash, _ = self.new_components("M1", store)
            self.route_one(placement, route, output_hash)
            identity = self.identity("M1", input_sha)
            checkpoint = Path(directory) / "state.checkpoint"
            FormalZacCheckpointManager.save(
                checkpoint,
                identity=identity,
                placement=placement,
                route=route,
                output_hash=output_hash,
            )

            for field, value in (
                ("git_commit", "b" * 40),
                ("input_sha256", "b" * 64),
                ("config_sha256", "c" * 64),
                ("architecture_sha256", "d" * 64),
            ):
                changed = identity.as_dict()
                changed[field] = value
                with self.subTest(field=field), self.assertRaisesRegex(
                        ValueError, "commit/input/config/architecture"):
                    FormalZacCheckpointManager.load(
                        checkpoint, expected_identity=changed)

            envelope = json.loads(checkpoint.read_text())
            envelope["summary"]["ghost_splits"] += 1
            checkpoint.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "envelope state summary"):
                FormalZacCheckpointManager.load(
                    checkpoint, expected_identity=identity)

    def test_rolling_hash_and_checkpoint_cadence_resume(self):
        records = ({"id": 0, "type": "init"}, {"id": 1, "type": "1qGate"})
        continuous = RollingInstructionHash()
        continuous.update_many(records)
        interrupted = RollingInstructionHash()
        interrupted.update(records[0])
        restored = RollingInstructionHash.from_state(interrupted.state_dict())
        restored.update(records[1])
        self.assertEqual(restored.state_dict(), continuous.state_dict())

        cadence = CheckpointCadence(
            every_stages=1000, every_seconds=300, monotonic_ns=1_000)
        self.assertFalse(cadence.due(999, monotonic_ns=299_000_001_000))
        self.assertTrue(cadence.due(1000, monotonic_ns=2_000))
        self.assertTrue(cadence.due(1, monotonic_ns=300_000_001_000))
        cadence.mark_saved(1000, monotonic_ns=400_000_000_000)
        self.assertFalse(cadence.due(1999, monotonic_ns=699_999_999_999))


if __name__ == "__main__":
    unittest.main()
