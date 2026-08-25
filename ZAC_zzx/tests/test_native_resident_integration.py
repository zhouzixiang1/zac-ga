"""End-to-end native checks at the real ResidentPlacer boundary.

Python/C++ semantic differential truth lives in ``test_native_rich_solver``
where both implementations consume the exact same rich boundary DTO.  This
module deliberately checks the separate integration contract: a full native
run is byte-deterministic and never changes its selected candidate through the
post-selection ghost safety net.
"""
from __future__ import annotations

import json
import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zac.ds.architecture import Architecture  # noqa: E402
from zzx.algorithm_v2 import (decay_lookahead_spec,
                              maximum_lookahead_horizon)  # noqa: E402
from zzx.boundary_problem import (  # noqa: E402
    ArchitectureSnapshot as BoundaryArchitectureSnapshot,
    Point as BoundaryPoint,
    RichGateOption,
)
from zzx.native_backend import native_available  # noqa: E402
from zzx.resident import ResidentRegistry  # noqa: E402
from zzx import zplacer as zplacer_module  # noqa: E402
from zzx.zplacer import ResidentPlacer  # noqa: E402


TIMING_KEYS = {
    "horizon_selection_ns", "search_kernel_ns", "marshal_ns",
    "backend_search_kernel_ns", "fitness_ns", "backend_selection_ns",
    "backend_calls", "backend_candidates", "cache",
}
NATIVE_WHEEL_SHA256 = (
    "1e991c0201b7bdb657257e47783d1d5b44e28b5d50b0ab323ecfaa47585f033b")


def architecture(*, frozen_physics=False):
    value = json.loads(
        (ROOT / "hardware_spec/toy_architecture.json").read_text())
    if frozen_physics:
        # This geometry-only toy predates the formal fidelity contract and
        # therefore omits ``operation_duration``.  A registered native run is
        # a formal run, so its scheduler fixture must carry the same immutable
        # ZAC model as ``full_architecture.json`` instead of inheriting
        # Architecture's historical 0.625-us diagnostic 1Q default.
        value["operation_duration"] = {
            "rydberg": 0.36,
            "1qGate": 52.0,
            "atom_transfer": 15.0,
        }
    result = Architecture(value)
    result.preprocessing()
    return result


def run(schedule, *, backend, horizon, seed, ablation_policy="optimize",
        ablation_fitness_mode="phase"):
    n_qubits = 1 + max(
        q for layer in schedule for gate in layer for q in gate)
    initial = [(0, q, 0) for q in range(n_qubits)]
    placer = ResidentPlacer(
        initial,
        seed=seed,
        experiment_schema=2,
        method_id=("ours_nl" if maximum_lookahead_horizon(horizon) == 0
                   else "ours_lk"),
        objective="physical_log_fidelity",
        lookahead_horizon=horizon,
        alpha_lookahead=0.1,
        engine="ga",
        fitness_cache=True,
        population_size=6,
        iterations=2,
        neighbors_per_solution=2,
        neighbor_sample_size=8,
        backend=backend,
        formal_native=backend == "native",
        native_wheel_sha256=(NATIVE_WHEEL_SHA256
                             if backend == "native" else ""),
        operator_profile="exact",
        ablation_policy=ablation_policy,
        ablation_fitness_mode=ablation_fitness_mode,
    )
    placer.run(
        architecture(frozen_physics=backend == "native"),
        [initial], schedule, True,
        [set() for _ in schedule])
    return placer


def stable_log(row):
    result = {
        key: value for key, value in row.items()
        if key not in TIMING_KEYS | {"backend", "score", "physical"}
    }

    def stable(value):
        if isinstance(value, float):
            return round(value, 12)
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, tuple):
            return tuple(stable(item) for item in value)
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()}
        return value

    return stable(result)


