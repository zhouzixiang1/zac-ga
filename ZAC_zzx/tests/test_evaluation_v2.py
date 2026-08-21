"""Golden tests for the Schema-2 unified physical evaluation layer."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation import (  # noqa: E402
    CanonicalTraceEvent,
    EventType,
    FidelityModel,
    TraceValidationError,
    UnsupportedOperationError,
    normalize_na,
    normalize_zair,
    score_trace,
    validate_trace_physics,
)
from evaluation.cli import main as evaluation_main  # noqa: E402


MODEL = FidelityModel()
ARCHITECTURE = {
    "storage_zones": [
        {
            "zone_id": 0,
            "slms": [
                {"id": 0, "site_seperation": [2, 2], "r": 4, "c": 4, "location": [0, 0]}
            ],
        }
    ],
    "entanglement_zones": [
        {
            "zone_id": 0,
            "slms": [
                {"id": 1, "site_seperation": [4, 4], "r": 2, "c": 2, "location": [10, 0]},
                {"id": 2, "site_seperation": [4, 4], "r": 2, "c": 2, "location": [12, 0]},
            ],
        }
    ],
    "aods": [{"id": 0, "site_seperation": 2, "r": 4, "c": 4}],
    "arch_range": [[0, -2], [20, 10]],
    "rydberg_range": [[[9, -1], [17, 9]]],
}


def init_event(n_qubits: int) -> CanonicalTraceEvent:
    return CanonicalTraceEvent(
        EventType.INIT,
        0,
        0,
        atoms=tuple(range(n_qubits)),
        end_positions=tuple((float(q), 0.0) for q in range(n_qubits)),
        end_regions=("storage",) * n_qubits,
    )


class TestHandCalculatedFidelity(unittest.TestCase):
    def test_empty_trace(self):
        result = score_trace([], MODEL, n_qubits=0)
        self.assertEqual(result.fidelity, 1.0)
        self.assertEqual(result.log_fidelity, 0.0)
        self.assertEqual(result.move_batches, 0)

    def test_one_qubit_gate(self):
        events = [
            init_event(1),
            CanonicalTraceEvent(
                EventType.ONE_QUBIT_GATE,
                0,
                52,
                atoms=(0,),
                gate_names=("u3",),
            ),
        ]
        result = score_trace(events, MODEL)
        self.assertAlmostEqual(result.fidelity, MODEL.one_qubit_fidelity, places=15)
        self.assertEqual(result.one_qubit_gates, 1)
        self.assertEqual(result.idle_time_us, (0.0,))

    def test_cz_and_idle_excitation(self):
        events = [
            init_event(3),
            CanonicalTraceEvent(
                EventType.TWO_QUBIT_GATE,
                0,
                MODEL.rydberg_duration_us,
                atoms=(0, 1),
                gate_pairs=((0, 1),),
                region_atoms=(0, 1, 2),
                region="entanglement:0",
            ),
        ]
        result = score_trace(events, MODEL)
        expected = (MODEL.two_qubit_fidelity * MODEL.idle_excitation_fidelity
                    * (1 - MODEL.rydberg_duration_us / MODEL.coherence_time_us))
        self.assertAlmostEqual(result.fidelity, expected, places=15)
        self.assertEqual(result.two_qubit_gates, 1)
        self.assertEqual(result.idle_excitations, 1)
        self.assertEqual(result.idle_time_us, (0.0, 0.0, MODEL.rydberg_duration_us))

    def test_one_complete_move_batch(self):
        events = [
            init_event(1),
            CanonicalTraceEvent(EventType.LOAD, 0, 15, atoms=(0,), batch_id="b0"),
            CanonicalTraceEvent(EventType.MOVE, 15, 25, atoms=(0,), batch_id="b0"),
            CanonicalTraceEvent(EventType.STORE, 25, 40, atoms=(0,), batch_id="b0"),
        ]
        result = score_trace(events, MODEL)
        expected = MODEL.transfer_fidelity**2 * (1 - 10 / MODEL.coherence_time_us)
        self.assertAlmostEqual(result.fidelity, expected, places=15)
        self.assertEqual(result.transfers, 2)
        self.assertEqual(result.move_batches, 1)
        self.assertEqual(result.move_time_us, 40)
        self.assertEqual(result.idle_time_us, (10.0,))

    def test_cross_layer_resident(self):
        t = MODEL.rydberg_duration_us
        events = [
            init_event(3),
            CanonicalTraceEvent(
                EventType.TWO_QUBIT_GATE,
                0,
                t,
                atoms=(0, 1),
                gate_pairs=((0, 1),),
                region_atoms=(0, 1, 2),
            ),
            CanonicalTraceEvent(EventType.WAIT, t, t + 1),
            CanonicalTraceEvent(
                EventType.TWO_QUBIT_GATE,
                t + 1,
                2 * t + 1,
                atoms=(1, 2),
                gate_pairs=((1, 2),),
                region_atoms=(0, 1, 2),
            ),
        ]
        result = score_trace(events, MODEL)
        coherence = ((1 - (1 + t) / MODEL.coherence_time_us) ** 2
                     * (1 - 1 / MODEL.coherence_time_us))
        expected = MODEL.two_qubit_fidelity**2 * MODEL.idle_excitation_fidelity**2 * coherence
        self.assertAlmostEqual(result.fidelity, expected, places=15)
        self.assertEqual(result.idle_excitations, 2)
        for actual, wanted in zip(result.idle_time_us, (1 + t, 1.0, 1 + t)):
            self.assertAlmostEqual(actual, wanted, places=14)

    def test_linear_coherence_ood_and_exponential_sensitivity(self):
        events = [init_event(1), CanonicalTraceEvent(EventType.WAIT, 0, MODEL.coherence_time_us)]
        result = score_trace(events, MODEL)
        self.assertTrue(result.ood)
        self.assertIsNone(result.fidelity)
        self.assertIsNone(result.log_fidelity)
        self.assertAlmostEqual(result.exponential_sensitivity_fidelity, math.exp(-1), places=15)
        self.assertIn("atoms: 0", result.warnings[0])

    def test_linear_coherence_warns_above_ten_percent(self):
        duration = MODEL.coherence_time_us * 0.11
        events = [init_event(1), CanonicalTraceEvent(EventType.WAIT, 0, duration)]
        result = score_trace(events, MODEL)
        self.assertFalse(result.ood)
        self.assertTrue(any("above 10%" in warning for warning in result.warnings))

    def test_log_components_sum_exactly_to_result(self):
        events = [
            init_event(2),
            CanonicalTraceEvent(
                EventType.TWO_QUBIT_GATE,
                0,
                MODEL.rydberg_duration_us,
                atoms=(0, 1),
                gate_pairs=((0, 1),),
                region_atoms=(0, 1),
            ),
        ]
        result = score_trace(events, MODEL)
        self.assertAlmostEqual(
            math.fsum(value for value in result.component_log_fidelity.values() if value is not None),
            result.log_fidelity,
            delta=1e-12,
        )


def equivalent_zair_and_na():
    distance = 10.0
    move_duration = math.sqrt(distance / MODEL.movement_acceleration)
    zair = {
        "instructions": [
            {"type": "init", "id": 0, "init_locs": [[0, 0, 0, 0], [1, 0, 0, 1]]},
            {
                "type": "1qGate",
                "id": 1,
                "begin_time": 0,
                "end_time": 52,
                "gates": [{"name": "u3", "q": 0}],
            },
            {
                "type": "rearrangeJob",
                "id": 2,
                "aod_qubits": [0, 1],
                "begin_locs": [[0, 0, 0, 0], [1, 0, 0, 1]],
                "end_locs": [[0, 1, 0, 0], [1, 2, 0, 0]],
                "begin_time": 52,
                "end_time": 82 + move_duration,
                "insts": [
                    {"type": "activate", "begin_time": 52, "end_time": 67},
                    {
                        "type": "move:big",
                        "begin_time": 67,
                        "end_time": 67 + move_duration,
                        "end_coord": [[{"id": 0, "x": 10, "y": 0}, {"id": 1, "x": 12, "y": 0}]],
                    },
                    {
                        "type": "deactivate",
                        "begin_time": 67 + move_duration,
                        "end_time": 82 + move_duration,
                    },
                ],
            },
            {
                "type": "rydberg",
                "id": 3,
                "zone_id": 0,
                "begin_time": 82 + move_duration,
                "end_time": 82 + move_duration + 0.36,
                "gates": [{"q0": 0, "q1": 1}],
            },
        ]
    }
    na = """
