"""Exact differential tests for incremental ZAIR event normalization."""

from __future__ import annotations

from copy import deepcopy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from evaluation import (  # noqa: E402
    CanonicalTraceEvent,
    EventType,
    TraceValidationError,
    normalize_zair,
)
from streaming.trace_pipeline import (  # noqa: E402
    IncrementalTracePipeline,
    IncrementalTraceScorer,
    IncrementalTraceValidator,
    consume_trace_incrementally,
)
from streaming.zac_m1_transition import ZACM1TransitionKernel  # noqa: E402
from streaming.zac_route_transition import ZACRouteTransitionDriver  # noqa: E402
from streaming.zair_instruction_stream import (  # noqa: E402
    IncrementalZairNormalizer,
    zair_chronological_sort_key,
)
from test_zac_route_transition import _architecture, _fixture  # noqa: E402


def _resident_native(architecture):
    mappings, schedule, gate_ids, one_qubit, initial_one_qubit = _fixture()
    driver = ZACRouteTransitionDriver(
        architecture,
        mappings[0],
        initial_one_qubit_gates=initial_one_qubit,
        placer_kind="resident",
    )
    instructions = list(deepcopy(driver.initial_instructions))
    for layer in range(len(schedule)):
        result = driver.route_layer(
            layer,
            mappings[2 * layer],
            mappings[2 * layer + 1],
            mappings[2 * layer + 2],
            schedule[layer],
            gate_ids[layer],
            one_qubit[layer],
        )
        instructions.extend(deepcopy(result.instructions))
    return instructions, len(mappings[0])


def _m1_native(architecture):
    initial = [(0, 0, q) for q in range(8)]
    schedule = [
        [[0, 1], [2, 3]],
        [[1, 4], [3, 5]],
        [[0, 4], [2, 5]],
        [[0, 6], [2, 7]],
    ]
    placement = ZACM1TransitionKernel(architecture, initial, schedule)
    state = placement.bootstrap()
    mappings = [list(state.b_l), list(state.g_l)]
    for _layer in range(len(schedule)):
        transition = placement.step(state)
        mappings.append(list(transition.b_l_plus_1))
        if transition.g_l_plus_1 is not None:
            mappings.append(list(transition.g_l_plus_1))
            assert transition.next_state is not None
            state = transition.next_state

    next_gate_id = 0
    gate_ids = []
    for stage in schedule:
        gate_ids.append(list(range(next_gate_id, next_gate_id + len(stage))))
        next_gate_id += len(stage)
    one_qubit = [
        [("u1", 0)],
        [("u2", 3), ("u3", 6)],
        [],
        [("u1", 7)],
    ]
    initial_one_qubit = [("u3", 5)]
    driver = ZACRouteTransitionDriver(
        architecture,
        mappings[0],
        initial_one_qubit_gates=initial_one_qubit,
        placer_kind="zac",
    )
    instructions = list(deepcopy(driver.initial_instructions))
    for layer in range(len(schedule)):
        result = driver.route_layer(
            layer,
            mappings[2 * layer],
            mappings[2 * layer + 1],
            mappings[2 * layer + 2],
            schedule[layer],
            gate_ids[layer],
            one_qubit[layer],
        )
        instructions.extend(deepcopy(result.instructions))
    return instructions, len(initial)


def _pipeline(n_qubits: int, event_order: str) -> IncrementalTracePipeline:
    return IncrementalTracePipeline(
        IncrementalTraceValidator(n_qubits, event_order=event_order),
        IncrementalTraceScorer(n_qubits, event_order=event_order),
    )


