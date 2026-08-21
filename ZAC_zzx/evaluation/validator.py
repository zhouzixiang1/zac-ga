"""Independent physical replay checks over canonical trace events."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from zzx.ghost import ghost_hits
from zzx.zcost import compatible_2d

from .model import CanonicalTraceEvent, EventType, TraceValidationError


def _same_position(left: tuple[float, float], right: tuple[float, float]) -> bool:
    return math.isclose(left[0], right[0], abs_tol=1e-7) and math.isclose(
        left[1], right[1], abs_tol=1e-7)


def validate_trace_physics(events: Iterable[CanonicalTraceEvent], *,
                           n_qubits: int | None = None) -> dict[str, Any]:
    """Replay occupancy, AOD ordering, continuity, and ghost safety.

    This validator trusts neither the compiler nor the scorer.  It consumes a
    freshly normalized event stream, follows every atom position, and treats a
    non-held atom as a static ghost during each AOD movement phase.
    """
    positions: dict[int, tuple[float, float]] = {}
    held: set[int] = set()
    batches: set[str] = set()
    move_phases = 0
    ghost_count = 0
    initialized = False

    for event in events:
        if event.event_type is EventType.INIT:
            if initialized:
                raise TraceValidationError("physical replay contains multiple init events")
            if len(event.atoms) != len(event.end_positions):
                raise TraceValidationError("init position ledger is incomplete")
            positions = dict(zip(event.atoms, event.end_positions))
            initialized = True
            continue
        if not initialized:
            raise TraceValidationError("physical replay must begin with init")

        if event.event_type is EventType.LOAD:
            if set(event.atoms) & held:
                raise TraceValidationError("physical replay loads an already-held atom")
            for q, position in zip(event.atoms, event.start_positions):
                if q not in positions or not _same_position(positions[q], position):
                    raise TraceValidationError(f"atom {q} load position is discontinuous")
            held.update(event.atoms)
            batches.add(str(event.batch_id))

        elif event.event_type is EventType.MOVE:
            if not set(event.atoms).issubset(held):
                raise TraceValidationError("physical replay moves a non-held atom")
            if len(event.start_positions) != len(event.atoms) or \
                    len(event.end_positions) != len(event.atoms):
                raise TraceValidationError("movement position ledger is incomplete")
            legs: list[tuple[float, float, float, float, float]] = []
            vectors: list[tuple[float, float, float, float]] = []
            for q, start, end in zip(
                    event.atoms, event.start_positions, event.end_positions):
                if q not in positions or not _same_position(positions[q], start):
                    raise TraceValidationError(f"atom {q} movement position is discontinuous")
                distance = math.dist(start, end)
                if distance > 1e-9:
                    legs.append((distance, start[0], start[1], end[0], end[1]))
                    vectors.append((start[0], end[0], start[1], end[1]))
            for i in range(len(vectors)):
                for j in range(i + 1, len(vectors)):
                    if not compatible_2d(vectors[i], vectors[j]):
                        raise TraceValidationError(
                            f"AOD-incompatible legs in batch {event.batch_id}")
            ghosts = [(q, *position) for q, position in positions.items()
                      if q not in held]
            hits = ghost_hits(legs, ghosts)
            ghost_count += len(hits)
            if hits:
                raise TraceValidationError(
                    f"ghost collision in batch {event.batch_id}: {hits[:3]}")
            for q, end in zip(event.atoms, event.end_positions):
                positions[q] = end
            move_phases += 1

        elif event.event_type is EventType.STORE:
            if not set(event.atoms).issubset(held):
                raise TraceValidationError("physical replay stores a non-held atom")
            for q, position in zip(event.atoms, event.end_positions):
                if q not in positions or not _same_position(positions[q], position):
                    raise TraceValidationError(f"atom {q} store position is discontinuous")
                positions[q] = position
                held.remove(q)
            occupied = [position for q, position in positions.items() if q not in held]
            if len(set(occupied)) != len(occupied):
                raise TraceValidationError("duplicate SLM occupancy after store")

        elif event.event_type in {EventType.ONE_QUBIT_GATE,
                                  EventType.TWO_QUBIT_GATE}:
            if set(event.atoms) & held:
                raise TraceValidationError("gate acts on an AOD-held atom")

    if held:
        raise TraceValidationError(f"physical replay ends with held atoms: {sorted(held)}")
    if n_qubits is not None and set(positions) != set(range(n_qubits)):
        raise TraceValidationError("physical replay atom ledger disagrees with n_qubits")
    return {
        "ok": True,
        "ghost_hits": ghost_count,
        "move_batches": len(batches),
        "move_phases": move_phases,
    }


__all__ = ["validate_trace_physics"]
