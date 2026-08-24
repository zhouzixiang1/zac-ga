"""Differential tests for the built native wheel (skipped when absent)."""
from __future__ import annotations

import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zzx.boundary_problem import (  # noqa: E402
    NATIVE_ABI_VERSION,
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
    NativeResidentBackend,
    build_info,
    native_available,
)
from zzx.reference_backend import (  # noqa: E402
    ReferenceResidentBackend,
    compatible_2d,
    ghost_hit_atoms,
    replay_phase_batches,
)


@unittest.skipUnless(native_available(), "zac_native_core wheel is not installed")
class TestCompiledBackend(unittest.TestCase):
    def setUp(self):
        self.architecture = ArchitectureSnapshot.from_coordinates(
            8, ((float(x), float(y)) for y in range(3) for x in range(3)))
        self.native = NativeResidentBackend(self.architecture)
        self.reference = ReferenceResidentBackend()

    def assertFitnessEqual(self, first, second):
        self.assertEqual(first.chromosome, second.chromosome)
        self.assertEqual(first.feasible, second.feasible)
        self.assertEqual(first.move_batches, second.move_batches)
        self.assertEqual(first.transfers, second.transfers)
        self.assertEqual(first.phase_batches, second.phase_batches)
        for name in ("negative_log_fidelity", "transfer_nll",
                     "idle_excitation_nll", "coherence_nll",
                     "move_time_us", "total_distance_um"):
            self.assertAlmostEqual(getattr(first, name), getattr(second, name),
                                   delta=1e-12)

    def test_build_manifest_is_auditable(self):
        info = build_info()
        self.assertEqual(info["native_abi_version"], NATIVE_ABI_VERSION)
        self.assertEqual(info["rich_boundary_wire_version"], 2)
        self.assertTrue(info["extension_sha256"])
        self.assertEqual(info["flat_wire_version"], 1)
        self.assertEqual(info["cxx_standard"], 17)
        self.assertFalse(info["openmp"])
        self.assertFalse(info["fast_math"])
        self.assertTrue(info["compiler_id"])

    def test_random_geometry_kernels_match(self):
        rng = random.Random(6101)
        for _ in range(250):
            legs = tuple(
                Leg.between(
                    (rng.randrange(-3, 4), rng.randrange(-3, 4)),
                    (rng.randrange(-3, 4), rng.randrange(-3, 4)),
                )
                for _ in range(rng.randrange(1, 6)))
            ghosts = tuple(
                Ghost(index, Point(rng.randrange(-3, 4), rng.randrange(-3, 4)))
                for index in range(rng.randrange(0, 6)))
            expected = ghost_hit_atoms(legs, ghosts)
            actual = tuple(self.native._module.ghost_hits(
                [leg.to_wire() for leg in legs],
                [ghost.to_wire() for ghost in ghosts]))
            self.assertEqual(actual, expected)
            if len(legs) > 1:
                first = legs[0]
                second = legs[1]
                first_vector = (first.source.x, first.target.x,
                                first.source.y, first.target.y)
                second_vector = (second.source.x, second.target.x,
                                 second.source.y, second.target.y)
                self.assertEqual(
                    self.native._module.compatible_2d(first_vector, second_vector),
                    compatible_2d(first_vector, second_vector),
                )

    def test_hand_built_candidates_match(self):
        safe = MovementPhase((
            Leg.between((0.0, 0.0), (2.0, 0.0)),
            Leg.between((0.0, 2.0), (2.0, 2.0)),
        ), owners=(0, 1))
        conflicting = MovementPhase((
            Leg.between((0.0, 0.0), (2.0, 2.0)),
            Leg.between((2.0, 2.0), (0.0, 0.0)),
        ), owners=(0, 1))
        problem = BoundaryProblem(self.architecture, (
            CandidatePlan((0,), (safe,), 2),
            CandidatePlan((1,), (conflicting,), 0),
            CandidatePlan((2,), (), 5),
        ), boundary_id="hand")
        reference = self.reference.solve_boundary(problem)
        native = self.native.solve_boundary(problem)
        legacy = self.native.solve_boundary_legacy(problem)
        self.assertFitnessEqual(reference.winner, native.winner)
        self.assertFitnessEqual(native.winner, legacy.winner)
        for flat_value, legacy_value in zip(native.evaluated, legacy.evaluated):
            self.assertFitnessEqual(flat_value, legacy_value)
        self.assertEqual(native.evaluations, 3)
        self.assertGreater(native.fitness_ns, 0)
        self.assertGreaterEqual(native.marshal_ns, native.native_parse_ns)

    def test_malformed_flat_offsets_fail_closed(self):
        problem = BoundaryProblem(
            self.architecture, (CandidatePlan((0,), (), 0),))
        flat = flatten_candidates(problem.candidates)
        flat.chromosome_offsets[-1] = 2
        with self.assertRaisesRegex(ValueError, "offset"):
            self.native._module.evaluate_many_flat(
                self.native._architecture, flat.buffers(), BoundaryConfig().to_wire())

    def test_single_leg_ghost_failure_matches(self):
        phase = MovementPhase(
            (Leg.between((0.0, 0.0), (2.0, 2.0)),),
            (Ghost(7, Point(1.0, 1.0)),), (0,))
        problem = BoundaryProblem(
            self.architecture, (CandidatePlan((0,), (phase,), 0),))
        first = self.reference.evaluate_many(problem)[0]
        second = self.native.evaluate_many(problem)[0]
        self.assertEqual(first.feasible, second.feasible)
        self.assertTrue(math.isinf(second.negative_log_fidelity))

    def test_resident_migration_can_defer_single_leg_repair(self):
        phase = MovementPhase(
            (Leg.between((0.0, 0.0), (2.0, 2.0)),),
            (Ghost(7, Point(1.0, 1.0)),), (0,))
        problem = BoundaryProblem(
            self.architecture, (CandidatePlan((0,), (phase,), 0),))
        config = BoundaryConfig(enforce_single_leg_ghost=False)
        first = self.reference.evaluate_many(problem, config=config)[0]
        second = self.native.evaluate_many(problem, config=config)[0]
        self.assertFitnessEqual(first, second)
        self.assertTrue(second.feasible)

    def test_pairwise_ghost_conflict_matches(self):
        phase = MovementPhase(
            (Leg.between((0.0, 0.0), (0.0, 1.0)),
             Leg.between((1.0, 1.0), (1.0, 2.0))),
            (Ghost(99, Point(0.0, 2.0)),),
            (0, 1),
        )
        problem = BoundaryProblem(
            self.architecture, (CandidatePlan((0,), (phase,), 0),))
        first = self.reference.evaluate_many(problem)[0]
        second = self.native.evaluate_many(problem)[0]
        self.assertFitnessEqual(first, second)
        self.assertTrue(second.feasible)
        self.assertEqual(second.move_batches, 2)

    def test_endpoint_precedence_matches_and_avoids_greedy_dead_end(self):
        first_leg = Leg.between((0.0, 0.0), (2.0, 0.0))
        second_leg = Leg.between((10.0, 10.0), (1.0, 0.0))
        phase = MovementPhase(
            (first_leg, second_leg),
            (Ghost(1, first_leg.source), Ghost(2, second_leg.source)),
            (1, 2),
        )
        problem = BoundaryProblem(
            self.architecture, (CandidatePlan((0,), (phase,), 0),))
        first = self.reference.evaluate_many(problem)[0]
        second = self.native.evaluate_many(problem)[0]
        self.assertFitnessEqual(first, second)
        self.assertTrue(second.feasible)
        self.assertEqual(second.phase_batches, (((0,), (1,)),))

    def test_public_replay_kernel_matches_independent_python_oracle(self):
        rng = random.Random(38127)
        for _ in range(250):
            points = tuple(Point(float(rng.randrange(0, 7)),
                                 float(rng.randrange(0, 7)))
                           for _ in range(8))
            owners = tuple(rng.sample(range(8), rng.randrange(1, 7)))
            legs = []
            for owner in owners:
                target = Point(float(rng.randrange(0, 7)),
                               float(rng.randrange(0, 7)))
                if target == points[owner]:
                    target = Point(target.x + 1.0, target.y)
                legs.append(Leg.between(points[owner], target))
            phase = MovementPhase(
                tuple(legs),
                tuple(Ghost(atom, point)
                      for atom, point in enumerate(points)),
                owners,
            )
            try:
                expected = replay_phase_batches(phase)
            except ValueError:
                with self.assertRaises(RuntimeError):
                    self.native._module.replay_phase_batches(phase.to_wire(), 0)
            else:
                actual = tuple(tuple(batch) for batch in
                               self.native._module.replay_phase_batches(
                                   phase.to_wire(), 0))
                self.assertEqual(expected, actual)
                raw = tuple(tuple(batch) for batch in
                            self.native._module.replay_phase_batches_raw(
                                [leg.to_wire() for leg in phase.legs],
                                [ghost.to_wire() for ghost in phase.ghosts],
                                list(phase.owners), 0))
                self.assertEqual(expected, raw)

    def test_seeded_random_phases_match(self):
        rng = random.Random(9473)
        candidates = []
        for chromosome in range(40):
            legs = []
            owners = []
            for owner in range(rng.randrange(0, 6)):
                source = Point(float(rng.randrange(0, 5)),
                               float(rng.randrange(0, 5)))
                target = Point(float(rng.randrange(0, 5)),
                               float(rng.randrange(0, 5)))
                if source == target:
                    target = Point(target.x + 1.0, target.y)
                legs.append(Leg.between(source, target))
                owners.append(owner)
            phases = (MovementPhase(tuple(legs), owners=tuple(owners)),)
            candidates.append(CandidatePlan(
                (chromosome,), phases, rng.randrange(0, 8)))
        problem = BoundaryProblem(self.architecture, tuple(candidates),
                                  boundary_id="random")
        first = self.reference.evaluate_many(problem)
        second = self.native.evaluate_many(problem)
        legacy = self.native.evaluate_many_legacy(problem)
        for reference, native, old_wire in zip(first, second, legacy):
            self.assertFitnessEqual(reference, native)
            self.assertFitnessEqual(native, old_wire)
        self.assertEqual(
            self.reference.solve_boundary(problem).winner.chromosome,
            self.native.solve_boundary(problem).winner.chromosome,
        )

    def test_allocation_free_phase_path_matches_generic_oracle(self):
        """The <=64-leg formal fast path must preserve exact replay order."""
        rng = random.Random(88231)
        candidates = []
        for chromosome in range(400):
            points = tuple(Point(float(rng.randrange(0, 8)),
                                 float(rng.randrange(0, 8)))
                           for _ in range(8))
            owners = tuple(rng.sample(range(8), rng.randrange(0, 9)))
            legs = []
            for owner in owners:
                target = Point(float(rng.randrange(0, 8)),
                               float(rng.randrange(0, 8)))
                if target == points[owner]:
                    target = Point(target.x + 1.0, target.y)
                legs.append(Leg.between(points[owner], target))
            ghosts = tuple(Ghost(atom, point)
                           for atom, point in enumerate(points))
            candidates.append(CandidatePlan(
                (chromosome,),
                (MovementPhase(tuple(legs), ghosts, owners),),
                rng.randrange(0, 5),
            ))
        problem = BoundaryProblem(
            self.architecture, tuple(candidates), boundary_id="phase-fast-path")
        fast = self.native.evaluate_many(
            problem, config=BoundaryConfig(exact_coloring_threshold=0))
        # A nonzero threshold smaller than every nontrivial phase forces the
        # established generic replay without activating exact coloring.
        generic = self.native.evaluate_many(
            problem, config=BoundaryConfig(exact_coloring_threshold=1))
        self.assertEqual(len(fast), len(generic))
        for optimized, oracle in zip(fast, generic):
            self.assertFitnessEqual(oracle, optimized)

    def test_exact_coloring_path_matches(self):
        phase = MovementPhase((
            Leg.between((0.0, 0.0), (3.0, 3.0)),
            Leg.between((1.0, 3.0), (2.0, 0.0)),
            Leg.between((2.0, 0.0), (1.0, 3.0)),
            Leg.between((3.0, 3.0), (0.0, 0.0)),
        ), owners=(0, 1, 2, 3))
        problem = BoundaryProblem(
            self.architecture, (CandidatePlan((0,), (phase,), 0),))
        config = BoundaryConfig(exact_coloring_threshold=8)
        first = self.reference.evaluate_many(problem, config=config)[0]
        second = self.native.evaluate_many(problem, config=config)[0]
        self.assertFitnessEqual(first, second)

    def test_evaluation_budget_is_deterministic(self):
        problem = BoundaryProblem(self.architecture, tuple(
            CandidatePlan((index,), (), index)
            for index in range(5)))
        config = BoundaryConfig(seed=91, max_unique_evaluations=3)
        first = self.native.solve_boundary(problem, config)
        second = self.native.solve_boundary(problem, config)
        self.assertEqual(first.winner.chromosome, second.winner.chromosome)
        self.assertEqual(first.evaluations, 3)


if __name__ == "__main__":
    unittest.main()
