"""Tests for the disk-backed QMAP 3.2 ASAP schedule spool."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.qasm_sqlite import build_layer_store  # noqa: E402
from streaming.qmap_schedule_sqlite import (  # noqa: E402
    QmapScheduleStore,
    build_qmap_schedule_store,
)


class TestQmapScheduleSqlite(unittest.TestCase):
    def build(self, directory, body, *, capacity=2, cache=2):
        root = Path(directory)
        qasm = root / "input.qasm"
        layers = root / "layers.sqlite"
        schedule = root / "qmap.sqlite"
        qasm.write_text(
            "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[6];\n" + body,
            encoding="utf-8",
        )
        build_layer_store(qasm, layers)
        build_qmap_schedule_store(
            layers, schedule,
            max_two_qubit_gates_per_layer=capacity,
            count_cache_size=cache,
            commit_interval=2,
        )
        return schedule

    def test_capacity_backfill_matches_qmap_schedule_operation_semantics(self):
        body = """cz q[0],q[1];
cz q[0],q[2];
u1(0.1) q[5];
cz q[3],q[4];
cz q[3],q[5];
u2(0.2,0.3) q[5];
"""
        with tempfile.TemporaryDirectory() as directory:
            path = self.build(directory, body, capacity=2, cache=1)
            with QmapScheduleStore(path) as store:
                verified = store.verify()
                self.assertEqual(verified["gates_2q"], 4)
                self.assertEqual(verified["gates_1q"], 2)
                self.assertEqual(verified["two_qubit_layers"], 2)
                self.assertEqual(store.scheduled_items, 3)
                layers = list(store.iter_layers())

            self.assertEqual(
                [[(op.q0, op.q1) for op in layer.two_qubit]
                 for layer in layers],
                [[(0, 1), (3, 4)], [(0, 2), (3, 5)], []],
            )
            self.assertEqual(
                [[op.operation for op in layer.single_qubit]
                 for layer in layers],
                [["u1"], [], ["u2"]],
            )
            self.assertEqual(
                [layer.has_two_qubit_layer for layer in layers],
                [True, True, False],
            )

    def test_only_single_qubit_circuit_has_one_final_item(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.build(
                directory, "u1(0.1) q[0];\nu3(0.1,0.2,0.3) q[1];\n")
            with QmapScheduleStore(path) as store:
                self.assertEqual(store.scheduled_items, 1)
                layer = store.layer(0)
                self.assertFalse(layer.has_two_qubit_layer)
                self.assertEqual([op.operation for op in layer.single_qubit],
                                 ["u1", "u3"])
                self.assertEqual(layer.two_qubit, ())

    def test_schedule_digest_and_counts_fail_closed_after_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.build(directory, "cz q[0],q[1];\n")
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE qmap_operations SET layer=1 WHERE seq=0")
            connection.commit()
            connection.close()
            with QmapScheduleStore(path) as store:
                with self.assertRaisesRegex(ValueError, "metadata mismatch"):
                    store.verify()


if __name__ == "__main__":
    unittest.main()
