"""Reproducible compact-wire one-call microbenchmark (not a formal runtime claim)."""
from __future__ import annotations

import itertools
import argparse
import json
import random
import statistics
import time
from pathlib import Path

from zzx.boundary_problem import (
    ArchitectureSnapshot, BoundaryConfig, BoundaryProblem, CandidatePlan,
    Ghost, Leg, MovementPhase, Point, RichGateOption, RichH0Problem,
    RichReturnOption, RichSearchConfig, flatten_candidates,
)
from zzx.native_backend import NativeResidentBackend, build_info
from zzx.reference_backend import ReferenceResidentBackend, ghost_hit_atoms


def fixture():
    n_atoms = 14
    current = [Point(float(atom), 0.0) for atom in range(n_atoms)]
    targets = [Point(float(atom), 2.0) for atom in range(8)]
    storage = [Point(float(atom + 8), 4.0) for atom in range(6)]
    coordinates = current + targets + storage
    storage_ids = tuple(range(n_atoms + 8, n_atoms + 14))
    architecture = ArchitectureSnapshot(
        n_atoms, tuple(coordinates), storage_ids)
    gates = tuple(
        (RichGateOption(
            100 + gate, 2 * gate, 2 * gate + 1, None, None,
            n_atoms + 2 * gate, n_atoms + 2 * gate + 1),)
        for gate in range(4))
    problem = RichH0Problem(
        architecture=architecture,
        current_points=(),
        current_site_ids=tuple(range(n_atoms)),
        participants=tuple(range(8)),
        gate_domains=gates,
        static_ghosts=(),
        eligible=tuple(range(8, n_atoms)),
        min_returns=0,
        eviction_order_indices=tuple(range(6)),
        forced_return_mask=(False,) * 6,
        return_domains=tuple(
            (RichReturnOption(storage_ids[index], None, 1.0),)
            for index in range(6)),
        matched_gate_genes=(0,) * 4,
        selected_horizon=0,
        boundary_id="rich-microbenchmark",
    )
    return architecture, problem, current, targets, storage


def python_direct(architecture, current, targets, storage):
    candidates = []
    for bits in itertools.product((0, 1), repeat=6):
        positions = list(current)
        back, back_owners = [], []
        for index, bit in enumerate(bits):
            if bit:
                atom = index + 8
                back.append(Leg.between(positions[atom], storage[index]))
                back_owners.append(atom)
                positions[atom] = storage[index]
        out = [Leg.between(positions[atom], targets[atom])
               for atom in range(8)]
        ghosts_t0 = tuple(Ghost(atom, current[atom])
                          for atom in range(len(current)))
        ghosts_t1 = tuple(Ghost(atom, positions[atom])
                          for atom in range(len(current)))
        candidates.append(CandidatePlan(
            (0, 0, 0, 0) + bits,
            (MovementPhase(tuple(back), ghosts_t0, tuple(back_owners)),
             MovementPhase(tuple(out), ghosts_t1, tuple(range(8)))),
            6 - sum(bits),
        ))
    return ReferenceResidentBackend().solve_boundary(
        BoundaryProblem(architecture, tuple(candidates)),
        BoundaryConfig(enforce_single_leg_ghost=False),
    ).winner


def one_call_benchmark(repeats=20):
    architecture, problem, current, targets, storage = fixture()
    backend = NativeResidentBackend(architecture)
    config = RichSearchConfig(operator_profile="exact")
    state = random.Random(7).getstate()
    for _ in range(3):
        backend.solve_rich_h0(problem, config, state)
        python_direct(architecture, current, targets, storage)
    native_times, reference_times = [], []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        native = backend.solve_rich_h0(problem, config, state)
        native_times.append(time.perf_counter_ns() - started)
        started = time.perf_counter_ns()
        reference = python_direct(
            architecture, current, targets, storage)
        reference_times.append(time.perf_counter_ns() - started)
    assert native.winner.chromosome == reference.chromosome
    native_median = statistics.median(native_times)
    reference_median = statistics.median(reference_times)
    return {
        "candidates": 64,
        "native_median_ns": int(native_median),
        "python_reference_median_ns": int(reference_median),
        "speedup": reference_median / native_median,
        "marshal_ns": native.timing["marshal_ns"],
        "search_kernel_ns": native.timing["search_kernel_ns"],
        "marshal_fraction": native.timing["marshal_ns"] / (
            native.timing["marshal_ns"]
            + native.timing["search_kernel_ns"]),
    }


