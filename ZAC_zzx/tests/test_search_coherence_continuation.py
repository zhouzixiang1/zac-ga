"""Search-time coherence continuation at the linear model's OOD boundary."""
from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zzx.boundary_problem import (  # noqa: E402
    ArchitectureSnapshot,
    BoundaryProblem,
    CandidatePlan,
    Leg,
    MovementPhase,
)
from zzx.exact_current_reference import _coherence_log_ratio  # noqa: E402
from zzx.reference_backend import (  # noqa: E402
    T2_US,
    ReferenceResidentBackend,
    _search_coherence_absolute_nll,
    _search_coherence_delta_nll,
)


class TestSearchCoherenceContinuation(unittest.TestCase):
    def test_in_domain_absolute_ratio_is_unchanged(self):
        before = (100.0, 200.0)
        after = (150.0, 260.0)
        expected = sum(
            math.log1p(-prior / T2_US)
            - math.log1p(-current / T2_US)
            for prior, current in zip(before, after))
        self.assertAlmostEqual(
            _search_coherence_absolute_nll(before, after), expected,
            delta=1e-15)
        self.assertAlmostEqual(
            _coherence_log_ratio(before, after, T2_US), expected,
            delta=1e-15)

    def test_one_crossing_switches_whole_boundary_to_exponential(self):
        before = (T2_US - 1.0, 100.0)
        after = (T2_US + 2.0, 113.0)
        expected = (3.0 + 13.0) / T2_US
        self.assertEqual(
            _search_coherence_absolute_nll(before, after), expected)
        self.assertEqual(
            _coherence_log_ratio(before, after, T2_US), expected)

    def test_already_ood_boundary_uses_increment_not_absolute_idle(self):
        before = (T2_US + 100.0, 20.0)
        delta = (7.0, 11.0)
        expected = sum(delta) / T2_US
        self.assertEqual(
            _search_coherence_delta_nll(before, delta), expected)
        self.assertEqual(
            _coherence_log_ratio(
                before, tuple(a + b for a, b in zip(before, delta)), T2_US),
            expected)

    def test_real_boundary_candidate_remains_feasible_after_crossing(self):
        phase = MovementPhase(
            (Leg.between((0.0, 0.0), (2.0, 0.0)),), owners=(0,))
        candidate = CandidatePlan((0,), (phase,), idle_exposures=0)
        problem = BoundaryProblem(
            ArchitectureSnapshot(2), (candidate,),
            prior_idle_time_us=(T2_US - 1.0, 10.0))
        result = ReferenceResidentBackend().evaluate_many(problem)[0]

        self.assertTrue(result.feasible, result.error)
        self.assertEqual(len(result.candidate_idle_time_us), 2)
        self.assertAlmostEqual(
            result.coherence_nll,
            sum(result.candidate_idle_time_us) / T2_US,
            delta=1e-15)
        self.assertTrue(math.isfinite(result.negative_log_fidelity))

    def test_legacy_ownerless_diagnostic_also_continues(self):
        # This intentionally huge non-formal leg makes one phase exceed T2.
        # Owner-less DTOs lack an absolute per-atom ledger, so their compatible
        # continuation uses the aggregate stationary/mover phase increments.
        phase = MovementPhase(
            (Leg.between((0.0, 0.0), (7_000_000_000.0, 0.0)),))
        candidate = CandidatePlan((0,), (phase,), idle_exposures=0)
        result = ReferenceResidentBackend().evaluate_many(BoundaryProblem(
            ArchitectureSnapshot(2), (candidate,)))[0]

        self.assertTrue(result.feasible, result.error)
        mover_idle = result.move_time_us - 30.0
        expected = (result.move_time_us + mover_idle) / T2_US
        self.assertAlmostEqual(result.coherence_nll, expected, delta=1e-12)

    def test_invalid_absolute_inputs_still_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "finite and non-negative"):
            _search_coherence_absolute_nll((0.0,), (math.inf,))
        with self.assertRaisesRegex(ValueError, "finite and non-negative"):
            _coherence_log_ratio((0.0,), (-1.0,), T2_US)


if __name__ == "__main__":
    unittest.main()
