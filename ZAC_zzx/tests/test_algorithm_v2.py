"""Focused regression tests for the formal Schema-2 M3/M4 algorithm."""
from __future__ import annotations

import json
import math
import random
import sys
import unittest
from unittest.mock import patch
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

    def test_idle_resident_depends_on_zone_rydberg_pulse(self):
        compiler = ZAC_zzx()
        compiler.architecture = make_arch()
        compiler.qubit_dependency = [1, 9]
        compiler.result_json = {"instructions": [{
            "type": "rydberg",
            "id": 7,
            "zone_id": 0,
            "dependency": {"qubit": []},
        }]}
        # Toy architecture arrays 1 and 2 belong to entanglement zone 0;
        # storage array 0 has entanglement_id=-1.
        compiler._bind_resident_rydberg_dependencies(
            [[1, 0, 0], [0, 0, 0]], 0)
        self.assertEqual(compiler.qubit_dependency, [7, 9])
        self.assertEqual(compiler.result_json["instructions"][0]
                         ["dependency"]["qubit"], [1])

    def test_disjoint_one_qubit_blocks_share_global_dependency(self):
        compiler = ZAC_zzx()
        compiler.result_json = {"instructions": [
            {"type": "init", "id": 0, "begin_time": 0.0, "end_time": 0.0},
            {"type": "1qGate", "id": 1,
             "begin_time": 0.0, "end_time": 52.0},
        ]}
        compiler._last_global_1q_instruction = 1
        dependency = {"qubit": [0]}
        compiler.write_1q_gate_instruction(
            2, [{"name": "u3", "q": 1}], dependency,
            [[0, 0, 0], [0, 1, 0]])
        self.assertEqual(dependency["qubit"], [0, 1])
        self.assertEqual(compiler.get_begin_time(2, dependency), 52.0)


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

    def test_residency_break_even_rejects_two_idle_pulses(self):
        model = PhysicalIncrementalCost(13)
        leg_out = (10.0, 0.0, 0.0, 10.0, 0.0)
        leg_back = (10.0, 10.0, 0.0, 0.0, 0.0)
        phases = (
            model.movement_phase([leg_out], owners=[0]),
            model.movement_phase([leg_back], owners=[0]),
        )
        one_pulse = model.residency_break_even(phases, 1)
        two_pulses = model.residency_break_even(phases, 2)
        self.assertTrue(one_pulse[0])
        self.assertFalse(two_pulses[0])
        self.assertLess(one_pulse[1], one_pulse[2])
        self.assertGreater(two_pulses[1], two_pulses[2])

    def test_multirow_move_time_matches_router_expansion(self):
        arch = make_arch()
        compiler = ZAC_zzx()
        compiler.architecture = arch
        begin = [
            [[0, 0, 0, 0]],
            [[1, 0, 1, 1]],
        ]
        end = [
            [[0, 1, 0, 0]],
            [[1, 1, 1, 1]],
        ]
        details = compiler.expand_arrangement({
            "begin_locs": begin,
            "end_locs": end,
        })
        routed_duration = compiler.get_duration({"insts": details})
        legs = []
        for source_row, target_row in zip(begin, end):
            source = arch.exact_SLM_location_tuple(source_row[0][1:])
            target = arch.exact_SLM_location_tuple(target_row[0][1:])
            legs.append((math.dist(source, target), *source, *target))
        phase = PhysicalIncrementalCost(2).movement_phase(
            legs, owners=[0, 1])
        self.assertEqual(phase.batches, 1)
        self.assertAlmostEqual(phase.move_time_us, routed_duration, places=12)


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

    def test_schema2_compiler_never_runs_an_auxiliary_full_schedule_baseline(self):
        """H=0 must not bypass ForecastOracle through a post-hoc ZAC stream."""
        initial = [(0, i, 0) for i in range(6)]
        schedule = [[[0, 1]], [[2, 3]], [[0, 2]]]
        compiler = ZAC_zzx()
        compiler.parse_setting({
            **load_setting("ours_nl_v2.json"),
            "name": "horizon-boundary-regression",
        })
        compiler.architecture = self.arch
        compiler.qubit_mapping = [initial]
        compiler.gate_scheduling = schedule
        compiler.dynamic_placement = True
        compiler.reuse_qubit = [set() for _ in schedule]
        with patch(
                "zac.placer.vmplacer.VertexMatchingPlacer.run",
                side_effect=AssertionError("full-schedule baseline leaked")):
            compiler.place_qubit_intermedeiate()
        self.assertEqual(len(compiler.qubit_mapping), 2 * len(schedule) + 1)

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

    def test_partial_sparse_return_match_is_completed_deterministically(self):
        initial = [(0, i, 0) for i in range(4)]
        registry = ResidentRegistry(self.arch, initial)
        registry.enter_zone(0, (1, 0, 0))
        registry.enter_zone(1, (2, 0, 1))
        with patch(
                "zzx.resident.min_weight_full_bipartite_matching",
                return_value=([0], [0])):
            result = match_return_sites(
                registry, [0, 1], NextUse([]), 0,
                forecast=ForecastOracle([], 0), candidate_mode="nearest")
        self.assertEqual(set(result), {0, 1})
        self.assertEqual(len(set(result.values())), 2)

    def test_exhausted_return_box_falls_back_to_global_physical_minimum(self):
        zone = (1, 0, 0)
        near = self.arch.nearest_storage_site(*zone)
        slm = self.arch.dict_SLM[near[0]]
        crowded = [
            (near[0], row, column)
            for row in range(max(0, near[1] - 3),
                             min(slm.n_r, near[1] + 4))
            for column in range(max(0, near[2] - 3),
                                min(slm.n_c, near[2] + 4))]
        far_home = next(
            (0, row, column)
            for row in range(slm.n_r)
            for column in range(slm.n_c)
            if (0, row, column) not in crowded)
        registry = ResidentRegistry(self.arch, [far_home, *crowded])
        registry.enter_zone(0, zone)

        result = match_return_sites(
            registry, [0], NextUse([]), 0, box_ratio=3,
            forecast=ForecastOracle([], 0), candidate_mode="nearest")
        occupied = registry.occupied_storage()
        free = [
            (0, row, column)
            for row in range(slm.n_r)
            for column in range(slm.n_c)
            if (0, row, column) not in occupied]
        zone_xy = self.arch.exact_SLM_location_tuple(zone)
        expected = min(
            free,
            key=lambda site: (
                math.sqrt(math.dist(
                    zone_xy, self.arch.exact_SLM_location_tuple(site))),
                site))
        self.assertEqual(result[0], expected)

    def test_seed_population_contains_stay_return_and_physical_greedy(self):
        population = build_seed_population(
            [3], 2, [1, 0], 6, random.Random(0),
            greedy_gate_genes=[2])
        self.assertIn([0, 0, 0], population)
        self.assertIn([0, 1, 1], population)
        self.assertIn([2, 1, 0], population)

    def test_seed_population_rejects_misaligned_greedy_gate_vector(self):
        with self.assertRaisesRegex(ValueError, "greedy_gate_genes"):
            build_seed_population(
                [2, 3], 1, [0], 4, random.Random(0),
                greedy_gate_genes=[1])

    def test_zero_weight_matching_edges_remain_legal(self):
        """Sparse scipy matrices must not drop exact zero-cost candidates."""
        initial = [(0, i, 0) for i in range(4)]
        placer = ResidentPlacer(initial)
        placer.architecture = self.arch
        placer.registry = ResidentRegistry(self.arch, initial)
        left = (1, 0, 0)
        right = (1, 0, 1)
        candidates = [
            [(left, 0.0, 0, 1), (right, 10.0, 0, 1)],
            [(right, 0.0, 2, 3), (left, 10.0, 2, 3)],
        ]
        matched = placer._match_gates(candidates, [(0, 1), (2, 3)])
        self.assertEqual([placement["site"] for placement in matched],
                         [left, right])

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
        # q0 is worth retaining for its visible reuse with q4, whereas dead q1
        # is physically cheaper to return.  This separately exercises both
        # decision outcomes under the fully expanded rollout cost.
        schedule = [[[0, 1]], [[2, 3]], [[0, 4]]]
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

    def test_two_idle_pulse_residency_is_rejected_by_physical_guard(self):
        initial = [(0, i, 0) for i in range(8)]
        # At boundary 0, q0 is not reused until L+3: retaining it would incur
        # two Rydberg-idle pulses before the visible gate (q0,q6).
        schedule = [
            [[0, 1]], [[2, 3]], [[4, 5]], [[0, 6]],
        ]
        placer = ResidentPlacer(
            initial, seed=0, experiment_schema=2,
            method_id="ours_lk", objective="physical_log_fidelity",
            lookahead_horizon=2, engine="ga", fitness_cache=True,
            population_size=6, iterations=2,
            neighbors_per_solution=2, neighbor_sample_size=6)
        placer.run(self.arch, [initial], schedule, True,
                   [set() for _ in schedule])
        first = placer.decision_log[0]
        q0_guard = next(row for row in first["physical_guard"]
                        if row["q"] == 0)
        self.assertEqual(q0_guard["idle_exposures"], 2)
        self.assertTrue(q0_guard["forced_return"])
        self.assertEqual(first["physical_guard_returns"], 1)
        self.assertNotEqual(placer.mapping[1][0], placer.mapping[2][0])

    def test_dual_horizon_search_is_byte_deterministic(self):
        initial = [(0, i, 0) for i in range(8)]
        schedule = [
            [[0, 1]], [[2, 3]], [[0, 4]], [[1, 5]],
        ]
        outputs = []
        for _ in range(2):
            placer = ResidentPlacer(
                initial, seed=7, experiment_schema=2,
                method_id="ours_lk", objective="physical_log_fidelity",
                lookahead_horizon=2, engine="ga", fitness_cache=True,
                population_size=6, iterations=3,
                neighbors_per_solution=2, neighbor_sample_size=8)
            placer.run(self.arch, [initial], schedule, True,
                       [set() for _ in schedule])
            outputs.append(json.dumps({
                "mapping": placer.mapping,
                "decision_log": placer.decision_log,
            }, sort_keys=True, separators=(",", ":")))
        self.assertEqual(outputs[0], outputs[1])

    def test_lookahead_can_cycle_an_adjacent_chain_without_perturbing_h0(self):
        """H=2 may relocate a long chain via RETURN/re-entry; H=0 may not.

        The cycle bit is deliberately outside the stochastic residency GA.  This
        golden case guards both sides of that contract: the no-lookahead method
        retains its original search stream, while lookahead accepts only cycle
        improvements larger than one physical load+store fidelity pair.
        """
        n_qubits = 70
        initial = [(0, q // 10, q % 10) for q in range(n_qubits)]
        schedule = [[[q, q + 1]] for q in range(n_qubits - 1)]
        runs = {}
        for method, horizon in (("ours_nl", 0), ("ours_lk", 2)):
            placer = ResidentPlacer(
                initial, seed=0, experiment_schema=2,
                method_id=method, objective="physical_log_fidelity",
                lookahead_horizon=horizon, engine="ga", fitness_cache=True,
                population_size=6, iterations=2,
                neighbors_per_solution=2, neighbor_sample_size=12)
            placer.run(self.arch, [initial], schedule, True,
                       [set() for _ in schedule])
            runs[horizon] = placer

        self.assertEqual(
            sum(row.get("adjacent_cycle_returns", 0)
                for row in runs[0].decision_log), 0)
        accepted = [
            entry
            for row in runs[2].decision_log
            for entry in row.get("adjacent_cycle_search", ())
            if entry["accepted"]
        ]
        self.assertGreater(len(accepted), 0)
        transfer_pair_nll = -2.0 * math.log(PhysicalIncrementalCost.F_TRANSFER)
        self.assertTrue(all(
            entry["negative_log_fidelity_gain"] > transfer_pair_nll
            for entry in accepted))
        self.assertEqual(len(runs[2].mapping), 2 * len(schedule) + 1)

    def test_adjacent_cycle_refinement_is_serial_only(self):
        """Independent cycle trials must not perturb a coupled parallel front."""
        initial = [(0, q, 0) for q in range(8)]
        schedule = [
            [[0, 1], [2, 3]],
            [[1, 4], [3, 5]],
            [[4, 6], [5, 7]],
        ]
        placer = ResidentPlacer(
            initial, seed=0, experiment_schema=2,
            method_id="ours_lk", objective="physical_log_fidelity",
            lookahead_horizon=2, engine="ga", fitness_cache=True,
            population_size=6, iterations=2,
            neighbors_per_solution=2, neighbor_sample_size=12)
        placer.run(self.arch, [initial], schedule, True,
                   [set() for _ in schedule])

        first = placer.decision_log[0]
        self.assertEqual(first.get("adjacent_cycle_candidates"), 0)
        self.assertEqual(first.get("adjacent_cycle_returns"), 0)

    def test_returned_partner_seat_is_reused_without_moving_shared_atom(self):
        """The back phase must make a RETURNed gate seat available to out.

        A chain layer ``(q0, hub) -> (q1, hub)`` is the minimal regression for
        the old menu-time blockage: q0 returns first, q1 takes q0's old seat,
        and the hub remains on its half of the same Rydberg pair.
        """
        initial = [(0, i, 0) for i in range(6)]
        schedule = [[[0, 5]], [[1, 5]], [[2, 5]]]
        placer = ResidentPlacer(
            initial, seed=0, experiment_schema=2,
            method_id="ours_lk", objective="physical_log_fidelity",
            lookahead_horizon=2, engine="ga", fitness_cache=True,
            population_size=6, iterations=2,
            neighbors_per_solution=2, neighbor_sample_size=6)
        placer.run(self.arch, [initial], schedule, True,
                   [set() for _ in schedule])

        first_gate, first_boundary, second_gate = placer.mapping[1:4]
        self.assertEqual(placer.decision_log[0]["return"], 1)
        self.assertNotEqual(first_gate[0], first_boundary[0])
        self.assertEqual(first_gate[5], first_boundary[5])
        self.assertEqual(second_gate[5], first_gate[5])
        self.assertEqual(second_gate[1], first_gate[0])

    def test_return_ghost_repair_search_orders_candidates_without_name_error(self):
        initial = [(0, i, 0) for i in range(3)]
        placer = ResidentPlacer(initial)
        placer.architecture = self.arch
        placer.mapping = [initial]
        placer.registry = ResidentRegistry(self.arch, initial)
        placer.registry.enter_zone(0, (1, 0, 0))
        decisions = {0: ("RETURN", (0, 0, 0))}
        ex = lambda loc: self.arch.exact_SLM_location_tuple(tuple(loc))

        def t1(q):
            return decisions[q][1] if q in decisions else \
                placer.registry.current_pos(q)

        def both(q, left, right):
            a, b = ex(left(q)), ex(right(q))
            return [(q, *a)] if a == b else [(q, *a), (q, *b)]

        start, end = ex((1, 0, 0)), ex((0, 0, 0))
        issue = ("back", 0, (math.dist(start, end), *start, *end),
                 1, *ex(initial[1]))
        changed = placer._fix_leg_ghost(
            issue, [], decisions, set(), t1, t1, both,
            lambda _q: None, len(initial), {})
        self.assertTrue(changed)
        self.assertEqual(decisions[0][0], "RETURN")
        self.assertNotEqual(decisions[0][1], (0, 0, 0))

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
