"""Stage-aligned timing contracts for M1/M2/M3/M4.

The primary metric is transition placement/decision time.  Initial placement,
QASM parsing, native routing, code generation, scoring, and file output are
recorded separately and never folded into the ICCAD A* comparison.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .protocol import FORMAL_TIMING_REPETITIONS


RUNTIME_PROTOCOL_ID = "transition-placement-v1"
METHODS = ("M1", "M2", "M3", "M4")
TERMINAL_STATUSES = {
    "success", "timeout", "oom", "compiler_error", "verifier_fail",
    "scorer_error",
}


def ordered_two_qubit_layer_ledger(
        n_qubits: int,
        layers: Sequence[Sequence[Sequence[int]]]) -> dict[str, Any]:
    """Hash the *observed* ordered two-qubit transition layers.

    Unlike :func:`two_qubit_layer_ledger`, this helper never reconstructs an
    ASAP schedule from a flat input stream.  Layer boundaries must come from
    the compiler's own scheduling/placement evidence.  CZ endpoints and the
    independent gates within one layer are canonicalised because their order
    has no physical meaning; the order of the layers is retained exactly.
    """
    if n_qubits <= 0:
        raise ValueError("layer ledger requires a positive qubit count")
    frozen_layers: list[list[tuple[int, int]]] = []
    for layer_index, raw_layer in enumerate(layers):
        frozen_layer: list[tuple[int, int]] = []
        occupied: set[int] = set()
        for raw_pair in raw_layer:
            if len(raw_pair) != 2:
                raise ValueError(
                    f"two-qubit pair must have width two: {raw_pair!r}")
            q0, q1 = (int(raw_pair[0]), int(raw_pair[1]))
            if not (0 <= q0 < n_qubits and 0 <= q1 < n_qubits) or q0 == q1:
                raise ValueError(f"invalid CZ pair: {(q0, q1)!r}")
            if q0 in occupied or q1 in occupied:
                raise ValueError(
                    "one qubit occurs twice in observed two-qubit layer "
                    f"{layer_index}")
            occupied.update((q0, q1))
            frozen_layer.append((min(q0, q1), max(q0, q1)))
        if not frozen_layer:
            raise ValueError(
                f"observed two-qubit layer {layer_index} is empty")
        frozen_layers.append(sorted(frozen_layer))
    payload = json.dumps(
        frozen_layers, sort_keys=True, separators=(",", ":"))
    return {
        "layer_ledger_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "layers_2q": len(frozen_layers),
        "transitions": max(0, len(frozen_layers) - 1),
        "gates_2q": sum(len(layer) for layer in frozen_layers),
        "layers": frozen_layers,
    }


def two_qubit_layer_ledger(n_qubits: int,
                           pairs: Sequence[Sequence[int]]) -> dict[str, Any]:
    """Return the canonical ASAP layer ledger for an ordered CZ-pair list."""
    if n_qubits <= 0:
        raise ValueError("layer ledger requires a positive qubit count")
    next_layer = [0] * int(n_qubits)
    layers: list[list[tuple[int, int]]] = []
    for raw_pair in pairs:
        if len(raw_pair) != 2:
            raise ValueError(f"two-qubit pair must have width two: {raw_pair!r}")
        q0, q1 = (int(raw_pair[0]), int(raw_pair[1]))
        if not (0 <= q0 < n_qubits and 0 <= q1 < n_qubits) or q0 == q1:
            raise ValueError(f"invalid canonical CZ pair: {(q0, q1)!r}")
        layer = max(next_layer[q0], next_layer[q1])
        while len(layers) <= layer:
            layers.append([])
        layers[layer].append((min(q0, q1), max(q0, q1)))
        next_layer[q0] = layer + 1
        next_layer[q1] = layer + 1
    return ordered_two_qubit_layer_ledger(n_qubits, layers)


def canonical_two_qubit_layer_ledger(circuit) -> dict[str, Any]:
    """Freeze an implementation-independent ASAP ledger of canonical CZs.

    The four compilers may use different private layer containers.  Their
    strict timing cohort is therefore keyed to this canonical two-qubit partial
    order.  Authored one-qubit gates remain covered by the independent gate
    ledger, but do not create an intermediate-placement boundary.
    """
    pairs = []
    for instruction in circuit.data:
        operation = instruction.operation
        if len(instruction.qubits) != 2:
            continue
        if operation.name != "cz":
            raise ValueError(
                f"canonical timing ledger only accepts CZ, found {operation.name}")
        pairs.append(tuple(int(circuit.find_bit(bit).index)
                           for bit in instruction.qubits))
    return two_qubit_layer_ledger(int(circuit.num_qubits), pairs)


@dataclass(frozen=True)
class TimingJob:
    order: int
    circuit: str
    method: str
    repetition: int


@dataclass(frozen=True)
class StageTiming:
    circuit: str
    method: str
    repetition: int
    status: str
    layer_ledger_sha256: str
    transition_count: int
    transition_decision_ns: int | None
    search_kernel_ns: int | None
    marshal_ns: int | None
    initial_placement_ns: int | None
    routing_ns: int | None
    full_compile_ns: int | None

    def validate(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"unknown method: {self.method}")
        if self.repetition < 0 or self.transition_count < 0:
            raise ValueError("negative timing identity/count")
        if self.status not in TERMINAL_STATUSES:
            raise ValueError(f"unknown timing status: {self.status}")
        if self.status == "success":
            if not self.layer_ledger_sha256:
                raise ValueError("successful timing row lacks a layer ledger")
            required = (self.transition_decision_ns, self.initial_placement_ns,
                        self.routing_ns, self.full_compile_ns)
            if any(value is None or value < 0 for value in required):
                raise ValueError("successful timing row lacks a non-negative stage time")


def build_balanced_schedule(
        circuits: Sequence[str], *,
        repetitions: int = FORMAL_TIMING_REPETITIONS,
        seed: int = 0,
        methods: Sequence[str] = METHODS) -> dict[str, Any]:
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    if (not circuits or any(not isinstance(circuit, str) or not circuit
                            for circuit in circuits)
            or len(set(circuits)) != len(circuits)):
        raise ValueError("circuits must be a non-empty unique sequence")
    if (not methods or len(set(methods)) != len(methods)
            or any(method not in METHODS for method in methods)):
        raise ValueError("timing schedule contains an unknown method")
    rng = random.Random(seed)
    jobs: list[tuple[str, str, int]] = []
    ordered_circuits = sorted(circuits)
    # Each circuit is one four-method block.  The block order and the base
    # method permutation are randomized independently for every repetition,
    # while cyclic rotations balance positions across neighbouring circuits.
    for repetition in range(repetitions):
        circuit_order = list(ordered_circuits)
        method_order = list(methods)
        rng.shuffle(circuit_order)
        rng.shuffle(method_order)
        for circuit_position, circuit in enumerate(circuit_order):
            shift = (circuit_position + repetition) % len(method_order)
            rotated = method_order[shift:] + method_order[:shift]
            jobs.extend((circuit, method, repetition) for method in rotated)
    materialized = [TimingJob(index, *job) for index, job in enumerate(jobs)]
    core = {
        "protocol_id": RUNTIME_PROTOCOL_ID,
        "schedule_algorithm": "randomized-circuit-block-latin-rotation-v1",
        "seed": seed,
        "repetitions": repetitions,
        "methods": list(methods),
        "circuits": ordered_circuits,
        "warmup": "one untimed run per method before order=0",
        "primary_metric": "transition_decision_ns",
        "excluded": ["input_parse", "initial_placement", "routing",
                     "code_generation", "scoring", "file_io"],
        "jobs": [asdict(job) for job in materialized],
    }
    core["sha256"] = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return core


def validate_balanced_schedule(schedule: Mapping[str, Any],
                               circuits: Sequence[str], *,
                               repetitions: int = FORMAL_TIMING_REPETITIONS,
                               seed: int = 0,
                               methods: Sequence[str] = METHODS
                               ) -> dict[str, Any]:
    """Verify the schedule seal and replay its deterministic construction."""
    if not isinstance(schedule, Mapping):
        raise ValueError("timing schedule must be a JSON object")
    payload = dict(schedule)
    observed_sha256 = payload.pop("sha256", None)
    expected_sha256 = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if observed_sha256 != expected_sha256:
        raise ValueError("timing schedule SHA256 seal mismatch")
    expected = build_balanced_schedule(
        circuits, repetitions=repetitions, seed=seed, methods=methods)
    if dict(schedule) != expected:
        raise ValueError(
            "timing schedule differs from deterministic frozen reconstruction")
    return expected


def _quartiles(values: Sequence[float]) -> tuple[float, float]:
    if len(values) == 1:
        return values[0], values[0]
    cuts = statistics.quantiles(values, n=4, method="inclusive")
    return float(cuts[0]), float(cuts[2])


def _geometric_mean(values: Sequence[float]) -> float:
    if not values or any(value <= 0 or not math.isfinite(value)
                         for value in values):
        raise ValueError("geometric mean requires finite positive values")
    return math.exp(math.fsum(math.log(value) for value in values) / len(values))


def _bootstrap_geometric_ci(values: Sequence[float], *, iterations: int,
                            seed: int) -> tuple[float, float]:
    if iterations < 1:
        raise ValueError("bootstrap iterations must be positive")
    if not values:
        raise ValueError("bootstrap requires at least one observation")
    if len(values) == 1:
        return float(values[0]), float(values[0])
    rng = random.Random(seed)
    estimates = sorted(_geometric_mean([
        values[rng.randrange(len(values))] for _ in values
    ]) for _ in range(iterations))
    low = estimates[max(0, math.floor(0.025 * iterations))]
    high = estimates[min(iterations - 1, math.ceil(0.975 * iterations) - 1)]
    return float(low), float(high)


def summarize_stage_timing(rows: Sequence[StageTiming], *,
                           timeout_seconds: float = 600.0,
                           expected_repetitions: int = FORMAL_TIMING_REPETITIONS,
                           bootstrap_iterations: int = 10_000,
                           bootstrap_seed: int = 0) -> dict[str, Any]:
    if expected_repetitions < 1:
        raise ValueError("expected repetitions must be positive")
    for row in rows:
        row.validate()
    grouped: dict[tuple[str, str], list[StageTiming]] = defaultdict(list)
    for row in rows:
        grouped[(row.circuit, row.method)].append(row)
    for identity, values in grouped.items():
        repetitions = [row.repetition for row in values]
        if len(repetitions) != len(set(repetitions)):
            raise ValueError(f"duplicate timing repetition for {identity}")
    circuits = sorted({row.circuit for row in rows})
    table = []
    strict_circuits = []
    for circuit in circuits:
        record: dict[str, Any] = {"circuit": circuit}
        ledgers = set()
        transition_counts = set()
        all_methods_complete = True
        for method in METHODS:
            values = grouped.get((circuit, method), [])
            success = [row for row in values if row.status == "success"]
            ledgers.update(row.layer_ledger_sha256 for row in success)
            transition_counts.update(row.transition_count for row in success)
            all_methods_complete = all_methods_complete and (
                len(values) == expected_repetitions and
                {row.repetition for row in values} ==
                set(range(expected_repetitions)) and
                len(success) == expected_repetitions)
            stage = [float(row.transition_decision_ns) for row in success
                     if row.transition_decision_ns is not None]
            full = [float(row.full_compile_ns) for row in success
                    if row.full_compile_ns is not None]
            par2 = [
                (float(row.transition_decision_ns) / 1e9
                 if row.status == "success" and row.transition_decision_ns is not None
                 else 2.0 * timeout_seconds)
                for row in values
            ]
            if stage:
                q1, q3 = _quartiles(stage)
                record[method] = {
                    "valid": len(success), "attempted": len(values),
                    "transition_decision_ns_median": statistics.median(stage),
                    "transition_decision_ns_q1": q1,
                    "transition_decision_ns_q3": q3,
                    "full_compile_ns_median": statistics.median(full),
                    "par2_seconds": statistics.mean(par2),
                }
            else:
                record[method] = {"valid": 0, "attempted": len(values),
                                  "par2_seconds": (statistics.mean(par2)
                                                   if par2 else None)}
        record["strict_stage_aligned"] = (
            all_methods_complete and len(ledgers) == 1 and bool(ledgers) and
            len(transition_counts) == 1)
        if record["strict_stage_aligned"]:
            strict_circuits.append(circuit)
            baseline = record["M2"].get("transition_decision_ns_median")
            for method in ("M3", "M4"):
                ours = record[method].get("transition_decision_ns_median")
                record[method]["speedup_vs_m2"] = (
                    baseline / ours if baseline is not None and ours not in (None, 0)
                    else None)
        table.append(record)
    paired_summaries = {}
    for offset, method in enumerate(("M3", "M4")):
        ratios = [
            float(record[method]["speedup_vs_m2"])
            for record in table
            if record["strict_stage_aligned"] and
            record[method].get("speedup_vs_m2") is not None
        ]
        if ratios:
            q1, q3 = _quartiles(ratios)
            ci_low, ci_high = _bootstrap_geometric_ci(
                ratios, iterations=bootstrap_iterations,
                seed=bootstrap_seed + offset)
            paired_summaries[method] = {
                "n": len(ratios),
                "geometric_mean_speedup_vs_m2": _geometric_mean(ratios),
                "speedup_q1": q1,
                "speedup_q3": q3,
                "speedup_iqr": q3 - q1,
                "bootstrap95_low": ci_low,
                "bootstrap95_high": ci_high,
            }
        else:
            paired_summaries[method] = {
                "n": 0,
                "geometric_mean_speedup_vs_m2": None,
                "speedup_q1": None,
                "speedup_q3": None,
                "speedup_iqr": None,
                "bootstrap95_low": None,
                "bootstrap95_high": None,
            }
    return {
        "protocol_id": RUNTIME_PROTOCOL_ID,
        "expected_repetitions": expected_repetitions,
        "strict_circuits": strict_circuits,
        "strict_valid": len(strict_circuits),
        "paired_speedup_vs_m2": paired_summaries,
        "circuits": table,
    }


__all__ = [
    "METHODS", "RUNTIME_PROTOCOL_ID", "StageTiming", "TimingJob",
    "build_balanced_schedule", "canonical_two_qubit_layer_ledger",
    "ordered_two_qubit_layer_ledger",
    "summarize_stage_timing", "two_qubit_layer_ledger",
    "validate_balanced_schedule",
]
