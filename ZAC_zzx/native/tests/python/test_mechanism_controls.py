"""Opt-in mechanism controls, physical fixtures, and pre-edit ABI9 parity.

Run against the independent build with PYTHONPATH pointing to its extension
and ZAC_zzx. The parity fixture compares every public result field except timing.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
from test_native_rich_solver import (
    toy_problem, participant_cycle_capacity_problem, ghost_sensitive_return_problem,
    stay_blocker_problem, tuned_search_problem, serial_participant_pair_polish_problem,
)
from zzx.boundary_problem import ArchitectureSnapshot, Point, RichH0Problem, RichSearchConfig
from zzx.native_backend import (
    NativeResidentBackend, NativeBackendUnavailable, mechanism_controls,
)


def future_problem():
    architecture = ArchitectureSnapshot.from_coordinates(
        2, ((0, 0), (1, 0), (0, 4), (1, 4)), (2, 3))
    architecture = replace(architecture, entangling_site_pairs=((0, 1),))
    return RichH0Problem(
        architecture=architecture, current_points=(Point(0, 4), Point(1, 4)),
        participants=(), gate_domains=(), static_ghosts=(), eligible=(),
        min_returns=0, eviction_order_indices=(), forced_return_mask=(),
        return_domains=(), matched_gate_genes=(), selected_horizon=2,
        future_layers=((1, ((0, 1),)), (2, ((0, 1),))), boundary_id="mechanism-test")


def result_snapshot(result):
    value = asdict(result)
    value.pop("timing")
    return value


def parity_snapshot():
    problems = [toy_problem(), toy_problem(indexed=True)]
    for make in (participant_cycle_capacity_problem, ghost_sensitive_return_problem,
                 stay_blocker_problem, tuned_search_problem,
                 serial_participant_pair_polish_problem):
        fixture = make()
        problems.append(fixture[1] if isinstance(fixture, tuple) else fixture)
    problems.append(future_problem())
    output = []
    for index, problem in enumerate(problems):
        for policy, limit in (("ga", 512), ("ga", 1), ("greedy_only", 1)):
            config = RichSearchConfig(
                operator_profile="tuned", search_policy=policy,
                max_horizon=problem.selected_horizon, max_unique_evaluations=128,
                direct_enumeration_limit=limit)
            result = NativeResidentBackend(problem.architecture).solve_rich_boundary(
                problem, config, random.Random(index).getstate())
            output.append(result_snapshot(result))
    return output


class MechanismControlsTest(unittest.TestCase):
    def solve(self, problem, **controls):
        config = RichSearchConfig(operator_profile="tuned", max_unique_evaluations=128,
                                  max_horizon=problem.selected_horizon,
                                  alpha_lookahead=1.0, decay_rho=1.0, decay_epsilon=0.0)
        with mechanism_controls(**controls):
            return NativeResidentBackend(problem.architecture).solve_rich_boundary(
                problem, config, random.Random(0).getstate())

    def test_static_is_candidate_dependent_but_does_not_propagate(self):
        problem = future_problem()
        propagated = self.solve(problem)
        static = self.solve(problem, propagation="static_poststate")
        placed = self.solve(replace(problem, current_points=(Point(0, 0), Point(1, 0))),
                            propagation="static_poststate")
        self.assertTrue(propagated.winner.feasible and static.winner.feasible)
        self.assertEqual(propagated.forecast_by_depth[1], static.forecast_by_depth[1])
        self.assertEqual(static.forecast_by_depth[1], static.forecast_by_depth[2])
        self.assertLess(propagated.forecast_by_depth[2], static.forecast_by_depth[2])
        self.assertLess(placed.forecast_nll, static.forecast_nll)
        self.assertEqual(static.forecast_breakdown["terminal"], 0.0)
        self.assertEqual(propagated.forecast_breakdown["terminal"], 0.0)
        self.assertGreater(static.operator_stats["mechanism_snapshot_resets"], 0)

    def test_small_space_sequential_control_and_budget(self):
        result = self.solve(toy_problem(), decision="sequential")
        self.assertTrue(result.winner.feasible)
        self.assertTrue(result.search_mode.startswith("sequential-residency-gates/"))
        stats = result.operator_stats
        self.assertEqual(stats["mechanism_sequential_decision"], 1)
        self.assertEqual(sum(stats[key] for key in (
            "mechanism_seed_evaluations", "mechanism_residency_evaluations",
            "mechanism_gate_evaluations")), result.unique_evaluations)
        self.assertLessEqual(result.unique_evaluations, result.stochastic_budget)

    def test_exit_restores_default_wire_and_default_result(self):
        problem = toy_problem()
        config = RichSearchConfig(operator_profile="tuned", max_unique_evaluations=128)
        state = random.Random(1).getstate()
        before = NativeResidentBackend(problem.architecture).solve_rich_boundary(problem, config, state)
        wire = config.to_wire()
        self.solve(problem)
        after = NativeResidentBackend(problem.architecture).solve_rich_boundary(problem, config, state)
        self.assertEqual(wire, config.to_wire())
        self.assertEqual(result_snapshot(before), result_snapshot(after))
        self.assertFalse(any(key.startswith("mechanism_") for key in after.operator_stats))

    def test_controls_preserve_capacity_ghost_and_coupled_cycle_feasibility(self):
        for make in (participant_cycle_capacity_problem, ghost_sensitive_return_problem,
                     tuned_search_problem, serial_participant_pair_polish_problem):
            problem = make()[1]
            for decision in ("joint", "sequential"):
                with self.subTest(fixture=make.__name__, decision=decision):
                    result = self.solve(problem, decision=decision)
                    self.assertTrue(result.winner.feasible)
                    if make is ghost_sensitive_return_problem:
                        self.assertEqual(result.return_assignments, ((2, 4),))
                    self.assertLessEqual(result.unique_evaluations, result.stochastic_budget)

    def test_reject_unsupported_and_nested_controls(self):
        for kwargs in ({"terminal": "on"}, {"propagation": "constant"},
                       {"propagation": "static_poststate", "decision": "sequential"}):
            with self.assertRaises(ValueError), mechanism_controls(**kwargs):
                pass
        with mechanism_controls():
            with self.assertRaises(ValueError), mechanism_controls():
                pass

    def test_legacy_runtime_fails_closed(self):
        import zac_native_core
        with patch.object(zac_native_core, "MECHANISM_CONTROL_VERSION", 0):
            with mechanism_controls(), self.assertRaises(NativeBackendUnavailable):
                NativeResidentBackend(toy_problem().architecture)

    def test_pre_edit_binary_default_parity(self):
        baseline = ROOT.parent / "IEEE_conference_template/build/native/mechanism-v1/baseline"
        self.assertTrue(list(baseline.glob("zac_native_core*.so")))
        environment = dict(os.environ, PYTHONPATH=f"{baseline}:{ROOT}")
        completed = subprocess.run([sys.executable, "-B", __file__, "--snapshot"],
                                   env=environment, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(completed.stdout), json.loads(json.dumps(parity_snapshot())))


if __name__ == "__main__":
    if "--snapshot" in sys.argv:
        print(json.dumps(parity_snapshot(), sort_keys=True))
    else:
        unittest.main()
