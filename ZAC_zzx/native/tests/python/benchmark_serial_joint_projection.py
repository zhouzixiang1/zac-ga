"""Regression gate for serial joint-only gate projection.

This development fixture exposes a complete 140-option gate domain plus four
recommended-RETURN singletons and two disjoint recommended-STAY residents.
The winner fingerprint must remain identical while the public projection
counter demonstrates that only provisional/RETURN-joint/STAY-joint suffixes
receive a complete domain scan.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import asdict

from zzx.boundary_problem import (
    ArchitectureSnapshot,
    Point,
    RichForecastTerm,
    RichGateOption,
    RichH0Problem,
    RichReturnOption,
    RichSearchConfig,
)
from zzx.native_backend import NativeResidentBackend


N_ELIGIBLE = 2
DOMAIN_SIZE = 140


def fixture():
    points = tuple(Point(*value) for value in (
        (0, 0), (1, 0), (2, 0), (3, 0),
        (4, 0), (5, 0), (2, 1), (4, 1)))
    architecture = ArchitectureSnapshot(
        4, points, (6, 7), ((0, 1), (2, 3), (4, 5)))
    gate_options = tuple(
        RichGateOption(1000 + option, 0, 1, points[0], points[1])
        for option in range(DOMAIN_SIZE)
    )
    problem = RichH0Problem(
        architecture=architecture,
        current_points=(points[0], points[1], points[2], points[4]),
        participants=(0, 1),
        gate_domains=(gate_options,),
        static_ghosts=(),
        eligible=(2, 3),
        min_returns=0,
        eviction_order_indices=tuple(range(N_ELIGIBLE)),
        forced_return_mask=(False,) * N_ELIGIBLE,
        recommended_return_mask=(True, True),
        recommended_stay_mask=(False, False),
        return_domains=(
            (RichReturnOption(6, points[6], 1.0),),
            (RichReturnOption(7, points[7], 1.0),),
        ),
        matched_gate_genes=(0,),
        forecast_terms=(
            RichForecastTerm(1, "stay", "residency", 0.1, index=0),
            RichForecastTerm(1, "stay", "residency", 0.1, index=1),
        ),
        selected_horizon=1,
        boundary_id="serial-joint-projection-140",
    )
    config = RichSearchConfig(
        operator_profile="exact",
        max_horizon=1,
        alpha_lookahead=0.2,
        population_size=6,
        iterations=1,
        neighbors_per_solution=1,
        neighbor_sample_size=1,
        max_unique_evaluations=6,
        direct_enumeration_limit=1,
        local_polish_sweeps=0,
    )
    cached = (DOMAIN_SIZE - 1, 0, 0)
    return architecture, problem, config, cached


def main() -> None:
    architecture, problem, config, cached = fixture()
    state = random.Random(20260826).getstate()
    started = time.perf_counter_ns()
    result = NativeResidentBackend(architecture).solve_rich_boundary(
        problem, config, state, cached_winner=cached)
    elapsed_ns = time.perf_counter_ns() - started
    semantic = {
        "winner": asdict(result.winner),
        "return_assignments": result.return_assignments,
        "reseat_assignments": result.reseat_assignments,
        "participant_parking_assignments":
            result.participant_parking_assignments,
        "gate_option_indices": result.gate_option_indices,
        "search_negative_log_fidelity":
            result.search_negative_log_fidelity,
        "forecast_nll": result.forecast_nll,
        "current_ghost_rejections": result.current_ghost_rejections,
    }
    encoded = json.dumps(
        semantic, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")
    print(json.dumps({
        "protocol": "abi8-serial-joint-projection-140-v1",
        "elapsed_ns": elapsed_ns,
        "semantic_sha256": hashlib.sha256(encoded).hexdigest(),
        "projection_source": result.current_gate_projection_source,
        "projection_evaluated": result.current_gate_projection_evaluated,
        "guard_cohort": result.current_gate_guard_cohort_size,
        "guard_admitted": result.current_gate_guard_admitted_size,
        "winner": semantic["winner"],
        "gate_option_indices": list(result.gate_option_indices),
        "return_assignments": [list(value)
                               for value in result.return_assignments],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