class TestIncrementalZairNormalizer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.architecture, cls.spec = _architecture()
        cls.fixtures = {
            "resident": _resident_native(cls.architecture),
            "m1": _m1_native(cls.architecture),
        }

    def test_event_dictionaries_are_exact_for_resident_and_m1(self):
        for name, (instructions, _n_qubits) in self.fixtures.items():
            with self.subTest(name=name):
                expected = list(normalize_zair(
                    {"instructions": deepcopy(instructions)},
                    architecture=self.spec,
                ))
                with patch(
                    "streaming.zair_instruction_stream._load_architecture",
                    wraps=__import__(
                        "streaming.zair_instruction_stream",
                        fromlist=["_load_architecture"],
                    )._load_architecture,
                ) as load_architecture:
                    normalizer = IncrementalZairNormalizer(self.spec)
                    dependency_events = list(
                        normalizer.consume_many(deepcopy(instructions)))
                self.assertEqual(load_architecture.call_count, 1)
                self.assertEqual(
                    [event.to_dict() for event in expected],
                    [event.to_dict() for event in sorted(
                        dependency_events,
                        key=zair_chronological_sort_key,
                    )],
                )
                self.assertEqual(
                    normalizer.instructions_consumed, len(instructions))
                self.assertEqual(normalizer.n_qubits, _n_qubits)
                self.assertEqual(
                    sorted(set(event.source_index for event in dependency_events)),
                    list(range(len(instructions))),
                )

    def test_dependency_pipeline_is_exact_to_chronological_batch(self):
        saw_global_regression = False
        for name, (instructions, n_qubits) in self.fixtures.items():
            with self.subTest(name=name):
                chronological = list(normalize_zair(
                    {"instructions": deepcopy(instructions)},
                    architecture=self.spec,
                ))
                normalizer = IncrementalZairNormalizer(self.spec)
                dependency = list(
                    normalizer.consume_many(deepcopy(instructions)))
                saw_global_regression |= any(
                    right.start_us < left.start_us - 1e-9
                    for left, right in zip(dependency, dependency[1:])
                )
                expected = consume_trace_incrementally(
                    chronological,
                    n_qubits=n_qubits,
                )
                actual = consume_trace_incrementally(
                    dependency,
                    n_qubits=n_qubits,
                    event_order="dependency",
                )
                # This compares both FidelityResult and ValidationSummary,
                # including all decomposition terms and all ledger hashes.
                self.assertEqual(actual, expected)
        self.assertTrue(
            saw_global_regression,
            "fixture must exercise dependency order rather than sorted order",
        )

    def test_dependency_checkpoint_resume_is_event_exact(self):
        instructions, n_qubits = self.fixtures["resident"]
        dependency = list(IncrementalZairNormalizer(
            self.spec).consume_many(deepcopy(instructions)))
        uninterrupted = _pipeline(n_qubits, "dependency")
        for event in dependency:
            uninterrupted.consume(event)
        expected = uninterrupted.finalize()

        # Stop inside a native rearrangeJob after MOVE but before STORE.  This
        # checkpoints the per-atom timeline and the live single-AOD dependency.
        split = next(
            index + 1 for index, event in enumerate(dependency)
            if event.event_type is EventType.MOVE
        )
        interrupted = _pipeline(n_qubits, "dependency")
        for event in dependency[:split]:
            interrupted.consume(event)
        state = json.loads(json.dumps(
            interrupted.state_dict(), sort_keys=True))
        self.assertEqual(
            state["validator"]["event_order"], "dependency")
        self.assertEqual(
            state["scorer"]["event_order"], "dependency")
        resumed = IncrementalTracePipeline.from_state(state)
        for event in dependency[split:]:
            resumed.consume(event)
        self.assertEqual(resumed.finalize(), expected)

    def test_normalizer_checkpoint_preserves_global_source_index(self):
        instructions, _n_qubits = self.fixtures["m1"]
        split = len(instructions) // 2
        first = IncrementalZairNormalizer(self.spec)
        prefix = list(first.consume_many(deepcopy(instructions[:split])))
        state = json.loads(json.dumps(first.state_dict(), sort_keys=True))
        resumed = IncrementalZairNormalizer.from_state(
            state, architecture=self.spec)
        suffix = list(resumed.consume_many(deepcopy(instructions[split:])))
        expected = list(IncrementalZairNormalizer(
            self.spec).consume_many(deepcopy(instructions)))
        self.assertEqual(
            [event.to_dict() for event in prefix + suffix],
            [event.to_dict() for event in expected],
        )

    def test_dependency_mode_rejects_resource_local_time_regressions(self):
        init = CanonicalTraceEvent(
            EventType.INIT, 0.0, 0.0, atoms=(0, 1, 2),
            end_positions=((0.0, 0.0), (1.0, 0.0), (2.0, 0.0)),
            end_regions=("storage", "storage", "storage"),
        )

        scorer = IncrementalTraceScorer(3, event_order="dependency")
        scorer.consume(init)
        scorer.consume(CanonicalTraceEvent(
            EventType.ONE_QUBIT_GATE, 100.0, 152.0,
            atoms=(0,), gate_names=("u1",)))
        with self.assertRaisesRegex(TraceValidationError, "global one-qubit"):
            scorer.consume(CanonicalTraceEvent(
                EventType.ONE_QUBIT_GATE, 0.0, 52.0,
                atoms=(1,), gate_names=("u2",)))

        scorer = IncrementalTraceScorer(3, event_order="dependency")
        scorer.consume(init)
        scorer.consume(CanonicalTraceEvent(
            EventType.TWO_QUBIT_GATE, 100.0, 100.36,
            atoms=(0, 1), gate_pairs=((0, 1),),
            region_atoms=(0, 1), gate_names=("cz",)))
        with self.assertRaisesRegex(TraceValidationError, "atom 0"):
            scorer.consume(CanonicalTraceEvent(
                EventType.TWO_QUBIT_GATE, 0.0, 0.36,
                atoms=(0, 2), gate_pairs=((0, 2),),
                region_atoms=(0, 2), gate_names=("cz",)))

        scorer = IncrementalTraceScorer(3, event_order="dependency")
        scorer.consume(init)
        for event in (
            CanonicalTraceEvent(
                EventType.LOAD, 100.0, 115.0, atoms=(0,), batch_id="a",
            ),
            CanonicalTraceEvent(
                EventType.MOVE, 115.0, 116.0, atoms=(0,), batch_id="a",
            ),
            CanonicalTraceEvent(
                EventType.STORE, 116.0, 131.0, atoms=(0,), batch_id="a",
            ),
        ):
            scorer.consume(event)
        with self.assertRaisesRegex(TraceValidationError, "single AOD"):
            scorer.consume(CanonicalTraceEvent(
                EventType.LOAD, 0.0, 15.0, atoms=(1,), batch_id="b"))

    def test_long_dependency_stream_retains_only_qubit_sized_state(self):
        n_qubits = 6
        init = {
            "id": 0,
            "type": "init",
            "init_locs": [[q, 0, 0, q] for q in range(n_qubits)],
            "begin_time": 0,
            "end_time": 0,
        }
        normalizer = IncrementalZairNormalizer(self.spec)
        pipeline = _pipeline(n_qubits, "dependency")
        for event in normalizer.consume(init):
            pipeline.consume(event)

        early_size = None
        for source_index in range(1, 4_001):
            begin = (source_index - 1) * 52.0
            instruction = {
                "id": source_index,
                "type": "1qGate",
                "gates": [{"name": "u1", "q": source_index % n_qubits}],
                "begin_time": begin,
                "end_time": begin + 52.0,
            }
            for event in normalizer.consume(instruction):
                pipeline.consume(event)
            if source_index == 100:
                early_size = len(json.dumps({
                    "normalizer": normalizer.state_dict(),
                    "pipeline": pipeline.state_dict(),
                }, sort_keys=True))

        late_state = {
            "normalizer": normalizer.state_dict(),
            "pipeline": pipeline.state_dict(),
        }
        late_size = len(json.dumps(late_state, sort_keys=True))
        assert early_size is not None
        # Only decimal representations of counters/timestamps grow; no
        # collection gains one entry per event.
        self.assertLessEqual(late_size, early_size + 256)
        self.assertEqual(
            len(late_state["normalizer"]["current"]), n_qubits)
        self.assertNotIn("events", json.dumps(late_state, sort_keys=True))
        self.assertEqual(pipeline.finalize()["validation"]["event_count"], 4_001)


if __name__ == "__main__":
    unittest.main()
