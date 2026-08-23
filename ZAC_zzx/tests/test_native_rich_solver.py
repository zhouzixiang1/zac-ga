"""ABI3 one-call residency search differential and fail-closed tests."""
from __future__ import annotations

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
    evaluate_decay_forecast,
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


@unittest.skipUnless(native_available(), "ABI3 native extension is not installed")
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
        reference_problem = BoundaryProblem(self.arch, (candidate,))
        reference = ReferenceResidentBackend().solve_boundary(
            reference_problem,
            BoundaryConfig(enforce_single_leg_ghost=False)).winner
        self.assertAlmostEqual(reference.negative_log_fidelity,
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
                self.assertEqual(reference.return_assignment_rank,
                                 native.return_assignment_rank)
                self.assertAlmostEqual(
                    reference.search_negative_log_fidelity,
                    native.search_negative_log_fidelity, delta=1e-12)

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
            self.assertEqual(first.rng_state, other.rng_state)
        self.assertGreater(first.operator_stats["crossovers"], 0)
        self.assertGreater(first.operator_stats["local_polish_evaluations"], 0)
        self.assertLessEqual(first.unique_evaluations,
                             config.max_unique_evaluations)


if __name__ == "__main__":
    unittest.main()
