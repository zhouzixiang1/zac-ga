"""Differential gates for the exact bounded-memory M3/M4 transition kernel."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.forecast import CachedForecastLayerProvider  # noqa: E402
from streaming.resident_transition import ResidentTransitionKernel  # noqa: E402
from zac.ds.architecture import Architecture  # noqa: E402
from zzx.zplacer import ResidentPlacer  # noqa: E402


def make_architecture():
    spec = json.loads((ROOT / "hardware_spec/toy_architecture.json").read_text())
    architecture = Architecture(spec)
    architecture.preprocessing()
    return architecture


def make_placer(initial, *, horizon, seed=0, ablation_policy="optimize"):
    return ResidentPlacer(
        initial,
        seed=seed,
        experiment_schema=2,
        method_id="ours_nl" if horizon == 0 else "ours_lk",
        objective="physical_log_fidelity",
        lookahead_horizon=horizon,
        engine="ga",
        fitness_cache=True,
        population_size=6,
        iterations=2,
        neighbors_per_solution=2,
        neighbor_sample_size=8,
        ablation_policy=ablation_policy,
    )


class TestResidentTransitionKernel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.architecture = make_architecture()

    def assert_batch_stream_equal(self, schedule, *, horizon, seed=0,
                                  ablation_policy="optimize"):
        n_qubits = 1 + max(q for layer in schedule for gate in layer for q in gate)
        initial = [(0, q, 0) for q in range(n_qubits)]

        batch = make_placer(
            initial, horizon=horizon, seed=seed,
            ablation_policy=ablation_policy)
        batch.run(
            self.architecture, [initial], schedule, True,
            [set() for _ in schedule])

        reads = []

        def load(layer):
            reads.append(layer)
            return schedule[layer]

        provider = CachedForecastLayerProvider(
            len(schedule), load, max_cached_layers=horizon + 2)
        streamed = make_placer(
            initial, horizon=horizon, seed=seed,
            ablation_policy=ablation_policy)
        kernel = ResidentTransitionKernel(
            streamed, self.architecture, initial, provider)

        rebuilt = [list(kernel.initial_mapping),
                   list(kernel.initial_gate_mapping)]
        streamed_logs = []
        while kernel.current_layer < len(schedule) - 1:
            transition = kernel.advance()
            rebuilt.extend([
                list(transition.boundary_mapping),
                list(transition.target_gate_mapping),
            ])
            streamed_logs.append(transition.decision_log)
            self.assertLessEqual(kernel.retained_mapping_count, 2)
            self.assertLessEqual(len(provider.cached_layers), horizon + 2)
        terminal = kernel.finish()
        rebuilt.append(list(terminal.boundary_mapping))
        streamed_logs.append(terminal.decision_log)

        self.assertEqual(rebuilt, batch.mapping)
        self.assertEqual(streamed_logs, batch.decision_log)
        self.assertEqual(streamed.registry.storage_site,
                         batch.registry.storage_site)
        self.assertEqual(streamed.registry.zone_seat, batch.registry.zone_seat)
        self.assertEqual(streamed.residency_commitments,
                         batch.residency_commitments)
        self.assertEqual(streamed.rng.getstate(), batch.rng.getstate())
        self.assertEqual(streamed.safety_rng.getstate(), batch.safety_rng.getstate())
        self.assertEqual(streamed.cache_stats.as_dict(), batch.cache_stats.as_dict())
        self.assertTrue(reads)

    def test_nl_batch_and_stream_are_exactly_equal(self):
        self.assert_batch_stream_equal(
            [
                [[0, 1], [2, 3]],
                [[1, 4], [3, 5]],
                [[0, 4]],
                [[2, 5]],
            ],
            horizon=0,
            seed=7,
        )

    def test_lk_batch_and_stream_are_exactly_equal(self):
        self.assert_batch_stream_equal(
            [
                [[0, 1]],
                [[2, 3]],
                [[0, 4]],
                [[1, 5]],
                [[4, 6]],
            ],
            horizon=2,
            seed=3,
        )

    def test_terminal_always_return_uses_shared_batch_semantics(self):
        self.assert_batch_stream_equal(
            [[[0, 1]], [[2, 3]], [[0, 2]]],
            horizon=2,
            ablation_policy="always_return",
        )

    def test_empty_schedule_retains_only_initial_mapping(self):
        initial = [(0, 0, 0), (0, 1, 0)]
        provider = CachedForecastLayerProvider(0, lambda _: ())
        placer = make_placer(initial, horizon=0)
        kernel = ResidentTransitionKernel(
            placer, self.architecture, initial, provider)
        self.assertTrue(kernel.finished)
        self.assertIsNone(kernel.initial_gate_mapping)
        self.assertEqual(kernel.retained_mapping_count, 1)
        with self.assertRaisesRegex(RuntimeError, "already finished"):
            kernel.advance()

    def test_finish_rejects_nonterminal_layer(self):
        initial = [(0, q, 0) for q in range(4)]
        schedule = [[[0, 1]], [[2, 3]]]
        provider = CachedForecastLayerProvider(
            len(schedule), lambda layer: schedule[layer])
        kernel = ResidentTransitionKernel(
            make_placer(initial, horizon=0), self.architecture,
            initial, provider)
        with self.assertRaisesRegex(RuntimeError, "advance all"):
            kernel.finish()


if __name__ == "__main__":
    unittest.main()
