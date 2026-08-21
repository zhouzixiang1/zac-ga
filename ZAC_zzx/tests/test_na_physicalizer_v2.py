"""Regression tests for common QMAP endpoint-preserving physicalization."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation import normalize_na, validate_trace_physics  # noqa: E402
from experiments_v2.na_physicalizer import physicalize_na  # noqa: E402


ARCHITECTURE = {
    "storage_zones": [{
        "zone_id": 0,
        "slms": [{
            "id": 0,
            "site_seperation": [1, 1],
            "r": 8,
            "c": 8,
            "location": [-2, -2],
        }],
    }],
    "entanglement_zones": [],
    "aods": [{"id": 0, "site_seperation": 1, "r": 8, "c": 8}],
    "arch_range": [[-2, -2], [6, 6]],
    "rydberg_range": [],
}


class TestNAPhysicalizer(unittest.TestCase):
    def test_cross_beam_ghost_is_split_without_changing_endpoints(self):
        raw = """\
atom (0, 0) atom0
atom (2, 2) atom1
atom (1, 1) atom2
@+ load [
    atom0
    atom1
]
@+ move [
    (2, -1) atom0
    (3, 0) atom1
]
@+ store [
    atom0
    atom1
]
"""
        repaired, stats = physicalize_na(raw, ARCHITECTURE)
        result = validate_trace_physics(
            normalize_na(repaired, architecture=ARCHITECTURE), n_qubits=3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["ghost_hits"], 0)
        self.assertEqual(stats["raw_move_batches"], 1)
        self.assertEqual(stats["repaired_move_batches"], 2)
        self.assertEqual(stats["ghost_splits"], 1)
        self.assertEqual(stats["waypoint_atoms"], 0)

    def test_single_atom_collision_uses_visible_waypoint(self):
        raw = """\
atom (0, 0) atom0
atom (1, 1) atom1
@+ load atom0
@+ move (2, 2) atom0
@+ store atom0
"""
        repaired, stats = physicalize_na(raw, ARCHITECTURE)
        result = validate_trace_physics(
            normalize_na(repaired, architecture=ARCHITECTURE), n_qubits=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["move_batches"], 1)
        self.assertEqual(result["move_phases"], 2)
        self.assertEqual(stats["waypoint_atoms"], 1)
        self.assertIn("@+ move (", repaired)

    def test_safe_trace_is_byte_deterministic(self):
        raw = """\
atom (0, 0) atom0
atom (3, 3) atom1
@+ load atom0
@+ move (0, 2) atom0
@+ store atom0
"""
        first = physicalize_na(raw, ARCHITECTURE)
        second = physicalize_na(raw, ARCHITECTURE)
        self.assertEqual(first, second)
        self.assertEqual(first[1]["ghost_splits"], 0)
        self.assertEqual(first[1]["waypoint_atoms"], 0)


if __name__ == "__main__":
    unittest.main()