atom (0.0, 0.0) atom0
atom (2.0, 0.0) atom1
@+ u 1.0 2.0 3.0 atom0
@+ load [
  atom0
  atom1
]
@+ move [
  (10.0, 0.0) atom0
  (12.0, 0.0) atom1
]
@+ store [
  atom0
  atom1
]
@+ cz zone_cz0
"""
    return zair, na


class TestAdapters(unittest.TestCase):
    def test_na_atom_suffix_is_logical_id_not_declaration_order(self):
        na = """
atom (10, 0) atom2
atom (0, 0) atom0
atom (12, 0) atom3
atom (2, 0) atom1
@+ u 1 2 3 atom2
"""
        events = list(normalize_na(na))
        init, gate = events
        self.assertEqual(init.atoms, (0, 1, 2, 3))
        self.assertEqual(
            init.end_positions,
            ((0.0, 0.0), (2.0, 0.0), (10.0, 0.0), (12.0, 0.0)),
        )
        self.assertEqual(gate.atoms, (2,))

    def test_na_cz_uses_architecture_pairs_and_leaves_singleton_idle(self):
        na = """
atom (10, 0) atom0
atom (14, 0) atom1
atom (16, 0) atom2
@+ cz zone_cz0
"""
        events = list(normalize_na(na, architecture=ARCHITECTURE))
        cz = events[-1]
        self.assertEqual(cz.gate_pairs, ((1, 2),))
        self.assertEqual(cz.region_atoms, (0, 1, 2))
        self.assertEqual(score_trace(events, MODEL).idle_excitations, 1)

        explicit = list(normalize_na(
            na, architecture=ARCHITECTURE, gate_pairs=[((1, 2),)]
        ))[-1]
        self.assertEqual(explicit.gate_pairs, ((1, 2),))

    def test_zair_na_identical_four_metrics(self):
        zair, na = equivalent_zair_and_na()
        zair_events = list(normalize_zair(zair, architecture=ARCHITECTURE, model=MODEL))
        na_events = list(normalize_na(na, architecture=ARCHITECTURE, model=MODEL))
        self.assertEqual([event.kind for event in zair_events], [event.kind for event in na_events])
        zair_result = score_trace(zair_events, MODEL)
        na_result = score_trace(na_events, MODEL)
        self.assertAlmostEqual(zair_result.fidelity, na_result.fidelity, places=15)
        self.assertEqual(zair_result.move_batches, na_result.move_batches)
        self.assertAlmostEqual(zair_result.move_time_us, na_result.move_time_us, places=12)
        self.assertAlmostEqual(zair_result.duration_us, na_result.duration_us, places=12)
        self.assertEqual(zair_result.one_qubit_gates, 1)

    def test_event_json_round_trip(self):
        event = CanonicalTraceEvent(
            EventType.TWO_QUBIT_GATE,
            0,
            0.36,
            atoms=(0, 1),
            gate_pairs=((0, 1),),
            region_atoms=(0, 1, 2),
            metadata={"source": "toy"},
        )
        self.assertEqual(CanonicalTraceEvent.from_dict(event.to_dict()), event)

    def test_all_na_one_qubit_instructions_are_preserved(self):
        na = """
