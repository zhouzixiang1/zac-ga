"""Semantic tests for the GA initial-placement chromosome direction."""
from __future__ import annotations

import contextlib
import io
import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zzx.gainit import GAInitialPlacer  # noqa: E402


class LineStorageArchitecture:
    """Minimal deterministic architecture implementing GAInitialPlacer's API."""

    def __init__(self, columns: int):
        self.storage_zone = [0]
        self.dict_SLM = {
            0: SimpleNamespace(n_r=1, n_c=columns),
        }

    @staticmethod
    def nearest_entanglement_site_distance(_slm, _row, _column):
        return 0.0

    @staticmethod
    def nearest_entanglement_site_dis(
            _slm1, _row1, column1, _slm2, _row2, column2):
        # gainit takes the square root, yielding exact line distance.
        return float(column1 - column2) ** 2


class TestGAInitialPlacer(unittest.TestCase):
    schedule = [
        [[4, 5]],
        [[0, 4], [1, 5]],
        [[0, 2]],
        [[1, 3]],
        [[2, 3]],
    ]

    def test_structured_warm_starts_invert_atom_at_seat_order(self):
        placer = GAInitialPlacer()
        weights = placer._weight_matrix(6, self.schedule)
        warm_starts = placer._warm_starts(6, weights, self.schedule)

        self.assertEqual(len(warm_starts), 3)
        for warm_start in warm_starts:
            self.assertEqual(sorted(warm_start), list(range(6)))

        # q4/q5 are the first-used and strongest-connected pair.  Both
        # structured heuristics put those atoms in seat indices 0 and 1.  The
        # old atom_at_seat-as-p bug instead read p[4:6] as [2, 3].
        for warm_start in warm_starts[1:]:
            self.assertEqual(warm_start[4], 0)
            self.assertEqual(warm_start[5], 1)

    def test_cost_and_final_mapping_use_p_atom_equals_seat_index(self):
        architecture = LineStorageArchitecture(6)
        placer = GAInitialPlacer({
            "init_pop": 3,
            "init_gens": 0,
            "seed": 11,
        })
        seats = placer._enumerate_seats(architecture, 6)
        weights = placer._weight_matrix(6, self.schedule)
        distance = placer._distance_matrix(architecture, seats)
        weight_array = np.asarray(weights)
        warm_starts = placer._warm_starts(6, weights, self.schedule)

        def cost(assignment):
            return 0.5 * float((
                weight_array
                * distance[np.ix_(assignment, assignment)]).sum())

        expected_cost, _seed_index, expected_assignment = min(
            (cost(seed), index, seed)
            for index, seed in enumerate(warm_starts))
        with contextlib.redirect_stdout(io.StringIO()):
            placer.run(architecture, 6, self.schedule)

        seat_index = {seat: index for index, seat in enumerate(seats)}
        actual_assignment = [
            seat_index[tuple(location)] for location in placer.best_mapping]
        self.assertEqual(actual_assignment, expected_assignment)
        self.assertAlmostEqual(placer.best_cost, expected_cost, delta=1e-12)
        self.assertAlmostEqual(
            placer.best_cost, cost(actual_assignment), delta=1e-12)

    def test_single_atom_neighbor_and_solver_are_safe(self):
        for seed in range(32):
            self.assertEqual(
                GAInitialPlacer._neighbor([0], random.Random(seed)), [0])

        architecture = LineStorageArchitecture(1)
        placer = GAInitialPlacer({
            "init_pop": 4,
            "init_gens": 8,
            "seed": 3,
        })
        with contextlib.redirect_stdout(io.StringIO()):
            placer.run(architecture, 1, [])
        self.assertEqual(placer.best_mapping, [(0, 0, 0)])
        self.assertEqual(placer.best_cost, 0.0)


if __name__ == "__main__":
    unittest.main()
