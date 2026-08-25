"""ABI7 one-call residency search differential and fail-closed tests."""
from __future__ import annotations

import math
import random
import unittest
from dataclasses import replace

from zzx.boundary_problem import (
    ArchitectureSnapshot,
    BoundaryConfig,
    BoundaryProblem,
    CandidatePlan,
    Ghost,
    Leg,
    MovementPhase,
    Point,
    RichForecastTerm,
    RichGateOption,
    RichH0Problem,
    RichReturnOption,
    RichSearchConfig,
)
from zzx.native_backend import (
    NativeBackendError,
    NativeBackendUnavailable,
    NativeResidentBackend,
    build_info,
    native_available,
)
from zzx.reference_backend import (
    ReferenceResidentBackend,
    _guard_rich_forecast_gate_projection,
    _recover_rich_infeasible_current_gate_projection,
    _rich_normalize,
    evaluate_decay_forecast,
    evaluate_rich_exact_candidate,
    solve_rich_exact_reference,
)


def architecture():
    return ArchitectureSnapshot.from_coordinates(
        3, ((0, 0), (1, 0), (2, 2), (2, 3)), (3,))


def toy_problem(*, indexed=False, terms=(), horizon=0):
    arch = architecture()
    gate = RichGateOption(
        10, 0, 1,
        None if indexed else Point(0, 0),
        None if indexed else Point(1, 0),
        0 if indexed else None,
        1 if indexed else None,
    )
    returned = RichReturnOption(3, None if indexed else Point(2, 3), 1.0)
    return RichH0Problem(
        architecture=arch,
        current_points=() if indexed else (
            Point(0, 0), Point(1, 0), Point(2, 2)),
        current_site_ids=(0, 1, 2) if indexed else (),
        participants=(0, 1),
        gate_domains=((gate,),),
        static_ghosts=(),
        eligible=(2,),
        min_returns=0,
        eviction_order_indices=(0,),
        forced_return_mask=(False,),
        return_domains=((returned,),),
        matched_gate_genes=(0,),
        boundary_id="toy",
        selected_horizon=horizon,
        forecast_terms=tuple(terms),
    )


def participant_cycle_capacity_problem():
    """A target participant cycle cannot satisfy resident eviction demand."""
    arch = ArchitectureSnapshot.from_coordinates(
        3,
        ((0, 0), (10, 10), (20, 20), (1, 2), (21, 22)),
        (3, 4),
    )
    return arch, RichH0Problem(
        architecture=arch,
        current_points=(Point(0, 0), Point(10, 10), Point(20, 20)),
        participants=(0, 1),
        gate_domains=((RichGateOption(
            10, 0, 1, Point(0, 0), Point(10, 10)),),),
        static_ghosts=(),
        # Atom 0 is both eligible and a target participant.  Its forced
        # RETURN is a back->out cycle, while atom 2 is the only resident that
        # can actually release one unit of target-layer capacity.
        eligible=(0, 2),
        min_returns=1,
        eviction_order_indices=(0, 1),
        forced_return_mask=(True, False),
        return_domains=(
            (RichReturnOption(3, Point(1, 2), 1.0),),
            (RichReturnOption(4, Point(21, 22), 1.0),),
        ),
        matched_gate_genes=(0,),
        boundary_id="participant-cycle-capacity",
    )


def ghost_sensitive_return_problem(*, terms=(), horizon=0):
    arch = ArchitectureSnapshot.from_coordinates(
        3,
        ((1, 1), (10, 10), (0, 0), (2, 2), (2, 3)),
        (3, 4),
    )
    return arch, RichH0Problem(
        architecture=arch,
        current_points=(Point(1, 1), Point(10, 10), Point(0, 0)),
        participants=(0, 1),
        gate_domains=((RichGateOption(
            10, 0, 1, Point(1, 1), Point(10, 10)),),),
        static_ghosts=(),
        eligible=(2,),
        min_returns=1,
        eviction_order_indices=(0,),
        forced_return_mask=(True,),
        return_domains=((
            RichReturnOption(3, Point(2, 2), 1.0),
            RichReturnOption(4, Point(2, 3), 2.0),
        ),),
        matched_gate_genes=(0,),
        boundary_id="ghost-sensitive-return",
        selected_horizon=horizon,
        forecast_terms=tuple(terms),
    )


def equal_distance_return_problem(*, terms=(), horizon=0):
    arch = ArchitectureSnapshot.from_coordinates(
        3,
        ((10, 10), (11, 10), (0, 0), (2, 0), (0, 2)),
        (3, 4),
    )
    return arch, RichH0Problem(
        architecture=arch,
        current_points=(Point(10, 10), Point(11, 10), Point(0, 0)),
        participants=(0, 1),
        gate_domains=((RichGateOption(
            10, 0, 1, Point(10, 10), Point(11, 10)),),),
        static_ghosts=(),
        eligible=(2,),
        min_returns=1,
        eviction_order_indices=(0,),
        forced_return_mask=(True,),
        return_domains=((
            RichReturnOption(3, Point(2, 0), 1.0),
            RichReturnOption(4, Point(0, 2), 1.0),
        ),),
        matched_gate_genes=(0,),
        boundary_id="equal-distance-return",
        selected_horizon=horizon,
        forecast_terms=tuple(terms),
    )


def tuned_search_problem():
    coordinates = tuple((float(atom), 0.0) for atom in range(9)) + tuple(
        (float(site + 4), 4.0) for site in range(5))
    arch = ArchitectureSnapshot.from_coordinates(
        9, coordinates, tuple(range(9, 14)))
    gate_domains = []
    for gate in range(2):
        q1, q2 = gate * 2, gate * 2 + 1
        gate_domains.append(tuple(RichGateOption(
            100 + gate * 20 + option, q1, q2,
            Point(*coordinates[q1]), Point(*coordinates[q2]))
                                  for option in range(10)))
    problem = RichH0Problem(
        architecture=arch,
        current_points=tuple(Point(*value) for value in coordinates[:9]),
        participants=(0, 1, 2, 3),
        gate_domains=tuple(gate_domains),
        static_ghosts=(),
        eligible=(4, 5, 6, 7, 8),
        min_returns=0,
        eviction_order_indices=(0, 1, 2, 3, 4),
        forced_return_mask=(False,) * 5,
        return_domains=tuple((RichReturnOption(
            9 + index, Point(*coordinates[9 + index]), 1.0),)
                             for index in range(5)),
        matched_gate_genes=(0, 0),
        boundary_id="tuned-search",
    )
    config = RichSearchConfig(
        operator_profile="tuned",
        population_size=6,
        iterations=1,
        neighbor_sample_size=16,
        neighbors_per_solution=2,
        elite_count=2,
        early_stop_patience=2,
        max_unique_evaluations=256,
        crossover_rate=.5,
        local_polish_sweeps=2,
    )
    return arch, problem, config