atom (0, 0) atom0
atom (2, 0) atom1
@+ u 1 2 3 atom0
@+ rz 0.5 atom1
@+ ry [
  0.2 atom0
  0.3 atom1
]
        """
        events = list(normalize_na(na))
        gates = [event for event in events if event.event_type is EventType.ONE_QUBIT_GATE]
        self.assertEqual([len(event.atoms) for event in gates], [1, 1, 1, 1])
        result = score_trace(events, MODEL)
        self.assertEqual(result.one_qubit_gates, 4)
        self.assertEqual(result.duration_us, 4 * MODEL.one_qubit_duration_us)

    def test_zair_multi_1q_block_is_globally_serial(self):
        code = {
            "instructions": [
                {"type": "init", "init_locs": [[0, 0, 0, 0], [1, 0, 0, 1]]},
                {
                    "type": "1qGate",
                    "gates": [{"name": "u1", "q": 0}, {"name": "u3", "q": 1}],
                    "begin_time": 0,
                    "end_time": 104,
                },
            ]
        }
        result = score_trace(normalize_zair(code, architecture=ARCHITECTURE), MODEL)
        expected_coherence = (1 - 52 / MODEL.coherence_time_us) ** 2
        expected = MODEL.one_qubit_fidelity**2 * expected_coherence
        self.assertEqual(result.duration_us, 104)
        self.assertEqual(result.idle_time_us, (52.0, 52.0))
        self.assertAlmostEqual(result.fidelity, expected, places=15)

    def test_na_multi_1q_block_is_globally_serial(self):
        na = """
