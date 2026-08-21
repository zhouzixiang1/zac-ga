"""Focused regression tests for the formal Schema-2 M3/M4 algorithm."""
from __future__ import annotations

import json
import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zac.ds.architecture import Architecture  # noqa: E402
from zac.router.router import Router_mixin  # noqa: E402
from zzx.algorithm_v2 import (  # noqa: E402
    ForecastBoundaryError,
    ForecastOracle,
    PhysicalIncrementalCost,
    build_seed_population,
    resident_decision_candidates,
    validate_schema2_pair,
    validate_schema2_setting,
)
from zzx.resident import NextUse, ResidentRegistry, match_return_sites  # noqa: E402
from zzx.zac_zzx import ZAC_zzx  # noqa: E402
from zzx.zplacer import ResidentPlacer  # noqa: E402


def load_setting(name):
    return json.loads((ROOT / "exp_setting" / name).read_text())["zac_setting"][0]


def make_arch():
    spec = json.loads((ROOT / "hardware_spec/toy_architecture.json").read_text())
    arch = Architecture(spec)
    arch.preprocessing()
    return arch


class _ExpansionArchitecture:
    """Minimal coordinate oracle for the reused-column parking regression."""

    _positions = {
        (1, 1, 0): (35.0, 317.0),
        (1, 2, 0): (35.0, 327.0),
        (1, 3, 1): (47.0, 337.0),
        (1, 5, 1): (47.0, 357.0),
        (0, 0, 0): (59.0, 294.0),
        (0, 1, 0): (60.0, 307.0),
        (2, 0, 0): (49.0, 317.0),
        (2, 1, 0): (61.0, 337.0),
    }

    def exact_SLM_location(self, array, row, column):
        return self._positions[(array, row, column)]

    def exact_SLM_location_tuple(self, location):
        return self._positions[tuple(location)]


class _ExpansionRouter(Router_mixin):
    PARKING_DIST = 1

    def __init__(self):
        self.architecture = _ExpansionArchitecture()


class TestPhysicalExpansion(unittest.TestCase):
    def test_reused_parked_column_moves_every_held_atom(self):
        details = _ExpansionRouter().expand_arrangement({
            "begin_locs": [
                [[8, 1, 1, 0]],
                [[4, 1, 2, 0]],
            ],
            "end_locs": [
                [[8, 1, 3, 1]],
                [[4, 1, 5, 1]],
            ],
        })
        shift_back = details[2]
        big_move = details[4]
        self.assertEqual(shift_back["move_type"], "before")
        self.assertEqual(
            shift_back["end_coord"][0][0],
            {"id": 8, "x": 35.0, "y": 318.0},
        )
        self.assertEqual(big_move["begin_coord"][0][0]["x"], 35.0)
        self.assertEqual(big_move["begin_coord"][1][0]["x"], 35.0)

    def test_endpoint_safe_batch_is_split_when_parking_merges_columns(self):
        compiler = ZAC_zzx()
        compiler.architecture = _ExpansionArchitecture()
        mapping_from = [[0, 0, 0], [0, 1, 0]]
        mapping_to = [[2, 0, 0], [2, 1, 0]]
        owner = {0: 0, 1: 1}
        positions = {
            q: compiler.architecture.exact_SLM_location_tuple(mapping_from[q])
            for q in range(2)
        }
        self.assertEqual(
            compiler._expanded_batch_conflicts(
                [0, 1], owner, mapping_from, mapping_to, positions),
            {0, 1},
        )
        self.assertEqual(
            compiler._expanded_batch_conflicts(
                [0], owner, mapping_from, mapping_to, positions),
            set(),
        )
        self.assertEqual(
            compiler._expanded_batch_conflicts(
                [1], owner, mapping_from, mapping_to, positions),
            set(),
        )