def serial_participant_pair_polish_problem():
    """One gate and one cycle bit form a strict two-gene local trap.

    Gate option 1 is short but its direct first leg hits atom 2 at (1, 1).
    Cycling participant 0 through the real storage site removes that hit.  The
    long gate option 0 makes the cycle alone worse, while the joint gate/cycle
    edit is the exhaustive physical optimum.
    """
    coordinates = (
        (0.0, 0.0), (0.0, 4.0), (1.0, 1.0), (0.0, -300.0),
        (0.0, 10000.0), (2.0, 10000.0),
        (2.0, 2.0), (2.0, 4.0),
        (0.0, 12000.0), (2.0, 12000.0),
        (0.0, 13000.0), (2.0, 13000.0),
        (0.0, 14000.0), (2.0, 14000.0),
    )
    arch = ArchitectureSnapshot.from_coordinates(3, coordinates, (3,))
    target_pairs = ((4, 5), (6, 7), (8, 9), (10, 11), (12, 13))
    gates = tuple(
        RichGateOption(10 + option, 0, 1,
                       Point(*coordinates[first]),
                       Point(*coordinates[second]), first, second)
        for option, (first, second) in enumerate(target_pairs)
    )
    problem = RichH0Problem(
        architecture=arch,
        current_points=tuple(Point(*value) for value in coordinates[:3]),
        participants=(0, 1),
        gate_domains=(gates,),
        static_ghosts=(),
        # Participant 0's true decision bit is a RETURN->re-entry cycle.
        eligible=(0,),
        min_returns=0,
        eviction_order_indices=(0,),
        forced_return_mask=(False,),
        return_domains=((RichReturnOption(
            3, Point(*coordinates[3]), 300.0),),),
        matched_gate_genes=(0,),
        boundary_id="serial-participant-pair-polish",
    )
    return arch, problem


