"""Exact-parity microbenchmark for dense ``color_phase`` scratch reuse.

The fixture mirrors the allocator-sensitive shape of a serial cm85a boundary:
140 complete-domain candidates, two moving gate atoms, and the other atoms as
stationary ghosts.  It hashes every returned batch, so pre/post Release builds
must agree byte-for-byte before their timings are compared.  This is a native
kernel development benchmark, not a formal compiler-runtime result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import time

import zac_native_core


def small_phase_parity_suite() -> tuple[tuple, tuple]:
    cases: list[tuple[dict, int]] = []
    # GHZ-shaped boundary: one returning resident followed by a two-atom gate
    # move.  Moving-owner ghosts must be ignored in both phases.
    cases.append(({
        "legs": [[5.0, 0.0, 5.0, 0.0, 0.0]],
        "ghosts": [[0, 0.0, 5.0], [2, 100.0, 100.0]],
        "owners": [0],
        "batching": "phase",
    }, 0))
    cases.append(({
        "legs": [
            [10.0, 0.0, 0.0, 0.0, 10.0],
            [10.0, 2.0, 0.0, 2.0, 10.0],
        ],
        "ghosts": [
            [0, 0.0, 0.0], [1, 2.0, 0.0], [2, 100.0, 100.0],
        ],
        "owners": [0, 1],
        "batching": "phase",
    }, 0))

    rng = random.Random(20260826)
    thresholds = (0, 1, 2, 24)
    for case_index in range(512):
        leg_count = case_index % 3
        legs = []
        ghosts = []
        owners = []
        for leg_index in range(leg_count):
            source = (
                0.5 * rng.randrange(-12, 13),
                0.5 * rng.randrange(-12, 13),
            )
            target = (
                source[0] + 0.5 * rng.randrange(-8, 9),
                source[1] + 0.5 * rng.randrange(-8, 9),
            )
            distance = math.hypot(
                target[0] - source[0], target[1] - source[1])
            legs.append([
                distance, source[0], source[1], target[0], target[1],
            ])
            owners.append(leg_index)
            ghosts.append([leg_index, source[0], source[1]])
        # Static points span both likely corridors and far-away positions.
        for ghost_index in range(4):
            ghosts.append([
                100 + ghost_index,
                0.5 * rng.randrange(-16, 17),
                0.5 * rng.randrange(-16, 17),
            ])
        cases.append(({
            "legs": legs,
            "ghosts": ghosts,
            "owners": owners,
            "batching": "phase" if case_index % 2 else "greedy",
        }, thresholds[case_index % len(thresholds)]))
    color_results = tuple(
        tuple(tuple(batch) for batch in
              zac_native_core.color_phase(phase, threshold))
        for phase, threshold in cases
    )
    replay_results = []
    for phase, threshold in cases:
        try:
            replay_results.append((
                "ok",
                tuple(tuple(batch) for batch in
                      zac_native_core.replay_phase_batches(phase, threshold)),
            ))
        except RuntimeError as exc:
            replay_results.append(("infeasible", str(exc)))
    return color_results, tuple(replay_results)


def fixture(*, domain_size: int = 140, n_atoms: int = 85) -> tuple[dict, ...]:
    if n_atoms < 3:
        raise ValueError("n_atoms must be at least three")
    positions = [(0.0, 0.0), (2.0, 0.0)]
    positions.extend(
        (1000.0 + 4.0 * float(atom), -1000.0 - float(atom % 7))
        for atom in range(2, n_atoms)
    )
    ghosts = [[atom, point[0], point[1]]
              for atom, point in enumerate(positions)]
    phases = []
    for option in range(domain_size):
        row, column = divmod(option, 14)
        first_target = (20.0 + 3.0 * column, 20.0 + 4.0 * row)
        # Alternate the second displacement direction.  This produces both
        # one- and two-batch outcomes while retaining the same complete domain.
        if option % 2:
            second_target = (first_target[0] - 2.0, first_target[1] + 1.0)
        else:
            second_target = (first_target[0] + 2.0, first_target[1])
        first_distance = math.hypot(
            first_target[0] - positions[0][0],
            first_target[1] - positions[0][1])
        second_distance = math.hypot(
            second_target[0] - positions[1][0],
            second_target[1] - positions[1][1])
        phases.append({
            "legs": [
                [first_distance, positions[0][0], positions[0][1],
                 first_target[0], first_target[1]],
                [second_distance, positions[1][0], positions[1][1],
                 second_target[0], second_target[1]],
            ],
            "ghosts": ghosts,
            "owners": [0, 1],
            "batching": "phase",
        })
    return tuple(phases)


def benchmark(*, repeats: int, domain_size: int, n_atoms: int) -> dict:
    phases = fixture(domain_size=domain_size, n_atoms=n_atoms)
    small_phase_expected, small_replay_expected = small_phase_parity_suite()
    small_phase_encoded = json.dumps(
        small_phase_expected, separators=(",", ":")).encode("utf-8")
    small_replay_encoded = json.dumps(
        small_replay_expected, separators=(",", ":")).encode("utf-8")

    def evaluate_domain() -> tuple[tuple[tuple[int, ...], ...], ...]:
        return tuple(
            tuple(tuple(batch) for batch in zac_native_core.color_phase(phase))
            for phase in phases
        )

    expected = evaluate_domain()
    encoded = json.dumps(expected, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    for _ in range(5):
        if evaluate_domain() != expected:
            raise AssertionError("color_phase changed during warm-up")

    elapsed_ns = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        actual = evaluate_domain()
        elapsed_ns.append(time.perf_counter_ns() - started)
        if actual != expected:
            raise AssertionError("color_phase is not deterministic")
    return {
        "protocol": "abi8-color-phase-scratch-exact-parity-v1",
        "claim_scope": "native development microbenchmark; not formal runtime",
        "domain_size": domain_size,
        "n_atoms": n_atoms,
        "repeats": repeats,
        "semantic_sha256": digest,
        "small_phase_parity_sha256": hashlib.sha256(
            small_phase_encoded).hexdigest(),
        "small_replay_parity_sha256": hashlib.sha256(
            small_replay_encoded).hexdigest(),
        "small_phase_case_count": len(small_phase_expected),
        "median_elapsed_ns": int(statistics.median(elapsed_ns)),
        "min_elapsed_ns": min(elapsed_ns),
        "max_elapsed_ns": max(elapsed_ns),
        "batch_histogram": {
            str(count): sum(len(value) == count for value in expected)
            for count in sorted({len(value) for value in expected})
        },
        "native_build": zac_native_core.build_info(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--domain-size", type=int, default=140)
    parser.add_argument("--n-atoms", type=int, default=85)
    parser.add_argument("--output")
    args = parser.parse_args()
    rendered = json.dumps(benchmark(
        repeats=args.repeats,
        domain_size=args.domain_size,
        n_atoms=args.n_atoms,
    ), indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