atom (0, 0) atom0
atom (2, 0) atom1
@+ u [
  1 2 3 atom0
  1 2 3 atom1
]
"""
        result = score_trace(normalize_na(na), MODEL)
        expected_coherence = (1 - 52 / MODEL.coherence_time_us) ** 2
        expected = MODEL.one_qubit_fidelity**2 * expected_coherence
        self.assertEqual(result.duration_us, 104)
        self.assertEqual(result.idle_time_us, (52.0, 52.0))
        self.assertAlmostEqual(result.fidelity, expected, places=15)

    def test_na_move_phase_uses_longest_trajectory_once(self):
        na = """
atom (0, 0) atom0
atom (0, 2) atom1
@+ load [
  atom0
  atom1
]
@+ move [
  (9, 0) atom0
  (4, 2) atom1
]
@+ store [
  atom0
  atom1
]
"""
        result = score_trace(normalize_na(na), MODEL)
        expected_phase = math.sqrt(9 / MODEL.movement_acceleration)
        self.assertEqual(result.move_batches, 1)
        self.assertAlmostEqual(result.move_time_us, 30 + expected_phase, places=12)

    def test_zair_staggered_activation_is_one_complete_batch(self):
        code = {
            "instructions": [
                {"type": "init", "init_locs": [[0, 0, 0, 0], [1, 0, 1, 0]]},
                {
                    "type": "rearrangeJob",
                    "id": 1,
                    "aod_qubits": [0, 1],
                    "begin_locs": [[0, 0, 0, 0], [1, 0, 1, 0]],
                    "end_locs": [[0, 1, 0, 0], [1, 2, 0, 0]],
                    "begin_time": 0,
                    "end_time": 55,
                    "insts": [
                        {"type": "activate", "row_id": [0], "row_y": [0], "col_id": [0], "col_x": [0],
                         "begin_time": 0, "end_time": 15},
                        {
                            "type": "move",
                            "row_id": [0],
                            "row_y_end": [1],
                            "col_id": [0],
                            "col_x_end": [0],
                            "begin_time": 15,
                            "end_time": 20,
                            "end_coord": [[{"id": 0, "x": 0, "y": 1}, {"id": 1, "x": 0, "y": 2}]],
                        },
                        {"type": "activate", "row_id": [1], "row_y": [2], "col_id": [], "col_x": [],
                         "begin_time": 20, "end_time": 35},
                        {
                            "type": "move:big",
                            "row_id": [0, 1],
                            "row_y_end": [0, 0],
                            "col_id": [0, 1],
                            "col_x_end": [10, 12],
                            "begin_time": 35,
                            "end_time": 40,
                            "end_coord": [[{"id": 0, "x": 10, "y": 0}, {"id": 1, "x": 12, "y": 0}]],
                        },
                        {"type": "deactivate", "begin_time": 40, "end_time": 55},
                    ],
                },
            ]
        }
        events = list(normalize_zair(code, architecture=ARCHITECTURE))
        movement = [event for event in events if event.batch_id == "zair:1"]
        self.assertEqual(
            [event.event_type for event in movement],
            [EventType.LOAD, EventType.MOVE, EventType.LOAD, EventType.MOVE, EventType.STORE],
        )
        result = score_trace(events, MODEL)
        self.assertEqual(result.move_batches, 1)
        self.assertEqual(result.transfers, 4)
        self.assertEqual(result.move_time_us, 55)

    def test_zair_preserves_legal_native_overlap(self):
        code = {
            "instructions": [
                {"type": "init", "init_locs": [[0, 0, 0, 0], [1, 0, 1, 0]]},
                {"type": "1qGate", "id": 1, "begin_time": 0, "end_time": 52,
                 "gates": [{"name": "u3", "q": 0}]},
                {"type": "rearrangeJob", "id": 2, "aod_qubits": [1],
                 "begin_locs": [[1, 0, 1, 0]], "end_locs": [[1, 1, 0, 0]],
                 "begin_time": 0, "end_time": 40,
                 "insts": [
                     {"type": "activate", "begin_time": 0, "end_time": 15},
                     {"type": "move", "begin_time": 15, "end_time": 25,
                      "end_coord": [[{"id": 1, "x": 10, "y": 0}]]},
                     {"type": "deactivate", "begin_time": 25, "end_time": 40},
                 ]},
            ]
        }
        result = score_trace(normalize_zair(code, architecture=ARCHITECTURE), MODEL)
        self.assertEqual(result.duration_us, 52)
        self.assertEqual(result.move_time_us, 40)

    def test_zair_legacy_point625_timeline_is_reproduction_only(self):
        code = {
            "instructions": [
                {"type": "init", "init_locs": [[0, 0, 0, 0]]},
                {"type": "1qGate", "begin_time": 0, "end_time": 0.625,
                 "gates": [{"name": "u3", "q": 0}]},
            ]
        }
        with self.assertRaisesRegex(TraceValidationError, "frozen 52-us"):
            list(normalize_zair(code, architecture=ARCHITECTURE))

    def test_cli_scores_zair(self):
        zair, _ = equivalent_zair_and_na()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "code.json"
            arch = directory / "arch.json"
            output = directory / "score.json"
            source.write_text(json.dumps(zair))
            arch.write_text(json.dumps(ARCHITECTURE))
            rc = evaluation_main(
                ["score", str(source), "--format", "zair", "--architecture", str(arch), "--output", str(output)]
            )
            self.assertEqual(rc, 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["move_batches"], 1)
            self.assertFalse(result["ood"])


class TestIllegalInput(unittest.TestCase):
    def test_na_atom_names_must_be_a_contiguous_logical_bijection(self):
        with self.assertRaisesRegex(TraceValidationError, "logical-id bijection"):
            list(normalize_na(
                "atom (0, 0) atom0\natom (2, 0) atom2"
            ))
        with self.assertRaisesRegex(TraceValidationError, "duplicate NA logical atom id"):
            list(normalize_na(
                "atom (0, 0) atom0\natom (2, 0) atom00"
            ))
        with self.assertRaisesRegex(TraceValidationError, "logical id as atomN"):
            list(normalize_na("atom (0, 0) ancilla"))

    def test_na_cz_rejects_nonpaired_architecture_sites(self):
        source = """
