"""End-to-end differential checks at the real ResidentPlacer boundary."""
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
    "backend_calls", "backend_candidates", "cache", "rich_search",
}
NATIVE_WHEEL_SHA256 = (
    "0616479e5bcf116d553dfafba3ca1cbef5ec3a4016d684f61407267710a282cc")


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
    def assertRunParity(self, schedule, *, horizon, seed):
        reference = run(
            schedule, backend="reference", horizon=horizon, seed=seed)
        native = run(schedule, backend="native", horizon=horizon, seed=seed)
        self.assertEqual(reference.mapping, native.mapping)
        self.assertEqual(reference.registry.zone_seat, native.registry.zone_seat)
        self.assertEqual(reference.registry.storage_site,
                         native.registry.storage_site)
        self.assertEqual(reference.rng.getstate(), native.rng.getstate())
        self.assertEqual(len(reference.decision_log), len(native.decision_log))
        for first, second in zip(reference.decision_log, native.decision_log):
            self.assertEqual(stable_log(first), stable_log(second))
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
            row.get("backend_calls", 0) for row in native.decision_log))
        self.assertGreater(native.boundary_backend_metrics["fitness_ns"], 0)
        nonterminal = native.backend_timing_log[:-1]
        self.assertTrue(nonterminal)
        self.assertTrue(all(row["calls"] == 1 for row in nonterminal))
        self.assertTrue(all(
            row["rich_search"]["operator_profile"] == "exact"
            for row in native.decision_log[:-1]))
        configured = maximum_lookahead_horizon(horizon)
        self.assertTrue(all(
            row["forecast_objective"]["configured_depth"] == configured
            for row in native.decision_log[:-1]))

    def test_real_toy_nl_and_bounded_decay_lk(self):
        schedule = [
            [[0, 1], [2, 3]],
            [[1, 4], [3, 5]],
            [[0, 4]],
            [[2, 5]],
            [[1, 5]],
        ]
        self.assertRunParity(
            schedule, horizon=decay_lookahead_spec(0), seed=7)
        self.assertRunParity(
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
            self.assertRunParity(
                schedule, horizon=decay_lookahead_spec(0), seed=seed)
            self.assertRunParity(
                schedule, horizon=decay_lookahead_spec(8), seed=seed)


if __name__ == "__main__":
    unittest.main()
