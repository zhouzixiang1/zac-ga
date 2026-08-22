"""Differential tests for the exact bounded-window formal M1 transition."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.zac_m1_transition import (  # noqa: E402
    ZACM1TransitionKernel,
    adjacent_reuse_qubits,
)
from zac.ds.architecture import Architecture  # noqa: E402
from zac.placer.vmplacer import VertexMatchingPlacer  # noqa: E402
from zac.zac import ZAC  # noqa: E402


def _freeze(mapping):
    return tuple(tuple(location) for location in mapping)


def _initial_mapping(n_qubits: int):
    return [(0, q // 10, q % 10) for q in range(n_qubits)]


class _AuditStageProvider:
    def __init__(self, schedule):
        self.schedule = schedule
        self.reads: list[int] = []

    @property
    def stage_count(self):
        return len(self.schedule)

    def stage(self, index):
        self.reads.append(index)
        return self.schedule[index]


def _production_reuse(schedule, n_qubits):
    compiler = ZAC()
    compiler.n_q = n_qubits
    compiler.gate_scheduling = deepcopy(schedule)
    compiler.collect_reuse_qubit()
    return compiler.reuse_qubit


def _production_batch(architecture, initial, schedule):
    reuse = _production_reuse(schedule, len(initial))
    placer = VertexMatchingPlacer(deepcopy(initial))
    with contextlib.redirect_stdout(io.StringIO()):
        placer.run(
            architecture,
            [deepcopy(initial)],
            deepcopy(schedule),
            True,
            reuse,
        )
    return placer.mapping, reuse


class TestZACM1Transition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = json.loads(
            (ROOT / "hardware_spec" / "toy_architecture.json").read_text())
        cls.architecture = Architecture(spec)
        cls.architecture.preprocessing()

    def test_adjacent_reuse_is_exact_production_pair_semantics(self):
        schedules = [
            [[[0, 1]], [[1, 2]]],
            [[[0, 1], [2, 3]], [[1, 4], [3, 5]]],
            [[[0, 1], [2, 3]], [[0, 1], [4, 5]]],
            [[[0, 1]], [[2, 3]]],
        ]
        for schedule in schedules:
            with self.subTest(schedule=schedule):
                expected = _production_reuse(schedule, 6)[0]
                actual = adjacent_reuse_qubits(
                    schedule[0], schedule[1], n_qubits=6)
                self.assertEqual(actual, frozenset(expected))

    def test_every_transition_matches_batch_vertex_placer_exactly(self):
        cases = {
            "single": (
                4,
                [[[0, 1], [2, 3]]],
            ),
            "no_reuse": (
                6,
                [[[0, 1]], [[2, 3]], [[4, 5]]],
            ),
            "chain": (
                6,
                [[[0, 1]], [[1, 2]], [[2, 3]], [[3, 4]], [[4, 5]]],
            ),
            "parallel_reuse": (
                8,
                [
                    [[0, 1], [2, 3]],
                    [[1, 4], [3, 5]],
                    [[0, 4], [2, 5]],
                    [[0, 6], [2, 7]],
                ],
            ),
            "pair_reappears": (
                8,
                [
                    [[0, 1], [2, 3], [4, 5]],
                    [[0, 1], [3, 6], [5, 7]],
                    [[1, 2], [4, 7], [0, 6]],
                ],
            ),
        }

        observed_reuse_candidate = False
        for name, (n_qubits, schedule) in cases.items():
            with self.subTest(case=name):
                initial = _initial_mapping(n_qubits)
                batch_mapping, batch_reuse = _production_batch(
                    self.architecture, initial, schedule)
                provider = _AuditStageProvider(schedule)
                kernel = ZACM1TransitionKernel(
                    self.architecture, initial, provider)

                state = kernel.bootstrap()
                self.assertEqual(state.b_l, _freeze(batch_mapping[0]))
                self.assertEqual(state.g_l, _freeze(batch_mapping[1]))
                self.assertEqual(provider.reads, [0])

                for layer in range(len(schedule)):
                    reads_before = len(provider.reads)
                    result = kernel.step(state)
                    reads = provider.reads[reads_before:]
                    self.assertTrue(reads)
                    self.assertTrue(all(
                        layer <= index <= min(layer + 2, len(schedule) - 1)
                        for index in reads))
                    self.assertEqual(result.layer, layer)
                    self.assertEqual(result.b_l, _freeze(batch_mapping[2 * layer]))
                    self.assertEqual(result.g_l, _freeze(batch_mapping[2 * layer + 1]))
                    self.assertEqual(
                        result.b_l_plus_1,
                        _freeze(batch_mapping[2 * layer + 2]),
                    )
                    self.assertEqual(
                        result.route_triplet,
                        tuple(_freeze(batch_mapping[2 * layer + offset])
                              for offset in range(3)),
                    )
                    self.assertEqual(
                        result.selected_reuse,
                        bool(batch_reuse[layer]),
                    )
                    observed_reuse_candidate |= bool(result.reuse_candidates)

                    if layer + 1 < len(schedule):
                        self.assertEqual(
                            result.g_l_plus_1,
                            _freeze(batch_mapping[2 * layer + 3]),
                        )
                        self.assertIsNotNone(result.next_state)
                        state = result.next_state
                    else:
                        self.assertIsNone(result.g_l_plus_1)
                        self.assertIsNone(result.next_state)

        self.assertTrue(observed_reuse_candidate)

    def test_sequence_provider_and_stage_method_provider_are_equivalent(self):
        schedule = [[[0, 1]], [[1, 2]], [[2, 3]]]
        initial = _initial_mapping(4)
        sequence_kernel = ZACM1TransitionKernel(
            self.architecture, initial, schedule)
        method_kernel = ZACM1TransitionKernel(
            self.architecture, initial, _AuditStageProvider(schedule))
        left, right = sequence_kernel.bootstrap(), method_kernel.bootstrap()
        while True:
            self.assertEqual(left, right)
            left_result = sequence_kernel.step(left)
            right_result = method_kernel.step(right)
            self.assertEqual(left_result, right_result)
            if left_result.next_state is None:
                break
            left, right = left_result.next_state, right_result.next_state

    def test_rejects_nonphysical_stage_and_foreign_state_width(self):
        initial = _initial_mapping(4)
        with self.assertRaisesRegex(ValueError, "pairwise disjoint"):
            ZACM1TransitionKernel(
                self.architecture, initial,
                [[[0, 1], [1, 2]]],
            ).bootstrap()

        kernel = ZACM1TransitionKernel(
            self.architecture, initial, [[[0, 1]]])
        state = kernel.bootstrap()
        bad_state = type(state)(
            layer=state.layer,
            b_l=state.b_l[:-1],
            g_l=state.g_l,
        )
        with self.assertRaisesRegex(ValueError, "width"):
            kernel.step(bad_state)


if __name__ == "__main__":
    unittest.main()
