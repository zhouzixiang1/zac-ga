from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation import normalize_zair, score_trace  # noqa: E402
from zac.ds.architecture import Architecture  # noqa: E402
from zzx.exact_current_reference import (  # noqa: E402
    ExactCurrentReferenceScheduler,
)
from zzx.boundary_problem import (  # noqa: E402
    ArchitectureSnapshot,
    BoundaryConfig,
    BoundaryProblem,
    CandidatePlan,
    Ghost,
    Leg,
    MovementPhase,
    Point,
    RichGateOption,
    RichH0Problem,
    RichReturnOption,
    RichSearchConfig,
)
from zzx.native_backend import (  # noqa: E402
    NativeResidentBackend,
    native_available,
)
from zzx.reference_backend import (  # noqa: E402
    ReferenceResidentBackend,
    _production_replay_phase_batches,
    color_phase,
    replay_phase_batches,
    solve_rich_exact_reference,
)
from zzx.zac_zzx import ZAC_zzx  # noqa: E402
from zzx.zcost import phase_batches as production_phase_batches  # noqa: E402


def _architecture():
    spec = json.loads(
        (ROOT / "hardware_spec" / "toy_architecture.json").read_text())
    spec["operation_duration"] = {
        "rydberg": 0.36,
        "1qGate": 52.0,
        "atom_transfer": 15.0,
    }
    architecture = Architecture(spec)
    architecture.preprocessing()
    return architecture, spec


