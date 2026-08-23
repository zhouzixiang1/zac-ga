"""ABI3 one-call residency search differential and fail-closed tests."""
from __future__ import annotations

import random
import unittest

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


if __name__ == "__main__":
    unittest.main()
