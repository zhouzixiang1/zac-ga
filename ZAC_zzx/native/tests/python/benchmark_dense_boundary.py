"""Reproducible microbenchmark for a captured dense resident boundary.

The benchmark intentionally consumes a development-only pickle captured just
before the native call.  It measures the C++ solve region, freezes the complete
non-timing result to a SHA256 digest, and is therefore suitable for comparing a
candidate build (via ``PYTHONPATH=/path/to/build``) with an installed control
wheel without accepting a faster but semantically different answer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import statistics
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path


for _name in (
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

ZAC_ZZX_ROOT = Path(__file__).resolve().parents[3]
if str(ZAC_ZZX_ROOT) not in sys.path:
    sys.path.insert(0, str(ZAC_ZZX_ROOT))

from zzx.native_backend import NativeResidentBackend, build_info  # noqa: E402


def _semantic_digest(result) -> str:
    payload = asdict(result)
    payload.pop("timing", None)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--problem", type=Path, required=True,
        help="captured ABI8 (problem, config, RNG, extra) pickle")
    parser.add_argument("--budget", type=int, default=32)
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--neighbor-sample-size", type=int)
    parser.add_argument("--local-polish-sweeps", type=int)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    problem, config, rng_state, extra = pickle.loads(
        args.problem.read_bytes())
    overrides = {"max_unique_evaluations": args.budget}
    for name in (
            "iterations", "neighbor_sample_size", "local_polish_sweeps"):
        value = getattr(args, name)
        if value is not None:
            overrides[name] = value
    config = replace(config, **overrides)
    elapsed_ns = []
    digests = []
    final = None
    for _ in range(args.repeats):
        backend = NativeResidentBackend(problem.architecture)
        started = time.perf_counter_ns()
        final = backend.solve_rich_boundary(
            problem, config, rng_state,
            cached_winner=extra.get("cached_winner"))
        elapsed_ns.append(time.perf_counter_ns() - started)
        digests.append(_semantic_digest(final))
    if len(set(digests)) != 1:
        raise AssertionError("dense boundary result is not reproducible")
    assert final is not None
    payload = {
        "schema": 1,
        "protocol": "abi8-dense-ghost-boundary-v1",
        "problem": str(args.problem.resolve()),
        "budget": args.budget,
        "iterations": config.iterations,
        "neighbor_sample_size": config.neighbor_sample_size,
        "local_polish_sweeps": config.local_polish_sweeps,
        "repeats": args.repeats,
        "elapsed_ns": elapsed_ns,
        "median_elapsed_ns": int(statistics.median(elapsed_ns)),
        "semantic_sha256": digests[0],
        "winner": {
            "chromosome": list(final.winner.chromosome),
            "negative_log_fidelity": final.winner.negative_log_fidelity,
            "move_batches": final.winner.move_batches,
            "move_time_us": final.winner.move_time_us,
            "total_distance_um": final.winner.total_distance_um,
            "return_assignments": [list(row)
                                   for row in final.return_assignments],
            "reseat_assignments": [list(row)
                                   for row in final.reseat_assignments],
            "participant_parking_assignments": [
                list(row) for row in
                final.participant_parking_assignments],
        },
        "native_build": build_info(),
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
