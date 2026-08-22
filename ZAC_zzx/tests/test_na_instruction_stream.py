"""Differential and bounded-state tests for incremental QMAP NA decoding."""

from __future__ import annotations

import gzip
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation import (  # noqa: E402
    EventType,
    TraceValidationError,
    UnsupportedOperationError,
    normalize_na,
)
from streaming.na_instruction_stream import (  # noqa: E402
    IncrementalNANormalizer,
    normalize_na_incrementally,
)


ARCHITECTURE = {
    "storage_zones": [{
        "slms": [{
            "id": 0,
            "location": [0, 0],
            "site_seperation": [2, 2],
            "r": 2,
            "c": 4,
        }],
    }],
    "entanglement_zones": [{
        "slms": [
            {
                "id": 1,
                "location": [10, 0],
                "site_seperation": [4, 4],
                "r": 1,
                "c": 2,
            },
            {
                "id": 2,
                "location": [12, 0],
                "site_seperation": [4, 4],
                "r": 1,
                "c": 2,
            },
        ],
    }],
    "rydberg_range": [[[9, -1], [17, 1]]],
}


TOY_NA = """
// declaration order is deliberately not logical order
atom (2, 0) atom1
atom (0, 0) atom0

@+ u 1.0 2.0 3.0 atom0
@+ load [
  atom0
  atom1
]
@+ move [
  (10, 0) atom0
  (12, 0) atom1
]
@+ store [
  atom0
  atom1
]
@+ cz zone_cz0
# all tail-layer one-qubit instructions must survive
@+ rz [
  0.25 atom1
  0.50 atom0
]
@+ sh atom1
"""


def _dicts(events):
    return [event.to_dict() for event in events]


