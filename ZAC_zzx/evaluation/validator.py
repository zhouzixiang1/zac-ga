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
                           n_qubits: int | None = None,
                           enforce_ghost_safety: bool = True) -> dict[str, Any]:
    """Replay occupancy, AOD ordering, continuity, and ghost safety.

    This validator trusts neither the compiler nor the scorer.  It consumes a
    freshly normalized event stream, follows every atom position, and treats a
    non-held atom as a static ghost during each AOD movement phase.  Ghost hits
    are always counted.  ``enforce_ghost_safety=False`` is reserved for the
    unmodified M1/M2 paper baselines, whose published algorithms did not impose
    our stronger stationary-ghost constraint; every other physical invariant
    remains fail-closed.
    """
    positions: dict[int, tuple[float, float]] = {}
    held: set[int] = set()
    completed_batches: set[str] = set()
    active_batch: str | None = None
    batch_loaded: set[int] = set()
    batch_moved = False
    batch_storing = False
    move_phases = 0
    ghost_count = 0
    initialized = False
    previous_start = -1.0
    aod_end = -1.0

    for event in events:
        if event.start_us + 1e-9 < previous_start:
            raise TraceValidationError("physical replay events must be ordered by start_us")
        previous_start = event.start_us
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
            batch_id = str(event.batch_id)
            if event.start_us < aod_end - 1e-9:
                raise TraceValidationError("single AOD operations cannot overlap")
            aod_end = event.end_us
            if active_batch is None:
                if batch_id in completed_batches:
                    raise TraceValidationError("movement batch id is reused after completion")
                active_batch = batch_id
                batch_loaded = set()
                batch_moved = False
                batch_storing = False
            elif active_batch != batch_id:
                raise TraceValidationError("single AOD cannot interleave movement batches")
            if batch_storing:
                raise TraceValidationError("load occurs after the store phase begins")
            if len(event.start_positions) != len(event.atoms):
                raise TraceValidationError("load position ledger is incomplete")
            if set(event.atoms) & held:
                raise TraceValidationError("physical replay loads an already-held atom")
            if set(event.atoms) & batch_loaded:
                raise TraceValidationError("movement batch loads an atom twice")
            for q, position in zip(event.atoms, event.start_positions):
                if q not in positions or not _same_position(positions[q], position):
                    raise TraceValidationError(f"atom {q} load position is discontinuous")
            held.update(event.atoms)
            batch_loaded.update(event.atoms)

        elif event.event_type is EventType.MOVE:
            batch_id = str(event.batch_id)
            if event.start_us < aod_end - 1e-9:
                raise TraceValidationError("single AOD operations cannot overlap")
            aod_end = event.end_us
            if active_batch is None or active_batch != batch_id:
                raise TraceValidationError("move occurs before its load phase")
            if batch_storing:
                raise TraceValidationError("move occurs after the store phase begins")
            if not set(event.atoms).issubset(held):
                raise TraceValidationError("physical replay moves a non-held atom")
            if len(event.start_positions) != len(event.atoms) or \
                    len(event.end_positions) != len(event.atoms):
                raise TraceValidationError("movement position ledger is incomplete")
            legs: list[tuple[float, float, float, float, float]] = []
            vectors: list[tuple[float, float, float, float]] = []
            moving_atoms: set[int] = set()
            for q, start, end in zip(
                    event.atoms, event.start_positions, event.end_positions):
                if q not in positions or not _same_position(positions[q], start):
                    raise TraceValidationError(f"atom {q} movement position is discontinuous")
                distance = math.dist(start, end)
                if distance > 1e-9:
                    legs.append((distance, start[0], start[1], end[0], end[1]))
                    vectors.append((start[0], end[0], start[1], end[1]))
                    moving_atoms.add(q)
            for i in range(len(vectors)):
                for j in range(i + 1, len(vectors)):
                    if not compatible_2d(vectors[i], vectors[j]):
                        raise TraceValidationError(
                            f"AOD-incompatible legs in batch {event.batch_id}")
            # Every atom that is stationary in this MOVE phase is a physical
            # obstacle, including atoms already held by the same staggered AOD
            # batch.  Excluding all held atoms would miss LOAD-MOVE-LOAD-MOVE
            # collisions when only a subset moves in the later phase.
            ghosts = [(q, *position) for q, position in positions.items()
                      if q not in moving_atoms]
            hits = ghost_hits(legs, ghosts)
            ghost_count += len(hits)
            if hits and enforce_ghost_safety:
                raise TraceValidationError(
                    f"ghost collision in batch {event.batch_id}: {hits[:3]}")
            for q, end in zip(event.atoms, event.end_positions):
                positions[q] = end
            move_phases += 1
            batch_moved = True

        elif event.event_type is EventType.STORE:
            batch_id = str(event.batch_id)
            if event.start_us < aod_end - 1e-9:
                raise TraceValidationError("single AOD operations cannot overlap")
            aod_end = event.end_us
            if active_batch is None or active_batch != batch_id:
                raise TraceValidationError("store occurs before its load phase")
            if not batch_moved:
                raise TraceValidationError("movement batch requires at least one move phase")
            if len(event.end_positions) != len(event.atoms):
                raise TraceValidationError("store position ledger is incomplete")
            if not set(event.atoms).issubset(held):
                raise TraceValidationError("physical replay stores a non-held atom")
            batch_storing = True
            for q, position in zip(event.atoms, event.end_positions):
                if q not in positions or not _same_position(positions[q], position):
                    raise TraceValidationError(f"atom {q} store position is discontinuous")
                positions[q] = position
                held.remove(q)
            occupied = [position for q, position in positions.items() if q not in held]
            if len(set(occupied)) != len(occupied):
                raise TraceValidationError("duplicate SLM occupancy after store")
            if not held:
                completed_batches.add(batch_id)
                active_batch = None
                batch_loaded = set()
                batch_moved = False
                batch_storing = False

        elif event.event_type in {EventType.ONE_QUBIT_GATE,
                                  EventType.TWO_QUBIT_GATE}:
            gate_atoms = set(event.atoms)
            if event.event_type is EventType.TWO_QUBIT_GATE:
                gate_atoms.update(event.region_atoms)
            if gate_atoms & held:
                raise TraceValidationError("gate acts on an AOD-held atom")

    if active_batch is not None or held:
        raise TraceValidationError(f"physical replay ends with held atoms: {sorted(held)}")
    if n_qubits is not None and set(positions) != set(range(n_qubits)):
        raise TraceValidationError("physical replay atom ledger disagrees with n_qubits")
    return {
        "ok": True,
        "ghost_hits": ghost_count,
        "move_batches": len(completed_batches),
        "move_phases": move_phases,
    }


__all__ = ["validate_trace_physics"]
