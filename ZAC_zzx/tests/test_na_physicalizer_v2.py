"""Regression tests for common QMAP endpoint-preserving physicalization."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation import normalize_na, validate_trace_physics  # noqa: E402
from experiments_v2.na_physicalizer import (  # noqa: E402
    physicalize_na,
    physicalize_na_streaming,
)
from streaming.formal_large_qmap_compiler import (  # noqa: E402
    _hash_na_placement_semantics,
)
from streaming.na_instruction_stream import normalize_na_incrementally  # noqa: E402
from streaming.trace_pipeline import IncrementalTraceValidator  # noqa: E402


FULL_ARCHITECTURE = ROOT / "hardware_spec" / "full_architecture.json"
BWT37_FIRST31_MOVES = (
    ROOT / "tests" / "fixtures" / "formal_m2_bwt37_first31_moves.na"
)


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

    def test_bwt37_first31_batches_stream_exact_and_strict(self):
        whole_text, whole_stats = physicalize_na(
            BWT37_FIRST31_MOVES, FULL_ARCHITECTURE
        )
        with tempfile.TemporaryDirectory() as directory:
            repaired = Path(directory) / "native.na"
            stream_stats = physicalize_na_streaming(
                BWT37_FIRST31_MOVES, repaired, FULL_ARCHITECTURE
            )
            self.assertEqual(repaired.read_text(), whole_text)
            self.assertEqual(stream_stats, whole_stats)
            self.assertEqual(
                _hash_na_placement_semantics(BWT37_FIRST31_MOVES),
                _hash_na_placement_semantics(repaired),
            )
            result = validate_trace_physics(
                normalize_na(repaired, architecture=FULL_ARCHITECTURE),
                n_qubits=37,
            )
            incremental = IncrementalTraceValidator(
                37,
                expected_one_qubit_gates=0,
                expected_two_qubit_gates=0,
                require_zero_ghost=True,
                event_order="chronological",
            )
            for event in normalize_na_incrementally(
                repaired, architecture=FULL_ARCHITECTURE
            ):
                incremental.consume(event)
            incremental_result = incremental.finalize().to_dict()

        self.assertEqual(stream_stats, {
            "raw_move_batches": 31,
            "repaired_move_batches": 31,
            "ghost_splits": 0,
            "waypoint_atoms": 1,
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["ghost_hits"], 0)
        self.assertEqual(result["move_batches"], 31)
        self.assertEqual(result["move_phases"], 32)
        self.assertTrue(incremental_result["ok"])
        self.assertEqual(incremental_result["ghost_hits"], 0)
        self.assertEqual(incremental_result["move_batches"], 31)
        self.assertIn(
            "@+ move (63.000000, 291.000000) atom34", whole_text
        )
        self.assertIn(
            "@+ move (73.000000, 307.000000) atom34", whole_text
        )


if __name__ == "__main__":
    unittest.main()
