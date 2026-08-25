"""Physical rent-or-return and per-atom idle-ledger regression tests."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.forecast import CachedForecastLayerProvider  # noqa: E402
from streaming.formal_zac_placement import FormalZacPlacementStream  # noqa: E402
from streaming.qasm_sqlite import LayerStore, build_layer_store  # noqa: E402
from streaming.resident_transition import ResidentTransitionKernel  # noqa: E402
from zac.ds.architecture import Architecture  # noqa: E402
from zzx.algorithm_v2 import decay_lookahead_spec  # noqa: E402
from zzx.resident import ResidentRegistry  # noqa: E402
from zzx.zplacer import ResidentPlacer  # noqa: E402


def architecture():
    spec = json.loads(
        (ROOT / "hardware_spec/toy_architecture.json").read_text())
    value = Architecture(spec)
    value.preprocessing()
    return value


def placer(initial, *, horizon: int, seed: int = 0):
    return ResidentPlacer(
        initial,
        seed=seed,
        experiment_schema=2,
        method_id="ours_nl" if horizon == 0 else "ours_lk",
        objective="physical_log_fidelity",
        lookahead_horizon=decay_lookahead_spec(horizon),
        alpha_lookahead=0.1,
        engine="ga",
        backend="reference",
        fitness_cache=True,
        population_size=6,
        iterations=2,
        neighbors_per_solution=2,
        neighbor_sample_size=8,
        direct_enumeration_limit=512,
        max_unique_evaluations=256,
    )


def reference_setting(horizon: int) -> dict:
    return {
        "experiment_schema": 2,
        "method_id": "ours_nl" if horizon == 0 else "ours_lk",
        "objective": "physical_log_fidelity",
        "lookahead_horizon": decay_lookahead_spec(horizon),
        "population_size": 6,
        "iterations": 2,
        "neighbors_per_solution": 2,
        "neighbor_sample_size": 8,
        "seed": 0,
        "placer": "resident",
        "engine": "ga",
        "routing_strategy": "coloring",
        "resyn": False,
        "fitness_cache": True,
        "alpha_lookahead": 0.1,
        "backend": "reference",
        "direct_enumeration_limit": 512,
        "max_unique_evaluations": 256,
    }


class TestResidentPhysicalLedgers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.arch = architecture()

    def test_rent_resets_but_global_coherence_never_resets(self):
        initial = [(0, 0, q) for q in range(3)]
        registry = ResidentRegistry(self.arch, initial)
        registry.enter_zone(0, (1, 0, 0))
        registry.enter_zone(1, (2, 0, 0))

        registry.record_movement_phase(40.0, movers=(0,))
        registry.record_rydberg_pulse((0,), 0.36)

        self.assertEqual(registry.resident_rent(0), (0, 10.0))
        self.assertEqual(registry.resident_rent(1), (1, 40.36))
        self.assertEqual(
            registry.coherence_idle_snapshot(), (10.0, 40.36, 40.36))

        registry.enter_zone(1, (2, 0, 1))
        self.assertEqual(registry.resident_rent(1), (0, 0.0))
        self.assertEqual(registry.coherence_idle_snapshot()[1], 40.36)
        registry.return_to_storage(0, (0, 1, 0))
        self.assertEqual(registry.resident_rent(0), (0, 0.0))
        self.assertEqual(registry.coherence_idle_snapshot()[0], 10.0)

    def test_h0_reads_no_future_and_past_rent_is_not_recharged(self):
        schedule = (
            ((0, 1),),
            ((2, 3),),
            ((4, 5),),
            ((0, 6),),
        )
        initial = [(0, 0, q) for q in range(7)]
        reads = []

        def load(layer):
            reads.append(layer)
            return schedule[layer]

        provider = CachedForecastLayerProvider(
            len(schedule), load, max_cached_layers=2)
        kernel = ResidentTransitionKernel(
            placer(initial, horizon=0), self.arch, initial, provider)
        self.assertEqual(set(reads), {0})

        first = kernel.advance()
        self.assertNotIn(2, reads)
        guards = {row["q"]: row for row in first.decision_log["rent_guard"]}
        self.assertEqual(guards[0]["mode"], "h0_current_only")
        self.assertEqual(guards[0]["history_idle_exposures"], 0)
        self.assertEqual(guards[0]["future_idle_exposures"], 1)
        self.assertFalse(guards[0]["forced_return"])
        self.assertTrue(guards[0]["stay_admitted"])

        # Inject one already-paid physical idle pulse into an otherwise equal
        # boundary.  It remains visible in the audit ledger and conditions the
        # linear-T2 log-ratio, but is sunk cost: the H=0 decision still compares
        # exactly one *future* pulse against RETURN/re-entry.
        long_provider = CachedForecastLayerProvider(
            len(schedule), lambda layer: schedule[layer], max_cached_layers=2)
        long_kernel = ResidentTransitionKernel(
            placer(initial, horizon=0), self.arch, initial, long_provider)
        long_kernel.placer.registry.record_rydberg_pulse((1,), 0.36)
        second = long_kernel.advance()
        guards = {row["q"]: row for row in second.decision_log["rent_guard"]}
        self.assertEqual(guards[0]["history_idle_exposures"], 1)
        self.assertEqual(guards[0]["future_idle_exposures"], 1)
        self.assertFalse(guards[0]["forced_return"])
        self.assertEqual(guards[0]["reason"], "rent_below_return")
        self.assertEqual(
            guards[0]["stay_excitation_nll"],
            -math.log(0.9975),
        )
        expected_coherence = (
            math.log1p(-guards[0]["coherence_prior_us"] / 1.5e6)
            - math.log1p(
                -(guards[0]["coherence_prior_us"] + 0.36) / 1.5e6))
        self.assertAlmostEqual(
            guards[0]["stay_coherence_increment_nll"],
            expected_coherence, places=15)

    def test_m4_no_visible_reuse_is_recommended_not_hard_masked(self):
        schedule = (
            ((0, 1),),
            ((2, 3),),
            ((0, 4),),
        )
        initial = [(0, 0, q) for q in range(5)]
        provider = CachedForecastLayerProvider(
            len(schedule), lambda layer: schedule[layer],
            max_cached_layers=10)
        kernel = ResidentTransitionKernel(
            placer(initial, horizon=8), self.arch, initial, provider)

        first = kernel.advance()
        guards = {row["q"]: row for row in first.decision_log["rent_guard"]}
        self.assertEqual(guards[0]["mode"], "visible_break_even")
        self.assertFalse(guards[0]["forced_return"])
        self.assertTrue(guards[0]["stay_admitted"])
        self.assertEqual(guards[1]["mode"], "no_visible_reuse")
        self.assertEqual(guards[1]["future_idle_exposures"], 2)
        self.assertTrue(guards[1]["recommended_return"])
        self.assertFalse(guards[1]["forced_return"])
        self.assertTrue(guards[1]["stay_admitted"])
        self.assertEqual(guards[1]["selected_decision"], "RETURN")
        self.assertEqual(first.decision_log["rent_guard_returns"], 0)
        self.assertEqual(first.decision_log["rent_recommended_returns"], 1)

    def test_rent_guard_keeps_ranking_after_linear_coherence_ood(self):
        schedule = (
            ((0, 1),),
            ((2, 3),),
            ((0, 4),),
        )
        initial = [(0, 0, q) for q in range(5)]
        provider = CachedForecastLayerProvider(
            len(schedule), lambda layer: schedule[layer],
            max_cached_layers=10)
        kernel = ResidentTransitionKernel(
            placer(initial, horizon=8), self.arch, initial, provider)
        # Search must continue on very long circuits even though the final
        # paper-linear scorer will correctly mark the run OOD.  This injects
        # an already-OOD scheduler prior without altering that final contract.
        kernel.placer.registry.record_movement_phase(
            1.5e6 + 10.0, movers=(0,))

        transition = kernel.advance()
        guards = transition.decision_log["rent_guard"]
        self.assertTrue(guards)
        self.assertTrue(all(math.isfinite(row["stay_increment_nll"])
                            for row in guards))
        self.assertTrue(any(math.isfinite(row["return_round_trip_nll"])
                            for row in guards))

    def test_break_even_audit_keeps_causal_return_as_soft_recommendation(self):
        cases = (
            (0, (((0, 1),), ((2, 3),)), 4, "RETURN", 0),
            (0, (((0, 1),), ((2, 3),)), 20, "STAY", 0),
            (8, (((0, 1),), ((2, 3),), ((0, 4),)), 5, "RETURN", 0),
            (8, (((0, 1),), ((2, 3),), ((0, 4),)), 40, "STAY", 0),
        )
        for horizon, schedule, n_atoms, expected, expected_forced in cases:
            with self.subTest(
                    horizon=horizon, n_atoms=n_atoms, expected=expected):
                initial = [
                    (0, q // 10, q % 10) for q in range(n_atoms)]
                provider = CachedForecastLayerProvider(
                    len(schedule), lambda layer, value=schedule: value[layer],
                    max_cached_layers=10)
                kernel = ResidentTransitionKernel(
                    placer(initial, horizon=horizon),
                    self.arch, initial, provider)
                transition = kernel.advance()
                guards = transition.decision_log["rent_guard"]
                self.assertTrue(guards)
                self.assertEqual(
                    sum(row["forced_return"] for row in guards),
                    expected_forced,
                )
                self.assertEqual(
                    {row["selected_decision"] for row in guards},
                    {expected},
                )
        # The scalar bound is diagnostic only.  It injects an exact-scored
        # RETURN candidate but never hard-masks the joint native decision.
        self.assertTrue(any(row["recommended_return"] for row in guards))
        self.assertTrue(any(row["mode"] == "visible_break_even"
                            and not row["forced_return"] for row in guards))

    def test_transition_lru_keys_distinct_absolute_scheduler_priors(self):
        schedule = (
            ((0, 1),),
            ((2, 3),),
        )
        initial = [(0, 0, q) for q in range(5)]

        first_provider = CachedForecastLayerProvider(
            len(schedule), lambda layer: schedule[layer],
            max_cached_layers=2)
        first = ResidentTransitionKernel(
            placer(initial, horizon=0), self.arch, initial, first_provider)
        first.advance()
        self.assertEqual(len(first.placer.transition_cache), 1)

        second_provider = CachedForecastLayerProvider(
            len(schedule), lambda layer: schedule[layer],
            max_cached_layers=2)
        second = ResidentTransitionKernel(
            placer(initial, horizon=0), self.arch, initial, second_provider,
            leading_one_qubit_gates=(("u1", 4),))
        second.placer.transition_cache = first.placer.transition_cache.copy()
        # The real leading 1Q instruction changes the authoritative scheduler
        # clocks and absolute idle vector while leaving boundary geometry and
        # the current two-qubit layer unchanged.  It must therefore produce a
        # distinct cache state rather than reuse the first transition elite.
        second.advance()
        self.assertEqual(len(second.placer.transition_cache), 2)

    def test_stream_checkpoint_restores_both_idle_ledgers_exactly(self):
        qasm = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[7];
u1(0) q[0];
u2(0,0) q[1];
cz q[0],q[1];
cz q[2],q[3];
cz q[4],q[5];
cz q[0],q[6];
"""
        initial = [(0, 0, q) for q in range(7)]
        setting = reference_setting(0)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.qasm"
            database = Path(directory) / "layers.sqlite"
            source.write_text(qasm, encoding="utf-8")
            build_layer_store(source, database)
            store = LayerStore(database)
            self.addCleanup(store.close)
            stream = FormalZacPlacementStream(
                method="M3", architecture=self.arch,
                initial_mapping=initial, store=store,
                max_gates_per_stage=1, setting=setting)
            stream.next_placement()
            stream.next_placement()
            state = stream.state_dict()
            saved = state["resident_state"]["registry"]
            self.assertIn("resident_idle_exposures", saved)
            self.assertIn("resident_idle_time_us", saved)
            self.assertIn("coherence_idle_time_us", saved)

            resumed = FormalZacPlacementStream.from_state(
                architecture=self.arch, initial_mapping=initial,
                store=store, max_gates_per_stage=1,
                state=state, setting=setting)
            expected_registry = stream._resident_kernel.placer.registry
            actual_registry = resumed._resident_kernel.placer.registry
            self.assertEqual(actual_registry.resident_rent_snapshot(),
                             expected_registry.resident_rent_snapshot())
            self.assertEqual(actual_registry.coherence_idle_snapshot(),
                             expected_registry.coherence_idle_snapshot())

            expected_tail = list(stream)
            actual_tail = list(resumed)
            timing = {"horizon_selection_ns", "search_kernel_ns",
                      "marshal_ns", "backend_search_kernel_ns",
                      "fitness_ns", "backend_selection_ns"}

            def semantic(rows):
                return [
                    (row.layer, row.b_l, row.g_l, row.b_l_plus_1,
                     row.gates,
                     {key: value for key, value in row.decision_log.items()
                      if key not in timing})
                    for row in rows]

            self.assertEqual(semantic(expected_tail), semantic(actual_tail))


if __name__ == "__main__":
    unittest.main()
