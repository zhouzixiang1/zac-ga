"""Streaming scorer for canonical neutral-atom traces."""

from __future__ import annotations

from collections import defaultdict
import math
from typing import Iterable

from .model import (
    CanonicalTraceEvent,
    EventType,
    FidelityModel,
    FidelityResult,
    TraceValidationError,
)


_TIME_TOLERANCE_US = 1e-9


def _safe_exp(value: float) -> float:
    # exp(-746) already rounds to zero on IEEE-754 double precision.  Returning
    # zero is fine for display; the log-domain value remains the primary result.
    return 0.0 if value < -745.0 else math.exp(value)


def _union_duration(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    total = 0.0
    begin, end = sorted(intervals)[0]
    for next_begin, next_end in sorted(intervals)[1:]:
        if next_begin <= end + _TIME_TOLERANCE_US:
            end = max(end, next_end)
        else:
            total += end - begin
            begin, end = next_begin, next_end
    return total + end - begin


def _assert_duration(event: CanonicalTraceEvent, expected: float) -> None:
    if not math.isclose(
        event.duration_us,
        expected,
        rel_tol=0.0,
        abs_tol=_TIME_TOLERANCE_US,
    ):
        raise TraceValidationError(
            f"{event.kind} duration must be {expected} us, got {event.duration_us} us"
        )


def score_trace(
    events: Iterable[CanonicalTraceEvent],
    model: FidelityModel | None = None,
    *,
    n_qubits: int | None = None,
) -> FidelityResult:
    """Calculate fidelity, movement batches and movement time in one pass.

    The adapters are responsible for turning native schedules into normalized
    physical time.  This function then enforces the frozen durations for 1Q,
    Rydberg, load and store events.  Movement duration remains trace-defined.

    Linear ZAC coherence is intentionally *not* clipped.  If any atom has been
    idle for at least ``T2``, ``fidelity`` and ``log_fidelity`` are ``None`` and
    the result is marked OOD.  The exponential-coherence sensitivity score is
    always returned alongside it.
    """

    model = model or FidelityModel()
    if n_qubits is not None and n_qubits < 0:
        raise ValueError("n_qubits must be non-negative")

    active: dict[int, list[tuple[float, float]]] = defaultdict(list)
    # Events are start-time ordered, so one last-end record per atom is enough
    # for strict overlap detection and keeps the scorer memory-bounded on Large.
    physical_use_end: dict[int, tuple[float, str]] = {}
    seen_atoms: set[int] = set()
    batches: dict[str, list[CanonicalTraceEvent]] = defaultdict(list)
    one_qubit_gates = 0
    two_qubit_gates = 0
    idle_excitations = 0
    transfers = 0
    duration_us = 0.0
    previous_start = -1.0
    init_atoms: tuple[int, ...] | None = None
    event_count = 0

    for value in events:
        if not isinstance(value, CanonicalTraceEvent):
            raise TypeError(f"expected CanonicalTraceEvent, got {type(value).__name__}")
        event = value
        event_count += 1
        if event.start_us + _TIME_TOLERANCE_US < previous_start:
            raise TraceValidationError("canonical events must be ordered by start_us")
        previous_start = event.start_us
        duration_us = max(duration_us, event.end_us)

        event_atoms = set(event.atoms)
        if event.event_type is EventType.TWO_QUBIT_GATE:
            event_atoms.update(event.region_atoms)
            event_atoms.update(q for pair in event.gate_pairs for q in pair)
        seen_atoms.update(event_atoms)

        # An atom cannot simultaneously participate in two physical events.
        # MOVE is coherence-idle, but still physically occupies the atom and
        # therefore belongs in this overlap ledger.
        overlap_atoms = set(event_atoms)
        if event.event_type is not EventType.INIT and event.duration_us > _TIME_TOLERANCE_US:
            for q in overlap_atoms:
                previous = physical_use_end.get(q)
                if previous is not None and event.start_us < previous[0] - _TIME_TOLERANCE_US:
                    raise TraceValidationError(
                        f"atom {q} has overlapping {previous[1]} and {event.kind} events"
                    )
                physical_use_end[q] = (event.end_us, event.kind)

        if n_qubits is not None and any(q >= n_qubits for q in event_atoms):
            raise TraceValidationError(
                f"event atom exceeds declared n_qubits={n_qubits}: {sorted(event_atoms)}"
            )

        if event.event_type is EventType.INIT:
            if event_count != 1 or init_atoms is not None:
                raise TraceValidationError("init must be the first and only init event")
            init_atoms = event.atoms
            if event.start_us != 0.0 or event.end_us != 0.0:
                raise TraceValidationError("init event must occur at t=0")
            if event.end_positions and len(set(event.end_positions)) != len(event.end_positions):
                raise TraceValidationError("two atoms occupy the same initial position")
            continue

        if event.event_type is EventType.ONE_QUBIT_GATE:
            if len(event.atoms) != 1:
                raise TraceValidationError(
                    "one-qubit events must contain exactly one atom for global serial execution"
                )
            if event.gate_names and len(event.gate_names) != len(event.atoms):
                raise TraceValidationError("gate_names must match one-qubit event atoms")
            _assert_duration(event, model.one_qubit_duration_us)
            one_qubit_gates += len(event.atoms)
            for q in event.atoms:
                active[q].append((event.start_us, event.end_us))

        elif event.event_type is EventType.TWO_QUBIT_GATE:
            _assert_duration(event, model.rydberg_duration_us)
            participants = {q for pair in event.gate_pairs for q in pair}
            illuminated = set(event.region_atoms or event.atoms)
            two_qubit_gates += len(event.gate_pairs)
            idle_excitations += len(illuminated - participants)
            # Only actual gate participants are busy.  An illuminated resident
            # that is not in a CZ pays both idle-excitation error and 0.36 us of
            # coherence idle time; treating it as busy would reproduce the old
            # simulator bug that this unified scorer is meant to remove.
            for q in participants:
                active[q].append((event.start_us, event.end_us))

        elif event.event_type in {EventType.LOAD, EventType.STORE}:
            _assert_duration(event, model.transfer_duration_us)
            transfers += len(event.atoms)
            batches[event.batch_id].append(event)  # type: ignore[index]
            for q in event.atoms:
                active[q].append((event.start_us, event.end_us))

        elif event.event_type is EventType.MOVE:
            batches[event.batch_id].append(event)  # type: ignore[index]

        elif event.event_type is EventType.WAIT:
            if event.atoms:
                raise TraceValidationError("wait is a global idle interval and takes no atoms")

        else:  # pragma: no cover - Enum exhaustiveness guard
            raise TraceValidationError(f"unsupported canonical event: {event.kind}")

    inferred_n = max(seen_atoms, default=-1) + 1
    if n_qubits is None:
        n_qubits = inferred_n
    if init_atoms is not None:
        expected = set(range(n_qubits))
        if set(init_atoms) != expected:
            raise TraceValidationError(
                f"init atoms must be exactly 0..{n_qubits - 1}, got {sorted(init_atoms)}"
            )

    move_time_us = 0.0
    for batch_id, batch_events in batches.items():
        phases = [event.event_type for event in batch_events]
        if EventType.LOAD not in phases or EventType.MOVE not in phases or EventType.STORE not in phases:
            raise TraceValidationError(
                f"movement batch {batch_id!r} must contain load, move and store"
            )
        held: set[int] = set()
        finished = False
        for event in batch_events:
            if finished:
                raise TraceValidationError(f"movement batch {batch_id!r} continues after final store")
            if event.event_type is EventType.LOAD:
                if set(event.atoms) & held:
                    raise TraceValidationError(
                        f"movement batch {batch_id!r} loads an atom twice"
                    )
                held.update(event.atoms)
            elif event.event_type is EventType.MOVE:
                if not set(event.atoms).issubset(held):
                    raise TraceValidationError(
                        f"movement batch {batch_id!r} moves an atom before load"
                    )
            else:
                if not set(event.atoms).issubset(held):
                    raise TraceValidationError(
                        f"movement batch {batch_id!r} stores an atom before load"
                    )
                held.difference_update(event.atoms)
                finished = not held
        if held or not finished:
            raise TraceValidationError(f"movement batch {batch_id!r} ends with atoms held")
        loaded = [q for event in batch_events if event.event_type is EventType.LOAD for q in event.atoms]
        stored = [q for event in batch_events if event.event_type is EventType.STORE for q in event.atoms]
        if sorted(loaded) != sorted(stored) or len(set(loaded)) != len(loaded):
            raise TraceValidationError(
                f"movement batch {batch_id!r} load/store atoms do not match"
            )
        move_time_us += math.fsum(event.duration_us for event in batch_events)

    idle_time: list[float] = []
    for q in range(n_qubits):
        busy = _union_duration(active[q])
        if busy > duration_us + _TIME_TOLERANCE_US:
            raise TraceValidationError(
                f"atom {q} has {busy} us active time in a {duration_us} us trace"
            )
        idle_time.append(max(0.0, duration_us - busy))

    log_1q = one_qubit_gates * math.log(model.one_qubit_fidelity)
    log_2q = two_qubit_gates * math.log(model.two_qubit_fidelity)
    log_exc = idle_excitations * math.log(model.idle_excitation_fidelity)
    log_transfer = transfers * math.log(model.transfer_fidelity)
    common_logs = {
        "one_qubit_gate": log_1q,
        "two_qubit_gate": log_2q,
        "idle_excitation": log_exc,
        "atom_transfer": log_transfer,
    }

    ood_atoms = [q for q, idle in enumerate(idle_time) if idle >= model.coherence_time_us]
    warnings: list[str] = []
    high_idle_atoms = [q for q, idle in enumerate(idle_time)
                       if 0.1 < idle / model.coherence_time_us < 1.0]
    if high_idle_atoms:
        warnings.append(
            "linear coherence model used above 10% idle/T2 for atoms: "
            + ",".join(str(q) for q in high_idle_atoms))
    if ood_atoms:
        warnings.append(
            "linear coherence model out of domain for atoms: "
            + ",".join(str(q) for q in ood_atoms)
        )
        log_coherence: float | None = None
        log_fidelity: float | None = None
        fidelity: float | None = None
    else:
        log_coherence = math.fsum(
            math.log1p(-idle / model.coherence_time_us) for idle in idle_time
        )
        log_fidelity = math.fsum((*common_logs.values(), log_coherence))
        fidelity = _safe_exp(log_fidelity)

    exponential_log_coherence = -math.fsum(idle_time) / model.coherence_time_us
    exponential_log_fidelity = math.fsum((*common_logs.values(), exponential_log_coherence))

    component_logs: dict[str, float | None] = dict(common_logs)
    component_logs["coherence_linear"] = log_coherence
    component_fidelity = {
        key: (None if value is None else _safe_exp(value))
        for key, value in component_logs.items()
    }

    return FidelityResult(
        fidelity=fidelity,
        log_fidelity=log_fidelity,
        exponential_sensitivity_fidelity=_safe_exp(exponential_log_fidelity),
        exponential_sensitivity_log_fidelity=exponential_log_fidelity,
        component_fidelity=component_fidelity,
        component_log_fidelity=component_logs,
        one_qubit_gates=one_qubit_gates,
        two_qubit_gates=two_qubit_gates,
        idle_excitations=idle_excitations,
        transfers=transfers,
        duration_us=duration_us,
        move_batches=len(batches),
        move_time_us=move_time_us,
        idle_time_us=tuple(idle_time),
        ood=bool(ood_atoms),
        warnings=tuple(warnings),
    )
