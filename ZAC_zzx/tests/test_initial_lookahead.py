"""Initializer defaults, prefix isolation, physical-score and horizon regressions."""
from copy import deepcopy
import json
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zac.ds.architecture import Architecture
from zac.zac import ZAC
from zzx.initial_lookahead import (InitialLookaheadConfig, PrefixLayerProvider,
                                   architecture_spec, candidate_pool, evaluate_mapping,
                                   resolve_initial_setting, rollout_params,
                                   select_initial_mapping)
from zzx.native_backend import native_available
from zzx.zac_zzx import ZAC_zzx


def fixture():
    spec = json.loads((ROOT / "hardware_spec/toy_architecture.json").read_text())
    spec["operation_duration"] = {"rydberg": .36, "1qGate": 52., "atom_transfer": 15.}
    architecture = Architecture(spec)
    architecture.preprocessing()
    return architecture, [(0, 0, q) for q in range(4)], [[(0, 1)], [(2, 3)], [(0, 2)]]


PARAMS = {"backend": "reference", "alpha_lookahead": .5,
          "return_assignment_k": 1, "return_candidate_limit": 2,
          "pin_radius": 1, "coloring_exact_threshold": 0}


class InitialLookaheadTests(unittest.TestCase):
    def test_pool_is_unique_storage_permutations_with_sa_first(self):
        _, mapping, _ = fixture()
        pool = candidate_pool(mapping, count=4, seed=19)
        self.assertEqual(pool, candidate_pool(mapping, count=4, seed=19))
        self.assertEqual(pool[0], tuple(mapping))
        self.assertEqual(len(set(pool)), 4)
        for candidate in pool:
            self.assertEqual(set(candidate), set(mapping))

    def test_small_pool_does_not_fake_duplicate_candidates(self):
        self.assertEqual(len(candidate_pool([(0, 0, 0)], count=4, seed=0)), 1)
        self.assertEqual(len(candidate_pool([(0, 0, 0), (0, 0, 1)], count=4, seed=0)), 2)

    def test_invalid_controls_fail(self):
        for kwargs in ({"horizon": -1}, {"horizon": True}, {"horizon": 9},
                       {"candidates": 0}, {"rho": 0}, {"rho": float("nan")},
                       {"rho": "0.7"}, {"rho": None}, {"seed": -1},
                       {"rollout_evaluations": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                InitialLookaheadConfig(**kwargs)
        with self.assertRaises(ValueError):
            InitialLookaheadConfig.from_mapping({"horizn": 2})
        for value in ([], False, 0, "horizon=2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                InitialLookaheadConfig.from_mapping(value)

    def test_provider_keeps_full_length_but_rejects_future_reads(self):
        _, _, schedule = fixture()
        provider = PrefixLayerProvider(schedule, [[], [], []])
        self.assertEqual(provider.layer_count, 3)
        self.assertEqual(provider.read_layer(0), ((0, 1),))
        with self.assertRaises(RuntimeError):
            provider.read_layer(1)
        with self.assertRaises(RuntimeError):
            provider.one_qubit_for_stage(1)

    def test_effective_policy_does_not_change_dynamic_config(self):
        original = {**PARAMS, "method_id": "ours_lk", "lookahead_horizon": 8, "seed": 9}
        before = deepcopy(original)
        low = rollout_params(original, InitialLookaheadConfig(horizon=0))
        high = rollout_params(original, InitialLookaheadConfig(horizon=8))
        self.assertEqual(low, high)
        self.assertEqual(low["lookahead_horizon"]["max_horizon"], 0)
        self.assertEqual(original, before)

    def test_real_h0_scores_first_layer_and_one_qubit_time_without_terminal(self):
        architecture, mapping, schedule = fixture()
        record = evaluate_mapping(
            architecture, mapping, schedule, params=PARAMS,
            config=InitialLookaheadConfig(horizon=0, rollout_evaluations=1),
            leading_one_qubit=[("u3", 3)], one_qubit=[[("u1", 0)], [], []])
        self.assertEqual(record["layers_scored"], 1)
        self.assertEqual(record["actual_total_layers"], 3)
        self.assertEqual(set(record["ordered_layer_reads"]), {0})
        row = record["rows"][0]
        self.assertFalse(row["terminal_boundary"])
        self.assertEqual(row["validation"]["ghost_hits"], 0)
        self.assertEqual(row["score"]["counts"]["one_qubit_gates"], 2)
        self.assertEqual(row["score"]["counts"]["two_qubit_gates"], 1)
        self.assertGreater(row["score"]["duration_us"], 104)
        self.assertEqual(len(row["score"]["idle_time_us"]), 4)

    def test_no_candidate_error_is_silently_replaced_with_sa(self):
        architecture, mapping, schedule = fixture()
        with patch("zzx.initial_lookahead.evaluate_mapping", side_effect=RuntimeError("infeasible")):
            with self.assertRaises(RuntimeError) as raised:
                select_initial_mapping(architecture, mapping, schedule, params=PARAMS,
                                       config=InitialLookaheadConfig())
        self.assertEqual(len(raised.exception.initial_lookahead_report["candidates"]), 4)

    def test_selection_preserves_global_rng_and_uses_stable_tie_break(self):
        architecture, mapping, schedule = fixture()
        random.seed(37)
        before = random.getstate()
        np.random.seed(37)
        numpy_before = np.random.get_state()
        def score(*args, **kwargs):
            random.random()
            np.random.random()
            return {"status": "success", "weighted_nll": 1.0}
        with patch("zzx.initial_lookahead.evaluate_mapping", side_effect=score):
            selected, report = select_initial_mapping(architecture, mapping, schedule, params=PARAMS,
                                                      config=InitialLookaheadConfig())
        self.assertEqual(selected, mapping)
        self.assertEqual(report["selected_candidate"], 0)
        self.assertEqual(random.getstate(), before)
        numpy_after = np.random.get_state()
        self.assertEqual(numpy_before[0], numpy_after[0])
        np.testing.assert_array_equal(numpy_before[1], numpy_after[1])
        self.assertEqual(numpy_before[2:], numpy_after[2:])

    def test_invalid_strategy_is_not_silently_consumed(self):
        with self.assertRaises(ValueError):
            ZAC_zzx().parse_setting({"init_strategy": "typo"})
        with self.assertRaises(ValueError):
            ZAC_zzx().parse_setting({"initial_lookahead": {"horizon": 2}})

    def test_original_default_parser_remains_usable(self):
        compiler = ZAC_zzx()
        compiler.parse_setting({"placer": "zac", "reuse": True})
        self.assertEqual(compiler.placer_kind, "zac")
        self.assertNotIn("init_strategy", compiler.zzx_params)

    def test_empty_schedule_is_explicit_and_does_not_read_layer_zero(self):
        architecture, mapping, _ = fixture()
        record = evaluate_mapping(architecture, mapping, [], params=PARAMS,
                                  config=InitialLookaheadConfig(horizon=2))
        self.assertEqual(record["layers_scored"], 0)
        self.assertEqual(record["ordered_layer_reads"], [])
        self.assertEqual(record["weighted_nll"], 0)

    def test_unconfigured_given_and_trivial_paths_do_not_invoke_selector(self):
        for kind in ("unconfigured", "given", "trivial"):
            with self.subTest(kind=kind):
                compiler = ZAC_zzx()
                if kind != "unconfigured":
                    compiler.zzx_params = {"init_strategy": "physical_prefix"}
                if kind == "given":
                    compiler.given_initial_mapping = [(0, 0, 0)]
                compiler.trivial_placement = kind == "trivial"
                with patch.object(ZAC, "place_qubit_initial") as original, patch(
                        "zzx.initial_lookahead.select_initial_mapping") as selector:
                    compiler.place_qubit_initial()
                original.assert_called_once()
                selector.assert_not_called()

    def test_opt_in_hook_runs_sa_once_and_records_selection(self):
        architecture, mapping, schedule = fixture()
        compiler = ZAC_zzx()
        compiler.architecture = architecture
        compiler.gate_scheduling = schedule
        compiler.gate_1q_scheduling = [[], [], []]
        compiler.qubit_mapping = []
        compiler.zzx_params = {**PARAMS, "init_strategy": "physical_prefix",
                               "initial_lookahead": {"horizon": 0}}
        def sa(_compiler):
            _compiler.qubit_mapping.append(deepcopy(mapping))
        with patch.object(ZAC, "place_qubit_initial", autospec=True, side_effect=sa) as original, patch(
                "zzx.initial_lookahead.select_initial_mapping",
                return_value=(list(reversed(mapping)), {"selected_candidate": 1})) as selector:
            compiler.place_qubit_initial()
        original.assert_called_once()
        selector.assert_called_once()
        self.assertEqual(compiler.qubit_mapping, [list(reversed(mapping))])
        self.assertGreater(compiler.zzx_initial_lookahead_report["sa_initialization_ns"], 0)

    @staticmethod
    def public_setting():
        return json.loads((ROOT / "exp_setting/ga_lk_default.json").read_text())["zac_setting"][0]

    def test_public_abi9_entry_does_not_need_private_ablation_contract(self):
        setting = self.public_setting()
        before = deepcopy(setting)
        compiler = ZAC_zzx()
        compiler.parse_setting(setting)
        self.assertEqual(setting, before)
        self.assertEqual(compiler.zzx_params["native_abi_version"], 9)
        self.assertEqual(compiler.zzx_params["native_wheel_sha256"], setting["native_wheel_sha256"])
        self.assertEqual(compiler.zzx_params["init_strategy"], "physical_prefix")
        self.assertEqual(compiler.zzx_params["initial_lookahead"],
                         {"horizon": 2, "candidates": 4, "rho": .7,
                          "rollout_evaluations": 32, "seed": 0})

    def test_omitted_strategy_enables_default_and_inherits_seed(self):
        setting = self.public_setting()
        setting.pop("init_strategy")
        setting.pop("initial_lookahead")
        setting["seed"] = 17
        before = deepcopy(setting)
        compiler = ZAC_zzx()
        compiler.parse_setting(setting)
        self.assertEqual(setting, before)
        self.assertEqual(compiler.zzx_params["initial_lookahead"],
                         {"horizon": 2, "candidates": 4, "rho": .7,
                          "rollout_evaluations": 32, "seed": 17})

    def test_explicit_legacy_disables_default_and_selector(self):
        setting = self.public_setting()
        setting["init_strategy"] = "legacy"
        setting.pop("initial_lookahead")
        compiler = ZAC_zzx()
        compiler.parse_setting(setting)
        self.assertEqual(compiler.zzx_params["init_strategy"], "legacy")
        self.assertNotIn("initial_lookahead", compiler.zzx_params)
        with patch.object(ZAC, "place_qubit_initial") as original, patch(
                "zzx.initial_lookahead.select_initial_mapping") as selector:
            compiler.place_qubit_initial()
        original.assert_called_once()
        selector.assert_not_called()

    def test_default_hook_runs_one_sa_and_one_selection(self):
        architecture, mapping, schedule = fixture()
        setting = self.public_setting()
        setting.pop("init_strategy")
        setting.pop("initial_lookahead")
        compiler = ZAC_zzx()
        compiler.parse_setting(setting)
        compiler.architecture = architecture
        compiler.gate_scheduling = schedule
        compiler.gate_1q_scheduling = [[], [], []]
        compiler.qubit_mapping = []
        def sa(_compiler):
            _compiler.qubit_mapping.append(deepcopy(mapping))
        with patch.object(ZAC, "place_qubit_initial", autospec=True, side_effect=sa) as original, patch(
                "zzx.initial_lookahead.select_initial_mapping",
                return_value=(list(reversed(mapping)), {"selected_candidate": 1})) as selector:
            compiler.place_qubit_initial()
        original.assert_called_once()
        selector.assert_called_once()
        self.assertEqual(selector.call_args.kwargs["config"], InitialLookaheadConfig())
        self.assertEqual(compiler.qubit_mapping, [list(reversed(mapping))])

    def test_historical_abi8_configs_retain_original_initialization(self):
        setting = json.loads((ROOT / "exp_setting/ours_lk_v2.json").read_text())["zac_setting"][0]
        compiler = ZAC_zzx()
        compiler.parse_setting(setting)
        self.assertNotIn("init_strategy", compiler.zzx_params)
        self.assertNotIn("initial_lookahead", compiler.zzx_params)

    def test_historical_ablation_contract_requires_explicit_opt_in(self):
        setting = self.public_setting()
        setting.pop("init_strategy")
        setting.pop("initial_lookahead")
        compiler = ZAC_zzx()
        compiler._paper_ablation_contract = {"native_abi_version": 9, "max_horizon": 8}
        compiler.parse_setting(setting)
        self.assertNotIn("init_strategy", compiler.zzx_params)
        setting["init_strategy"] = "physical_prefix"
        compiler.parse_setting(setting)
        self.assertEqual(compiler.zzx_params["init_strategy"], "physical_prefix")

    def test_baseline_nl_h0_non_ga_and_non_sa_do_not_acquire_default(self):
        setting = self.public_setting()
        setting.pop("init_strategy")
        setting.pop("initial_lookahead")
        variants = ({"method_id": "ours_nl"}, {"placer": "zac"}, {"placer": "batch"},
                    {"init_engine": "ga"}, {"engine": "greedy"},
                    {"search_policy": "greedy_only"}, {"experiment_schema": None},
                    {"lookahead_horizon": {**setting["lookahead_horizon"], "max_horizon": 0}})
        for changes in variants:
            with self.subTest(changes=changes):
                effective = resolve_initial_setting({**setting, **changes})
                self.assertNotIn("init_strategy", effective)
                self.assertNotIn("initial_lookahead", effective)

    def test_internal_rollout_cannot_reenable_or_nest_initialization(self):
        setting = self.public_setting()
        effective = rollout_params(setting, InitialLookaheadConfig())
        resolved = resolve_initial_setting(effective)
        self.assertEqual(resolved["method_id"], "ours_nl")
        self.assertEqual(resolved["lookahead_horizon"]["max_horizon"], 0)
        self.assertNotIn("init_strategy", resolved)
        self.assertNotIn("initial_lookahead", resolved)

    def test_default_reference_path_is_available_without_native_provenance(self):
        from zzx.algorithm_v2 import SCHEMA2_NATIVE_REQUIRED_KEYS
        setting = self.public_setting()
        for key in SCHEMA2_NATIVE_REQUIRED_KEYS | {"init_strategy", "initial_lookahead"}:
            setting.pop(key, None)
        setting["backend"] = "reference"
        compiler = ZAC_zzx()
        compiler.parse_setting(setting)
        self.assertEqual(compiler.zzx_params["init_strategy"], "physical_prefix")

    def test_public_abi9_entry_preserves_fail_closed_contract_checks(self):
        variants = ({"native_abi_version": 10}, {"native_abi_version": 9.0},
                    {"native_fail_closed": False},
                    {"native_wheel_sha256": "invalid"}, {"backend": "reference"},
                    {"resyn": True}, {"unrecognized_option": 1},
                    {"init_strategy": []}, {"initial_lookahead": []},
                    {"initial_lookahead": {"horizon": 9}},
                    {"initial_lookahead": {"rho": "0.7"}})
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ZAC_zzx().parse_setting({**self.public_setting(), **changes})

    def test_disabled_strategy_rejects_unused_initializer_controls(self):
        with self.assertRaisesRegex(ValueError, "requires init_strategy"):
            ZAC_zzx().parse_setting({**self.public_setting(), "init_strategy": "legacy"})

    def test_historical_manual_initializer_explicitly_uses_legacy(self):
        import ast
        tree = ast.parse((ROOT / "experiments_v2/initial_lookahead_runner.py").read_text())
        # The research runner compares SA and its own selected mappings; it
        # must never call the public default selector before making that pool.
        assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                       and isinstance(node.value, ast.Constant) and node.value.value == "legacy"]
        self.assertTrue(any(isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name) and target.value.id == "setting"
                            and isinstance(target.slice, ast.Constant) and target.slice.value == "init_strategy"
                            for node in assignments for target in node.targets))

    @unittest.skipUnless(native_available(), "matching native extension required")
    def test_native_horizon_pair_same_pool_common_prefix_and_true_discount(self):
        architecture, mapping, schedule = fixture()
        params = {**PARAMS, "backend": "native"}
        records = []
        for horizon in (0, 1):
            _, report = select_initial_mapping(
                architecture, mapping, schedule, params=params,
                config=InitialLookaheadConfig(horizon=horizon, candidates=2,
                                              rollout_evaluations=4, rho=.7))
            records.append(report)
        self.assertEqual(records[0]["candidate_pool_sha256"], records[1]["candidate_pool_sha256"])
        self.assertEqual(records[0]["rollout_config"], records[1]["rollout_config"])
        for low, high in zip(records[0]["candidates"], records[1]["candidates"]):
            self.assertEqual(high["status"], "success", high)
            self.assertEqual(low["rows"][0], high["rows"][0])
            self.assertLessEqual(max(high["ordered_layer_reads"]), 1)
            self.assertAlmostEqual(high["weighted_nll"], sum(
                row["increment_nll"] * .7**row["layer"] for row in high["rows"]), places=12)
            self.assertTrue(all(row["validation"]["ghost_hits"] == 0 for row in high["rows"]))

    @unittest.skipUnless(native_available(), "matching native extension required")
    def test_native_full_window_only_finishes_at_true_terminal_and_rho_one_telescopes(self):
        architecture, mapping, schedule = fixture()
        args = dict(params={**PARAMS, "backend": "native"},
                    config=InitialLookaheadConfig(horizon=8, candidates=1,
                                                  rollout_evaluations=4, rho=1.0))
        first = evaluate_mapping(architecture, mapping, schedule, **args)
        second = evaluate_mapping(architecture, mapping, schedule, **args)
        self.assertEqual(first["rows"], second["rows"])
        self.assertEqual([row["terminal_boundary"] for row in first["rows"]], [False, False, True])
        self.assertAlmostEqual(first["weighted_nll"], first["rows"][-1]["cumulative_nll"], places=12)


if __name__ == "__main__":
    unittest.main()
