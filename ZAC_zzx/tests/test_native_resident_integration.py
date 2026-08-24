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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zac.ds.architecture import Architecture  # noqa: E402
from zzx.algorithm_v2 import (decay_lookahead_spec,
                              maximum_lookahead_horizon)  # noqa: E402
from zzx.native_backend import native_available  # noqa: E402
from zzx.zplacer import ResidentPlacer  # noqa: E402


TIMING_KEYS = {
    "horizon_selection_ns", "search_kernel_ns", "marshal_ns",
    "backend_search_kernel_ns", "fitness_ns", "backend_selection_ns",
    "backend_calls", "backend_candidates", "cache",
}
NATIVE_WHEEL_SHA256 = (
    "b5c8c7ae2ff13893c15b28b364ffd7c7b96121d2aecd920ff837a98c2421ef29")


def architecture():
    value = json.loads(
        (ROOT / "hardware_spec/toy_architecture.json").read_text())
    result = Architecture(value)
    result.preprocessing()
    return result


def run(schedule, *, backend, horizon, seed):
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
    )
    placer.run(
        architecture(), [initial], schedule, True,
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


if __name__ == "__main__":
    unittest.main()
