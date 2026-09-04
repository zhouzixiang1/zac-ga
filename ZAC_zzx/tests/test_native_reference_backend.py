"""Backend-contract tests that do not require the compiled wheel."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zzx.boundary_problem import (  # noqa: E402
    ArchitectureSnapshot,
    BoundaryConfig,
    BoundaryProblem,
    CandidatePlan,
    Ghost,
    Leg,
    MovementPhase,
    Point,
    flatten_candidates,
)
from zzx.native_backend import (  # noqa: E402
    NativeBackendError,
    NativeBackendUnavailable,
    select_backend,
)
from zzx.reference_backend import (  # noqa: E402
    ReferenceResidentBackend,
    color_phase,
    ghost_hit_atoms,
    replay_phase_batches,
)


def candidate(chromosome=(0,), *, idle=0, phases=()):
    return CandidatePlan(tuple(chromosome), tuple(phases), idle)


class TestBoundaryDto(unittest.TestCase):
    def test_duplicate_chromosome_fails_closed(self):
        architecture = ArchitectureSnapshot(2)
        value = candidate()
        with self.assertRaisesRegex(ValueError, "unique"):
            BoundaryProblem(architecture, (value, value))

    def test_dynamic_effective_horizon_is_explicit(self):
        problem = BoundaryProblem(
            ArchitectureSnapshot(2), (candidate(),), selected_horizon=3)
        config = BoundaryConfig(horizon_policy="dynamic", max_horizon=4)
        self.assertEqual(problem.effective_horizon, 3)
        self.assertEqual(problem.selected_horizon, 3)
        self.assertEqual(config.to_wire()["horizon_policy"], "dynamic")

    def test_flat_offsets_cover_all_nested_values(self):
        phase = MovementPhase(
            (Leg.between((0.0, 0.0), (2.0, 1.0)),),
            (Ghost(5, Point(4.0, 4.0)),),
            (1,),
        )
        values = (
            candidate((3, 4), idle=2, phases=(phase,)),
            candidate((8,), idle=0, phases=()),
        )
        flat = flatten_candidates(values)
        self.assertEqual(list(flat.chromosome_offsets), [0, 2, 3])
        self.assertEqual(list(flat.candidate_phase_offsets), [0, 1, 1])
        self.assertEqual(list(flat.phase_leg_offsets), [0, 1])
        self.assertEqual(list(flat.phase_ghost_offsets), [0, 1])
        self.assertEqual(list(flat.phase_owner_offsets), [0, 1])
        self.assertEqual(list(flat.idle_exposures), [2, 0])
        self.assertEqual(len(flat.leg_values), 5)
        self.assertEqual(len(flat.ghost_xy), 2)


class TestReferenceGeometry(unittest.TestCase):
    def test_single_leg_ghost_hit(self):
        leg = Leg.between((0.0, 0.0), (2.0, 2.0))
        hits = ghost_hit_atoms((leg,), (Ghost(9, Point(1.0, 1.0)),))
        self.assertEqual(hits, (9,))

    def test_crossing_legs_need_two_batches(self):
        phase = MovementPhase((
            Leg.between((0.0, 0.0), (2.0, 2.0)),
            Leg.between((2.0, 2.0), (0.0, 0.0)),
        ))
        self.assertEqual(color_phase(phase), ((0,), (1,)))

    def test_pairwise_ghost_edge_splits_individually_safe_legs(self):
        phase = MovementPhase(
            (Leg.between((0.0, 0.0), (0.0, 1.0)),
             Leg.between((1.0, 1.0), (1.0, 2.0))),
            (Ghost(99, Point(0.0, 2.0)),),
            (0, 1),
        )
        for leg in phase.legs:
            self.assertEqual(ghost_hit_atoms((leg,), phase.ghosts), ())
        self.assertEqual(color_phase(phase), ((0,), (1,)))

    def test_endpoint_precedence_avoids_greedy_dead_end(self):
        first = Leg.between((0.0, 0.0), (2.0, 0.0))
        second = Leg.between((10.0, 10.0), (1.0, 0.0))
        phase = MovementPhase(
            (first, second),
            (Ghost(1, first.source), Ghost(2, second.source)),
            (1, 2),
        )
        # Distance ordering would run leg 1 first and place atom 2 at (1, 0),
        # permanently blocking leg 0.  Endpoint precedence must reverse them.
        self.assertEqual(color_phase(phase), ((1,), (0,)))
        self.assertEqual(replay_phase_batches(phase), ((0,), (1,)))

    def test_physical_terms_and_lexicographic_winner(self):
        architecture = ArchitectureSnapshot(4)
        moving = MovementPhase(
            (Leg.between((0.0, 0.0), (2.0, 0.0)),), owners=(0,))
        problem = BoundaryProblem(architecture, (
            candidate((1,), idle=1, phases=(moving,)),
            candidate((0,), idle=1, phases=(moving,)),
        ))
        result = ReferenceResidentBackend().solve_boundary(problem)
        self.assertEqual(result.winner.chromosome, (0,))
        self.assertEqual(result.winner.transfers, 2)
        self.assertEqual(result.winner.move_batches, 1)
        self.assertGreater(result.winner.negative_log_fidelity, 0.0)
        self.assertGreater(result.fitness_ns, 0)

    def test_single_leg_ghost_is_infeasible(self):
        phase = MovementPhase(
            (Leg.between((0.0, 0.0), (2.0, 2.0)),),
            (Ghost(8, Point(1.0, 1.0)),),
            (0,),
        )
        problem = BoundaryProblem(ArchitectureSnapshot(2),
                                  (candidate(phases=(phase,)),))
        result = ReferenceResidentBackend().evaluate_many(problem)[0]
        self.assertFalse(result.feasible)
        self.assertTrue(math.isinf(result.negative_log_fidelity))

    def test_reference_uses_exact_abi7_movement_objective(self):
        legacy_legs = (
            (2.0, 0.0, 0.0, 2.0, 0.0),
            (2.0, 0.0, 2.0, 2.0, 2.0),
        )
        phase = MovementPhase(tuple(
            Leg(value[0], Point(value[1], value[2]), Point(value[3], value[4]))
            for value in legacy_legs), owners=(0, 1))
        problem = BoundaryProblem(
            ArchitectureSnapshot(6),
            (candidate((4, 2), idle=3, phases=(phase,)),),
        )
        actual = ReferenceResidentBackend().evaluate_many(problem)[0]
        mover_idle = actual.move_time_us - 2 * 15.0
        expected_coherence = (
            -4 * math.log1p(-actual.move_time_us / 1.5e6)
            -2 * math.log1p(-mover_idle / 1.5e6))
        expected_nll = (
            -actual.transfers * math.log(0.999)
            -3 * math.log(0.9975)
            + expected_coherence)
        self.assertAlmostEqual(actual.negative_log_fidelity,
                               expected_nll, delta=1e-12)
        self.assertAlmostEqual(actual.coherence_nll,
                               expected_coherence, delta=1e-12)

    def test_prior_idle_and_two_phases_use_one_exact_log_ratio(self):
        architecture = ArchitectureSnapshot(2)
        phases = (
            MovementPhase(
                (Leg.between((0.0, 0.0), (2.0, 0.0)),), owners=(0,)),
            MovementPhase(
                (Leg.between((0.0, 1.0), (2.0, 1.0)),), owners=(1,)),
        )
        value = candidate((0,), phases=phases)
        problem = BoundaryProblem(
            architecture, (value,), prior_idle_time_us=(100.0, 200.0))
        actual = ReferenceResidentBackend().evaluate_many(problem)[0]
        phase_time = actual.move_time_us / 2.0
        delta = 2.0 * phase_time - 30.0
        expected = sum(
            math.log1p(-prior / 1.5e6)
            - math.log1p(-(prior + delta) / 1.5e6)
            for prior in (100.0, 200.0))
        self.assertAlmostEqual(actual.coherence_nll, expected, delta=1e-12)
        self.assertEqual(actual.candidate_idle_time_us, (delta, delta))

    def test_prior_idle_vector_is_validated(self):
        architecture = ArchitectureSnapshot(2)
        value = candidate()
        with self.assertRaisesRegex(ValueError, "every atom"):
            BoundaryProblem(
                architecture, (value,), prior_idle_time_us=(1.0,))
        with self.assertRaisesRegex(ValueError, "non-negative"):
            BoundaryProblem(
                architecture, (value,), prior_idle_time_us=(-1.0, 0.0))


class TestFailClosedSelection(unittest.TestCase):
    def test_formal_reference_backend_is_rejected(self):
        with self.assertRaisesRegex(NativeBackendError, "require"):
            select_backend(ArchitectureSnapshot(2), backend="reference", formal=True)

    def test_missing_native_does_not_fall_back(self):
        with patch("zzx.native_backend.import_module",
                   side_effect=ImportError("missing")):
            with self.assertRaises(NativeBackendUnavailable):
                select_backend(ArchitectureSnapshot(2), backend="native", formal=True)


if __name__ == "__main__":
    unittest.main()
