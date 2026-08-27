"""End-to-end placement differentials over the frozen SQLite ZAC view."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.formal_zac_placement import FormalZacPlacementStream  # noqa: E402
from streaming.qasm_sqlite import LayerStore, build_layer_store  # noqa: E402
from zac.ds.architecture import Architecture  # noqa: E402
from zac.placer.vmplacer import VertexMatchingPlacer  # noqa: E402
from zac.zac import ZAC  # noqa: E402
from zzx.zplacer import ResidentPlacer  # noqa: E402


def semantic_decision_log(rows):
    return [
        {key: value for key, value in row.items()
         if not key.endswith("_ns")}
        for row in rows
    ]


def setting(name):
    value = json.loads((ROOT / "exp_setting" / name).read_text())
    return dict(value["zac_setting"][0])


class TestFormalZacPlacementStream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = json.loads(
            (ROOT / "hardware_spec/full_architecture.json").read_text())
        cls.architecture = Architecture(spec)
        cls.architecture.preprocessing()
        cls.initial = [(0, q, 0) for q in range(6)]

    def make_store(self, directory):
        qasm = """OPENQASM 2.0;
include \"qelib1.inc\";
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
        source = Path(directory) / "input.qasm"
        database = Path(directory) / "layers.sqlite"
        source.write_text(qasm, encoding="utf-8")
        build_layer_store(source, database)
        return LayerStore(database)

    @staticmethod
    def rebuild_mapping(initial, rows):
        rebuilt = [list(initial)]
        for row in rows:
            rebuilt.extend([list(row.g_l), list(row.b_l_plus_1)])
        return rebuilt

    def test_m1_stream_matches_batch_and_preserves_parent_1q(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_store(directory) as store:
                stages = list(store.iter_zac_stages(10))
                schedule = [[list(gate.gate_pair) for gate in stage.gates]
                            for stage in stages]
                compiler = ZAC()
                compiler.n_q = len(self.initial)
                compiler.gate_scheduling = deepcopy(schedule)
                compiler.collect_reuse_qubit()
                batch = VertexMatchingPlacer(deepcopy(self.initial))
                with contextlib.redirect_stdout(io.StringIO()):
                    batch.run(
                        self.architecture, [deepcopy(self.initial)], schedule,
                        True, compiler.reuse_qubit)

                stream = FormalZacPlacementStream(
                    method="M1",
                    architecture=self.architecture,
                    initial_mapping=self.initial,
                    store=store,
                    max_gates_per_stage=10,
                )
                self.assertEqual(stream.leading_one_qubit_gates, (("u3", 5),))
                rows = list(stream)
                self.assertEqual(self.rebuild_mapping(self.initial, rows), batch.mapping)
                self.assertEqual(
                    [row.parent_one_qubit_gates for row in rows],
                    [(('u1', 0), ('u2', 3)), (('u3', 4),)],
                )

    @patch("zzx.native_backend._runtime_metadata")
    def test_m3_and_m4_stream_match_batch_placer(self, runtime_metadata):
        runtime_metadata.side_effect = lambda _module, **kwargs: {
            "extension_path": "development-test-extension",
            "extension_sha256": "0" * 64,
            "wheel_registration_path": "development-test-registration",
            "native_wheel_sha256": kwargs.get("expected_wheel_sha256"),
            "wheel_registered": True,
        }
        for method, filename in (("M3", "ours_nl_v2.json"),
                                 ("M4", "ours_lk_v2.json")):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                with self.make_store(directory) as store:
                    stages = list(store.iter_zac_stages(10))
                    schedule = [[list(gate.gate_pair) for gate in stage.gates]
                                for stage in stages]
                    params = setting(filename)
                    batch = ResidentPlacer(deepcopy(self.initial), **params)
                    leading = tuple(
                        (event.operation, event.qubits[0])
                        for event in store.iter_zac_leading_one_qubit())
                    one_qubit_by_layer = tuple(tuple(
                        (event.operation, event.qubits[0])
                        for event in store.iter_zac_one_qubit_for_stage(stage))
                        for stage in stages)
                    batch.run(
                        self.architecture, [deepcopy(self.initial)], schedule,
                        True, [set() for _ in schedule],
                        leading_one_qubit_gates=leading,
                        one_qubit_gates_by_layer=one_qubit_by_layer)

                    stream = FormalZacPlacementStream(
                        method=method,
                        architecture=self.architecture,
                        initial_mapping=self.initial,
                        store=store,
                        max_gates_per_stage=10,
                        setting=params,
                    )
                    rows = list(stream)
                    self.assertEqual(
                        self.rebuild_mapping(self.initial, rows), batch.mapping)
                    self.assertEqual(
                        semantic_decision_log(
                            [dict(row.decision_log) for row in rows]),
                        semantic_decision_log(batch.decision_log),
                    )
                    self.assertLessEqual(len(stream.provider.cached_stages), 4)


if __name__ == "__main__":
    unittest.main()