atom (10, 0) atom0
atom (16, 0) atom1
@+ cz zone_cz0
"""
        with self.assertRaisesRegex(TraceValidationError, "no executable gate pair"):
            list(normalize_na(source, architecture=ARCHITECTURE))

    def test_na_explicit_cz_pairs_are_revalidated_against_architecture(self):
        source = """
atom (10, 0) atom0
atom (14, 0) atom1
atom (16, 0) atom2
@+ cz zone_cz0
"""
        with self.assertRaisesRegex(TraceValidationError, "not a legal architecture pair"):
            list(normalize_na(
                source, architecture=ARCHITECTURE, gate_pairs=[((0, 1),)]
            ))

    def test_na_operation_names_are_exact_tokens(self):
        sources = (
            "atom (0, 0) atom0\n@+ unicorn atom0",
            "atom (10, 0) atom0\natom (12, 0) atom1\n@+ czombie zone_cz0",
        )
        for source in sources:
            with self.subTest(source=source):
                with self.assertRaises(UnsupportedOperationError):
                    list(normalize_na(source, architecture=ARCHITECTURE))

    def test_strict_replay_rejects_ghost_collision(self):
        events = [
            CanonicalTraceEvent(
                EventType.INIT, 0, 0, atoms=(0, 1),
                end_positions=((0.0, 0.0), (5.0, 0.0)),
                end_regions=("storage", "storage")),
            CanonicalTraceEvent(
                EventType.LOAD, 0, 15, atoms=(0,), batch_id="b0",
                start_positions=((0.0, 0.0),), end_positions=((0.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.MOVE, 15, 25, atoms=(0,), batch_id="b0",
                start_positions=((0.0, 0.0),), end_positions=((10.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.STORE, 25, 40, atoms=(0,), batch_id="b0",
                start_positions=((10.0, 0.0),), end_positions=((10.0, 0.0),)),
        ]
        with self.assertRaisesRegex(TraceValidationError, "ghost collision"):
            validate_trace_physics(events, n_qubits=2)

    def test_strict_replay_accepts_ghost_safe_move(self):
        events = [
            CanonicalTraceEvent(
                EventType.INIT, 0, 0, atoms=(0, 1),
                end_positions=((0.0, 0.0), (5.0, 5.0)),
                end_regions=("storage", "storage")),
            CanonicalTraceEvent(
                EventType.LOAD, 0, 15, atoms=(0,), batch_id="b0",
                start_positions=((0.0, 0.0),), end_positions=((0.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.MOVE, 15, 25, atoms=(0,), batch_id="b0",
                start_positions=((0.0, 0.0),), end_positions=((10.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.STORE, 25, 40, atoms=(0,), batch_id="b0",
                start_positions=((10.0, 0.0),), end_positions=((10.0, 0.0),)),
        ]
        self.assertEqual(
            validate_trace_physics(events, n_qubits=2)["ghost_hits"], 0)

    def test_strict_replay_treats_held_stationary_atom_as_ghost(self):
        events = [
            CanonicalTraceEvent(
                EventType.INIT, 0, 0, atoms=(0, 1),
                end_positions=((0.0, 0.0), (5.0, 0.0)),
                end_regions=("storage", "storage")),
            CanonicalTraceEvent(
                EventType.LOAD, 0, 15, atoms=(0,), batch_id="staggered",
                start_positions=((0.0, 0.0),), end_positions=((0.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.MOVE, 15, 20, atoms=(0,), batch_id="staggered",
                start_positions=((0.0, 0.0),), end_positions=((1.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.LOAD, 20, 35, atoms=(1,), batch_id="staggered",
                start_positions=((5.0, 0.0),), end_positions=((5.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.MOVE, 35, 40, atoms=(0,), batch_id="staggered",
                start_positions=((1.0, 0.0),), end_positions=((10.0, 0.0),)),
        ]
        with self.assertRaisesRegex(TraceValidationError, "ghost collision"):
            validate_trace_physics(events, n_qubits=2)

    def test_strict_replay_rejects_interleaved_aod_batches(self):
        events = [
            CanonicalTraceEvent(
                EventType.INIT, 0, 0, atoms=(0, 1),
                end_positions=((0.0, 0.0), (5.0, 5.0)),
                end_regions=("storage", "storage")),
            CanonicalTraceEvent(
                EventType.LOAD, 0, 15, atoms=(0,), batch_id="a",
                start_positions=((0.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.LOAD, 15, 30, atoms=(1,), batch_id="b",
                start_positions=((5.0, 5.0),)),
        ]
        with self.assertRaisesRegex(TraceValidationError, "cannot interleave"):
            validate_trace_physics(events, n_qubits=2)
        with self.assertRaisesRegex(TraceValidationError, "cannot interleave"):
            score_trace(events, MODEL, n_qubits=2)

    def test_strict_replay_allows_unrelated_gate_to_overlap_open_aod_batch(self):
        events = [
            CanonicalTraceEvent(
                EventType.INIT, 0, 0, atoms=(0, 1),
                end_positions=((0.0, 0.0), (5.0, 5.0)),
                end_regions=("storage", "storage")),
            CanonicalTraceEvent(
                EventType.LOAD, 0, 15, atoms=(0,), batch_id="a",
                start_positions=((0.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.ONE_QUBIT_GATE, 5, 57, atoms=(1,)),
            CanonicalTraceEvent(
                EventType.MOVE, 15, 20, atoms=(0,), batch_id="a",
                start_positions=((0.0, 0.0),), end_positions=((1.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.STORE, 20, 35, atoms=(0,), batch_id="a",
                end_positions=((1.0, 0.0),)),
        ]
        self.assertEqual(
            validate_trace_physics(events, n_qubits=2)["move_batches"], 1)

    def test_same_atom_operation_overlap_is_rejected(self):
        events = [
            init_event(1),
            CanonicalTraceEvent(EventType.ONE_QUBIT_GATE, 0, 52, atoms=(0,)),
            CanonicalTraceEvent(EventType.ONE_QUBIT_GATE, 10, 62, atoms=(0,)),
        ]
        with self.assertRaisesRegex(TraceValidationError, "overlapping"):
            score_trace(events, MODEL)

    def test_unknown_zair_instruction(self):
        code = {
            "instructions": [
                {"type": "init", "init_locs": [[0, 0, 0, 0]]},
                {"type": "measure", "begin_time": 0, "end_time": 1},
            ]
        }
        with self.assertRaises(UnsupportedOperationError):
            list(normalize_zair(code, architecture=ARCHITECTURE))

    def test_unknown_na_instruction(self):
        with self.assertRaises(UnsupportedOperationError):
            list(normalize_na("atom (0, 0) atom0\n@+ teleport atom0"))

    def test_duplicate_na_occupancy(self):
        with self.assertRaisesRegex(TraceValidationError, "same initial position"):
            list(normalize_na("atom (0, 0) atom0\natom (0, 0) atom1"))

    def test_na_cz_while_held(self):
        source = """
