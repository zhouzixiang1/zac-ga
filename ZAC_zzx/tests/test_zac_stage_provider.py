"""Tests for the forward-only stock-ZAC physical-stage ring."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.qasm_sqlite import (  # noqa: E402
    LayerStore,
    ZacGate,
    ZacStage,
    build_layer_store,
)
from streaming.zac_stage_provider import (  # noqa: E402
    LayerStoreZacStageProvider,
    SequentialZacStageProvider,
    count_zac_stages,
)


def make_stage(index):
    gate = ZacGate(
        two_qubit_index=index,
        gate_pair=(index % 4, (index + 1) % 4),
        source_seq=index,
        asap_layer=index,
    )
    return ZacStage(
        stage_index=index,
        asap_layer=index,
        chunk_index=0,
        chunk_count=1,
        gates=(gate,),
    )


class TestSequentialZacStageProvider(unittest.TestCase):
    def test_h2_prefetch_then_old_route_read_preserves_next_stage(self):
        provider = SequentialZacStageProvider(
            (make_stage(i) for i in range(8)), 8, max_cached_stages=4)
        for index in range(4):
            provider.read_layer(index)
        self.assertEqual(provider.cached_stages, (0, 1, 2, 3))
        provider.stage_record(0)  # route L after forecasting through L+3
        provider.read_layer(4)
        self.assertEqual(provider.cached_stages, (1, 2, 3, 4))
        self.assertEqual(provider.stage_record(1).stage_index, 1)

    def test_evicted_and_mismatched_iterators_fail_closed(self):
        provider = SequentialZacStageProvider(
            (make_stage(i) for i in range(3)), 3, max_cached_stages=1)
        provider.read_layer(1)
        with self.assertRaisesRegex(IndexError, "evicted"):
            provider.read_layer(0)
        provider.read_layer(2)
        provider.assert_exhausted()

        with self.assertRaisesRegex(ValueError, "ended"):
            SequentialZacStageProvider([make_stage(0)], 2).assert_exhausted()

    def test_sqlite_count_and_capacity_split_match_iterator(self):
        qasm = """OPENQASM 2.0;
include \"qelib1.inc\";
qreg q[6];
cz q[0],q[1];
cz q[2],q[3];
cz q[4],q[5];
cz q[0],q[2];
cz q[1],q[3];
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.qasm"
            database = root / "layers.sqlite"
            source.write_text(qasm, encoding="utf-8")
            build_layer_store(source, database)
            with LayerStore(database) as store:
                expected = list(store.iter_zac_stages(max_gates=2))
                self.assertEqual(count_zac_stages(store, 2), len(expected))
                provider = LayerStoreZacStageProvider(
                    store, 2, max_cached_stages=3)
                actual = [provider.stage_record(i) for i in range(len(provider))]
                self.assertEqual(actual, expected)
                provider.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