class TestGateCacheFastPath(unittest.TestCase):
    """The rich solver owns decode; legacy fitness still owns its leg cache."""

    SCHEDULE = [[[0, 1]], [[0, 1]]]

    def test_reference_rich_path_never_enters_legacy_decode(self):
        # Rich paths deliberately leave the legacy Python gate cache empty.
        # Any accidental call to decode therefore fails closed in production;
        # successful compilation is the regression assertion.
        placer = run(
            self.SCHEDULE, backend="reference",
            horizon=decay_lookahead_spec(0), seed=11)
        self.assertEqual(len(placer.decision_log), len(self.SCHEDULE))
        self.assertEqual(placer.decision_log[0]["backend_calls"], 1)
        self.assertEqual(
            placer.decision_log[0]["rich_search"]["native_future_layers"], 0)

    def test_formal_serial_chain_exposes_joint_participant_cycle_gene(self):
        schedule = [[[0, 1]], [[1, 2]], [[2, 3]]]
        for horizon in (
                decay_lookahead_spec(0), decay_lookahead_spec(8)):
            with self.subTest(
                    horizon=maximum_lookahead_horizon(horizon)):
                placer = run(
                    schedule, backend="reference", horizon=horizon, seed=5)
                first = placer.decision_log[0]
                # q0 is the ordinary dead resident; reused participant q1 is
                # the additional back-to-storage then out-to-gate cycle bit.
                self.assertEqual(first["eligible_decisions"], 2)
                self.assertEqual(first["adjacent_cycle_candidates"], 1)
                self.assertEqual(
                    [entry["q"] for entry in first["adjacent_cycle_search"]
                     if entry.get("native_joint")],
                    [1],
                )

    @unittest.skipUnless(
        native_available(), "zac_native_core wheel is not installed")
    def test_native_rich_path_never_enters_legacy_decode(self):
        placer = run(
            self.SCHEDULE, backend="native",
            horizon=decay_lookahead_spec(8), seed=11)
        self.assertEqual(len(placer.decision_log), len(self.SCHEDULE))
        self.assertEqual(placer.decision_log[0]["backend_calls"], 1)
        self.assertEqual(placer.decision_log[0].get("ghost_fix", 0), 0)

    def test_legacy_and_lumped_paths_still_call_python_decode(self):
        original = zplacer_module.new_conflicts
        for label, horizon, fitness_mode in (
                ("legacy", 0, "phase"),
                ("decay_lumped", decay_lookahead_spec(0), "lumped_greedy")):
            calls = 0

            def tracked_new_conflicts(*args, **kwargs):
                nonlocal calls
                calls += 1
                return original(*args, **kwargs)

            with self.subTest(label=label), mock.patch.object(
                    zplacer_module, "new_conflicts",
                    side_effect=tracked_new_conflicts):
                placer = run(
                    self.SCHEDULE, backend="reference", horizon=horizon,
                    seed=11, ablation_fitness_mode=fitness_mode)
                self.assertEqual(len(placer.decision_log), len(self.SCHEDULE))
                self.assertGreater(calls, 0)


