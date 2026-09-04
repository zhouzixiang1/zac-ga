"""Differential tests for bounded construction of stock ZAC's SA objective."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.zac_initial_placement import build_stock_sa_interactions  # noqa: E402
from zac.ds.architecture import Architecture  # noqa: E402
from zac.placer.saplacer import SAPlacer  # noqa: E402


class TestStreamingStockSA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = json.loads(
            (ROOT / "hardware_spec/toy_architecture.json").read_text())
        cls.architecture = Architecture(spec)
        cls.architecture.preprocessing()

    def test_streamed_interactions_are_float_exact_stock_preprocessing(self):
        schedule = [
            [[0, 1], [2, 3]],
            [[1, 2]],
            [[0, 1]],
            [[3, 4]],
            [[0, 4]],
            [[0, 1]],
            [[0, 1]],
            [[2, 4]],
        ]
        batch = SAPlacer()
        batch.n_qubit = 5
        batch.list_gate = schedule
        batch.preprocessing()

        streamed = build_stock_sa_interactions(5, iter(schedule))
        self.assertEqual(streamed, batch.list_qubit_dict_gate)
        self.assertEqual(
            streamed[0][1].hex(), batch.list_qubit_dict_gate[0][1].hex())

    def test_preprocessed_entry_point_produces_exact_stock_mapping(self):
        schedule = [
            [[0, 1], [2, 3]],
            [[1, 4], [3, 5]],
            [[0, 4], [2, 5]],
            [[0, 5]],
            [[1, 3]],
            [[2, 4]],
        ]
        interactions = build_stock_sa_interactions(6, iter(schedule))
        with contextlib.redirect_stdout(io.StringIO()):
            batch = SAPlacer()
            batch.run(self.architecture, 6, schedule)
            streamed = SAPlacer()
            streamed.run_preprocessed(self.architecture, 6, interactions)
        self.assertEqual(streamed.best_mapping, batch.best_mapping)
        self.assertEqual(streamed.best_cost, batch.best_cost)

    def test_invalid_preprocessed_matrix_fails_closed(self):
        placer = SAPlacer()
        with self.assertRaisesRegex(ValueError, "symmetric"):
            placer.run_preprocessed(
                self.architecture, 2, [{1: 1.0}, {0: 0.9}])


if __name__ == "__main__":
    unittest.main()
