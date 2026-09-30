"""Physical initializer contracts, independent of frozen experiment evidence."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import random
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation import TraceValidationError, UnsupportedOperationError
from zac.ds.architecture import Architecture
from zac.zac import ZAC
from zzx.initial_lookahead import InfeasibleInitialMapping, rollout_params
from zzx.native_backend import NativeBackendError, NativeBackendUnavailable
from zzx.physical_initial_ga import (
    InitialMappingInfeasible, PhysicalInitialGAConfig, build_initial_population,
    enumerate_storage_seats, mutate, order_crossover, select_initial_mapping,
)
from zzx.zac_zzx import ZAC_zzx


PARAMS = {"backend": "reference", "alpha_lookahead": .5,
          "return_assignment_k": 1, "return_candidate_limit": 2,
          "pin_radius": 1, "coloring_exact_threshold": 0}


def fixture():
    spec = json.loads((ROOT / "hardware_spec/toy_architecture.json").read_text())
    spec["operation_duration"] = {"rydberg": .36, "1qGate": 52., "atom_transfer": 15.}
    arch = Architecture(spec)
    arch.preprocessing()
    return arch, [[(0, 1)], [(2, 3)], [(0, 2)]]


def score(_architecture, mapping, _schedule, **kwargs):
    return {"status": "success", "weighted_nll": float(sum(
        (q + 1) * location[2] for q, location in enumerate(mapping)))}


class StorageArchitecture:
    def __init__(self):
        self.storage_zone = [4, 9]
        self.dict_SLM = {4: SimpleNamespace(n_r=2, n_c=3),
                         9: SimpleNamespace(n_r=3, n_c=2)}

    def nearest_entanglement_site_distance(self, slm_id, row, _column):
        return -row if slm_id == 4 else row

    def is_valid_SLM_position(self, slm_id, row, column):
        slm = self.dict_SLM[slm_id]
        return 0 <= row < slm.n_r and 0 <= column < slm.n_c


class PhysicalInitialGATests(unittest.TestCase):
    def run_mock(self, config=None, evaluator=score, n=4):
        architecture, schedule = fixture()
        if n < 4:
            schedule = []
        with patch("zzx.physical_initial_ga.evaluate_mapping", side_effect=evaluator) as mock:
            selected, report = select_initial_mapping(
                architecture, schedule, n_qubits=n, params=PARAMS,
                config=config or PhysicalInitialGAConfig())
        return selected, report, mock.call_count

    def test_defaults_are_outer_controls_not_dynamic_controls(self):
        config = PhysicalInitialGAConfig()
        self.assertEqual((config.population_size, config.elite_count,
                          config.max_unique_evaluations, config.max_generations,
                          config.early_stop_patience, config.max_proposals), (8, 2, 32, 6, 3, 320))
        self.assertEqual((config.crossover_probability, config.swap_probability,
                          config.insert_probability, config.reverse_probability), (.25, .4, .4, .2))
        self.assertEqual((config.horizon, config.rho, config.rollout_evaluations), (2, .7, 32))

    def test_controls_reject_ambiguous_or_unbounded_values(self):
        for kwargs in ({"population_size": True}, {"elite_count": 9}, {"horizon": 9},
                       {"rho": 0}, {"rho": float("nan")}, {"max_unique_evaluations": 0},
                       {"max_proposals": 7}, {"max_generations": -1}, {"seed": -1},
                       {"search_policy": "distance"}, {"crossover_probability": 1.1},
                       {"swap_probability": .5}, {"reverse_probability": "0.2"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PhysicalInitialGAConfig(**kwargs)
        for value in (False, [], 4, "ga"):
            with self.assertRaises(ValueError):
                PhysicalInitialGAConfig.from_mapping(value)
        with self.assertRaises(ValueError):
            PhysicalInitialGAConfig.from_mapping({"init_pop": 8})

    def test_domain_handles_reverse_rows_exact_capacity_and_nonconsecutive_slms(self):
        arch = StorageArchitecture()
        expected = [(4, row, column) for row in (1, 0) for column in range(3)]
        expected += [(9, row, column) for row in range(3) for column in range(2)]
        self.assertEqual(enumerate_storage_seats(arch, 6), tuple(expected[:6]))
        self.assertEqual(enumerate_storage_seats(arch, 12), tuple(expected))
        for n in (0, 13, True):
            with self.assertRaises(ValueError):
                enumerate_storage_seats(arch, n)

    def test_domain_rejects_repeated_or_invalid_traps(self):
        arch = StorageArchitecture()
        arch.storage_zone = [4, 4]
        with self.assertRaises(ValueError):
            enumerate_storage_seats(arch, 7)
        arch = StorageArchitecture()
        arch.is_valid_SLM_position = lambda *_: False
        with self.assertRaises(ValueError):
            enumerate_storage_seats(arch, 1)

    def test_ox_matches_cyclic_order_and_preserves_parents(self):
        first, second = [2, 0, 4, 1, 3], [4, 2, 3, 0, 1]
        before = deepcopy((first, second))
        rng = SimpleNamespace(sample=lambda *_: [1, 3])
        self.assertEqual(order_crossover(first, second, rng), (3, 0, 4, 1, 2))
        self.assertEqual((first, second), before)
        for seed in range(30):
            self.assertEqual(sorted(order_crossover(first, second, random.Random(seed))), list(range(5)))

    def test_mutations_preserve_non_self_inverse_assignment_and_parents(self):
        p = [2, 0, 4, 1, 3]
        for kwargs in ({"swap_probability": 1., "insert_probability": 0., "reverse_probability": 0.},
                       {"swap_probability": 0., "insert_probability": 1., "reverse_probability": 0.},
                       {"swap_probability": 0., "insert_probability": 0., "reverse_probability": 1.}):
            config = PhysicalInitialGAConfig(**kwargs)
            for seed in range(20):
                before = p[:]
                child = mutate(p, random.Random(seed), config)
                self.assertEqual(sorted(child), list(range(5)))
                self.assertNotEqual(tuple(p), child)
                self.assertEqual(p, before)
        self.assertEqual(mutate((0,), random.Random(0), PhysicalInitialGAConfig()), (0,))
        for invalid in ((0, 0), (1, 2), (False, 1)):
            with self.assertRaises(ValueError):
                mutate(invalid, random.Random(0), PhysicalInitialGAConfig())

    def test_three_arms_share_initial_physical_population_and_domain(self):
        config = PhysicalInitialGAConfig(max_generations=0)
        reports = [self.run_mock(replace(config, horizon=h, search_policy=p))[1]
                   for h, p in ((2, "ga"), (0, "ga"), (2, "random"))]
        self.assertEqual(len({r["initial_population_sha256"] for r in reports}), 1)
        self.assertEqual(len({r["seat_domain_sha256"] for r in reports}), 1)
        self.assertEqual(len(reports[0]["initial_population"]), 8)
        self.assertEqual(len({r["evaluation_context_sha256"] for r in reports}), 2)
        for report in reports:
            self.assertEqual(report["initial_population_hash"], report["initial_population_sha256"])
            self.assertEqual(report["unique_evaluations"], 8)

    def test_structured_seeds_keep_direction_and_all_receive_physical_fitness(self):
        arch, _ = fixture()
        schedule = [[(4, 5)], [(0, 4), (1, 5)], [(0, 2)], [(1, 3)], [(2, 3)]]
        config = PhysicalInitialGAConfig(max_generations=0)
        seats, initial = build_initial_population(arch, 6, schedule, config)
        self.assertEqual(initial[0], tuple(range(6)))
        self.assertEqual(initial[1][4:6], (0, 1))
        self.assertEqual(len(set(initial)), 8)
        def reverse_proxy(_arch, mapping, _schedule, **_):
            # Force the identity seed to win regardless of old W*D ranking.
            return {"status": "success", "weighted_nll": 0. if tuple(mapping) == seats else 1.}
        with patch("zzx.physical_initial_ga.evaluate_mapping", side_effect=reverse_proxy) as mock:
            selected, report = select_initial_mapping(arch, schedule, n_qubits=6,
                                                      params=PARAMS, config=config)
        self.assertEqual(tuple(selected), seats)
        self.assertEqual(mock.call_count, 8)
        self.assertEqual(report["selected_weighted_nll"], 0.)

    def test_small_spaces_finish_without_fake_duplicate_evaluations(self):
        for n, count in ((1, 1), (2, 2), (3, 6)):
            _, report, calls = self.run_mock(n=n)
            self.assertEqual(calls, count)
            self.assertEqual(report["unique_evaluations"], count)
            self.assertEqual(report["termination_reason"], "search_space_exhausted")

    def test_budget_is_hard_including_seed_population(self):
        for budget in (1, 5, 9, 16):
            config = PhysicalInitialGAConfig(max_unique_evaluations=budget,
                                             max_generations=50, early_stop_patience=50)
            _, report, calls = self.run_mock(config)
            self.assertEqual(calls, budget)
            self.assertEqual(report["unique_evaluations"], budget)
            self.assertEqual(report["termination_reason"], "unique_evaluation_budget")
            self.assertEqual(len({r["mapping_sha256"] for r in report["candidates"]}), budget)

    def test_duplicate_failure_cache_does_not_spend_unique_budget_twice(self):
        arch, schedule = fixture()
        config = PhysicalInitialGAConfig(max_generations=2, early_stop_patience=2)
        seats, _ = build_initial_population(arch, 4, schedule, config)
        def evaluator(_arch, mapping, _schedule, **_):
            if tuple(mapping) == seats:
                raise InfeasibleInitialMapping("fixture OOD")
            return {"status": "success", "weighted_nll": 1.}
        with patch("zzx.physical_initial_ga.mutate", side_effect=lambda p, *_: tuple(range(4))):
            _, report, calls = self.run_mock(config, evaluator)
        self.assertEqual(calls, 8)
        self.assertGreater(report["cache_hits"], 0)
        self.assertEqual(report["failed_candidates"], 1)
        self.assertEqual(report["candidates"][0]["status"], "infeasible")
        self.assertNotEqual(report["selected_candidate"], 0)

    def test_proposal_budget_terminates_repeated_children(self):
        config = PhysicalInitialGAConfig(max_proposals=12, early_stop_patience=20)
        with patch("zzx.physical_initial_ga.mutate", side_effect=lambda p, *_: p):
            _, report, _ = self.run_mock(config)
        self.assertEqual(report["proposals"], 12)
        self.assertEqual(report["proposals"], report["initial_generation_attempts"] + report["search_proposals"])
        self.assertEqual(report["termination_reason"], "proposal_budget")

    def test_random_proposals_do_not_depend_on_scores_or_patience(self):
        config = PhysicalInitialGAConfig(search_policy="random", max_unique_evaluations=20,
                                         max_generations=6, early_stop_patience=1)
        _, first, _ = self.run_mock(config)
        _, second, _ = self.run_mock(config, lambda *_, **__: {"status": "success", "weighted_nll": 1.})
        self.assertEqual(first["proposal_trace"], second["proposal_trace"])
        self.assertEqual([r["mapping_sha256"] for r in first["candidates"]],
                         [r["mapping_sha256"] for r in second["candidates"]])
        self.assertNotEqual(second["termination_reason"], "early_stop_patience")

    def test_repeat_search_and_per_candidate_rng_are_deterministic(self):
        def noisy(_arch, mapping, _schedule, **_):
            return {"status": "success", "weighted_nll": random.random() + float(np.random.random())}
        random.seed(11)
        np.random.seed(11)
        py_before, np_before = random.getstate(), np.random.get_state()
        selected, first, _ = self.run_mock(evaluator=noisy)
        self.assertEqual(random.getstate(), py_before)
        np.testing.assert_array_equal(np.random.get_state()[1], np_before[1])
        random.seed(923)
        np.random.seed(923)
        other, second, _ = self.run_mock(evaluator=noisy)
        self.assertEqual(selected, other)
        self.assertEqual(first["proposal_trace"], second["proposal_trace"])
        self.assertEqual(first["candidates"], second["candidates"])
        self.assertEqual(len({r["weighted_nll"] for r in first["candidates"]}), 1)

    def test_all_expected_infeasible_preserves_records_and_rng(self):
        py_before, np_before = random.getstate(), np.random.get_state()
        for error in (InfeasibleInitialMapping("OOD"), TraceValidationError("ghost hit"),
                      NativeBackendError("native rich search found no feasible candidate for boundary 0: blocked"),
                      RuntimeError("boundary 0 has no feasible candidate"),
                      RuntimeError("鬼点硬保证层修补失败：12轮耗尽"),
                      ValueError("phase has no ghost-safe straight-leg batch order")):
            with self.subTest(error=error), self.assertRaises(InitialMappingInfeasible) as raised:
                self.run_mock(PhysicalInitialGAConfig(max_unique_evaluations=4),
                              lambda *_, **__: (_ for _ in ()).throw(error))
            report = raised.exception.initial_ga_report
            self.assertEqual(report["status"], "infeasible")
            self.assertEqual(report["unique_evaluations"], 4)
            self.assertEqual({r["status"] for r in report["candidates"]}, {"infeasible"})
            self.assertGreater(report["selection_ns"], 0)
        self.assertEqual(random.getstate(), py_before)
        np.testing.assert_array_equal(np.random.get_state()[1], np_before[1])

    def test_unknown_errors_and_invalid_scores_fail_fast_and_restore_rng(self):
        errors = (ValueError("program bug"), RuntimeError("scheduler drift"),
                  AssertionError("invariant"), NativeBackendUnavailable("ABI mismatch"),
                  NativeBackendError("native ABI mismatch"), UnsupportedOperationError("unknown IR"))
        before = random.getstate(), np.random.get_state()
        for error in errors:
            def failing(*_, **__):
                random.random()
                np.random.random()
                raise error
            with self.subTest(error=error), self.assertRaises(type(error)) as raised:
                self.run_mock(evaluator=failing)
            self.assertIs(raised.exception, error)
            self.assertEqual(error.initial_ga_report["status"], "program_error")
            self.assertEqual(error.initial_ga_report["unique_evaluations"], 1)
        for value in (float("nan"), float("inf"), -float("inf"), True, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_mock(evaluator=lambda *_, **__: {"status": "success", "weighted_nll": value})
        self.assertEqual(random.getstate(), before[0])
        np.testing.assert_array_equal(np.random.get_state()[1], before[1][1])

    def test_cache_is_local_and_context_binds_horizon_rho_budget_and_problem(self):
        config = PhysicalInitialGAConfig(max_generations=0)
        reports = [self.run_mock(c)[1] for c in (config, replace(config, horizon=0),
                   replace(config, rho=.5), replace(config, rollout_evaluations=1))]
        self.assertEqual(len({r["evaluation_context_sha256"] for r in reports}), 4)
        for r in reports:
            self.assertEqual(r["unique_evaluations"], 8)
            self.assertEqual(r["cache_hits"], 0)

    def test_real_physical_prefix_preserves_h0_and_true_terminal_semantics(self):
        arch, schedule = fixture()
        config = PhysicalInitialGAConfig(horizon=0, rollout_evaluations=1,
                                         max_unique_evaluations=1)
        params_before = deepcopy(PARAMS)
        selected, report = select_initial_mapping(
            arch, schedule, n_qubits=4, params=PARAMS, config=config,
            leading_one_qubit=[("u3", 3)], one_qubit=[[("u1", 0)], [], []])
        record = report["candidates"][0]
        self.assertEqual(record["layers_scored"], 1)
        self.assertEqual(record["actual_total_layers"], 3)
        self.assertEqual(record["internal_horizon"], 0)
        self.assertFalse(record["rows"][0]["terminal_boundary"])
        self.assertEqual(record["rows"][0]["score"]["counts"]["one_qubit_gates"], 2)
        self.assertEqual(record["rows"][0]["validation"]["ghost_hits"], 0)
        self.assertEqual(tuple(selected), enumerate_storage_seats(arch, 4))
        self.assertEqual(PARAMS, params_before)


class PhysicalInitialGACompilerTests(unittest.TestCase):
    def setting(self, policy="ga"):
        setting = json.loads((ROOT / "exp_setting/ga_lk_default.json").read_text())["zac_setting"][0]
        setting.pop("initial_lookahead", None)
        setting.pop("init_pop", None)
        setting.pop("init_gens", None)
        setting.update(init_strategy="physical_prefix_ga", init_engine="ga",
                       initial_ga={"search_policy": policy})
        return setting

    def test_explicit_abi9_hook_parses_without_altering_default_or_dynamic_policy(self):
        for policy in ("ga", "random"):
            setting = self.setting(policy)
            before = deepcopy(setting)
            compiler = ZAC_zzx()
            compiler.parse_setting(setting)
            self.assertEqual(setting, before)
            self.assertEqual(compiler.zzx_params["initial_ga"]["search_policy"], policy)
            self.assertEqual(compiler.zzx_params.get("search_policy", "ga"), "ga")
            self.assertEqual(compiler.zzx_params["native_abi_version"], 9)
            self.assertEqual(compiler.zzx_params["lookahead_horizon"]["max_horizon"], 8)

    def test_new_config_cannot_silently_attach_to_old_paths(self):
        for updates in ({"init_strategy": "legacy"}, {"init_engine": "sa"},
                        {"initial_lookahead": {"horizon": 2}}, {"init_pop": 8},
                        {"initial_ga": {"population_sze": 8}}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                ZAC_zzx().parse_setting({**self.setting(), **updates})
        with self.assertRaises(ValueError):
            ZAC_zzx().parse_setting({**self.setting(), "native_abi_version": 10})

    def test_given_or_trivial_mapping_bypasses_new_ga(self):
        for given in (True, False):
            compiler = ZAC_zzx()
            compiler.parse_setting(self.setting())
            compiler.given_initial_mapping = [(0, 0, 0)] if given else None
            compiler.trivial_placement = not given
            with patch.object(ZAC, "place_qubit_initial") as original, patch(
                    "zzx.physical_initial_ga.select_initial_mapping") as selector:
                compiler.place_qubit_initial()
            original.assert_called_once()
            selector.assert_not_called()

    def test_compiler_calls_no_sa_and_only_commits_selected_mapping(self):
        compiler = ZAC_zzx()
        compiler.parse_setting(self.setting("random"))
        arch, schedule = fixture()
        compiler.architecture, compiler.gate_scheduling = arch, schedule
        compiler.n_q = 4
        compiler.gate_1q_scheduling = [[], [], []]
        compiler.dict_g_1q_parent = {-1: [("u3", 0)]}
        compiler.qubit_mapping = []
        before = deepcopy(compiler.zzx_params)
        selected = list(reversed(enumerate_storage_seats(arch, 4)))
        report = {"selected_mapping_sha256": "fixture", "selection_ns": 5}
        with patch.object(ZAC, "place_qubit_initial") as sa, patch(
                "zzx.physical_initial_ga.select_initial_mapping",
                return_value=(selected, report)) as selector:
            compiler.place_qubit_initial()
        sa.assert_not_called()
        self.assertEqual(compiler.qubit_mapping, [selected])
        self.assertIs(compiler.zzx_initial_ga_report, report)
        self.assertIs(compiler.zzx_initial_lookahead_report, report)
        self.assertEqual(compiler.zzx_params, before)
        self.assertEqual(selector.call_args.kwargs["leading_one_qubit"], (("u3", 0),))
        self.assertEqual(compiler.zzx_decision_log, [])
        self.assertGreater(compiler.zzx_stage_timing_ns["initial_placement_ns"], 0)

    def test_rollout_strips_outer_controls_and_fixes_inner_horizon(self):
        setting = self.setting("random")
        before = deepcopy(setting)
        config = PhysicalInitialGAConfig.from_mapping(setting["initial_ga"])
        inner = rollout_params(setting, config.prefix_config())
        self.assertEqual(setting, before)
        self.assertNotIn("initial_ga", inner)
        self.assertNotIn("init_strategy", inner)
        self.assertNotIn("init_engine", inner)
        self.assertEqual(inner["search_policy"], "ga")
        self.assertEqual(inner["lookahead_horizon"]["max_horizon"], 0)
        self.assertEqual(inner["max_unique_evaluations"], 32)


if __name__ == "__main__":
    unittest.main()