class TestResidentConstructionCaches(unittest.TestCase):
    """Construction caches may change allocation count, never semantics."""

    INITIAL = [(0, 0, 0), (0, 1, 0)]
    # Two equivalent nonterminal boundaries exercise cross-boundary reuse.  The
    # transition-winner LRU is disabled so the reference solver evaluates both
    # DTOs instead of taking its deliberately native-only approximate fast path.
    SCHEDULE = [[[0, 1]], [[0, 1]], [[0, 1]]]

    @classmethod
    def make_placer(cls, *, cache_limit=131_072):
        placer = ResidentPlacer(
            cls.INITIAL,
            seed=11,
            experiment_schema=2,
            method_id="ours_nl",
            objective="physical_log_fidelity",
            lookahead_horizon=decay_lookahead_spec(0),
            alpha_lookahead=0.1,
            engine="ga",
            fitness_cache=True,
            population_size=6,
            iterations=2,
            neighbors_per_solution=2,
            neighbor_sample_size=8,
            backend="reference",
            operator_profile="exact",
        )
        placer.transition_cache_limit = 0
        placer._rich_gate_option_cache_limit = cache_limit
        return placer

    def execute(self, *, cache_limit=131_072):
        placer = self.make_placer(cache_limit=cache_limit)
        real_option = zplacer_module.RichGateOption
        with mock.patch.object(
                zplacer_module, "RichGateOption",
                wraps=real_option) as constructor:
            placer.run(
                architecture(), [self.INITIAL], self.SCHEDULE, True,
                [set() for _ in self.SCHEDULE])
        return placer, constructor.call_count

    def test_run_initialization_clears_both_construction_caches(self):
        placer, _calls = self.execute()
        old_sites = ((999, 999, 999),)
        placer._zone_site_cache = old_sites
        placer._rich_gate_option_cache[("sentinel",)] = object()

        placer._initialize_run_state(
            architecture(), [self.INITIAL], self.SCHEDULE)

        self.assertNotIn(("sentinel",), placer._rich_gate_option_cache)
        self.assertEqual(placer._rich_gate_option_cache, {})
        self.assertIsNot(placer._zone_site_cache, old_sites)
        sites = placer._all_zone_sites()
        self.assertIs(sites, placer._zone_site_cache)
        self.assertIs(sites, placer._all_zone_sites())
        self.assertIsInstance(sites, tuple)
        self.assertTrue(all(isinstance(site, tuple) for site in sites))
        with self.assertRaises(TypeError):
            sites[0] = (999, 999, 999)
        with self.assertRaises(TypeError):
            sites[0][0] = 999

    def test_rich_option_reuse_and_bounded_clear_preserve_result(self):
        cached, cached_constructions = self.execute()
        clearing, clearing_constructions = self.execute(cache_limit=1)

        # The repeated boundary constructs each unique DTO once with the normal
        # cache.  A one-entry bound forces reconstruction on the next boundary,
        # proving that the normal path really reused the same keyed objects.
        self.assertEqual(
            cached_constructions, len(cached._rich_gate_option_cache))
        self.assertGreater(clearing_constructions, cached_constructions)
        self.assertLessEqual(len(clearing._rich_gate_option_cache), 1)

        self.assertEqual(cached.mapping, clearing.mapping)
        self.assertEqual(cached.registry.zone_seat,
                         clearing.registry.zone_seat)
        self.assertEqual(cached.registry.storage_site,
                         clearing.registry.storage_site)
        self.assertEqual(
            [stable_log(row) for row in cached.decision_log],
            [stable_log(row) for row in clearing.decision_log],
        )


