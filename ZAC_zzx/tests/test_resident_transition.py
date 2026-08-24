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


def semantic_decision_log(rows):
    return [
        {key: value for key, value in row.items()
         if key not in {
             "horizon_selection_ns", "search_kernel_ns", "marshal_ns",
             "backend_search_kernel_ns", "fitness_ns",
             "backend_selection_ns",
         }}
        for row in rows
    ]


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
                                  ablation_policy="optimize",
                                  leading_one_qubit_gates=()):
        n_qubits = 1 + max(q for layer in schedule for gate in layer for q in gate)
        initial = [(0, q, 0) for q in range(n_qubits)]

        batch = make_placer(
            initial, horizon=horizon, seed=seed,
            ablation_policy=ablation_policy)
        batch.run(
            self.architecture, [initial], schedule, True,
            [set() for _ in schedule],
            leading_one_qubit_gates=leading_one_qubit_gates)

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
            streamed, self.architecture, initial, provider,
            leading_one_qubit_gates=leading_one_qubit_gates)

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
        self.assertEqual(
            semantic_decision_log(streamed_logs),
            semantic_decision_log(batch.decision_log),
        )
        self.assertEqual(streamed.registry.storage_site,
                         batch.registry.storage_site)
        self.assertEqual(streamed.registry.zone_seat, batch.registry.zone_seat)
        self.assertEqual(streamed.registry.resident_idle_exposures,
                         batch.registry.resident_idle_exposures)
        self.assertEqual(streamed.registry.resident_idle_time_us,
                         batch.registry.resident_idle_time_us)
        self.assertEqual(streamed.registry.coherence_idle_snapshot(),
                         batch.registry.coherence_idle_snapshot())
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
            leading_one_qubit_gates=(("u1", 0), ("u2", 3)),
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

    def test_leading_one_qubit_then_initial_out_enter_first_prior(self):
        schedule = [[[0, 1]], [[2, 3]]]
        initial = [(0, q, 0) for q in range(4)]

        def committed_prior(prefix=()):
            value = make_placer(initial, horizon=0)
            value._initialize_run_state(
                self.architecture, [initial], schedule,
                leading_one_qubit_gates=prefix)
            placement = value._plan_round(0)
            value._repair_ghosts(placement, {})
            summary = value._record_initial_out_phase(placement)
            value._commit_round(0, placement)
            return summary, value.registry.coherence_idle_snapshot()

        summary, prior = committed_prior()
        leading_summary, leading_prior = committed_prior(
            (("u1", 0), ("u2", 1)))

        self.assertGreater(summary["move_time_us"], 30.0)
        self.assertEqual(summary["movers"], 2)
        self.assertEqual(leading_summary, summary)
        # Layer-0 participants are busy during CZ, but still carry positive
        # coherence idle from the real initial AOD out phase.
        self.assertGreater(prior[0], 0.0)
        self.assertGreater(prior[1], 0.0)
        self.assertAlmostEqual(prior[2] - prior[0], 30.36, places=9)
        self.assertAlmostEqual(prior[3] - prior[1], 30.36, places=9)
        # The two globally serial 52-us prefix gates precede that same MOVE:
        # each target is idle during the other gate, while all other atoms are
        # idle during both.  Parent-layer 1Q gates are intentionally not part
        # of this placement-ledger interface because they may overlap AOD work.
        self.assertEqual(
            tuple(round(after - before, 9)
                  for before, after in zip(prior, leading_prior)),
            (52.0, 52.0, 104.0, 104.0),
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
