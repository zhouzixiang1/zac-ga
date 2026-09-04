"""Microbenchmark the ABI8 exact-scheduler endpoint lookup hot path.

This is a development benchmark, not a formal algorithm-runtime result.  It
uses a cm85a-shaped condition that matters to ``score_geometry``: many moving
owners target sites near the end of a large immutable architecture snapshot.
Run the same script with pre/post Release extensions on ``PYTHONPATH`` to
isolate the site-id lookup change.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time

from zzx.boundary_problem import (
    ArchitectureSnapshot,
    RichGateOption,
    RichH0Problem,
    RichSearchConfig,
)
from zzx.native_backend import NativeResidentBackend, build_info


def fixture(*, n_atoms: int = 32, padding_sites: int = 4096):
    if n_atoms % 2:
        raise ValueError("n_atoms must be even")
    current = tuple((float(atom), 0.0) for atom in range(n_atoms))
    padding = tuple(
        (10000.0 + float(site), 10000.0)
        for site in range(padding_sites)
    )
    targets = tuple((float(atom), 10.0) for atom in range(n_atoms))
    coordinates = current + padding + targets
    first_target = len(current) + len(padding)
    architecture = ArchitectureSnapshot.from_coordinates(
        n_atoms, coordinates, ())
    gates = tuple(
        (RichGateOption(
            1000 + gate,
            2 * gate,
            2 * gate + 1,
            None,
            None,
            first_target + 2 * gate,
            first_target + 2 * gate + 1,
        ),)
        for gate in range(n_atoms // 2)
    )
    problem = RichH0Problem(
        architecture=architecture,
        current_points=(),
        current_site_ids=tuple(range(n_atoms)),
        participants=tuple(range(n_atoms)),
        gate_domains=gates,
        static_ghosts=(),
        eligible=(),
        min_returns=0,
        eviction_order_indices=(),
        forced_return_mask=(),
        return_domains=(),
        matched_gate_genes=(0,) * len(gates),
        selected_horizon=0,
        boundary_id="exact-site-lookup-microbenchmark",
        prior_idle_time_us=(0.0,) * n_atoms,
        scheduler_trace_end_us=0.0,
        scheduler_active_union_us=(0.0,) * n_atoms,
        scheduler_aod_end_us=(0.0,),
        scheduler_one_qubit_end_us=0.0,
        scheduler_rydberg_end_us=(0.0,),
        scheduler_qubit_dependency_end_us=(0.0,) * n_atoms,
        scheduler_back_dependency_end_us=(0.0,) * n_atoms,
    )
    return architecture, problem


def benchmark(*, repeats: int, n_atoms: int, padding_sites: int) -> dict:
    architecture, problem = fixture(
        n_atoms=n_atoms, padding_sites=padding_sites)
    backend = NativeResidentBackend(architecture)
    config = RichSearchConfig(
        operator_profile="exact",
        max_horizon=0,
        direct_enumeration_limit=512,
        max_unique_evaluations=512,
    )
    state = random.Random(20260826).getstate()
    for _ in range(5):
        backend.solve_rich_boundary(problem, config, state)
    wall_ns = []
    kernel_ns = []
    fingerprint = None
    for _ in range(repeats):
        started = time.perf_counter_ns()
        result = backend.solve_rich_boundary(problem, config, state)
        wall_ns.append(time.perf_counter_ns() - started)
        kernel_ns.append(int(result.timing["search_kernel_ns"]))
        current = (
            result.winner.chromosome,
            result.winner.negative_log_fidelity,
            result.winner.move_batches,
            result.winner.move_time_us,
            result.winner.total_distance_um,
            result.gate_option_indices,
        )
        if fingerprint is None:
            fingerprint = current
        elif current != fingerprint:
            raise AssertionError("site lookup benchmark is not deterministic")
    return {
        "protocol": "abi8-exact-site-lookup-microbenchmark-v1",
        "claim_scope": "development microbenchmark; not formal runtime",
        "native_build": build_info(),
        "n_atoms": n_atoms,
        "architecture_sites": n_atoms * 2 + padding_sites,
        "moving_legs": n_atoms,
        "repeats": repeats,
        "wall_median_ns": int(statistics.median(wall_ns)),
        "kernel_median_ns": int(statistics.median(kernel_ns)),
        "wall_min_ns": min(wall_ns),
        "kernel_min_ns": min(kernel_ns),
        "fingerprint": {
            "chromosome": list(fingerprint[0]),
            "negative_log_fidelity": fingerprint[1],
            "move_batches": fingerprint[2],
            "move_time_us": fingerprint[3],
            "total_distance_um": fingerprint[4],
            "gate_option_indices": list(fingerprint[5]),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--n-atoms", type=int, default=32)
    parser.add_argument("--padding-sites", type=int, default=4096)
    args = parser.parse_args()
    print(json.dumps(benchmark(
        repeats=args.repeats,
        n_atoms=args.n_atoms,
        padding_sites=args.padding_sites,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