class TestIndexedNativeGateDomain(unittest.TestCase):
    """The native one-pass domain is an exact view of the legacy geometry."""

    INITIAL = [(0, q, 0) for q in range(8)]

    def make_placer(self):
        arch = architecture()
        placer = ResidentPlacer(self.INITIAL, backend="native")
        placer.architecture = arch
        placer.mapping = [list(self.INITIAL)]
        placer.registry = ResidentRegistry(
            arch, self.INITIAL, placer.theta_capacity)
        placer._prepare_boundary_architecture()
        return placer

    def test_formal_complete_domain_allows_other_participant_source(self):
        placer = self.make_placer()
        all_sites = placer._all_zone_sites()
        moving_participant_seat = all_sites[0]
        stationary_nonparticipant_seat = all_sites[1]
        placer.registry.enter_zone(2, moving_participant_seat)
        placer.registry.enter_zone(4, stationary_nonparticipant_seat)
        participants = {0, 1, 2, 3}
        blocked = zplacer_module._formal_rich_blocked_seats(
            placer.registry.zone_seat, participants,
            movable_before_out=set())
        self.assertNotIn(moving_participant_seat, blocked)
        self.assertIn(stationary_nonparticipant_seat, blocked)

        domain = placer._build_indexed_rich_gate_domain(0, 1)
        complete = placer._indexed_rich_complete_opts(
            domain, blocked, canonical=True)
        selected_rows = [domain.by_site[tuple(option[0])]
                         for option in complete]
        self.assertTrue(any(
            moving_participant_seat in row.seats
            for row in selected_rows))
        self.assertTrue(all(
            stationary_nonparticipant_seat not in row.seats
            for row in selected_rows))

    def test_native_phase_batch_indices_use_cpp_geometry_owner_order(self):
        coordinates = tuple(BoundaryPoint(float(index), 0.0)
                            for index in range(9))
        snapshot = BoundaryArchitectureSnapshot(
            n_atoms=5, site_coordinates=coordinates)
        gate_zero_unused = RichGateOption(
            site_id=8, q1=0, q2=2, target1=None, target2=None,
            target1_site_id=8, target2_site_id=7)
        gate_zero_selected = RichGateOption(
            site_id=0, q1=0, q2=2, target1=None, target2=None,
            target1_site_id=0, target2_site_id=7)
        gate_one_selected = RichGateOption(
            site_id=8, q1=1, q2=4, target1=None, target2=None,
            target1_site_id=8, target2_site_id=4)
        problem = SimpleNamespace(
            architecture=snapshot,
            current_site_ids=(0, 1, 2, 3, 4),
            current_points=(),
            gate_domains=(
                (gate_zero_unused, gate_zero_selected),
                (gate_one_selected,),
            ),
        )
        result = SimpleNamespace(
            # Atom 4's zero-length RETURN is omitted by C++, then RETURN atom
            # 3 precedes RESEAT atom 1 even if Python's decisions dict was
            # populated in the interleaved eligible order (1, 3, 4).
            return_assignments=((4, 4), (3, 5)),
            reseat_assignments=((1, 6),),
            gate_option_indices=(1, 0),
        )
        back_owners, out_owners = \
            zplacer_module._native_rich_phase_owner_orders(problem, result)
        self.assertEqual(back_owners, (3, 1))
        self.assertEqual(out_owners, (2, 1))
        self.assertEqual(
            zplacer_module._phase_batches_by_owner(
                ((0,), (1,)), back_owners, phase="source-back"),
            ((3,), (1,)))
        self.assertEqual(
            zplacer_module._phase_batches_by_owner(
                ((1,), (0,)), out_owners, phase="target-out"),
            ((1,), (2,)))

    def test_native_result_projects_registered_oriented_endpoints(self):
        locations = tuple((0, 0, index) for index in range(4))
        problem = SimpleNamespace(gate_domains=((RichGateOption(
            site_id=0, q1=5, q2=3, target1=None, target2=None,
            # Deliberately reverse the logical endpoints relative to the
            # interaction-site convention.  This is the source-seat handoff
            # orientation that must survive native selection unchanged.
            target1_site_id=2, target2_site_id=0),),))
        result = SimpleNamespace(gate_option_indices=(0,))
        self.assertEqual(
            zplacer_module._rich_result_placements(
                problem, result, locations),
            [{
                "gate": (5, 3),
                "site": locations[0],
                "seats": (locations[2], locations[0]),
            }],
        )

    def test_random_domains_keep_options_ties_ghosts_and_dto_exact(self):
        rng = random.Random(20260824)
        placer = self.make_placer()
        all_sites = placer._all_zone_sites()
        for case in range(24):
            placer.registry = ResidentRegistry(
                placer.architecture, self.INITIAL, placer.theta_capacity)
            resident_atoms = rng.sample(range(8), rng.randrange(4))
            resident_seats = rng.sample(
                [site for left in all_sites
                 for site in (left, (left[0] + 1, left[1], left[2]))],
                len(resident_atoms),
            )
            for q, seat in zip(resident_atoms, resident_seats):
                placer.registry.enter_zone(q, seat)
            q1, q2 = rng.sample(range(8), 2)

            real_pair = placer._pair_seats
            with mock.patch.object(
                    placer, "_pair_seats", wraps=real_pair) as pair_calls, \
                    mock.patch.object(
                        placer, "_site_weight",
                        wraps=placer._site_weight) as weight_calls:
                indexed = placer._build_indexed_rich_gate_domain(q1, q2)
            with self.subTest(case=case, check="one_pass"):
                self.assertEqual(pair_calls.call_count, len(all_sites))
                self.assertEqual(weight_calls.call_count, 0)

            subset = set(rng.sample(
                list(all_sites), rng.randint(1, len(all_sites))))
            blocked = set(rng.sample(
                [site for left in all_sites
                 for site in (left, (left[0] + 1, left[1], left[2]))],
                rng.randrange(5),
            ))
            expected_local = placer._build_opts(
                subset, q1, q2, blocked)
            actual_local = placer._indexed_rich_local_opts(
                indexed, subset, blocked)
            expected_full = placer._build_opts(
                set(all_sites), q1, q2, blocked)
            actual_full = placer._indexed_rich_complete_opts(
                indexed, blocked)
            expected_canonical = sorted(
                expected_full, key=lambda row: (row[1], row[0]))
            actual_canonical = placer._indexed_rich_complete_opts(
                indexed, blocked, canonical=True)
            with self.subTest(case=case, check="ordering"):
                self.assertEqual(actual_local, expected_local)
                self.assertEqual(actual_full, expected_full)
                self.assertEqual(actual_canonical, expected_canonical)

            ghosts = [
                (q, *placer.architecture.exact_SLM_location_tuple(
                    placer.registry.current_pos(q)))
                for q in range(8) if q not in (q1, q2)
            ]
            expected_filtered = placer._filter_menu_ghosts(
                expected_canonical, q1, q2, ghosts, 2)
            actual_filtered = placer._filter_menu_ghosts(
                actual_canonical, q1, q2, ghosts, 2,
                indexed_domain=indexed)
            with self.subTest(case=case, check="ghost_filter"):
                self.assertEqual(actual_filtered, expected_filtered)

            for site, row in indexed.by_site.items():
                expected_seats = placer._pair_seats(q1, q2, site)
                with self.subTest(case=case, site=site, check="dto"):
                    self.assertEqual(row.seats, expected_seats)
                    self.assertEqual(
                        row.rich_option.site_id,
                        placer.boundary_site_id[site])
                    self.assertEqual(
                        row.rich_option.target1_site_id,
                        placer.boundary_site_id[expected_seats[0]])
                    self.assertEqual(
                        row.rich_option.target2_site_id,
                        placer.boundary_site_id[expected_seats[1]])

    @unittest.skipUnless(
        native_available(), "zac_native_core wheel is not installed")
    def test_real_toy_native_run_matches_legacy_domain_builder(self):
        schedule = [
            [[0, 1], [2, 3]],
            [[1, 4], [3, 5]],
            [[0, 4]],
            [[2, 5]],
            [[1, 5]],
        ]
        for horizon in (
                decay_lookahead_spec(0), decay_lookahead_spec(8)):
            with self.subTest(horizon=maximum_lookahead_horizon(horizon)):
                with mock.patch.object(
                        ResidentPlacer,
                        "_use_indexed_native_gate_domains",
                        return_value=False):
                    legacy = run(
                        schedule, backend="native", horizon=horizon,
                        seed=17)
                indexed = run(
                    schedule, backend="native", horizon=horizon,
                    seed=17)
                self.assertEqual(indexed.mapping, legacy.mapping)
                self.assertEqual(indexed.registry.zone_seat,
                                 legacy.registry.zone_seat)
                self.assertEqual(indexed.registry.storage_site,
                                 legacy.registry.storage_site)
                self.assertEqual(indexed.rng.getstate(), legacy.rng.getstate())
                self.assertEqual(
                    [stable_log(row) for row in indexed.decision_log],
                    [stable_log(row) for row in legacy.decision_log],
                )