atom (0, 0) atom0
atom (2, 0) atom1
@+ load atom0
@+ cz zone_cz0
"""
        with self.assertRaisesRegex(TraceValidationError, "still held"):
            list(normalize_na(source))

    def test_incomplete_move_batch_is_rejected(self):
        events = [
            init_event(1),
            CanonicalTraceEvent(EventType.LOAD, 0, 15, atoms=(0,), batch_id="broken"),
            CanonicalTraceEvent(EventType.MOVE, 15, 16, atoms=(0,), batch_id="broken"),
        ]
        with self.assertRaisesRegex(TraceValidationError, "load, move and store"):
            score_trace(events, MODEL)

    def test_wrong_fixed_duration_is_rejected(self):
        events = [
            init_event(1),
            CanonicalTraceEvent(EventType.ONE_QUBIT_GATE, 0, 0.625, atoms=(0,)),
        ]
        with self.assertRaisesRegex(TraceValidationError, "duration must be 52"):
            score_trace(events, MODEL)

    def test_same_atom_operation_overlap_is_rejected(self):
        events = [
            init_event(1),
            CanonicalTraceEvent(EventType.ONE_QUBIT_GATE, 0, 52, atoms=(0,)),
            CanonicalTraceEvent(EventType.ONE_QUBIT_GATE, 40, 92, atoms=(0,)),
        ]
        with self.assertRaisesRegex(TraceValidationError, "atom 0 has overlapping"):
            score_trace(events, MODEL)


if __name__ == "__main__":
    unittest.main()
