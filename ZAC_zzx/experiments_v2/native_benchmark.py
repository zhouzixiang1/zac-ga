"""Deterministic medium-boundary Python-reference vs ABI8/wire-v6 benchmark.

This benchmark measures only ``ResidentPlacer.run`` after the architecture and
placer objects exist.  It is a language-migration gate, not an ICCAD timing
comparison: operator_profile is forced to ``exact`` and every repeat proves the
same mapping, resident registry, RNG state, current physical NLL and bounded
forecast NLL before its runtime can enter the speedup summary.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from zac.ds.architecture import Architecture
from zzx.algorithm_v2 import decay_lookahead_spec
from zzx.zplacer import ResidentPlacer


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DIRECT_EXACT_BUDGET = 16_384


def medium_schedule(*, seed: int = 42, qubits: int = 8,
                    layers: int = 9, gates_per_layer: int = 1
                    ) -> list[list[list[int]]]:
    if qubits < 2 * gates_per_layer or min(qubits, layers,
                                           gates_per_layer) <= 0:
        raise ValueError("invalid medium-boundary dimensions")
    rng = random.Random(seed)
    result = []
    for _ in range(layers):
        atoms = list(range(qubits))
        rng.shuffle(atoms)
        result.append([
            [atoms[2 * index], atoms[2 * index + 1]]
            for index in range(gates_per_layer)
        ])
    return result


def _architecture(path: Path) -> Architecture:
    value = json.loads(path.read_text(encoding="utf-8"))
    result = Architecture(value)
    result.preprocessing()
    return result


def _run_once(
        schedule: Sequence[Sequence[Sequence[int]]], *, backend: str,
        max_horizon: int, architecture_path: Path, wheel_sha256: str,
        seed: int = 9) -> tuple[ResidentPlacer, int]:
    n_qubits = 1 + max(q for layer in schedule for gate in layer for q in gate)
    initial = [(0, q // 10, q % 10) for q in range(n_qubits)]
    placer = ResidentPlacer(
        initial,
        seed=seed,
        experiment_schema=2,
        method_id="ours_nl" if max_horizon == 0 else "ours_lk",
        objective="physical_log_fidelity",
        lookahead_horizon=decay_lookahead_spec(max_horizon),
        alpha_lookahead=0.1,
        engine="ga",
        fitness_cache=True,
        population_size=6,
        iterations=8,
        neighbors_per_solution=2,
        neighbor_sample_size=24,
        direct_enumeration_limit=DIRECT_EXACT_BUDGET,
        max_unique_evaluations=DIRECT_EXACT_BUDGET,
        backend=backend,
        formal_native=backend == "native",
        native_wheel_sha256=(wheel_sha256 if backend == "native" else ""),
        operator_profile="exact",
    )
    architecture = _architecture(architecture_path)
    started_ns = time.perf_counter_ns()
    placer.run(
        architecture, [initial], schedule, True,
        [set() for _ in schedule])
    direct_spaces = tuple(
        int(row.get("rich_search", {}).get("direct_search_space", -1))
        for row in placer.decision_log[:-1]
    )
    if len(direct_spaces) != len(schedule) - 1:
        raise RuntimeError(
            "native medium benchmark transition-count audit drift")
    if any(value <= 0 or value > DIRECT_EXACT_BUDGET
           for value in direct_spaces):
        raise RuntimeError(
            "native medium benchmark boundary exceeds direct exact budget: "
            f"spaces={direct_spaces}, budget={DIRECT_EXACT_BUDGET}")
    return placer, time.perf_counter_ns() - started_ns


def _assert_parity(reference: ResidentPlacer,
                   native: ResidentPlacer) -> Mapping[str, Any]:
    if reference.mapping != native.mapping:
        raise RuntimeError("native medium benchmark mapping drift")
    if (reference.registry.zone_seat != native.registry.zone_seat
            or reference.registry.storage_site !=
            native.registry.storage_site):
        raise RuntimeError("native medium benchmark resident-registry drift")
    if reference.rng.getstate() != native.rng.getstate():
        raise RuntimeError("native medium benchmark RNG drift")
    if len(reference.decision_log) != len(native.decision_log):
        raise RuntimeError("native medium benchmark decision-count drift")
    max_current_error = 0.0
    max_forecast_error = 0.0
    direct_spaces = []
    for left, right in zip(reference.decision_log[:-1],
                           native.decision_log[:-1]):
        for key in ("stay", "return", "reseat", "participant_parking",
                    "eligible_decisions"):
            if left.get(key) != right.get(key):
                raise RuntimeError(f"native medium decision drift: {key}")
        current_error = abs(
            float(left["physical"]["negative_log_fidelity"])
            - float(right["physical"]["negative_log_fidelity"]))
        forecast_error = abs(
            float(left["forecast_objective"][
                "weighted_negative_log_fidelity"])
            - float(right["forecast_objective"][
                "weighted_negative_log_fidelity"]))
        max_current_error = max(max_current_error, current_error)
        max_forecast_error = max(max_forecast_error, forecast_error)
        left_space = int(left["rich_search"]["direct_search_space"])
        right_space = int(right["rich_search"]["direct_search_space"])
        if left_space != right_space:
            raise RuntimeError(
                "native medium benchmark direct-space audit drift")
        direct_spaces.append(left_space)
    if max(max_current_error, max_forecast_error) > 1e-12:
        raise RuntimeError("native medium benchmark NLL drift exceeds 1e-12")
    return {
        "mapping_equal": True,
        "registry_equal": True,
        "rng_equal": True,
        "direct_search_spaces": direct_spaces,
        "max_direct_search_space": max(direct_spaces, default=0),
        "max_current_nll_abs_error": max_current_error,
        "max_forecast_nll_abs_error": max_forecast_error,
    }


def run_medium_benchmark(
        *, architecture_path: Path, wheel_sha256: str,
        repetitions: int = 3, minimum_speedup: float = 5.0
        ) -> Mapping[str, Any]:
    if repetitions <= 0 or minimum_speedup <= 0.0:
        raise ValueError("repetitions and minimum_speedup must be positive")
    schedule = medium_schedule()
    # One unreported native warm-up loads caches/dylibs before either formal
    # median is formed.
    _run_once(
        schedule, backend="native", max_horizon=0,
        architecture_path=architecture_path, wheel_sha256=wheel_sha256)
    horizons = {}
    accepted = True
    for max_horizon in (0, 8):
        reference_ns = []
        native_ns = []
        parity = None
        for _ in range(repetitions):
            reference, reference_elapsed = _run_once(
                schedule, backend="reference", max_horizon=max_horizon,
                architecture_path=architecture_path,
                wheel_sha256=wheel_sha256)
            native, native_elapsed = _run_once(
                schedule, backend="native", max_horizon=max_horizon,
                architecture_path=architecture_path,
                wheel_sha256=wheel_sha256)
            parity = _assert_parity(reference, native)
            reference_ns.append(reference_elapsed)
            native_ns.append(native_elapsed)
        reference_median = int(statistics.median(reference_ns))
        native_median = int(statistics.median(native_ns))
        speedup = reference_median / native_median
        horizon_accepted = speedup >= minimum_speedup
        accepted = accepted and horizon_accepted
        horizons[str(max_horizon)] = {
            "reference_ns": reference_ns,
            "native_ns": native_ns,
            "reference_median_ns": reference_median,
            "native_median_ns": native_median,
            "speedup": speedup,
            "minimum_speedup": minimum_speedup,
            "accepted": horizon_accepted,
            "parity": parity,
        }
    return {
        "protocol": "resident-python-vs-abi8-medium-v2",
        "backend": "cpp-native-v8",
        "measurement": "ResidentPlacer.run only; preprocessed architecture",
        "operator_profile": "exact",
        "wheel_sha256": wheel_sha256,
        "schedule": {
            "seed": 42, "qubits": 8, "layers": 9,
            "gates_per_layer": len(schedule[0]), "transitions": 8,
            "direct_exact_budget": DIRECT_EXACT_BUDGET,
        },
        "repetitions": repetitions,
        "horizons": horizons,
        "accepted": accepted,
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--architecture", type=Path,
        default=PACKAGE_ROOT / "hardware_spec" / "full_architecture.json")
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--minimum-speedup", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run_medium_benchmark(
        architecture_path=args.architecture.resolve(),
        wheel_sha256=args.wheel_sha256,
        repetitions=args.repetitions,
        minimum_speedup=args.minimum_speedup)
    if args.output:
        _atomic_json(args.output.resolve(), result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["medium_schedule", "run_medium_benchmark"]