@unittest.skipUnless(native_available(), "zac_native_core wheel is not installed")
class TestNativeResidentIntegration(unittest.TestCase):
    def assertNativeRunContract(self, schedule, *, horizon, seed):
        first_run = run(
            schedule, backend="native", horizon=horizon, seed=seed)
        repeated = run(
            schedule, backend="native", horizon=horizon, seed=seed)
        self.assertEqual(first_run.mapping, repeated.mapping)
        self.assertEqual(first_run.registry.zone_seat,
                         repeated.registry.zone_seat)
        self.assertEqual(first_run.registry.storage_site,
                         repeated.registry.storage_site)
        self.assertEqual(first_run.rng.getstate(), repeated.rng.getstate())
        self.assertEqual(len(first_run.decision_log),
                         len(repeated.decision_log))
        for first, second in zip(first_run.decision_log,
                                 repeated.decision_log):
            self.assertEqual(stable_log(first), stable_log(second))
            self.assertEqual(0, first.get("ghost_fix", 0))
            if "physical" not in first:
                continue
            self.assertEqual(first["physical"]["move_batches"],
                             second["physical"]["move_batches"])
            self.assertEqual(first["physical"]["transfers"],
                             second["physical"]["transfers"])
            for key in ("negative_log_fidelity", "move_time_us",
                        "total_distance_um"):
                self.assertAlmostEqual(
                    first["physical"][key], second["physical"][key],
                    delta=1e-12)
        self.assertTrue(any(
            row.get("backend_calls", 0)
            for row in first_run.decision_log))
        self.assertGreater(
            first_run.boundary_backend_metrics["fitness_ns"], 0)
        nonterminal = first_run.backend_timing_log[:-1]
        self.assertTrue(nonterminal)
        self.assertTrue(all(row["calls"] == 1 for row in nonterminal))
        self.assertTrue(all(
            row["rich_search"]["operator_profile"] == "exact"
            for row in first_run.decision_log[:-1]))
        configured = maximum_lookahead_horizon(horizon)
        self.assertTrue(all(
            row["forecast_objective"]["configured_depth"] == configured
            for row in first_run.decision_log[:-1]))
        # H0 may carry depth-zero current-state/cycle terms; those are not a
        # future read.  H>0 uses raw native future layers and therefore must
        # not also carry the legacy precomputed forecast table.
        if configured > 0:
            self.assertTrue(all(
                row["rich_search"]["forecast_terms"] == 0
                for row in first_run.decision_log[:-1]))
        self.assertTrue(all(
            row["rich_search"]["native_future_layers"] == 0
            for row in first_run.decision_log[:-1]
            if configured == 0))
        if configured > 0:
            self.assertTrue(any(
                row["rich_search"]["native_future_layers"] > 0
                for row in first_run.decision_log[:-1]))

    def test_real_toy_nl_and_bounded_decay_lk(self):
        schedule = [
            [[0, 1], [2, 3]],
            [[1, 4], [3, 5]],
            [[0, 4]],
            [[2, 5]],
            [[1, 5]],
        ]
        self.assertNativeRunContract(
            schedule, horizon=decay_lookahead_spec(0), seed=7)
        self.assertNativeRunContract(
            schedule, horizon=decay_lookahead_spec(8), seed=7)

    def test_seeded_random_boundary_streams(self):
        rng = random.Random(20260823)
        for seed in range(5):
            n_qubits = 8
            schedule = []
            for _ in range(5):
                atoms = list(range(n_qubits))
                rng.shuffle(atoms)
                schedule.append([
                    [atoms[0], atoms[1]], [atoms[2], atoms[3]]])
            self.assertNativeRunContract(
                schedule, horizon=decay_lookahead_spec(0), seed=seed)
            self.assertNativeRunContract(
                schedule, horizon=decay_lookahead_spec(8), seed=seed)

    def test_joint_participant_source_seat_handoff_reaches_router_unchanged(self):
        # At the L1 -> L2 boundary q3 takes q2's source seat in the same phase.
        # All three legs are physically compatible, so q2 must not be frozen as
        # a false stationary ghost while q3 is considered.  A legacy
        # post-selection repair used to relocate the second gate and make the
        # production router deadlock on atom 5.
        schedule = [
            [[3, 4], [5, 0]],
            [[7, 6], [2, 5]],
            [[2, 7], [5, 3]],
            [[2, 0], [5, 6]],
            [[6, 2], [3, 0]],
        ]
        placer = run(
            schedule, backend="native",
            horizon=decay_lookahead_spec(0), seed=0)
        source_boundary = placer.mapping[4]
        target_gate_mapping = placer.mapping[5]
        self.assertEqual(source_boundary[2], target_gate_mapping[3])
        self.assertEqual(
            placer.decision_log[1]["production_candidate"][
                "target_out_batches"],
            [[2, 3, 7]],
        )
        self.assertEqual(0, placer.decision_log[1].get("ghost_fix", 0))

    def test_native_always_return_cycles_a_resident_target_participant(self):
        placer = run(
            [[[0, 1]], [[0, 2]], [[3, 4]]],
            backend="native", horizon=decay_lookahead_spec(8), seed=0,
            ablation_policy="always_return")
        first = placer.decision_log[0]
        self.assertEqual(first["ablation_policy"], "always_return")
        self.assertEqual(first["stay"], 0)
        self.assertGreaterEqual(first["return"], 2)
        self.assertEqual(first.get("ghost_fix", 0), 0)


if __name__ == "__main__":
    unittest.main()