class TestIncrementalNANormalizer(unittest.TestCase):
    def test_toy_is_field_exact_to_batch_adapter(self):
        expected = list(normalize_na(TOY_NA, architecture=ARCHITECTURE))
        actual = list(normalize_na_incrementally(
            TOY_NA, architecture=ARCHITECTURE
        ))
        self.assertEqual(_dicts(actual), _dicts(expected))

        self.assertEqual(
            [event.source_index for event in actual],
            [0, 2, 3, 7, 11, 15, 16, 16, 20],
        )
        tail = [
            event for event in actual
            if event.event_type is EventType.ONE_QUBIT_GATE
        ]
        self.assertEqual(
            [event.gate_names for event in tail],
            [("u",), ("rz",), ("rz",), ("sh",)],
        )
        self.assertEqual(
            [event.metadata["native_gate_index"] for event in tail],
            [0, 0, 1, 0],
        )

    def test_inline_and_block_payloads_are_field_exact(self):
        source = """
atom (0, 0) atom0
atom (2, 0) atom1
@+ ry 0.1 atom0
@+ u [
  1 2 3 atom1
  4 5 6 atom0
]
@+ load atom0
@+ move (4, 4) atom0
@+ store atom0
@+ rz 0.7 atom1
"""
        expected = list(normalize_na(source, architecture=ARCHITECTURE))
        actual = list(normalize_na_incrementally(
            source, architecture=ARCHITECTURE
        ))
        self.assertEqual(actual, expected)

    def test_plain_and_gzip_paths_are_field_exact(self):
        expected = _dicts(normalize_na(TOY_NA, architecture=ARCHITECTURE))
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "program.na"
            compressed = Path(directory) / "program.na.gz"
            plain.write_text(TOY_NA, encoding="utf-8")
            with gzip.open(compressed, "wt", encoding="utf-8") as handle:
                handle.write(TOY_NA)

            for source in (plain, str(plain), compressed, str(compressed)):
                with self.subTest(source=source):
                    self.assertEqual(
                        _dicts(normalize_na_incrementally(
                            source, architecture=ARCHITECTURE
                        )),
                        expected,
                    )

    def test_explicit_gate_pairs_are_exact_and_strictly_exhausted(self):
        ledger = [((0, 1),)]
        expected = list(normalize_na(
            TOY_NA,
            architecture=ARCHITECTURE,
            gate_pairs=ledger,
        ))
        actual = list(normalize_na_incrementally(
            TOY_NA,
            architecture=ARCHITECTURE,
            gate_pairs=ledger,
        ))
        self.assertEqual(actual, expected)

        with self.assertRaisesRegex(TraceValidationError, "fewer entries"):
            list(normalize_na_incrementally(
                TOY_NA,
                architecture=ARCHITECTURE,
                gate_pairs=[],
            ))
        with self.assertRaisesRegex(TraceValidationError, "more entries"):
            list(normalize_na_incrementally(
                TOY_NA,
                architecture=ARCHITECTURE,
                gate_pairs=[((0, 1),), ((0, 1),)],
            ))

    def test_trailing_one_qubit_only_layer_is_not_dropped(self):
        source = """
atom (10, 0) atom0
atom (12, 0) atom1
@+ cz zone_cz0
@+ u 1 2 3 atom0
@+ rz 0.2 atom1
@+ ry [
  0.3 atom0
  0.4 atom1
]
"""
        events = list(normalize_na_incrementally(
            source, architecture=ARCHITECTURE
        ))
        self.assertEqual(events, list(normalize_na(
            source, architecture=ARCHITECTURE
        )))
        self.assertEqual(
            [event.atoms for event in events
             if event.event_type is EventType.ONE_QUBIT_GATE],
            [(0,), (1,), (0,), (1,)],
        )

    def test_unknown_operation_and_late_declaration_fail_closed(self):
        with self.assertRaises(UnsupportedOperationError):
            list(normalize_na_incrementally(
                "atom (0, 0) atom0\n@+ teleport atom0"
            ))
        with self.assertRaisesRegex(
            TraceValidationError, "must precede all operations"
        ):
            list(normalize_na_incrementally(
                "atom (0, 0) atom0\n@+ u atom0\natom (2, 0) atom1"
            ))

    def test_cz_and_one_qubit_while_held_fail_closed(self):
        prefix = """
atom (10, 0) atom0
atom (12, 0) atom1
@+ load atom0
"""
        with self.assertRaisesRegex(TraceValidationError, "CZ occurs while"):
            list(normalize_na_incrementally(
                prefix + "@+ cz zone_cz0\n",
                architecture=ARCHITECTURE,
            ))
        with self.assertRaisesRegex(
            TraceValidationError, "one-qubit gate occurs while"
        ):
            list(normalize_na_incrementally(
                prefix + "@+ u atom1\n",
                architecture=ARCHITECTURE,
            ))

    def test_duplicate_initial_and_post_store_occupancy_fail_closed(self):
        with self.assertRaisesRegex(TraceValidationError, "initial position"):
            list(normalize_na_incrementally(
                "atom (0, 0) atom0\natom (0, 0) atom1"
            ))

        collision = """
atom (0, 0) atom0
atom (2, 0) atom1
@+ load atom0
@+ move (2, 0) atom0
@+ store atom0
"""
        with self.assertRaisesRegex(TraceValidationError, "same position"):
            list(normalize_na_incrementally(
                collision, architecture=ARCHITECTURE
            ))

    def test_malformed_and_incomplete_batches_fail_closed(self):
        cases = (
            ("atom (0, 0) atom0\n@+ move (1, 1) atom0", "outside"),
            ("atom (0, 0) atom0\n@+ load atom0", "still held"),
            (
                "atom (0, 0) atom0\n@+ load atom0\n"
                "@+ move (1, 1) atom0\n@+ store atom0\n]",
                "closing bracket",
            ),
            (
                "atom (0, 0) atom0\n@+ u [\n0.2 atom0",
                "unterminated",
            ),
        )
        for source, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(TraceValidationError, message):
                    list(normalize_na_incrementally(source))

    def test_long_path_retains_constant_state_and_small_heap(self):
        gate_count = 30_000
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "long.na"
            with source.open("wt", encoding="utf-8") as handle:
                for q in range(4):
                    handle.write(f"atom ({2 * q}, 0) atom{q}\n")
                for index in range(gate_count):
                    handle.write(f"@+ rz 0.25 atom{index % 4}\n")

            normalizer = IncrementalNANormalizer()
            tracemalloc.start()
            count = 0
            for event in normalizer.normalize(source):
                count += 1
                self.assertLessEqual(normalizer.retained_atom_records, 32)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        self.assertEqual(count, gate_count + 1)
        self.assertEqual(normalizer.n_qubits, 4)
        self.assertEqual(normalizer.max_payload_items, 1)
        # A full-text implementation grows by several MiB here.  The streaming
        # reader stays far below this generous platform-independent ceiling.
        self.assertLess(peak, 2_500_000)


if __name__ == "__main__":
    unittest.main()