@unittest.skipUnless(native_available(), "ABI7 native extension is not installed")
class TestNativeRichSolver(unittest.TestCase):
    def setUp(self):
        self.arch = architecture()
        self.backend = NativeResidentBackend(self.arch)

    def test_python_mt19937_state_is_exact(self):
        operations = []
        for index in range(128):
            operations.extend((
                ("random",),
                ("randbelow", 1 + index % 97),
                ("getrandbits", 1 + index % 64),
            ))
        for seed in (0, 1, 20260823, 2**63 - 1):
            rng = random.Random(seed)
            state = rng.getstate()
            expected = []
            for operation in operations:
                if operation[0] == "random":
                    expected.append(rng.random())
                elif operation[0] == "randbelow":
                    expected.append(rng.randrange(operation[1]))
                else:
                    expected.append(rng.getrandbits(operation[1]))
            actual = self.backend._module.python_random_probe(state, operations)
            self.assertEqual(expected, list(actual["values"]))
            self.assertEqual(rng.getstate(), actual["state"])

    def test_formal_abi7_requires_exact_scheduler_snapshot(self):
        self.backend._formal_native = True
        with self.assertRaisesRegex(
                NativeBackendError, "exact scheduler snapshot"):
            self.backend.solve_rich_boundary(
                toy_problem(), RichSearchConfig(operator_profile="exact"),
                random.Random(0).getstate())

    def test_hand_current_physical_parity(self):
        config = RichSearchConfig(operator_profile="exact")
        result = self.backend.solve_rich_h0(
            toy_problem(), config, random.Random(0).getstate())
        self.assertEqual((0, 1), result.winner.chromosome)
        self.assertEqual(((2, 3),), result.return_assignments)
        candidate = CandidatePlan(
            chromosome=(0, 1),
            phases=(
                MovementPhase(
                    legs=(Leg.between((2, 2), (2, 3)),),
                    ghosts=(Ghost(0, Point(0, 0)), Ghost(1, Point(1, 0)),
                            Ghost(2, Point(2, 2))),
                    owners=(2,),
                ),
                MovementPhase(),
            ),
            idle_exposures=0,
        )
        # Atom 2 is not a target-CZ participant, so ABI7 merges its 0.36 us
        # pulse idle with the movement before taking one linear-T2 log ratio.
        reference_problem = BoundaryProblem(
            self.arch, (candidate,), prior_idle_time_us=(0.0, 0.0, 0.36))
        reference = ReferenceResidentBackend().solve_boundary(
            reference_problem,
            BoundaryConfig(enforce_single_leg_ghost=False)).winner
        pulse_nll = -math.log1p(-0.36 / 1.5e6)
        self.assertAlmostEqual(reference.negative_log_fidelity + pulse_nll,
                               result.winner.negative_log_fidelity, delta=1e-15)
        self.assertEqual(reference.move_batches, result.winner.move_batches)
        self.assertAlmostEqual(reference.move_time_us,
                               result.winner.move_time_us, delta=1e-12)
        self.assertAlmostEqual(
            result.winner.negative_log_fidelity,
            result.winner.transfer_nll + result.winner.idle_excitation_nll
            + result.winner.coherence_nll,
            delta=1e-15,
        )

    def test_python_exact_rich_truth_matches_native(self):
        cases = []
        cases.append((self.arch, toy_problem(),
                      RichSearchConfig(operator_profile="exact")))
        cases.append((self.arch, replace(
            toy_problem(), prior_idle_time_us=(101.0, 202.0, 303.0)),
            RichSearchConfig(operator_profile="exact")))
        ghost_arch, ghost_problem = ghost_sensitive_return_problem()
        cases.append((ghost_arch, ghost_problem, RichSearchConfig(
            operator_profile="exact", return_assignment_k=4)))
        future_arch, future_problem = equal_distance_return_problem(
            terms=(RichForecastTerm(
                1, "return_site", "routing", 1.0,
                index=0, selector=3),),
            horizon=1)
        cases.append((future_arch, future_problem, RichSearchConfig(
            operator_profile="exact", max_horizon=1,
            alpha_lookahead=1.0, return_assignment_k=4)))
        for index, (arch, problem, config) in enumerate(cases):
            with self.subTest(index=index):
                state = random.Random(1700 + index).getstate()
                reference = solve_rich_exact_reference(problem, config, state)
                native = NativeResidentBackend(arch).solve_rich_boundary(
                    problem, config, state)
                self.assertEqual(reference.winner, native.winner)
                self.assertEqual(reference.gate_option_indices,
                                 native.gate_option_indices)
                self.assertEqual(reference.return_assignments,
                                 native.return_assignments)
                self.assertEqual(reference.reseat_assignments,
                                 native.reseat_assignments)
                self.assertEqual(
                    reference.participant_parking_assignments,
                    native.participant_parking_assignments)
                self.assertEqual(reference.return_assignment_rank,
                                 native.return_assignment_rank)
                self.assertAlmostEqual(
                    reference.search_negative_log_fidelity,
                    native.search_negative_log_fidelity, delta=1e-12)

    def test_participant_cycle_does_not_satisfy_min_returns(self):
        arch, problem = participant_cycle_capacity_problem()
        config = RichSearchConfig(operator_profile="exact")
        state = random.Random(20260825).getstate()

        # The participant bit is already one, but normalization must still
        # evict the ordinary resident despite the participant appearing first
        # in eviction_order_indices.
        expected = (0, 1, 1)
        self.assertEqual(expected, _rich_normalize(problem, (0, 1, 0)))
        reference = solve_rich_exact_reference(problem, config, state)
        backend = NativeResidentBackend(arch)
        native = backend.solve_rich_boundary(
            problem, config, state)
        native_from_raw = backend.solve_rich_boundary(
            problem, config, state, cached_winner=(0, 1, 0))
        self.assertEqual(expected, reference.winner.chromosome)
        self.assertEqual(reference.winner, native.winner)
        self.assertEqual("lru", native_from_raw.search_mode)
        self.assertEqual(expected, native_from_raw.winner.chromosome)
        self.assertEqual(reference.winner, native_from_raw.winner)
        self.assertEqual(reference.return_assignments,
                         native.return_assignments)
        self.assertEqual(((0, 3), (2, 4)), native.return_assignments)

    def test_min_returns_rejects_participant_only_capacity(self):
        arch, problem = participant_cycle_capacity_problem()
        impossible = replace(problem, eligible=(0,), min_returns=1,
                             eviction_order_indices=(0,),
                             forced_return_mask=(True,),
                             recommended_return_mask=(False,),
                             return_domains=(problem.return_domains[0],))
        config = RichSearchConfig(operator_profile="exact")
        state = random.Random(0).getstate()
        with self.assertRaisesRegex(
                ValueError, "nonparticipant eligible count"):
            solve_rich_exact_reference(impossible, config, state)
        with self.assertRaisesRegex(
                NativeBackendError, "nonparticipant eligible count"):
            NativeResidentBackend(arch).solve_rich_boundary(
                impossible, config, state)

    def test_random_small_rich_boundaries_match_python_truth(self):
        arch = ArchitectureSnapshot.from_coordinates(
            4,
            ((0, 0), (10, 10), (20, 20), (30, 30),
             (20, 25), (21, 25), (30, 25), (31, 25)),
            (4, 5, 6, 7),
        )
        rng = random.Random(20260823)
        for case in range(12):
            return_domains = (
                (RichReturnOption(4, Point(20, 25), rng.random() + .1),
                 RichReturnOption(5, Point(21, 25), rng.random() + .1)),
                (RichReturnOption(6, Point(30, 25), rng.random() + .1),
                 RichReturnOption(7, Point(31, 25), rng.random() + .1)),
            )
            terms = (
                RichForecastTerm(1, "return", "reentry", rng.random(),
                                 index=case % 2),
                RichForecastTerm(2, "stay", "residency", rng.random(),
                                 index=(case + 1) % 2),
            )
            problem = RichH0Problem(
                architecture=arch,
                current_points=(Point(0, 0), Point(10, 10),
                                Point(20, 20), Point(30, 30)),
                participants=(0, 1),
                gate_domains=((
                    RichGateOption(10, 0, 1, Point(0, 0), Point(10, 10)),
                    RichGateOption(11, 0, 1, Point(0, 0), Point(10, 10)),
                ),),
                static_ghosts=(),
                eligible=(2, 3),
                min_returns=case % 2,
                eviction_order_indices=(case % 2, (case + 1) % 2),
                forced_return_mask=(case % 3 == 0, case % 4 == 0),
                return_domains=return_domains,
                matched_gate_genes=(0,),
                boundary_id=f"random-small-{case}",
                selected_horizon=2,
                forecast_terms=terms,
            )
            config = RichSearchConfig(
                operator_profile="exact", max_horizon=2,
                alpha_lookahead=.2, decay_rho=.7,
                return_candidate_limit=6, return_assignment_k=4)
            state = random.Random(case).getstate()
            reference = solve_rich_exact_reference(problem, config, state)
            native = NativeResidentBackend(arch).solve_rich_boundary(
                problem, config, state)
            with self.subTest(case=case):
                self.assertEqual(reference.winner, native.winner)
                self.assertEqual(reference.gate_option_indices,
                                 native.gate_option_indices)
                self.assertEqual(reference.return_assignments,
                                 native.return_assignments)
                self.assertEqual(reference.reseat_assignments,
                                 native.reseat_assignments)
                self.assertEqual(
                    reference.participant_parking_assignments,
                    native.participant_parking_assignments)
                self.assertAlmostEqual(
                    reference.search_negative_log_fidelity,
                    native.search_negative_log_fidelity, delta=1e-12)

    def test_direct_enumeration_limit_is_512_not_64(self):
        arch = ArchitectureSnapshot.from_coordinates(
            2, ((0, 0), (1, 0)))
        domain = tuple(RichGateOption(
            1000 + option, 0, 1, Point(0, 0), Point(1, 0))
                       for option in range(256))
        problem = RichH0Problem(
            architecture=arch,
            current_points=(Point(0, 0), Point(1, 0)),
            participants=(0, 1),
            gate_domains=(domain,),
            static_ghosts=(),
            eligible=(),
            min_returns=0,
            eviction_order_indices=(),
            forced_return_mask=(),
            return_domains=(),
            matched_gate_genes=(0,),
            boundary_id="enumerate-256",
        )
        config = RichSearchConfig(
            operator_profile="exact", direct_enumeration_limit=512,
            max_unique_evaluations=256)
        state = random.Random(0).getstate()
        reference = solve_rich_exact_reference(problem, config, state)
        native = NativeResidentBackend(arch).solve_rich_boundary(
            problem, config, state)
        self.assertEqual("enumerate", native.search_mode)
        self.assertEqual(256, native.unique_evaluations)
        self.assertEqual(reference.winner, native.winner)

    def test_k_best_return_chooses_second_site_when_nearest_has_ghost(self):
        arch, problem = ghost_sensitive_return_problem()
        result = NativeResidentBackend(arch).solve_rich_h0(
            problem,
            RichSearchConfig(
                operator_profile="exact",
                return_candidate_limit=6,
                return_assignment_k=4,
                enforce_single_leg_ghost=True,
            ),
            random.Random(0).getstate(),
        )
        self.assertEqual(((2, 4),), result.return_assignments)
        self.assertEqual(2, result.return_assignment_rank)
        self.assertEqual(2, result.return_assignment_evaluated)
        self.assertEqual(1, result.current_ghost_rejections)
        self.assertEqual((), result.reseat_assignments)

    def test_m4_routing_replay_selects_future_safe_return_site(self):
        arch, h0_problem = equal_distance_return_problem()
        h0 = NativeResidentBackend(arch).solve_rich_h0(
            h0_problem,
            RichSearchConfig(
                operator_profile="exact", return_assignment_k=4),
            random.Random(0).getstate(),
        )
        self.assertEqual(((2, 3),), h0.return_assignments)
        routing_term = RichForecastTerm(
            1, "return_site", "routing", 1.0,
            index=0, selector=3)
        _arch, h1_problem = equal_distance_return_problem(
            terms=(routing_term,), horizon=1)
        h1 = NativeResidentBackend(arch).solve_rich_boundary(
            h1_problem,
            RichSearchConfig(
                operator_profile="exact", max_horizon=1,
                alpha_lookahead=1.0, return_assignment_k=4),
            random.Random(0).getstate(),
        )
        self.assertEqual(((2, 4),), h1.return_assignments)
        self.assertEqual(2, h1.return_assignment_rank)
        self.assertEqual(0.0, h1.future_ghost_cost)

    def test_m4_guard_pins_current_safe_k_best_assignment(self):
        """The H8/L2 forecast winner retains its complete guarded assignment.

        This is the minimal form of the medium-benchmark L2 regression: the
        raw search winner prefers the farther RETURN site, while the ABI7
        current-physics guard must pin the nearer assignment as one complete
        evaluated value in both Python and C++.
        """
        arch = ArchitectureSnapshot.from_coordinates(
            3,
            ((10, 10), (11, 10), (0, 0), (1, 0), (0, 10)),
            (3, 4),
        )
        problem = RichH0Problem(
            architecture=arch,
            current_points=(Point(10, 10), Point(11, 10), Point(0, 0)),
            participants=(0, 1),
            gate_domains=((RichGateOption(
                10, 0, 1, Point(10, 10), Point(11, 10)),),),
            static_ghosts=(),
            eligible=(2,),
            min_returns=1,
            eviction_order_indices=(0,),
            forced_return_mask=(True,),
            return_domains=((
                RichReturnOption(3, Point(1, 0), 1.0),
                RichReturnOption(4, Point(0, 10), 10.0),
            ),),
            matched_gate_genes=(0,),
            forecast_terms=(RichForecastTerm(
                2, "return_site", "routing", 100.0,
                index=0, selector=3),),
            boundary_id="ours_lk:L2:minimal-guard-parity",
            selected_horizon=8,
        )
        config = RichSearchConfig(
            operator_profile="exact", max_horizon=8,
            alpha_lookahead=1.0, return_assignment_k=4)
        rng_state = random.Random(0).getstate()
        result = NativeResidentBackend(arch).solve_rich_boundary(
            problem, config, rng_state)
        reference = solve_rich_exact_reference(
            problem, config, rng_state)
        self.assertEqual(((2, 3),), result.return_assignments)
        self.assertEqual((3,), result.current_gate_anchor_assignment_site_ids)
        self.assertEqual((3,), result.current_gate_final_assignment_site_ids)
        self.assertEqual(
            "residency-pareto-envelope", result.current_gate_guard_branch)
        self.assertGreaterEqual(result.current_gate_guard_cohort_size, 2)
        self.assertEqual(1, result.current_gate_guard_admitted_size)
        self.assertEqual(reference.winner, result.winner)
        self.assertEqual(reference.return_assignments,
                         result.return_assignments)
        self.assertEqual(reference.reseat_assignments,
                         result.reseat_assignments)
        self.assertEqual(
            reference.participant_parking_assignments,
            result.participant_parking_assignments)
        self.assertEqual(reference.current_gate_anchor,
                         result.current_gate_anchor)
        self.assertEqual(
            reference.current_gate_anchor_assignment_site_ids,
            result.current_gate_anchor_assignment_site_ids)
        self.assertEqual(
            reference.current_gate_final_assignment_site_ids,
            result.current_gate_final_assignment_site_ids)
        self.assertEqual(reference.current_gate_guard_branch,
                         result.current_gate_guard_branch)
        self.assertEqual(reference.current_gate_guard_cohort_size,
                         result.current_gate_guard_cohort_size)
        self.assertEqual(reference.current_gate_guard_admitted_size,
                         result.current_gate_guard_admitted_size)
        self.assertEqual(reference.current_gate_projection_source,
                         result.current_gate_projection_source)
        self.assertEqual(reference.current_gate_projection_evaluated,
                         result.current_gate_projection_evaluated)

    def test_m4_guard_projects_every_recommended_residency_suffix(self):
        """Rent hints survive current-gate projection as exact cohorts.

        The cached incumbent is deliberately all-STAY.  Both resident atoms
        occupy unused entangling sites, so RETURN replaces two idle-excitation
        errors with physical load/move/store work.  The guard must independently
        project the two singleton recommendations and their joint mask, then
        compare all four exact suffix cohorts in one Pareto envelope.  The
        Python truth and native result must select the same all-RETURN value.
        """
        points = tuple(Point(*value) for value in (
            (0, 0), (1, 0), (2, 0), (3, 0),
            (4, 0), (5, 0), (2, 1), (4, 1)))
        arch = ArchitectureSnapshot(
            4, points, (6, 7), ((0, 1), (2, 3), (4, 5)))
        problem = RichH0Problem(
            architecture=arch,
            current_points=(points[0], points[1], points[2], points[4]),
            participants=(0, 1),
            gate_domains=((RichGateOption(
                10, 0, 1, points[0], points[1]),),),
            static_ghosts=(),
            eligible=(2, 3),
            min_returns=0,
            eviction_order_indices=(0, 1),
            forced_return_mask=(False, False),
            recommended_return_mask=(True, True),
            return_domains=(
                (RichReturnOption(6, points[6], 1.0),),
                (RichReturnOption(7, points[7], 1.0),),
            ),
            matched_gate_genes=(0,),
            forecast_terms=(
                RichForecastTerm(
                    1, "stay", "residency", 0.1, index=0),
                RichForecastTerm(
                    1, "stay", "residency", 0.1, index=1),
            ),
            boundary_id="rent-recommendation-guard",
            selected_horizon=1,
        )
        config = RichSearchConfig(
            operator_profile="exact", max_horizon=1,
            alpha_lookahead=1.0)
        rng_state = random.Random(0).getstate()

        provisional = evaluate_rich_exact_candidate(
            problem, config, (0, 0, 0))
        reference, guard = _guard_rich_forecast_gate_projection(
            problem, config, provisional, (provisional,))
        native = NativeResidentBackend(arch).solve_rich_boundary(
            problem, config, rng_state, cached_winner=(0, 0, 0))

        self.assertEqual((0, 1, 1), reference.fitness.chromosome)
        self.assertEqual(reference.fitness, native.winner)
        self.assertEqual(((2, 6), (3, 7)), native.return_assignments)
        self.assertEqual((0, 1, 1), native.current_gate_anchor)
        self.assertEqual(4, native.current_gate_projection_evaluated)
        self.assertEqual(4, native.current_gate_guard_cohort_size)
        self.assertEqual(2, native.current_gate_guard_admitted_size)
        self.assertEqual(
            guard["current_gate_projection_evaluated"],
            native.current_gate_projection_evaluated)
        self.assertEqual(
            guard["current_gate_guard_cohort_size"],
            native.current_gate_guard_cohort_size)
        self.assertEqual(
            guard["current_gate_guard_admitted_size"],
            native.current_gate_guard_admitted_size)

    def test_current_recovery_is_forecast_independent_and_matches_python(self):
        points = tuple(Point(*value) for value in (
            (0, 0), (0, 1), (1, 1),
            (2, 2), (3, 2), (4, 4), (5, 4)))
        arch = ArchitectureSnapshot(3, points, (), ((1, 2),))
        problem = RichH0Problem(
            architecture=arch,
            current_points=(points[0], points[1], points[2]),
            participants=(0, 1),
            gate_domains=((
                RichGateOption(70, 0, 1, points[3], points[4]),
                RichGateOption(71, 0, 1, points[5], points[6]),
                RichGateOption(72, 0, 1, points[0], points[1]),
            ),),
            static_ghosts=(),
            eligible=(),
            min_returns=0,
            eviction_order_indices=(),
            forced_return_mask=(),
            return_domains=(),
            matched_gate_genes=(0,),
            # q2 blocks the only future pair and there is no storage site, so
            # forecast replay is intentionally impossible.  It may not erase
            # the executable current tail option.
            future_layers=((1, ((0, 1),)),),
            selected_horizon=1,
            boundary_id="current-recovery-forecast-independent",
        )
        config = RichSearchConfig(
            operator_profile="tuned",
            population_size=1,
            iterations=1,
            max_unique_evaluations=1,
            direct_enumeration_limit=1,
            max_horizon=1,
            alpha_lookahead=1.0,
            decay_rho=1.0,
            decay_epsilon=0.0,
        )
        reference, audit = _recover_rich_infeasible_current_gate_projection(
            problem, config, (0,))
        self.assertIsNotNone(reference)
        self.assertEqual((2,), reference.fitness.chromosome)
        self.assertEqual(3, audit["current_gate_projection_evaluated"])
        self.assertTrue(math.isfinite(reference.search_nll))

        native = NativeResidentBackend(arch).solve_rich_boundary(
            problem, config, random.Random(0).getstate())
        self.assertEqual(reference.fitness, native.winner)
        self.assertEqual((2,), native.gate_option_indices)
        self.assertIn("current-recovery", native.search_mode)
        self.assertTrue(math.isfinite(
            native.search_negative_log_fidelity))
        self.assertEqual(0.0, native.forecast_nll)

    def test_gate_target_blocker_is_reseated_before_candidate_scoring(self):
        arch = ArchitectureSnapshot.from_coordinates(
            3,
            ((0, 0), (1, 0), (2, 2), (3, 3), (2, 4)),
            (4,),
        )
        problem = RichH0Problem(
            architecture=arch,
            current_points=(Point(0, 0), Point(1, 0), Point(2, 2)),
            participants=(0, 1),
            gate_domains=((RichGateOption(
                10, 0, 1, Point(2, 2), Point(1, 0)),),),
            static_ghosts=(),
            eligible=(2,),
            min_returns=0,
            eviction_order_indices=(0,),
            forced_return_mask=(False,),
            return_domains=((RichReturnOption(4, Point(2, 4), 1.0),),),
            matched_gate_genes=(0,),
            decision_policy="always_stay",
            boundary_id="candidate-reseat",
        )
        result = NativeResidentBackend(arch).solve_rich_h0(
            problem, RichSearchConfig(operator_profile="exact"),
            random.Random(0).getstate())
        self.assertTrue(result.winner.feasible)
        self.assertEqual(((2, 3),), result.reseat_assignments)
        self.assertEqual(1, result.pre_score_reseats)
        self.assertEqual((0, 0), result.winner.chromosome)

    def test_stationary_participant_is_parked_then_reenters_before_scoring(self):
        # Atom 1 already occupies its selected target (1, 0), but that zero-leg
        # participant is a real stationary ghost on atom 0's path (0, 0)->(2, 0).
        # The only correct repair is an explicit storage parking in phase 0 and
        # re-entry after atom 0 has crossed in phase 1.
        arch = ArchitectureSnapshot.from_coordinates(
            3,
            ((1, 1), (2, 0), (1, 0), (0, 0), (10, 10)),
            (0,),
        )
        problem = RichH0Problem(
            architecture=arch,
            current_points=(Point(0, 0), Point(1, 0), Point(10, 10)),
            participants=(0, 1),
            gate_domains=((RichGateOption(
                10, 0, 1, Point(2, 0), Point(1, 0)),),),
            static_ghosts=(), eligible=(), min_returns=0,
            eviction_order_indices=(), forced_return_mask=(),
            return_domains=(), matched_gate_genes=(0,),
            boundary_id="participant-parking",
        )
        config = RichSearchConfig(
            operator_profile="exact", direct_enumeration_limit=512,
            max_unique_evaluations=512, exact_coloring_threshold=24)
        state = random.Random(0).getstate()
        reference = solve_rich_exact_reference(problem, config, state)
        native = NativeResidentBackend(arch).solve_rich_h0(
            problem, config, state)
        for result in (reference, native):
            self.assertTrue(result.winner.feasible)
            self.assertEqual(((1, 0),),
                             result.participant_parking_assignments)
            self.assertEqual(1, result.pre_score_participant_parkings)
            self.assertEqual(6, result.winner.transfers)
            self.assertEqual((((0,),), ((0,), (1,))),
                             result.winner.phase_batches)
        self.assertEqual(reference.winner, native.winner)

    def test_moving_atom_target_is_a_hard_ghost_during_out_phase(self):
        arch = ArchitectureSnapshot.from_coordinates(
            2, ((0, 0), (2, 0)))
        problem = RichH0Problem(
            architecture=arch,
            current_points=(Point(0, 0), Point(2, 0)),
            participants=(0, 1),
            gate_domains=((
                # This short option used to look attractive because atom 1
                # was omitted from the out-phase ghost set while moving.  Its
                # target (0.5, 0) lies on atom 0's single-leg trajectory.
                RichGateOption(
                    10, 0, 1, Point(1, 0), Point(0.5, 0)),
                # Parallel vertical legs are longer but strictly ghost-safe.
                RichGateOption(
                    11, 0, 1, Point(0, 10), Point(2, 10)),
            ),),
            static_ghosts=(),
            eligible=(),
            min_returns=0,
            eviction_order_indices=(),
            forced_return_mask=(),
            return_domains=(),
            matched_gate_genes=(0,),
            boundary_id="moving-endpoint-ghost",
        )
        config = RichSearchConfig(operator_profile="exact")
        state = random.Random(0).getstate()
        reference = solve_rich_exact_reference(problem, config, state)
        native = NativeResidentBackend(arch).solve_rich_boundary(
            problem, config, state)
        self.assertEqual((1,), reference.gate_option_indices)
        self.assertEqual(reference.winner, native.winner)
        self.assertEqual(reference.gate_option_indices,
                         native.gate_option_indices)
        bad_only = replace(
            problem,
            gate_domains=((problem.gate_domains[0][0],),),
            boundary_id="moving-endpoint-ghost-bad-only",
        )
        with self.assertRaisesRegex(RuntimeError, "no feasible candidate"):
            solve_rich_exact_reference(bad_only, config, state)
        with self.assertRaisesRegex(
                NativeBackendError, "no feasible candidate"):
            NativeResidentBackend(arch).solve_rich_boundary(
                bad_only, config, state)

    def test_indexed_and_legacy_geometry_are_identical(self):
        config = RichSearchConfig(operator_profile="exact")
        state = random.Random(4).getstate()
        legacy = self.backend.solve_rich_h0(toy_problem(), config, state)
        indexed = self.backend.solve_rich_h0(
            toy_problem(indexed=True), config, state)
        self.assertEqual(legacy.winner, indexed.winner)
        self.assertEqual(legacy.gate_option_indices,
                         indexed.gate_option_indices)
        self.assertEqual(legacy.return_assignments,
                         indexed.return_assignments)
        self.assertEqual(legacy.rng_state, indexed.rng_state)
        buffers = toy_problem(indexed=True).flat_buffers()
        self.assertFalse(buffers["current_xy"])
        self.assertFalse(buffers["gate_targets"])
        self.assertFalse(buffers["gate_leg_values"])
        self.assertFalse(buffers["return_xy"])

    def test_generic_decay_term_table_matches_python_oracle(self):
        terms = (
            RichForecastTerm(1, "return", "reentry", .2, index=0),
            RichForecastTerm(1, "stay", "residency", .3, index=0),
            RichForecastTerm(2, "return_site", "reentry", .4,
                             index=0, selector=3),
            RichForecastTerm(3, "gate_option", "residency", .5,
                             index=0, selector=0),
            RichForecastTerm(4, "constant", "terminal", .6),
            RichForecastTerm(8, "constant", "terminal", 100.0),
        )
        problem = toy_problem(indexed=True, terms=terms, horizon=8)
        config = RichSearchConfig(
            operator_profile="exact",
            max_horizon=8,
            alpha_lookahead=.1,
            decay_rho=.6,
            decay_epsilon=.05,
        )
        result = self.backend.solve_rich_boundary(
            problem, config, random.Random(10).getstate())
        expected = evaluate_decay_forecast(
            problem, config, result.winner.chromosome,
            result.gate_option_indices, result.return_assignments)
        self.assertAlmostEqual(expected[0], result.forecast_nll, delta=1e-15)
        self.assertEqual(expected[1], result.forecast_by_depth)
        self.assertEqual(expected[2], result.forecast_breakdown)
        self.assertGreaterEqual(result.forecast_terms_applied, expected[3])
        # Stats aggregate cutoff checks over every unique candidate.
        self.assertGreaterEqual(result.forecast_terms_skipped_cutoff,
                                expected[4])
        self.assertAlmostEqual(
            result.search_negative_log_fidelity,
            result.winner.negative_log_fidelity + result.forecast_nll,
            delta=1e-15,
        )

    def test_abi7_raw_future_layers_are_physically_rolled_out_in_cpp(self):
        arch = ArchitectureSnapshot.from_coordinates(
            4,
            (
                (0, 0), (1, 0), (4, 0), (5, 0),
                (0, 10), (1, 10), (4, 10), (5, 10),
            ),
            (4, 5, 6, 7),
        )
        arch = replace(
            arch, entangling_site_pairs=((0, 1), (2, 3)))
        problem = RichH0Problem(
            architecture=arch,
            current_points=(),
            current_site_ids=(0, 1, 6, 7),
            participants=(0, 1),
            gate_domains=((RichGateOption(
                0, 0, 1, None, None,
                target1_site_id=0, target2_site_id=1),),),
            static_ghosts=(),
            eligible=(),
            min_returns=0,
            eviction_order_indices=(),
            forced_return_mask=(),
            return_domains=(),
            matched_gate_genes=(0,),
            future_layers=((1, ((2, 3),)), (2, ((0, 1),))),
            boundary_id="native-future-layer",
            selected_horizon=2,
            prior_idle_time_us=(101.0, 202.0, 303.0, 404.0),
        )
        buffers = problem.flat_buffers()
        self.assertEqual(list(buffers["future_layer_depths"]), [1, 2])
        self.assertEqual(list(buffers["future_gate_atoms"]), [2, 3, 0, 1])
        config = RichSearchConfig(
            operator_profile="exact", max_horizon=2,
            alpha_lookahead=.2, decay_rho=.7)
        state = random.Random(11).getstate()
        result = NativeResidentBackend(arch).solve_rich_boundary(
            problem, config, state)
        reference = solve_rich_exact_reference(problem, config, state)
        self.assertEqual(reference.winner, result.winner)
        self.assertEqual(reference.gate_option_indices,
                         result.gate_option_indices)
        self.assertEqual(reference.return_assignments,
                         result.return_assignments)
        self.assertAlmostEqual(
            reference.forecast_nll, result.forecast_nll, delta=1e-15)
        self.assertEqual(reference.forecast_by_depth,
                         result.forecast_by_depth)
        self.assertEqual(reference.forecast_breakdown,
                         result.forecast_breakdown)
        self.assertGreater(result.forecast_nll, 0.0)
        self.assertTrue(math.isfinite(result.forecast_nll))
        self.assertAlmostEqual(
            result.search_negative_log_fidelity,
            result.winner.negative_log_fidelity + result.forecast_nll,
            delta=1e-12,
        )
        self.assertGreater(result.forecast_terms_applied, 0)

    def test_terminal_cleanup_is_unattenuated_bellman_potential(self):
        """Tiny alpha/rho cannot make the horizon-end RETURN disappear."""
        arch = ArchitectureSnapshot.from_coordinates(
            3,
            (
                (0, 0), (1, 0), (4, 0), (5, 0),
                (0, 10), (1, 10), (4, 10), (5, 10),
            ),
            (4, 5, 6, 7),
        )
        arch = replace(
            arch, entangling_site_pairs=((0, 1), (2, 3)))
        problem = RichH0Problem(
            architecture=arch,
            current_points=(),
            current_site_ids=(0, 1, 2),
            participants=(),
            gate_domains=(),
            static_ghosts=(),
            # Atom 2 is an ordinary resident throughout the visible window.
            # always_stay keeps the fixture focused on the endpoint value
            # rather than the current-boundary RETURN choice.
            eligible=(2,),
            min_returns=0,
            eviction_order_indices=(0,),
            forced_return_mask=(False,),
            return_domains=((RichReturnOption(6, None, 0.0),),),
            matched_gate_genes=(),
            # Both atoms stay live through depth 1.  Their joint ghost-safe
            # RETURN after depth 2 is exactly Phi(s_H), rather than another
            # alpha*rho-weighted in-window event.
            future_layers=(
                (1, ((0, 1),)),
                (2, ((0, 1),)),
            ),
            boundary_id="terminal-cleanup-potential",
            selected_horizon=2,
            prior_idle_time_us=(0.0, 0.0, 0.0),
            decision_policy="always_stay",
        )
        tiny = RichSearchConfig(
            operator_profile="exact",
            max_horizon=2,
            alpha_lookahead=0.0,
            decay_rho=1e-9,
            decay_epsilon=0.0,
            forecast_gate_candidate_budget=4,
        )
        unit = replace(tiny, alpha_lookahead=1.0, decay_rho=1.0)
        state = random.Random(20260825).getstate()
        tiny_reference = solve_rich_exact_reference(problem, tiny, state)
        unit_reference = solve_rich_exact_reference(problem, unit, state)
        tiny_native = NativeResidentBackend(arch).solve_rich_boundary(
            problem, tiny, state)

        terminal = tiny_reference.forecast_breakdown["terminal"]
        self.assertGreater(terminal, 0.0)
        self.assertAlmostEqual(
            unit_reference.forecast_breakdown["terminal"], terminal,
            delta=1e-15)
        self.assertAlmostEqual(
            tiny_reference.forecast_by_depth[2], terminal, delta=1e-15)
        self.assertEqual(tiny_reference.winner, tiny_native.winner)
        self.assertAlmostEqual(
            tiny_native.forecast_breakdown["terminal"], terminal,
            delta=1e-12)
        self.assertAlmostEqual(
            tiny_native.forecast_nll, tiny_reference.forecast_nll,
            delta=1e-12)

    def test_h0_terminal_potential_prevents_repeated_stay_deferral(self):
        """H=0 uses Phi(s0), but receives no future-layer representation."""
        arch = ArchitectureSnapshot.from_coordinates(
            3,
            (
                (0, 0), (1, 0), (4, 0), (5, 0),
                (1000, 1000),
            ),
            (4,),
        )
        arch = replace(
            arch, entangling_site_pairs=((0, 1), (2, 3)))
        problem = RichH0Problem(
            architecture=arch,
            current_points=(),
            current_site_ids=(0, 1, 2),
            participants=(0, 1),
            gate_domains=((RichGateOption(
                0, 0, 1, None, None,
                target1_site_id=0, target2_site_id=1),),),
            static_ghosts=(),
            eligible=(2,),
            min_returns=0,
            eviction_order_indices=(0,),
            forced_return_mask=(False,),
            return_domains=((RichReturnOption(4, None, 0.0),),),
            matched_gate_genes=(0,),
            future_layers=(),
            boundary_id="h0-terminal-cleanup-winner",
            selected_horizon=0,
            prior_idle_time_us=(0.0, 0.0, 0.0),
        )
        config = RichSearchConfig(
            operator_profile="exact",
            max_horizon=0,
            alpha_lookahead=1e-12,
            decay_rho=1e-12,
            decay_epsilon=0.0,
        )
        state = random.Random(20260825).getstate()
        stay = evaluate_rich_exact_candidate(problem, config, (0, 0))
        returned = evaluate_rich_exact_candidate(problem, config, (0, 1))
        reference = solve_rich_exact_reference(problem, config, state)
        native = NativeResidentBackend(arch).solve_rich_boundary(
            problem, config, state)

        # Myopic current physics prefers the cheap STAY.  Adding the physical
        # cleanup value to that same post-current state makes RETURN win now,
        # so the resident cannot repeat the one-layer deferral forever.
        self.assertLess(
            stay.fitness.negative_log_fidelity,
            returned.fitness.negative_log_fidelity)
        self.assertGreater(stay.forecast_breakdown["terminal"], 0.0)
        self.assertEqual(0.0, returned.forecast_breakdown["terminal"])
        self.assertGreater(stay.search_nll, returned.search_nll)
        self.assertEqual((0, 1), reference.winner.chromosome)
        self.assertEqual(reference.winner, native.winner)
        self.assertAlmostEqual(
            reference.search_negative_log_fidelity,
            native.search_negative_log_fidelity,
            delta=1e-12)
        self.assertEqual((), problem.future_layers)
        self.assertEqual((0.0,), returned.forecast_by_depth)

        # Entering the final 2Q layer is structurally marked by the scheduler.
        # H=0 still sees no future gate, but it must not price a cleanup after
        # the circuit has already ended.
        terminal_problem = replace(problem, terminal_boundary=True)
        terminal_reference = solve_rich_exact_reference(
            terminal_problem, config, state)
        terminal_native = NativeResidentBackend(arch).solve_rich_boundary(
            terminal_problem, config, state)
        self.assertEqual((0, 0), terminal_reference.winner.chromosome)
        self.assertEqual(terminal_reference.winner, terminal_native.winner)
        self.assertEqual(0.0, terminal_reference.forecast_nll)
        self.assertEqual(0.0, terminal_native.forecast_nll)
        self.assertEqual(0, terminal_native.forecast_terms_applied)

    def test_qft_style_reentry_interlock_uses_finite_physical_recovery(self):
        """A cyclic future front is parked/reentered, never scored as inf."""
        arch = ArchitectureSnapshot.from_coordinates(
            4,
            (
                (0, 0), (2, 0), (0, 4), (2, 4),
                (-6, -6), (6, -6), (-6, 10), (6, 10),
                (0, -8), (2, -8),
            ),
            (4, 5, 6, 7, 8, 9),
        )
        arch = replace(
            arch, entangling_site_pairs=((0, 1), (2, 3)))
        # The deterministic future placement maps each pair to the other's
        # occupied endpoints.  A strict whole-phase replay has a target/source
        # precedence cycle, while parking at the real storage sites breaks it.
        problem = RichH0Problem(
            architecture=arch,
            current_points=(
                Point(4, 3), Point(0, 2), Point(3, 4), Point(0, 4)),
            participants=(),
            gate_domains=(),
            static_ghosts=(),
            eligible=(),
            min_returns=0,
            eviction_order_indices=(),
            forced_return_mask=(),
            return_domains=(),
            matched_gate_genes=(),
            future_layers=((1, ((0, 1), (2, 3))),),
            boundary_id="qft-style-reentry-interlock",
            selected_horizon=1,
            prior_idle_time_us=(0.0, 0.0, 0.0, 0.0),
        )
        config = RichSearchConfig(
            operator_profile="exact",
            max_horizon=1,
            alpha_lookahead=1.0,
            decay_rho=1.0,
            decay_epsilon=0.0,
            forecast_gate_candidate_budget=4,
        )
        state = random.Random(0).getstate()
        reference = solve_rich_exact_reference(problem, config, state)
        result = NativeResidentBackend(arch).solve_rich_boundary(
            problem, config, state)
        for value in (
                reference.forecast_nll,
                reference.forecast_breakdown["reentry"],
                result.forecast_nll,
                result.forecast_breakdown["reentry"]):
            self.assertTrue(math.isfinite(value))
            self.assertGreater(value, 0.0)
        self.assertEqual(reference.winner, result.winner)
        self.assertAlmostEqual(
            reference.forecast_nll, result.forecast_nll, delta=1e-12)
        for category in ("residency", "reentry", "terminal", "routing"):
            self.assertAlmostEqual(
                reference.forecast_breakdown[category],
                result.forecast_breakdown[category],
                delta=1e-12,
            )

    def test_unrecoverable_forecast_rejects_candidate_then_falls_back_current(self):
        """Prefer a valid rollout, but never discard all current-safe work."""
        arch = ArchitectureSnapshot.from_coordinates(
            4, ((0, 0), (2, 0), (0, 4), (2, 4)), ())
        arch = replace(
            arch, entangling_site_pairs=((0, 1), (2, 3)))
        unsafe = (Point(4, 3), Point(0, 2))
        safe = (Point(-3, 2), Point(5, -3))
        fixed = (Point(3, 4), Point(0, 4))
        problem = RichH0Problem(
            architecture=arch,
            current_points=safe + fixed,
            participants=(0, 1),
            gate_domains=((
                RichGateOption(10, 0, 1, *unsafe),
                RichGateOption(11, 0, 1, *safe),
            ),),
            static_ghosts=(),
            eligible=(),
            min_returns=0,
            eviction_order_indices=(),
            forced_return_mask=(),
            return_domains=(),
            matched_gate_genes=(0,),
            # Depth 2 is below the cutoff, but keeps every depth-1 atom live
            # so this fixture isolates reentry rather than terminal RETURN.
            future_layers=(
                (1, ((0, 1), (2, 3))),
                (2, ((0, 1), (2, 3))),
            ),
            boundary_id="one-unrecoverable-forecast-candidate",
            selected_horizon=2,
            prior_idle_time_us=(0.0, 0.0, 0.0, 0.0),
        )
        config = RichSearchConfig(
            operator_profile="exact",
            max_horizon=2,
            alpha_lookahead=1.0,
            decay_rho=0.1,
            decay_epsilon=0.5,
            forecast_gate_candidate_budget=4,
            direct_enumeration_limit=512,
            max_unique_evaluations=512,
        )
        state = random.Random(0).getstate()
        reference = solve_rich_exact_reference(problem, config, state)
        result = NativeResidentBackend(arch).solve_rich_boundary(
            problem, config, state)
        self.assertEqual((1,), reference.winner.chromosome)
        self.assertEqual(reference.winner, result.winner)
        self.assertTrue(math.isfinite(result.forecast_nll))
        self.assertAlmostEqual(
            reference.forecast_nll, result.forecast_nll, delta=1e-12)

        bad_only = replace(
            problem,
            gate_domains=((problem.gate_domains[0][0],),),
            matched_gate_genes=(0,),
            boundary_id="all-forecast-candidates-unrecoverable",
        )
        bad_reference = solve_rich_exact_reference(bad_only, config, state)
        bad_native = NativeResidentBackend(arch).solve_rich_boundary(
            bad_only, config, state)
        self.assertEqual(bad_reference.winner, bad_native.winner)
        self.assertTrue(bad_native.winner.feasible)
        self.assertEqual(0.0, bad_native.forecast_nll)
        self.assertAlmostEqual(
            bad_native.search_negative_log_fidelity,
            bad_native.winner.negative_log_fidelity,
            delta=1e-12)
        self.assertIn("forecast-current-fallback", bad_native.search_mode)

    def test_native_future_rollout_reuses_identical_post_boundary_state(self):
        arch = ArchitectureSnapshot.from_coordinates(
            4,
            (
                (0, 0), (1, 0), (4, 0), (5, 0),
                (0, 10), (1, 10), (4, 10), (5, 10),
            ),
            (4, 5, 6, 7),
        )
        arch = replace(
            arch, entangling_site_pairs=((0, 1), (2, 3)))
        # Atom 2 is already at RETURN site 6.  STAY and RETURN therefore
        # produce the exact same physical post-boundary state, even though
        # they are distinct chromosomes and current actions.
        problem = RichH0Problem(
            architecture=arch,
            current_points=(),
            current_site_ids=(0, 1, 6, 7),
            participants=(0, 1),
            gate_domains=((RichGateOption(
                0, 0, 1, None, None,
                target1_site_id=0, target2_site_id=1),),),
            static_ghosts=(),
            eligible=(2,),
            min_returns=0,
            eviction_order_indices=(0,),
            forced_return_mask=(False,),
            return_domains=((RichReturnOption(6, None, 0.0),),),
            matched_gate_genes=(0,),
            future_layers=((1, ((2, 3),)),),
            boundary_id="native-future-state-cache",
            selected_horizon=1,
        )
        config = RichSearchConfig(
            operator_profile="exact", max_horizon=1,
            alpha_lookahead=.2, decay_rho=.7,
            direct_enumeration_limit=512,
            max_unique_evaluations=512,
        )
        state = random.Random(29).getstate()
        cached = NativeResidentBackend(arch).solve_rich_boundary(
            problem, config, state)
        uncached = NativeResidentBackend(arch).solve_rich_boundary(
            problem, replace(config, fitness_cache=False), state)
        self.assertEqual(cached.winner, uncached.winner)
        self.assertEqual(cached.gate_option_indices,
                         uncached.gate_option_indices)
        self.assertEqual(cached.return_assignments,
                         uncached.return_assignments)
        self.assertEqual(cached.forecast_nll, uncached.forecast_nll)
        self.assertGreater(
            cached.operator_stats["forecast_state_cache_hits"], 0)

    def test_strict_h0_rejects_future_and_horizon_mismatch(self):
        term = RichForecastTerm(1, "constant", "terminal", 1.0)
        future = toy_problem(terms=(term,), horizon=1)
        with self.assertRaises(NativeBackendError):
            self.backend.solve_rich_h0(
                future,
                RichSearchConfig(operator_profile="exact", max_horizon=1),
                random.Random(0).getstate(),
            )
        with self.assertRaises(NativeBackendError):
            self.backend.solve_rich_boundary(
                future,
                RichSearchConfig(operator_profile="exact", max_horizon=2),
                random.Random(0).getstate(),
            )

    def test_fixed_seed_cache_and_wire_are_deterministic(self):
        problem = toy_problem(indexed=True)
        state = random.Random(777).getstate()
        cached = (0, 1)
        first = self.backend.solve_rich_h0(
            problem, RichSearchConfig(operator_profile="exact"), state,
            cached_winner=cached)
        second = self.backend.solve_rich_h0(
            problem, RichSearchConfig(operator_profile="exact"), state,
            cached_winner=cached)
        self.assertEqual(first.winner, second.winner)
        self.assertEqual(first.rng_state, second.rng_state)
        self.assertEqual("lru", first.search_mode)

    def test_malformed_wire_and_wheel_hash_fail_closed(self):
        problem = toy_problem(indexed=True)
        buffers = problem.flat_buffers()
        buffers["gate_target_site_ids"].pop()
        with self.assertRaises(Exception):
            self.backend._module.solve_rich_boundary(
                self.backend._architecture,
                buffers,
                RichSearchConfig(operator_profile="exact").to_wire(),
                random.Random(0).getstate(),
                None,
            )
        with self.assertRaises(NativeBackendUnavailable):
            build_info(expected_wheel_sha256="0" * 64)
        info = build_info()
        if info["wheel_registered"]:
            registered = build_info(
                require_registered_wheel=True,
                expected_wheel_sha256=info["native_wheel_sha256"])
            self.assertEqual(info["native_wheel_sha256"],
                             registered["native_wheel_sha256"])
        else:
            with self.assertRaises(NativeBackendUnavailable):
                build_info(require_registered_wheel=True)

    def test_tuned_search_seed_and_cache_toggle_are_exact(self):
        arch, problem, config = tuned_search_problem()
        state = random.Random(20260823).getstate()
        backend = NativeResidentBackend(arch)
        first = backend.solve_rich_boundary(problem, config, state)
        repeated = backend.solve_rich_boundary(problem, config, state)
        uncached = backend.solve_rich_boundary(
            problem,
            RichSearchConfig(**{
                key: getattr(config, key)
                for key in config.__dataclass_fields__
                if key != "fitness_cache"
            }, fitness_cache=False),
            state,
        )
        for other in (repeated, uncached):
            self.assertEqual(first.winner, other.winner)
            self.assertEqual(first.gate_option_indices,
                             other.gate_option_indices)
            self.assertEqual(first.return_assignments,
                             other.return_assignments)
            self.assertEqual(first.reseat_assignments,
                             other.reseat_assignments)
            self.assertEqual(
                first.participant_parking_assignments,
                other.participant_parking_assignments)
            self.assertEqual(first.rng_state, other.rng_state)
        self.assertGreater(first.operator_stats["crossovers"], 0)
        self.assertGreater(first.operator_stats["local_polish_evaluations"], 0)
        self.assertLessEqual(first.unique_evaluations,
                             config.max_unique_evaluations)

    def test_serial_gate_cycle_pair_polish_escapes_two_gene_trap(self):
        arch, problem = serial_participant_pair_polish_problem()
        # Seed 1 deliberately avoids the conflict-cluster mutation, so the
        # tiny GA itself does not happen to sample the coupled edit.
        state = random.Random(1).getstate()
        exact_config = RichSearchConfig(
            operator_profile="exact",
            direct_enumeration_limit=16,
            max_unique_evaluations=16,
            local_polish_sweeps=0,
            fitness_cache=False,
        )
        baseline = evaluate_rich_exact_candidate(
            problem, exact_config, (0, 0))
        gate_only = evaluate_rich_exact_candidate(
            problem, exact_config, (1, 0))
        cycle_only = evaluate_rich_exact_candidate(
            problem, exact_config, (0, 1))
        self.assertFalse(gate_only.fitness.feasible)
        self.assertGreater(cycle_only.search_nll, baseline.search_nll)

        truth = solve_rich_exact_reference(problem, exact_config, state)
        self.assertEqual((1, 1), truth.winner.chromosome)

        small_ga = RichSearchConfig(
            operator_profile="tuned",
            population_size=1,
            iterations=1,
            neighbors_per_solution=1,
            neighbor_sample_size=2,
            elite_count=1,
            max_unique_evaluations=32,
            direct_enumeration_limit=1,
            crossover_rate=0.0,
            local_polish_sweeps=0,
            fitness_cache=False,
        )
        backend = NativeResidentBackend(arch)
        without_polish = backend.solve_rich_boundary(
            problem, small_ga, state)
        self.assertNotEqual(truth.winner.chromosome,
                            without_polish.winner.chromosome)

        with_polish = backend.solve_rich_boundary(
            problem, replace(small_ga, local_polish_sweeps=1), state)
        self.assertEqual(truth.winner.chromosome,
                         with_polish.winner.chromosome)
        self.assertAlmostEqual(
            truth.winner.negative_log_fidelity,
            with_polish.winner.negative_log_fidelity,
            places=12,
        )
        self.assertGreater(
            with_polish.operator_stats["local_polish_evaluations"], 0)
        self.assertLessEqual(with_polish.unique_evaluations,
                             small_ga.max_unique_evaluations)

    def test_repeated_tuned_direct_boundary_reuses_exact_result_without_rng(self):
        problem = toy_problem(indexed=True)
        config = RichSearchConfig(
            operator_profile="tuned",
            direct_enumeration_limit=512,
            max_unique_evaluations=512,
        )
        first_state = random.Random(17).getstate()
        second_state = random.Random(23).getstate()
        first = self.backend.solve_rich_boundary(
            problem, config, first_state)
        repeated = self.backend.solve_rich_boundary(
            replace(problem, boundary_id="same-physics-new-layer"),
            config, second_state)
        uncached = self.backend.solve_rich_boundary(
            problem,
            replace(config, fitness_cache=False),
            second_state,
        )
        self.assertEqual(first.winner, repeated.winner)
        self.assertEqual(first.gate_option_indices,
                         repeated.gate_option_indices)
        self.assertEqual(first.return_assignments,
                         repeated.return_assignments)
        self.assertEqual(first.reseat_assignments,
                         repeated.reseat_assignments)
        self.assertEqual(
            first.participant_parking_assignments,
            repeated.participant_parking_assignments)
        self.assertEqual(second_state, repeated.rng_state)
        self.assertEqual(1, repeated.operator_stats[
            "exact_result_cache_hits"])
        self.assertEqual(uncached.winner, repeated.winner)
        self.assertEqual(uncached.rng_state, repeated.rng_state)


if __name__ == "__main__":
    unittest.main()