class TestSchema2Contract(unittest.TestCase):
    def test_registered_pair_differs_only_in_horizon_identity_and_output(self):
        nl = load_setting("ours_nl_v2.json")
        lk = load_setting("ours_lk_v2.json")
        validate_schema2_pair(nl, lk)
        ZAC_zzx().parse_setting({**nl, "name": "toy"})
        ZAC_zzx().parse_setting({**lk, "name": "toy"})

    def test_legacy_proxy_weight_is_rejected(self):
        setting = load_setting("ours_nl_v2.json")
        setting["w_ghost"] = 0.0
        with self.assertRaisesRegex(ValueError, "旧代理权重"):
            validate_schema2_setting(setting)

    def test_method_horizon_is_fixed(self):
        setting = load_setting("ours_nl_v2.json")
        setting["lookahead_horizon"] = 2
        with self.assertRaisesRegex(ValueError, "必须使用"):
            validate_schema2_setting(setting)

    def test_pair_rejects_hidden_budget_difference(self):
        nl = load_setting("ours_nl_v2.json")
        lk = load_setting("ours_lk_v2.json")
        lk["iterations"] += 1
        with self.assertRaisesRegex(ValueError, "配置差异"):
            validate_schema2_pair(nl, lk)

    def test_pair_rejects_missing_key_versus_explicit_null(self):
        nl = load_setting("ours_nl_v2.json")
        lk = load_setting("ours_lk_v2.json")
        del nl["fitness_cache"]
        lk["fitness_cache"] = None
        with self.assertRaisesRegex(ValueError, "fitness_cache"):
            validate_schema2_pair(nl, lk)


class TestForecastBoundary(unittest.TestCase):
    SCHEDULE = [((0, 1),), ((2, 3),), ((0, 2),), ((1, 3),)]

    def test_h0_sees_target_but_no_future(self):
        oracle = ForecastOracle(self.SCHEDULE, 0)
        self.assertEqual(oracle.target_layer(0), ((2, 3),))
        self.assertIsNone(oracle.next_use(0, 0))
        self.assertEqual(list(oracle.visible_future(0)), [])
        with self.assertRaises(ForecastBoundaryError):
            oracle.future_layer(0, 1)

    def test_h2_sees_exactly_two_future_layers(self):
        oracle = ForecastOracle(self.SCHEDULE, 2)
        self.assertEqual(list(oracle.visible_future(0)), [
            (2, ((0, 2),)), (3, ((1, 3),))])
        self.assertEqual(oracle.next_use(0, 0), (2, 2))
        with self.assertRaises(ForecastBoundaryError):
            oracle.future_layer(0, 3)


class TestPhysicalObjective(unittest.TestCase):
    def test_idle_excitation_is_part_of_negative_log_fidelity(self):
        model = PhysicalIncrementalCost(4)
        objective, result = model.score([], idle_exposures=1, chromosome=[0])
        self.assertAlmostEqual(result.idle_excitation_nll, -math.log(0.9975), places=14)
        expected_coherence = -math.log1p(-0.36 / 1.5e6)
        self.assertAlmostEqual(result.coherence_nll, expected_coherence, places=14)
        self.assertAlmostEqual(
            result.negative_log_fidelity,
            result.idle_excitation_nll + expected_coherence,
            places=14,
        )
        self.assertEqual(objective[-1], (0,))

    def test_move_cost_counts_batches_time_transfer_and_coherence(self):
        model = PhysicalIncrementalCost(4)
        leg = (10.0, 0.0, 0.0, 10.0, 0.0)
        phase = model.movement_phase([leg], owners=[0])
        _, result = model.score([phase], idle_exposures=0, chromosome=[1])
        self.assertEqual(phase.batches, 1)
        self.assertAlmostEqual(
            phase.move_time_us, 30.0 + math.sqrt(10.0 / 0.00275), places=12)
        self.assertEqual(result.transfers, 2)
        self.assertGreater(result.transfer_nll, 0.0)
        self.assertGreater(result.coherence_nll, 0.0)