def core_fitness_benchmark(backend, repeats=15):
    rng = random.Random(9)
    architecture = ArchitectureSnapshot(64)
    candidates = []
    for candidate_index in range(256):
        legs = tuple(
            Leg.between(
                (rng.uniform(-10, 10), rng.uniform(-10, 10)),
                (rng.uniform(-10, 10), rng.uniform(-10, 10)),
            ) for _ in range(12))
        ghosts = tuple(
            Ghost(atom, Point(rng.uniform(-10, 10), rng.uniform(-10, 10)))
            for atom in range(24))
        candidates.append(CandidatePlan(
            (candidate_index,),
            (MovementPhase(legs, ghosts, tuple(range(12))),),
            candidate_index % 5,
        ))
    problem = BoundaryProblem(architecture, tuple(candidates))
    flat = flatten_candidates(candidates)
    config = BoundaryConfig(enforce_single_leg_ghost=False)
    native_architecture = backend._module.ArchitectureSnapshot(64, [], [])
    for _ in range(3):
        backend._module.evaluate_many_flat(
            native_architecture, flat.buffers(), config.to_wire())
        ReferenceResidentBackend().evaluate_many(problem, config=config)
    native_kernel, python_kernel = [], []
    for _ in range(repeats):
        value = backend._module.evaluate_many_flat(
            native_architecture, flat.buffers(), config.to_wire())
        native_kernel.append(int(value["fitness_ns"]))
        started = time.perf_counter_ns()
        reference = ReferenceResidentBackend().evaluate_many(
            problem, config=config)
        python_kernel.append(time.perf_counter_ns() - started)
    native_median = statistics.median(native_kernel)
    python_median = statistics.median(python_kernel)
    assert len(reference) == 256
    return {
        "candidates": 256,
        "native_fitness_ns": int(native_median),
        "python_fitness_ns": int(python_median),
        "speedup": python_median / native_median,
    }


def ghost_kernel_benchmark(backend, repeats=100):
    rng = random.Random(17)
    legs = tuple(
        Leg.between(
            (rng.uniform(-50, 50), rng.uniform(-50, 50)),
            (rng.uniform(-50, 50), rng.uniform(-50, 50)),
        ) for _ in range(48))
    ghosts = tuple(
        Ghost(atom, Point(rng.uniform(-50, 50), rng.uniform(-50, 50)))
        for atom in range(128))
    leg_wire = [leg.to_wire() for leg in legs]
    ghost_wire = [ghost.to_wire() for ghost in ghosts]
    expected = ghost_hit_atoms(legs, ghosts)
    assert tuple(backend._module.ghost_hits(leg_wire, ghost_wire)) == expected
    native_times, python_times = [], []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        backend._module.ghost_hits(leg_wire, ghost_wire)
        native_times.append(time.perf_counter_ns() - started)
        started = time.perf_counter_ns()
        ghost_hit_atoms(legs, ghosts)
        python_times.append(time.perf_counter_ns() - started)
    native_median = statistics.median(native_times)
    python_median = statistics.median(python_times)
    return {
        "legs": 48,
        "ghosts": 128,
        "native_bridge_and_kernel_ns": int(native_median),
        "python_kernel_ns": int(python_median),
        "speedup": python_median / native_median,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    architecture, _problem, _current, _targets, _storage = fixture()
    backend = NativeResidentBackend(architecture)
    native_build = build_info(require_registered_wheel=True)
    payload = {
        "schema": 2,
        "protocol": "abi5-registered-native-microbenchmark-v1",
        "native_build": native_build,
        "one_call": one_call_benchmark(args.repeats),
        "fitness_core": core_fitness_benchmark(backend),
        "ghost_core": ghost_kernel_benchmark(backend),
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
