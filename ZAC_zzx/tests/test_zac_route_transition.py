"""Exact batch differential tests for the bounded resident route driver."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from copy import deepcopy
from math import hypot
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation import normalize_zair, validate_trace_physics  # noqa: E402
from streaming.zac_m1_transition import ZACM1TransitionKernel  # noqa: E402
from streaming.zac_route_transition import ZACRouteTransitionDriver  # noqa: E402
from zac.ds.architecture import Architecture  # noqa: E402
from zzx.zac_zzx import ZAC_zzx  # noqa: E402
from zzx.zcost import greedy_phase_batches, phase_batches  # noqa: E402


def _architecture():
    spec = json.loads(
        (ROOT / "hardware_spec" / "toy_architecture.json").read_text())
    spec["operation_duration"] = {
        "rydberg": 0.36,
        "1qGate": 52.0,
        "atom_transfer": 15.0,
    }
    architecture = Architecture(spec)
    architecture.preprocessing()
    return architecture, spec


def _compiler(architecture, n_qubits, placer_kind="resident"):
    compiler = ZAC_zzx()
    compiler.architecture = architecture
    compiler.n_q = n_qubits
    compiler.placer_kind = placer_kind
    compiler.routing_strategy = (
        "greedy" if placer_kind == "zac" else "coloring")
    compiler.dynamic_placement = True
    compiler.reuse = True
    compiler.use_window = True
    compiler.window_size = 1000
    compiler.zzx_route_log = []
    compiler.zzx_ghost_splits = 0
    return compiler


def _route_batch(
    architecture, mappings, schedule, gate_ids, one_qubit, initial_one_qubit,
    placer_kind="resident",
):
    compiler = _compiler(
        architecture, len(mappings[0]), placer_kind=placer_kind)
    compiler.qubit_mapping = deepcopy(mappings)
    compiler.gate_scheduling = deepcopy(schedule)
    compiler.gate_scheduling_idx = deepcopy(gate_ids)
    compiler.gate_1q_scheduling = deepcopy(one_qubit)
    compiler.dict_g_1q_parent = {-1: deepcopy(initial_one_qubit)}
    with contextlib.redirect_stdout(io.StringIO()):
        compiler.route_qubit()
    return compiler


def _fixture():
    homes = [(0, 0, q) for q in range(6)]
    left0, right0 = (1, 0, 0), (2, 0, 0)
    left2, right2 = (1, 0, 2), (2, 0, 2)

    b0 = list(homes)
    g0 = list(b0)
    g0[0], g0[1] = left0, right0
    b1 = list(g0)                         # q0/q1 idle through the next pulse

    g1 = list(b1)
    g1[2], g1[3] = left2, right2
    b2 = list(g1)
    b2[0], b2[1], b2[3] = homes[0], homes[1], homes[3]

    g2 = list(b2)
    g2[1], g2[2] = right2, left2
    b3 = list(g2)                         # formal terminal: no forced return

    mappings = [b0, g0, b1, g1, b2, g2, b3]
    schedule = [[[0, 1]], [[2, 3]], [[1, 2]]]
    gate_ids = [[0], [1], [2]]
    one_qubit = [[("u1", 0)], [("u2", 2), ("u3", 4)], []]
    initial_one_qubit = [("u3", 4), ("u1", 5)]
    return mappings, schedule, gate_ids, one_qubit, initial_one_qubit


class TestZACRouteTransition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.architecture, cls.spec = _architecture()

    def test_single_leg_coloring_fast_path_matches_registered_batchers(self):
        mapping_from = [(0, 0, 0), (0, 9, 9)]
        mapping_to = [(0, 1, 0), (0, 9, 9)]
        remain = [0]
        for strategy in ("coloring", "greedy"):
            compiler = _compiler(self.architecture, len(mapping_from))
            compiler.routing_strategy = strategy
            vectors = compiler.graph_construction(
                remain, mapping_from, mapping_to)
            legs = [
                (hypot(x0 - x1, y0 - y1), x0, y0, x1, y1)
                for x0, x1, y0, y1 in vectors
            ]
            ghosts = [
                (q, *self.architecture.exact_SLM_location_tuple(location))
                for q, location in enumerate(mapping_from)
            ]
            if strategy == "greedy":
                expected = greedy_phase_batches(
                    legs, ghosts=ghosts, owners=remain)
            else:
                expected = phase_batches(
                    legs, ghosts=ghosts, owners=remain,
                    exact_threshold=compiler.zzx_exact_threshold,
                    node_budget=compiler.zzx_node_budget)
            with self.subTest(strategy=strategy):
                actual = compiler._coloring_batches(
                    remain, mapping_from, mapping_to)
                self.assertEqual(actual, expected)

    def test_single_leg_fast_path_still_runs_waypoint_audit(self):
        mapping_from = [(0, 0, 0), (0, 9, 9)]
        mapping_to = [(0, 1, 0), (0, 9, 9)]
        compiler = _compiler(self.architecture, len(mapping_from))
        waypoint = (0, 2, 0)
        forced_hit = [(1, 27.0, 27.0, (0.0, 0.0),
                       (0.0, 3.0), 0.5)]
        with patch("zzx.zac_zzx.ghost_hits", return_value=forced_hit), \
                patch.object(
                    compiler, "_safe_slm_waypoint",
                    return_value=waypoint) as safe_waypoint:
            result = compiler._coloring_batches(
                [0], mapping_from, mapping_to)
        self.assertEqual(result, (1, [[0]], "heuristic"))
        safe_waypoint.assert_called_once()
        self.assertEqual(
            compiler._zzx_waypoint_plan[
                (0, tuple(mapping_from[0]), tuple(mapping_to[0]))],
            waypoint)

    def test_single_leg_fast_path_fails_when_audit_cannot_repair(self):
        mapping_from = [(0, 0, 0), (0, 9, 9)]
        mapping_to = [(0, 1, 0), (0, 9, 9)]
        compiler = _compiler(self.architecture, len(mapping_from))
        forced_hit = [(1, 27.0, 27.0, (0.0, 0.0),
                       (0.0, 3.0), 0.5)]
        with patch("zzx.zac_zzx.ghost_hits", return_value=forced_hit), \
                patch.object(
                    compiler, "_safe_slm_waypoint", return_value=None
                ) as safe_waypoint:
            with self.assertRaisesRegex(
                    ValueError, "ghost-safe routing could not place atoms"):
                compiler._coloring_batches([0], mapping_from, mapping_to)
        self.assertGreaterEqual(safe_waypoint.call_count, 1)

    def test_streamed_native_chunks_are_dictionary_exact_to_batch(self):
        mappings, schedule, gate_ids, one_qubit, initial_one_qubit = _fixture()
        batch = _route_batch(
            self.architecture, mappings, schedule, gate_ids, one_qubit,
            initial_one_qubit)

        driver = ZACRouteTransitionDriver(
            self.architecture,
            mappings[0],
            initial_one_qubit_gates=initial_one_qubit,
        )
        streamed = list(deepcopy(driver.initial_instructions))
        route_log = []
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
            streamed.extend(deepcopy(result.instructions))
            route_log.extend(deepcopy(result.route_log))

        self.assertEqual(streamed, batch.result_json["instructions"])
        self.assertEqual(route_log, batch.zzx_route_log)
        self.assertEqual(
            driver.compiler.result_json["runtime"],
            batch.result_json["runtime"],
        )
        self.assertEqual(
            driver.compiler.zzx_ghost_splits,
            batch.zzx_ghost_splits,
        )

        validation = validate_trace_physics(
            normalize_zair(
                {"instructions": streamed},
                architecture=self.spec,
            ),
            n_qubits=len(mappings[0]),
        )
        self.assertEqual(validation["ghost_hits"], 0)

    def test_m1_kernel_mappings_route_dictionary_exact_to_batch(self):
        initial = [(0, 0, q) for q in range(8)]
        schedule = [
            [[0, 1], [2, 3]],
            [[1, 4], [3, 5]],
            [[0, 4], [2, 5]],
            [[0, 6], [2, 7]],
        ]
        placement = ZACM1TransitionKernel(
            self.architecture, initial, schedule)
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
            gate_ids.append(list(range(
                next_gate_id, next_gate_id + len(stage))))
            next_gate_id += len(stage)
        one_qubit = [
            [("u1", 0)],
            [("u2", 3), ("u3", 6)],
            [],
            [("u1", 7)],
        ]
        initial_one_qubit = [("u3", 5)]
        batch = _route_batch(
            self.architecture,
            mappings,
            schedule,
            gate_ids,
            one_qubit,
            initial_one_qubit,
            placer_kind="zac",
        )

        driver = ZACRouteTransitionDriver(
            self.architecture,
            mappings[0],
            initial_one_qubit_gates=initial_one_qubit,
            placer_kind="zac",
        )
        streamed = list(deepcopy(driver.initial_instructions))
        route_log = []
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
            streamed.extend(deepcopy(result.instructions))
            route_log.extend(deepcopy(result.route_log))

        self.assertEqual(streamed, batch.result_json["instructions"])
        self.assertEqual(route_log, batch.zzx_route_log)
        self.assertEqual(
            driver.compiler.result_json["runtime"],
            batch.result_json["runtime"],
        )
        self.assertEqual(
            driver.compiler.zzx_ghost_splits,
            batch.zzx_ghost_splits,
        )
        self.assertEqual(driver.compiler.placer_kind, "zac")
        self.assertEqual(driver.compiler.routing_strategy, "greedy")
        validation = validate_trace_physics(
            normalize_zair(
                {"instructions": streamed},
                architecture=self.spec,
            ),
            n_qubits=len(initial),
        )
        self.assertEqual(validation["ghost_hits"], 0)

    def test_active_instruction_window_is_bounded_across_many_layers(self):
        homes = [(0, 0, q) for q in range(4)]
        gate = list(homes)
        gate[0], gate[1] = (1, 0, 0), (2, 0, 0)
        driver = ZACRouteTransitionDriver(self.architecture, homes)
        peak_after_prune = driver.instructions.active_count
        final_result = None
        for layer in range(160):
            final_result = driver.route_layer(
                layer,
                homes,
                gate,
                homes,
                [[0, 1]],
                [layer],
            )
            peak_after_prune = max(
                peak_after_prune, result_count := final_result.active_instruction_count)
            self.assertLessEqual(result_count, 8)

        assert final_result is not None
        self.assertGreater(len(driver.instructions), 400)  # global ids keep growing
        self.assertLessEqual(peak_after_prune, 8)          # resident records do not
        self.assertLessEqual(final_result.peak_active_instruction_count, 12)
        self.assertLessEqual(driver.compiler.site_dependency.__len__(), 4)
        self.assertEqual(driver.compiler.zzx_route_log, [])

    def test_layer_and_mapping_contracts_fail_closed(self):
        homes = [(0, 0, q) for q in range(4)]
        gate = list(homes)
        gate[0], gate[1] = (1, 0, 0), (2, 0, 0)
        driver = ZACRouteTransitionDriver(self.architecture, homes)
        with self.assertRaisesRegex(ValueError, "expected physical layer"):
            driver.route_layer(1, homes, gate, homes, [[0, 1]], [0])

        invalid_gate = list(gate)
        invalid_gate[3] = (0, 1, 3)
        with self.assertRaisesRegex(ValueError, "non-participant"):
            driver.route_layer(
                0, homes, invalid_gate, homes, [[0, 1]], [0])

        with self.assertRaisesRegex(ValueError, "placer_kind"):
            ZACRouteTransitionDriver(
                self.architecture, homes, placer_kind="proxy")


if __name__ == "__main__":
    unittest.main()