class TestResidentDecisionMechanics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.arch = make_arch()

    def test_dead_resident_is_still_a_decision_candidate(self):
        # q1 has no use after the current layer; eligibility is based on residency,
        # not NextUse.has_future_use().
        candidates = resident_decision_candidates([0, 1, 2], participants=[2, 3])
        self.assertEqual(candidates, [0, 1])

    def test_empty_two_qubit_schedule_is_a_valid_fast_path_for_nl_and_lk(self):
        initial = [(0, i, 0) for i in range(4)]
        for method, horizon in (("ours_nl", 0), ("ours_lk", 2)):
            placer = ResidentPlacer(
                initial, seed=0, experiment_schema=2,
                method_id=method, objective="physical_log_fidelity",
                lookahead_horizon=horizon, engine="ga")
            placer.run(self.arch, [initial], [], True, [])
            self.assertEqual(placer.mapping, [initial])
            self.assertEqual(placer.decision_log, [])
            self.assertEqual(list(placer.forecast.visible_future(0)), [])
            self.assertEqual(placer.nu.rounds, {})

    def test_return_matching_uses_the_selected_subset(self):
        initial = [(0, i, 0) for i in range(4)]
        registry = ResidentRegistry(self.arch, initial)
        registry.enter_zone(0, (1, 0, 0))
        registry.enter_zone(1, (2, 0, 1))
        next_use = NextUse([])
        oracle = ForecastOracle([], 0)
        one = match_return_sites(
            registry, [0], next_use, 0, forecast=oracle,
            candidate_mode="nearest")
        two = match_return_sites(
            registry, [0, 1], next_use, 0, forecast=oracle,
            candidate_mode="nearest")
        self.assertEqual(set(one), {0})
        self.assertEqual(set(two), {0, 1})
        self.assertEqual(len(set(two.values())), 2)

    def test_seed_population_contains_stay_return_and_physical_greedy(self):
        population = build_seed_population(
            [3], 2, [1, 0], 6, random.Random(0))
        self.assertIn([0, 0, 0], population)
        self.assertIn([0, 1, 1], population)
        self.assertIn([0, 1, 0], population)

    def test_cache_toggle_preserves_schedule_and_dead_resident_is_searched(self):
        initial = [(0, i, 0) for i in range(6)]
        schedule = [[[0, 1]], [[2, 3]], [[0, 2]]]
        mappings, logs = [], []
        for enabled in (True, False):
            placer = ResidentPlacer(
                initial, seed=0, experiment_schema=2,
                method_id="ours_nl", objective="physical_log_fidelity",
                lookahead_horizon=0, engine="ga", fitness_cache=enabled,
                population_size=6, iterations=2,
                neighbors_per_solution=2, neighbor_sample_size=6)
            placer.run(self.arch, [initial], schedule, True,
                       [set() for _ in schedule])
            mappings.append(placer.mapping)
            logs.append(placer.decision_log)
        self.assertEqual(mappings[0], mappings[1])
        self.assertEqual(logs[0][0]["eligible_decisions"], 2)
        self.assertGreater(logs[0][0]["cache"]["fitness_hits"], 0)
        self.assertEqual(logs[1][0]["cache"]["fitness_hits"], 0)

    def test_physical_rollout_can_select_both_stay_and_return(self):
        initial = [(0, i, 0) for i in range(6)]
        schedule = [[[0, 1]], [[2, 3]], [[0, 2]]]
        placer = ResidentPlacer(
            initial, seed=0, experiment_schema=2,
            method_id="ours_lk", objective="physical_log_fidelity",
            lookahead_horizon=2, engine="ga", fitness_cache=True,
            population_size=6, iterations=2,
            neighbors_per_solution=2, neighbor_sample_size=6)
        placer.run(self.arch, [initial], schedule, True,
                   [set() for _ in schedule])
        first = placer.decision_log[0]
        self.assertEqual((first["stay"], first["return"]), (1, 1))

    def test_horizon_changes_only_visible_rollout_layers(self):
        initial = [(0, i, 0) for i in range(6)]

        def first_boundary(horizon, tail):
            placer = ResidentPlacer(
                initial, seed=0, experiment_schema=2,
                method_id="ours_nl" if horizon == 0 else "ours_lk",
                objective="physical_log_fidelity",
                lookahead_horizon=horizon, engine="ga", fitness_cache=True,
                population_size=6, iterations=2,
                neighbors_per_solution=2, neighbor_sample_size=6)
            schedule = [[[0, 1]], [[2, 3]], *tail]
            placer.run(self.arch, [initial], schedule, True,
                       [set() for _ in schedule])
            return placer.mapping[2], placer.decision_log[0]

        neutral = [[[4, 5]], [[4, 5]], [[4, 5]]]
        visible_l2 = [[[0, 2]], [[4, 5]], [[4, 5]]]
        visible_l3 = [[[4, 5]], [[1, 3]], [[4, 5]]]
        invisible_l4 = [[[4, 5]], [[4, 5]], [[0, 2]]]

        h0_neutral = first_boundary(0, neutral)
        h0_changed = first_boundary(0, visible_l2)
        self.assertEqual(h0_neutral, h0_changed)

        h2_neutral = first_boundary(2, neutral)
        h2_l2 = first_boundary(2, visible_l2)
        h2_l3 = first_boundary(2, visible_l3)
        h2_l4 = first_boundary(2, invisible_l4)
        self.assertNotEqual(h2_neutral[1]["score"], h2_l2[1]["score"])
        self.assertNotEqual(h2_neutral[1]["score"], h2_l3[1]["score"])
        self.assertEqual(h2_neutral, h2_l4)


if __name__ == "__main__":
    unittest.main()