class TestExactCurrentReferenceScheduler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.architecture, cls.spec = _architecture()

    def test_prefix_candidate_commit_and_resume_use_current_layer_one_q(self):
        homes = [(0, 0, q) for q in range(6)]
        g0 = list(homes)
        g0[0], g0[1] = (1, 0, 0), (2, 0, 0)
        b1 = list(g0)
        b1[0] = homes[0]
        g1 = list(b1)
        g1[2], g1[3] = (1, 0, 2), (2, 0, 2)

        reference = ExactCurrentReferenceScheduler(
            self.architecture, homes,
            leading_one_qubit_gates=(("u3", 5),))
        prefix = reference.prepare_source_prefix(
            0, g0, ((0, 1),), (("u1", 0), ("u2", 4)))
        candidate = reference.evaluate_candidate(
            prefix=prefix,
            boundary_mapping=b1,
            source_gates=((0, 1),),
            source_one_qubit_gates=(("u1", 0), ("u2", 4)),
            target_gate_mapping=g1,
            target_gates=((2, 3),),
            target_one_qubit_gates=(("u3", 2),),
        )
        self.assertEqual(candidate.source_layer, 0)
        self.assertGreater(candidate.source_instruction_count, 0)
        self.assertGreater(candidate.target_instruction_count, 0)
        self.assertNotEqual(
            candidate.scheduler_after.one_qubit_end_us,
            prefix.scheduler.one_qubit_end_us)

        # The candidate snapshot is itself a complete source+target-no-back
        # native trace.  Its absolute idle therefore agrees with the independent
        # canonical scorer, including all three current-layer 1Q gates.
        fork = reference._fork()
        source = fork.route_layer(
            0, homes, g0, b1, ((0, 1),), (0,),
            (("u1", 0), ("u2", 4)))
        target = fork.route_layer(
            1, b1, g1, g1, ((2, 3),), (0,), (("u3", 2),))
        instructions = list(deepcopy(fork.initial_instructions))
        instructions.extend(deepcopy(source.instructions))
        instructions.extend(deepcopy(target.instructions))
        events = tuple(normalize_zair(
            {"instructions": instructions}, architecture=self.spec))
        score = score_trace(events, n_qubits=len(homes))
        for actual, expected in zip(
                candidate.idle_after_us, score.idle_time_us):
            self.assertAlmostEqual(actual, expected, places=7)

        reference.commit_source(
            0, g0, b1, ((0, 1),), (("u1", 0), ("u2", 4)))
        restored = ExactCurrentReferenceScheduler.from_state(
            self.architecture, homes, deepcopy(reference.state_dict()))
        self.assertEqual(restored.state_dict(), reference.state_dict())
        for value in (reference, restored):
            value.commit_source(
                1, g1, g1, ((2, 3),), (("u3", 2),))
        self.assertEqual(restored.state_dict(), reference.state_dict())

    @unittest.skipUnless(native_available(), "ABI7 native extension unavailable")
    def test_native_compact_scheduler_matches_reference_and_emitted_router(self):
        locations = tuple(sorted(
            (int(array), row, column)
            for array, slm in self.architecture.dict_SLM.items()
            for row in range(slm.n_r)
            for column in range(slm.n_c)))
        site_id = {location: index for index, location in enumerate(locations)}
        coordinates = tuple(Point(
            *self.architecture.exact_SLM_location_tuple(location))
            for location in locations)
        storage = tuple(site_id[location] for location in locations
                        if location[0] in set(self.architecture.storage_zone))
        entangling_pairs = tuple(
            (site_id[(1, row, column)], site_id[(2, row, column)])
            for row in range(self.architecture.dict_SLM[1].n_r)
            for column in range(self.architecture.dict_SLM[1].n_c))
        architecture = ArchitectureSnapshot(
            6, coordinates, storage, entangling_pairs)

        homes = [(0, 0, q) for q in range(6)]
        g0 = list(homes)
        g0[0], g0[1] = (1, 0, 0), (2, 0, 0)
        b1 = list(g0)
        b1[0] = homes[0]
        g1 = list(b1)
        g1[2], g1[3] = (1, 0, 2), (2, 0, 2)
        reference = ExactCurrentReferenceScheduler(
            self.architecture, homes,
            leading_one_qubit_gates=(("u3", 5),))
        prefix = reference.prepare_source_prefix(
            0, g0, ((0, 1),), (("u1", 0), ("u2", 4)))
        site_dependency = tuple(
            (site_id[location], value) for location, value in
            prefix.site_dependency_activation_finish_us)
        problem = RichH0Problem(
            architecture=architecture,
            current_points=(),
            current_site_ids=tuple(site_id[location] for location in g0),
            participants=(2, 3),
            gate_domains=((RichGateOption(
                site_id=site_id[(1, 0, 2)], q1=2, q2=3,
                target1=None, target2=None,
                target1_site_id=site_id[(1, 0, 2)],
                target2_site_id=site_id[(2, 0, 2)]),),),
            static_ghosts=(), eligible=(0,), min_returns=0,
            eviction_order_indices=(0,), forced_return_mask=(True,),
            return_domains=((RichReturnOption(
                site_id[homes[0]], None, 0.0, homes[0]),),),
            matched_gate_genes=(0,), decision_policy="optimize",
            boundary_id="toy-exact-current", selected_horizon=0,
            prior_idle_time_us=prefix.scheduler.idle_time_us,
            scheduler_trace_end_us=prefix.scheduler.trace_end_us,
            scheduler_active_union_us=prefix.scheduler.active_union_us,
            scheduler_aod_end_us=prefix.scheduler.aod_end_us,
            scheduler_one_qubit_end_us=prefix.scheduler.one_qubit_end_us,
            scheduler_rydberg_end_us=prefix.scheduler.rydberg_end_us,
            scheduler_qubit_dependency_end_us=
                prefix.qubit_dependency_end_us,
            scheduler_back_dependency_end_us=prefix.back_dependency_end_us,
            scheduler_site_dependency_site_ids=tuple(
                row[0] for row in site_dependency),
            scheduler_site_dependency_activation_finish_us=tuple(
                row[1] for row in site_dependency),
            target_one_qubit_atoms=(2,),
        )
        with self.assertRaisesRegex(ValueError, "frozen physical model"):
            replace(
                problem,
                scheduler_one_qubit_common_us=19.0,
                enforce_frozen_physical_model=True,
            )
        config = RichSearchConfig(
            operator_profile="exact", max_horizon=0,
            direct_enumeration_limit=512, max_unique_evaluations=512,
            exact_coloring_threshold=24)
        rng_state = random.Random(0).getstate()
        python_result = solve_rich_exact_reference(
            problem, config, rng_state)
        native_result = NativeResidentBackend(
            architecture).solve_rich_boundary(problem, config, rng_state)
        production = reference.evaluate_candidate(
            prefix=prefix, boundary_mapping=b1,
            source_gates=((0, 1),),
            source_one_qubit_gates=(("u1", 0), ("u2", 4)),
            target_gate_mapping=g1, target_gates=((2, 3),),
            target_one_qubit_gates=(("u3", 2),))

        self.assertEqual(native_result.winner, python_result.winner)
        self.assertEqual(native_result.return_assignments,
                         python_result.return_assignments)
        self.assertEqual(
            native_result.participant_parking_assignments,
            python_result.participant_parking_assignments)
        self.assertEqual(production.source_back_batches, ((0,),))
        self.assertEqual(production.target_out_batches, ((2, 3),))
        self.assertEqual(native_result.winner.move_batches, 2)
        self.assertAlmostEqual(
            native_result.winner.move_time_us,
            production.source_back_time_us + production.target_out_time_us,
            places=9)
        reconstructed = tuple(
            before + delta for before, delta in zip(
                problem.prior_idle_time_us,
                native_result.winner.candidate_idle_time_us))
        for actual, expected in zip(reconstructed, production.idle_after_us):
            self.assertAlmostEqual(actual, expected, places=9)
        self.assertAlmostEqual(
            native_result.winner.coherence_nll,
            production.coherence_negative_log_fidelity, places=12)

        # Non-formal fixtures may carry their architecture's actual scheduler
        # constants.  C++ must consume the wire values rather than silently
        # reverting to the frozen formal 52 us/0 us 1Q model.
        diagnostic_problem = replace(
            problem,
            scheduler_one_qubit_duration_us=0.625,
            scheduler_one_qubit_common_us=19.0,
        )
        diagnostic_python = solve_rich_exact_reference(
            diagnostic_problem, config, rng_state)
        diagnostic_native = NativeResidentBackend(
            architecture).solve_rich_boundary(
                diagnostic_problem, config, rng_state)
        self.assertEqual(diagnostic_native.winner, diagnostic_python.winner)
        self.assertEqual(
            diagnostic_native.gate_option_indices,
            diagnostic_python.gate_option_indices)

        actual = reference._fork()
        actual.route_layer(
            0, homes, g0, b1, ((0, 1),), (0,),
            (("u1", 0), ("u2", 4)))
        actual.route_layer(
            1, b1, g1, g1, ((2, 3),), (0,), (("u3", 2),))
        self.assertEqual(actual.scheduler.snapshot(), production.scheduler_after)
        self.assertEqual(actual.scheduler.timing_sha256,
                         production.scheduler_after.timing_sha256)

    @unittest.skipUnless(native_available(), "ABI7 native extension unavailable")
    def test_native_splits_endpoint_safe_parking_collision_like_production(self):
        source = (Point(59.0, 294.0), Point(60.0, 307.0))
        target = (Point(49.0, 317.0), Point(61.0, 337.0))
        architecture = ArchitectureSnapshot(2, source + target)
        phase = MovementPhase(
            tuple(Leg.between(begin, end)
                  for begin, end in zip(source, target)),
            tuple(Ghost(atom, point)
                  for atom, point in enumerate(source)),
            (0, 1),
        )
        candidate = CandidatePlan((0,), (phase,), 0)
        problem = BoundaryProblem(architecture, (candidate,))
        config = BoundaryConfig(
            exact_coloring_threshold=24,
            production_parking_replay=True)
        python_result = ReferenceResidentBackend().evaluate_many(
            problem, config=config)[0]
        native_result = NativeResidentBackend(architecture).evaluate_many(
            problem, config=config)[0]
        self.assertEqual(python_result.move_batches, 2)
        self.assertEqual(native_result.move_batches, 2)
        self.assertEqual(python_result.phase_batches, (((0,), (1,)),))
        self.assertEqual(native_result.phase_batches, python_result.phase_batches)
        self.assertAlmostEqual(native_result.move_time_us,
                               python_result.move_time_us, places=12)

    def _production_owner_batches(self, mapping_from, mapping_to, movers):
        """Return the exact owner order emitted by the production resident router."""
        compiler = ZAC_zzx()
        compiler.architecture = self.architecture
        compiler.routing_strategy = "coloring"
        compiler.zzx_exact_threshold = 24
        compiler.zzx_node_budget = 200_000
        compiler._zzx_waypoint_plan = {}
        compiler.zzx_ghost_splits = 0
        compiler.window_size = 1000
        ordered = compiler._sorted_by_distance(
            list(movers), mapping_from, mapping_to)
        _chi, batches, _method = compiler._coloring_batches(
            ordered, mapping_from, mapping_to)
        self.assertFalse(compiler._zzx_waypoint_plan)
        return tuple(tuple(ordered[index] for index in batch)
                     for batch in batches)

    def _compact_phase(self, mapping_from, mapping_to, movers):
        points = tuple(Point(
            *self.architecture.exact_SLM_location_tuple(location))
            for location in mapping_from)
        return MovementPhase(
            tuple(Leg.between(
                points[atom],
                Point(*self.architecture.exact_SLM_location_tuple(
                    mapping_to[atom])))
                for atom in movers),
            tuple(Ghost(atom, point) for atom, point in enumerate(points)),
            tuple(movers),
        )

    def _native_owner_batches(self, phase, n_atoms):
        architecture = ArchitectureSnapshot(n_atoms)
        candidate = CandidatePlan((0,), (phase,), 0)
        result = NativeResidentBackend(architecture).evaluate_many(
            BoundaryProblem(architecture, (candidate,)),
            config=BoundaryConfig(
                exact_coloring_threshold=24,
                production_parking_replay=True),
        )[0]
        self.assertTrue(result.feasible, result.error)
        return tuple(tuple(phase.owners[index] for index in batch)
                     for batch in result.phase_batches[0])

    def test_compact_replay_defers_source_blocked_singleton_like_production(self):
        """A mover's source is a precedence edge, not a permanent ghost failure."""
        mapping_from = (
            (0, 0, 5), (0, 3, 6), (1, 2, 2), (2, 0, 4), (0, 9, 5),
            (2, 1, 5), (0, 1, 5), (0, 7, 9), (1, 2, 4), (0, 5, 8),
        )
        mapping_to = (
            (0, 2, 7), (0, 0, 5), (1, 2, 2), (2, 0, 4), (0, 9, 5),
            (2, 1, 5), (0, 1, 5), (0, 7, 9), (1, 2, 4), (0, 5, 8),
        )
        movers = (0, 1)
        expected = self._production_owner_batches(
            mapping_from, mapping_to, movers)
        self.assertEqual(expected, ((0,), (1,)))

        phase = self._compact_phase(mapping_from, mapping_to, movers)
        actual = _production_replay_phase_batches(phase, 24)
        self.assertEqual(tuple(batch.owners for batch in actual), expected)
        if native_available():
            self.assertEqual(
                self._native_owner_batches(phase, len(mapping_from)), expected)

    def test_compact_replay_preserves_production_deferred_order(self):
        """Deferred contributors retain the production distance-order identity."""
        mapping_from = (
            (0, 3, 4), (1, 1, 4), (2, 2, 5), (0, 5, 6),
            (1, 2, 5), (0, 1, 8), (0, 4, 2), (2, 2, 1),
            (1, 1, 2), (2, 0, 2), (0, 2, 7), (1, 0, 4),
        )
        mapping_to = (
            (0, 6, 2), (0, 9, 3), (0, 9, 2), (0, 5, 6),
            (1, 2, 5), (0, 1, 8), (0, 4, 2), (2, 2, 1),
            (1, 1, 2), (2, 0, 2), (0, 2, 7), (1, 0, 4),
        )
        movers = (0, 1, 2)
        expected = self._production_owner_batches(
            mapping_from, mapping_to, movers)
        self.assertEqual(expected, ((2,), (1,), (0,)))

        phase = self._compact_phase(mapping_from, mapping_to, movers)
        actual = _production_replay_phase_batches(phase, 24)
        self.assertEqual(tuple(batch.owners for batch in actual), expected)
        if native_available():
            self.assertEqual(
                self._native_owner_batches(phase, len(mapping_from)), expected)

    def test_compact_replay_falls_back_to_fresh_strict_precedence_and_splits(self):
        """A dead heuristic prefix is discarded before strict expanded replay."""
        sources = (
            Point(2.0, 3.0), Point(1.0, 0.0), Point(2.0, 6.0),
        )
        targets = (
            Point(6.0, 3.0), Point(3.0, 0.0), Point(5.0, 3.0),
        )
        phase = MovementPhase(
            tuple(Leg.between(source, target)
                  for source, target in zip(sources, targets)),
            tuple(Ghost(atom, source)
                  for atom, source in enumerate(sources)),
            (0, 1, 2),
        )
        strict_batches = []

        def capture_strict(*args, **kwargs):
            result = replay_phase_batches(*args, **kwargs)
            strict_batches.append(result)
            return result

        with patch(
                "zzx.reference_backend.replay_phase_batches",
                side_effect=capture_strict) as strict_replay:
            actual = _production_replay_phase_batches(phase, 24)

        self.assertEqual(strict_replay.call_count, 1)
        # The strict endpoint order contains the expanded-unsafe pair (q2,q1).
        self.assertEqual(strict_batches, [((1,), (0, 2))])
        # Fresh physical replay keeps q0 first, then splits that pair in its
        # deterministic canonical member order.  Original indices are public.
        self.assertEqual(
            tuple(batch.original_members for batch in actual),
            ((0,), (2,), (1,)),
        )
        self.assertEqual(
            tuple(batch.owners for batch in actual),
            ((0,), (2,), (1,)),
        )

    def test_exact_coloring_matches_production_zcost_on_small_graph(self):
        """The compact exact-color path must use the router's batch partition."""
        raw = (
            Leg.between(Point(2.0, 6.0), Point(0.0, 2.0)),
            Leg.between(Point(6.0, 3.0), Point(5.0, 0.0)),
            Leg.between(Point(0.0, 6.0), Point(2.0, 1.0)),
        )
        # ``_route_resident`` canonicalizes movers by descending distance before
        # calling zcost.  The native compact replay owns the same canonicalization.
        legs = tuple(sorted(raw, key=lambda leg: leg.distance_um, reverse=True))
        phase = MovementPhase(legs, owners=(0, 1, 2))
        compact = color_phase(phase, 24)
        wire = tuple((leg.distance_um, leg.source.x, leg.source.y,
                      leg.target.x, leg.target.y) for leg in legs)
        _chi, production, method = production_phase_batches(
            wire, owners=phase.owners, exact_threshold=24)
        self.assertEqual(method, "exact")
        expected = tuple(tuple(batch) for batch in production)
        self.assertEqual(compact, expected)
        if native_available():
            self.assertEqual(self._native_owner_batches(phase, 3), expected)


if __name__ == "__main__":
    unittest.main()
