"""Bounded-memory canonical trace validation and physical scoring.

Unlike :func:`evaluation.score_trace`, these consumers never retain the event
stream or per-gate intervals.  State is O(number of atoms + concurrently open
movement batches), which lets the Large compiler validate, score and optionally
archive each event at the moment it is generated.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Optional

from evaluation import (
    CanonicalTraceEvent,
    EventType,
    FidelityModel,
    FidelityResult,
    TraceValidationError,
    fixed_duration_matches,
)
from zzx.ghost import ghost_hits as find_ghost_hits
from zzx.zcost import compatible_2d

from .checkpoint import EventStreamWriter
from .qasm_sqlite import LogicalLedgerHasher


_TIME_TOLERANCE_US = 1e-9
_ZERO_HASH = "0" * 64
_HASH_MODULUS = 1 << 256
_EVENT_ORDERS = frozenset({"chronological", "dependency"})


def _event_order(value: str) -> str:
    result = str(value)
    if result not in _EVENT_ORDERS:
        raise ValueError(
            "event_order must be 'chronological' or 'dependency'"
        )
    return result


def _event(value: CanonicalTraceEvent | Mapping[str, Any]) -> CanonicalTraceEvent:
    if isinstance(value, CanonicalTraceEvent):
        return value
    if isinstance(value, Mapping):
        return CanonicalTraceEvent.from_dict(value)
    raise TypeError(f"expected canonical event or mapping, got {type(value).__name__}")


def _safe_exp(value: float) -> float:
    return 0.0 if value < -745.0 else math.exp(value)


def _assert_duration(event: CanonicalTraceEvent, expected: float) -> None:
    if not fixed_duration_matches(
            event.start_us, event.end_us, expected,
            tolerance_floor_us=_TIME_TOLERANCE_US):
        raise TraceValidationError(
            f"{event.kind} duration must be {expected} us, got {event.duration_us} us"
        )


def _same_position(left: tuple[float, float], right: tuple[float, float]) -> bool:
    return (
        math.isclose(left[0], right[0], rel_tol=0.0, abs_tol=1e-7)
        and math.isclose(left[1], right[1], rel_tol=0.0, abs_tol=1e-7)
    )


def _gate_records(event: CanonicalTraceEvent) -> tuple[tuple[Any, ...], ...]:
    if event.event_type is EventType.ONE_QUBIT_GATE:
        if len(event.atoms) != 1 or len(event.gate_names) != 1:
            raise TraceValidationError(
                "strict one-qubit gate ledger requires one atom and one gate name"
            )
        return (("1q", event.gate_names[0], event.atoms[0]),)
    if event.event_type is EventType.TWO_QUBIT_GATE:
        if event.gate_names and len(event.gate_names) != len(event.gate_pairs):
            raise TraceValidationError("two-qubit gate_names must match gate_pairs")
        names = event.gate_names or ("cz",) * len(event.gate_pairs)
        # CZ is symmetric.  Sorting endpoints avoids an adapter-only distinction.
        return tuple(
            ("2q", name, *sorted(pair))
            for name, pair in sorted(zip(names, event.gate_pairs),
                                     key=lambda item: (sorted(item[1]), item[0]))
        )
    return ()


@dataclass(frozen=True)
class ValidationSummary:
    ok: bool
    event_count: int
    one_qubit_gates: int
    two_qubit_gates: int
    move_batches: int
    ordered_gate_hash: str
    multiset_gate_hash: str
    logical_ledger_sha256: str
    ghost_hits: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _MovementState:
    batch_id: str
    held: set[int]
    loaded: set[int]
    moved: bool = False
    storing: bool = False


class IncrementalTraceValidator:
    """Strict event-wise verifier with no trace-sized Python collections."""

    def __init__(
        self,
        n_qubits: int,
        *,
        expected_one_qubit_gates: int | None = None,
        expected_two_qubit_gates: int | None = None,
        expected_ordered_gate_hash: str | None = None,
        expected_multiset_gate_hash: str | None = None,
        expected_logical_ledger_sha256: str | None = None,
        require_init: bool = True,
        require_zero_ghost: bool = True,
        event_order: str = "chronological",
    ):
        if n_qubits < 0:
            raise ValueError("n_qubits must be non-negative")
        self.n_qubits = int(n_qubits)
        self.expected_one_qubit_gates = expected_one_qubit_gates
        self.expected_two_qubit_gates = expected_two_qubit_gates
        self.expected_ordered_gate_hash = expected_ordered_gate_hash
        self.expected_multiset_gate_hash = expected_multiset_gate_hash
        self.expected_logical_ledger_sha256 = expected_logical_ledger_sha256
        self.require_init = bool(require_init)
        self.require_zero_ghost = bool(require_zero_ghost)
        self.event_order = _event_order(event_order)
        self.event_count = 0
        self.one_qubit_gates = 0
        self.two_qubit_gates = 0
        self.move_batches = 0
        self.ghost_hits = 0
        self._init_seen = False
        self._previous_start = -1.0
        self._last_one_qubit_end = -1.0
        self._last_aod_end = -1.0
        self._physical_end: list[float] = [-1.0] * self.n_qubits
        self._physical_kind: list[str] = [""] * self.n_qubits
        self._positions: list[tuple[float, float] | None] = [None] * self.n_qubits
        self._regions: list[str | None] = [None] * self.n_qubits
        self._held_atoms: set[int] = set()
        self._movement: _MovementState | None = None
        self._ordered_hash = _ZERO_HASH
        self._multiset_hash = 0
        self._logical_ledger = LogicalLedgerHasher(self.n_qubits)
        self._finalized = False

    def consume(self, value: CanonicalTraceEvent | Mapping[str, Any]) -> None:
        if self._finalized:
            raise RuntimeError("validator is already finalized")
        event = _event(value)
        if (self.event_order == "chronological" and
                event.start_us + _TIME_TOLERANCE_US < self._previous_start):
            raise TraceValidationError("canonical events must be ordered by start_us")
        if self.event_order == "chronological":
            self._previous_start = event.start_us
        else:
            self._previous_start = max(self._previous_start, event.start_us)
        self.event_count += 1

        atoms = set(event.atoms)
        if event.event_type is EventType.TWO_QUBIT_GATE:
            atoms.update(event.region_atoms)
            atoms.update(q for pair in event.gate_pairs for q in pair)
        if any(q < 0 or q >= self.n_qubits for q in atoms):
            raise TraceValidationError(
                f"event atom exceeds declared n_qubits={self.n_qubits}: {sorted(atoms)}"
            )

        if event.event_type is EventType.INIT:
            self._consume_init(event)
            return
        if self.require_init and not self._init_seen:
            raise TraceValidationError("init must be the first event")

        if (event.duration_us > _TIME_TOLERANCE_US or
                self.event_order == "dependency"):
            for q in atoms:
                if event.start_us < self._physical_end[q] - _TIME_TOLERANCE_US:
                    raise TraceValidationError(
                        f"atom {q} has overlapping {self._physical_kind[q]} and {event.kind} events"
                    )
                self._physical_end[q] = event.end_us
                self._physical_kind[q] = event.kind

        raw_ghost_hits = event.metadata.get("ghost_hits", 0)
        try:
            ghost_hits = int(raw_ghost_hits)
        except (TypeError, ValueError) as error:
            raise TraceValidationError(f"invalid ghost_hits metadata: {raw_ghost_hits!r}") from error
        if ghost_hits < 0:
            raise TraceValidationError("ghost_hits cannot be negative")
        self.ghost_hits += ghost_hits
        if self.require_zero_ghost and ghost_hits:
            raise TraceValidationError(f"strict Large trace has {ghost_hits} ghost hits")

        if event.event_type is EventType.ONE_QUBIT_GATE:
            if event.start_us < self._last_one_qubit_end - _TIME_TOLERANCE_US:
                raise TraceValidationError("one-qubit gates must execute globally in sequence")
            if set(event.atoms) & self._held_atoms:
                raise TraceValidationError("one-qubit gate occurs while its atom is AOD-held")
            self._last_one_qubit_end = event.end_us
            self.one_qubit_gates += 1
        elif event.event_type is EventType.TWO_QUBIT_GATE:
            participants = {q for pair in event.gate_pairs for q in pair}
            if participants & self._held_atoms:
                raise TraceValidationError("CZ occurs while a participant is AOD-held")
            self.two_qubit_gates += len(event.gate_pairs)
        elif event.event_type in {EventType.LOAD, EventType.MOVE, EventType.STORE}:
            if (self.event_order == "dependency" and
                    event.start_us < self._last_aod_end - _TIME_TOLERANCE_US):
                raise TraceValidationError(
                    "single AOD operations must be dependency/time ordered")
            if self.event_order == "dependency":
                self._last_aod_end = event.end_us
            self._consume_movement(event)
        elif event.event_type is EventType.WAIT and event.atoms:
            raise TraceValidationError("global wait cannot own atoms")

        self._update_gate_hashes(event)
        self._update_locations(event)

    def _consume_init(self, event: CanonicalTraceEvent) -> None:
        if self.event_count != 1 or self._init_seen:
            raise TraceValidationError("init must be the first and only init event")
        if event.start_us != 0.0 or event.end_us != 0.0:
            raise TraceValidationError("init event must occur at t=0")
        if set(event.atoms) != set(range(self.n_qubits)):
            raise TraceValidationError(
                f"init atoms must be exactly 0..{self.n_qubits - 1}"
            )
        if len(event.end_positions) != self.n_qubits:
            raise TraceValidationError("strict init position ledger is incomplete")
        if len(set(event.end_positions)) != len(event.end_positions):
            raise TraceValidationError("two atoms occupy the same initial position")
        for q, position in zip(event.atoms, event.end_positions):
            self._positions[q] = position
        if event.end_regions:
            for q, region in zip(event.atoms, event.end_regions):
                self._regions[q] = region
        self._init_seen = True

    def _consume_movement(self, event: CanonicalTraceEvent) -> None:
        batch_id = str(event.batch_id)
        if event.event_type is EventType.LOAD:
            if len(event.start_positions) != len(event.atoms):
                raise TraceValidationError("load position ledger is incomplete")
            if self._movement is None:
                self._movement = _MovementState(batch_id, set(), set())
            if self._movement.batch_id != batch_id:
                raise TraceValidationError("single AOD cannot interleave movement batches")
            # ZAIR may activate another row/column after an earlier movement
            # phase in the same physical rearrangeJob.  The reference scorer
            # treats LOAD, MOVE, LOAD, MOVE, STORE as one batch, so Large must
            # preserve the same staggered-activation semantics.
            if self._movement.storing:
                raise TraceValidationError("load occurs after the store phase begins")
            incoming = set(event.atoms)
            if incoming & self._held_atoms or incoming & self._movement.loaded:
                raise TraceValidationError("movement batch loads an atom twice")
            self._movement.held.update(incoming)
            self._movement.loaded.update(incoming)
            self._held_atoms.update(incoming)
        elif event.event_type is EventType.MOVE:
            if (len(event.start_positions) != len(event.atoms) or
                    len(event.end_positions) != len(event.atoms)):
                raise TraceValidationError("movement position ledger is incomplete")
            if self._movement is None or self._movement.batch_id != batch_id:
                raise TraceValidationError("move occurs before its load phase")
            if self._movement.storing:
                raise TraceValidationError("move occurs after the store phase begins")
            if not set(event.atoms).issubset(self._movement.held):
                raise TraceValidationError("movement batch moves an atom before load")
            self._validate_move_geometry(event)
            self._movement.moved = True
        else:
            if len(event.end_positions) != len(event.atoms):
                raise TraceValidationError("store position ledger is incomplete")
            if self._movement is None or self._movement.batch_id != batch_id:
                raise TraceValidationError("store occurs before its load phase")
            if not self._movement.moved:
                raise TraceValidationError("movement batch requires at least one move phase")
            outgoing = set(event.atoms)
            if not outgoing.issubset(self._movement.held):
                raise TraceValidationError("movement batch stores an atom before load")
            self._movement.storing = True
            self._movement.held.difference_update(outgoing)
            self._held_atoms.difference_update(outgoing)
            if not self._movement.held:
                if self._movement.loaded & self._held_atoms:
                    raise TraceValidationError("movement batch ends with held atoms")
                self.move_batches += 1
                self._movement = None

    def _validate_move_geometry(self, event: CanonicalTraceEvent) -> None:
        legs: list[tuple[float, float, float, float, float]] = []
        vectors: list[tuple[float, float, float, float]] = []
        moving_atoms: set[int] = set()
        for q, start, end in zip(
                event.atoms, event.start_positions, event.end_positions):
            known = self._positions[q]
            if known is None or not _same_position(known, start):
                raise TraceValidationError(f"atom {q} movement position is discontinuous")
            distance = math.dist(start, end)
            if distance > _TIME_TOLERANCE_US:
                legs.append((distance, start[0], start[1], end[0], end[1]))
                vectors.append((start[0], end[0], start[1], end[1]))
                moving_atoms.add(q)
        for left in range(len(vectors)):
            for right in range(left + 1, len(vectors)):
                if not compatible_2d(vectors[left], vectors[right]):
                    raise TraceValidationError(
                        f"AOD-incompatible legs in batch {event.batch_id}"
                    )
        ghosts = [
            (q, *position) for q, position in enumerate(self._positions)
            if q not in moving_atoms and position is not None
        ]
        hits = find_ghost_hits(legs, ghosts)
        self.ghost_hits += len(hits)
        if hits:
            raise TraceValidationError(
                f"ghost collision in batch {event.batch_id}: {hits[:3]}"
            )

    def _update_locations(self, event: CanonicalTraceEvent) -> None:
        if event.start_positions:
            for q, position in zip(event.atoms, event.start_positions):
                known = self._positions[q]
                if known is not None and not _same_position(known, position):
                    raise TraceValidationError(
                        f"atom {q} start position {position} does not match {known}"
                    )
        if event.start_regions:
            for q, region in zip(event.atoms, event.start_regions):
                known = self._regions[q]
                if known is not None and known != region:
                    raise TraceValidationError(
                        f"atom {q} start region {region!r} does not match {known!r}"
                    )
        if event.end_positions:
            moving = set(event.atoms)
            occupied = {
                position: q for q, position in enumerate(self._positions)
                if position is not None and q not in moving
            }
            if len(set(event.end_positions)) != len(event.end_positions):
                raise TraceValidationError("two moved atoms end at the same position")
            for q, position in zip(event.atoms, event.end_positions):
                other = occupied.get(position)
                if other is not None:
                    raise TraceValidationError(
                        f"atoms {q} and {other} occupy the same final position {position}"
                    )
                occupied[position] = q
            for q, position in zip(event.atoms, event.end_positions):
                self._positions[q] = position
        if event.end_regions:
            for q, region in zip(event.atoms, event.end_regions):
                self._regions[q] = region

    def _update_gate_hashes(self, event: CanonicalTraceEvent) -> None:
        if event.event_type is EventType.ONE_QUBIT_GATE:
            self._logical_ledger.record_one_qubit(event.atoms[0])
        elif event.event_type is EventType.TWO_QUBIT_GATE:
            for q0, q1 in event.gate_pairs:
                self._logical_ledger.record_cz(q0, q1)
        for record in _gate_records(event):
            line = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("ascii")
            digest = hashlib.sha256(line).digest()
            self._ordered_hash = hashlib.sha256(
                bytes.fromhex(self._ordered_hash) + digest
            ).hexdigest()
            self._multiset_hash = (self._multiset_hash + int.from_bytes(digest, "big")) % _HASH_MODULUS

    def finalize(self) -> ValidationSummary:
        if self._finalized:
            raise RuntimeError("validator is already finalized")
        if self.require_init and not self._init_seen:
            raise TraceValidationError("trace has no init event")
        if self._movement is not None or self._held_atoms:
            raise TraceValidationError("trace ends with an incomplete movement batch")
        if (self.expected_one_qubit_gates is not None and
                self.one_qubit_gates != self.expected_one_qubit_gates):
            raise TraceValidationError(
                f"1Q gate ledger mismatch: {self.one_qubit_gates} != "
                f"{self.expected_one_qubit_gates}"
            )
        if (self.expected_two_qubit_gates is not None and
                self.two_qubit_gates != self.expected_two_qubit_gates):
            raise TraceValidationError(
                f"2Q gate ledger mismatch: {self.two_qubit_gates} != "
                f"{self.expected_two_qubit_gates}"
            )
        multiset = f"{self._multiset_hash:064x}"
        if (self.expected_ordered_gate_hash is not None and
                self._ordered_hash != self.expected_ordered_gate_hash):
            raise TraceValidationError("ordered gate ledger hash mismatch")
        if (self.expected_multiset_gate_hash is not None and
                multiset != self.expected_multiset_gate_hash):
            raise TraceValidationError("multiset gate ledger hash mismatch")
        logical_ledger = self._logical_ledger.hexdigest()
        if (self.expected_logical_ledger_sha256 is not None and
                logical_ledger != self.expected_logical_ledger_sha256):
            raise TraceValidationError("per-atom logical gate ledger hash mismatch")
        self._finalized = True
        return ValidationSummary(
            ok=True,
            event_count=self.event_count,
            one_qubit_gates=self.one_qubit_gates,
            two_qubit_gates=self.two_qubit_gates,
            move_batches=self.move_batches,
            ordered_gate_hash=self._ordered_hash,
            multiset_gate_hash=multiset,
            logical_ledger_sha256=logical_ledger,
            ghost_hits=self.ghost_hits,
        )

    def state_dict(self) -> dict[str, Any]:
        """Return JSON-serialisable state for an atomic compiler checkpoint."""

        movement = None
        if self._movement is not None:
            movement = {
                "batch_id": self._movement.batch_id,
                "held": sorted(self._movement.held),
                "loaded": sorted(self._movement.loaded),
                "moved": self._movement.moved,
                "storing": self._movement.storing,
            }
        return {
            "format": "incremental-trace-validator-v2",
            "n_qubits": self.n_qubits,
            "expected_one_qubit_gates": self.expected_one_qubit_gates,
            "expected_two_qubit_gates": self.expected_two_qubit_gates,
            "expected_ordered_gate_hash": self.expected_ordered_gate_hash,
            "expected_multiset_gate_hash": self.expected_multiset_gate_hash,
            "expected_logical_ledger_sha256":
                self.expected_logical_ledger_sha256,
            "require_init": self.require_init,
            "require_zero_ghost": self.require_zero_ghost,
            "event_order": self.event_order,
            "event_count": self.event_count,
            "one_qubit_gates": self.one_qubit_gates,
            "two_qubit_gates": self.two_qubit_gates,
            "move_batches": self.move_batches,
            "ghost_hits": self.ghost_hits,
            "init_seen": self._init_seen,
            "previous_start": self._previous_start,
            "last_one_qubit_end": self._last_one_qubit_end,
            "last_aod_end": self._last_aod_end,
            "physical_end": list(self._physical_end),
            "physical_kind": list(self._physical_kind),
            "positions": [None if value is None else list(value)
                          for value in self._positions],
            "regions": list(self._regions),
            "held_atoms": sorted(self._held_atoms),
            "movement": movement,
            "ordered_hash": self._ordered_hash,
            "multiset_hash": f"{self._multiset_hash:064x}",
            "logical_ledger": self._logical_ledger.state_dict(),
        }

    @classmethod
    def from_state(cls, value: Mapping[str, Any]) -> "IncrementalTraceValidator":
        state = dict(value)
        if state.pop("format", None) != "incremental-trace-validator-v2":
            raise ValueError("unsupported incremental validator checkpoint")
        validator = cls(
            int(state["n_qubits"]),
            expected_one_qubit_gates=state["expected_one_qubit_gates"],
            expected_two_qubit_gates=state["expected_two_qubit_gates"],
            expected_ordered_gate_hash=state["expected_ordered_gate_hash"],
            expected_multiset_gate_hash=state["expected_multiset_gate_hash"],
            expected_logical_ledger_sha256=
                state["expected_logical_ledger_sha256"],
            require_init=bool(state["require_init"]),
            require_zero_ghost=bool(state["require_zero_ghost"]),
            event_order=str(state.get("event_order", "chronological")),
        )
        validator.event_count = int(state["event_count"])
        validator.one_qubit_gates = int(state["one_qubit_gates"])
        validator.two_qubit_gates = int(state["two_qubit_gates"])
        validator.move_batches = int(state["move_batches"])
        validator.ghost_hits = int(state["ghost_hits"])
        validator._init_seen = bool(state["init_seen"])
        validator._previous_start = float(state["previous_start"])
        validator._last_one_qubit_end = float(state["last_one_qubit_end"])
        validator._last_aod_end = float(state.get("last_aod_end", -1.0))
        validator._physical_end = [float(item) for item in state["physical_end"]]
        validator._physical_kind = [str(item) for item in state["physical_kind"]]
        validator._positions = [
            None if item is None else (float(item[0]), float(item[1]))
            for item in state["positions"]
        ]
        validator._regions = [None if item is None else str(item)
                              for item in state["regions"]]
        for name, sequence in (
            ("physical_end", validator._physical_end),
            ("physical_kind", validator._physical_kind),
            ("positions", validator._positions),
            ("regions", validator._regions),
        ):
            if len(sequence) != validator.n_qubits:
                raise ValueError(f"validator checkpoint {name} length mismatch")
        validator._held_atoms = {int(item) for item in state["held_atoms"]}
        movement = state["movement"]
        if movement is not None:
            validator._movement = _MovementState(
                str(movement["batch_id"]),
                {int(item) for item in movement["held"]},
                {int(item) for item in movement["loaded"]},
                bool(movement["moved"]),
                bool(movement["storing"]),
            )
        validator._ordered_hash = str(state["ordered_hash"])
        validator._multiset_hash = int(str(state["multiset_hash"]), 16)
        validator._logical_ledger = LogicalLedgerHasher.from_state(
            state["logical_ledger"])
        if validator._logical_ledger.qubits != validator.n_qubits:
            raise ValueError("validator logical-ledger checkpoint length mismatch")
        if len(validator._ordered_hash) != 64:
            raise ValueError("invalid validator ordered gate hash")
        return validator


@dataclass
class _ScoreBatch:
    held: set[int]
    loaded: set[int]
    moved: bool = False
    storing: bool = False
    duration_us: float = 0.0


class IncrementalTraceScorer:
    """Numerically equivalent, bounded-memory implementation of ZAC fidelity."""

    def __init__(
        self,
        n_qubits: int,
        model: FidelityModel | None = None,
        *,
        event_order: str = "chronological",
    ):
        if n_qubits < 0:
            raise ValueError("n_qubits must be non-negative")
        self.n_qubits = int(n_qubits)
        self.model = model or FidelityModel()
        self.event_order = _event_order(event_order)
        self.event_count = 0
        self.one_qubit_gates = 0
        self.two_qubit_gates = 0
        self.idle_excitations = 0
        self.transfers = 0
        self.duration_us = 0.0
        self.move_batches = 0
        self.move_time_us = 0.0
        self._previous_start = -1.0
        self._resource_end = [-1.0] * self.n_qubits
        self._resource_kind = [""] * self.n_qubits
        self._last_one_qubit_end = -1.0
        self._last_aod_end = -1.0
        self._active_aod_batch: str | None = None
        self._busy_closed = [0.0] * self.n_qubits
        self._busy_begin = [-1.0] * self.n_qubits
        self._busy_end = [-1.0] * self.n_qubits
        self._batches: dict[str, _ScoreBatch] = {}
        self._finalized = False

    def consume(self, value: CanonicalTraceEvent | Mapping[str, Any]) -> None:
        if self._finalized:
            raise RuntimeError("scorer is already finalized")
        event = _event(value)
        if (self.event_order == "chronological" and
                event.start_us + _TIME_TOLERANCE_US < self._previous_start):
            raise TraceValidationError("canonical events must be ordered by start_us")
        if self.event_order == "chronological":
            self._previous_start = event.start_us
        else:
            self._previous_start = max(self._previous_start, event.start_us)
        self.event_count += 1
        self.duration_us = max(self.duration_us, event.end_us)
        all_atoms = set(event.atoms)
        if event.event_type is EventType.TWO_QUBIT_GATE:
            all_atoms.update(event.region_atoms)
        if any(q < 0 or q >= self.n_qubits for q in all_atoms):
            raise TraceValidationError("event atom is outside scorer qubit range")

        if (self.event_order == "dependency" and
                event.event_type is not EventType.INIT):
            for q in all_atoms:
                if event.start_us < self._resource_end[q] - _TIME_TOLERANCE_US:
                    raise TraceValidationError(
                        f"atom {q} is not dependency/time ordered after "
                        f"{self._resource_kind[q]}"
                    )
                self._resource_end[q] = event.end_us
                self._resource_kind[q] = event.kind

        busy_atoms: set[int] = set()
        if event.event_type is EventType.ONE_QUBIT_GATE:
            if len(event.atoms) != 1:
                raise TraceValidationError("one-qubit event must contain one atom")
            _assert_duration(event, self.model.one_qubit_duration_us)
            if (self.event_order == "dependency" and
                    event.start_us <
                    self._last_one_qubit_end - _TIME_TOLERANCE_US):
                raise TraceValidationError(
                    "global one-qubit gates must be dependency/time ordered")
            if self.event_order == "dependency":
                self._last_one_qubit_end = event.end_us
            self.one_qubit_gates += 1
            busy_atoms.update(event.atoms)
        elif event.event_type is EventType.TWO_QUBIT_GATE:
            _assert_duration(event, self.model.rydberg_duration_us)
            participants = {q for pair in event.gate_pairs for q in pair}
            illuminated = set(event.region_atoms or event.atoms)
            self.two_qubit_gates += len(event.gate_pairs)
            self.idle_excitations += len(illuminated - participants)
            busy_atoms.update(participants)
        elif event.event_type in {EventType.LOAD, EventType.STORE}:
            _assert_duration(event, self.model.transfer_duration_us)
            self._check_dependency_aod(event)
            self.transfers += len(event.atoms)
            busy_atoms.update(event.atoms)
            self._consume_batch(event)
        elif event.event_type is EventType.MOVE:
            self._check_dependency_aod(event)
            self._consume_batch(event)
        elif event.event_type is EventType.WAIT:
            if event.atoms:
                raise TraceValidationError("global wait cannot own atoms")
        elif event.event_type is not EventType.INIT:
            raise TraceValidationError(f"unsupported event type: {event.kind}")

        for q in busy_atoms:
            if self._busy_begin[q] < 0.0:
                self._busy_begin[q] = event.start_us
                self._busy_end[q] = event.end_us
            elif event.start_us <= self._busy_end[q] + _TIME_TOLERANCE_US:
                self._busy_end[q] = max(self._busy_end[q], event.end_us)
            else:
                self._busy_closed[q] += self._busy_end[q] - self._busy_begin[q]
                self._busy_begin[q] = event.start_us
                self._busy_end[q] = event.end_us

    def _check_dependency_aod(self, event: CanonicalTraceEvent) -> None:
        if self.event_order != "dependency":
            return
        if event.start_us < self._last_aod_end - _TIME_TOLERANCE_US:
            raise TraceValidationError(
                "single AOD operations must be dependency/time ordered")
        batch_id = str(event.batch_id)
        if event.event_type is EventType.LOAD:
            if self._active_aod_batch is None:
                self._active_aod_batch = batch_id
            elif self._active_aod_batch != batch_id:
                raise TraceValidationError(
                    "single AOD cannot interleave movement batches")
        elif self._active_aod_batch != batch_id:
            raise TraceValidationError(
                "single AOD movement dependency is not active")
        self._last_aod_end = event.end_us

    def _consume_batch(self, event: CanonicalTraceEvent) -> None:
        batch_id = str(event.batch_id)
        state = self._batches.get(batch_id)
        if event.event_type is EventType.LOAD:
            if state is None:
                state = _ScoreBatch(set(), set())
                self._batches[batch_id] = state
            # A later activation can legally join an open batch after one or
            # more move phases, as long as storage has not begun.
            if state.storing:
                raise TraceValidationError("load occurs after store begins")
            incoming = set(event.atoms)
            if incoming & state.loaded:
                raise TraceValidationError("batch loads an atom twice")
            state.loaded.update(incoming)
            state.held.update(incoming)
        elif event.event_type is EventType.MOVE:
            if state is None or not set(event.atoms).issubset(state.held):
                raise TraceValidationError("batch moves an atom before load")
            if state.storing:
                raise TraceValidationError("batch moves after store begins")
            state.moved = True
        else:
            if state is None or not state.moved:
                raise TraceValidationError("batch stores before load/move")
            outgoing = set(event.atoms)
            if not outgoing.issubset(state.held):
                raise TraceValidationError("batch stores an atom before load")
            state.storing = True
            state.held.difference_update(outgoing)
        state.duration_us += event.duration_us
        if event.event_type is EventType.STORE and not state.held:
            self.move_batches += 1
            self.move_time_us += state.duration_us
            del self._batches[batch_id]
            if self.event_order == "dependency":
                self._active_aod_batch = None

    def finalize(self) -> FidelityResult:
        if self._finalized:
            raise RuntimeError("scorer is already finalized")
        if self._batches:
            raise TraceValidationError(
                f"trace ends with incomplete movement batches: {sorted(self._batches)}"
            )
        busy_time = tuple(
            0.0 if self._busy_begin[q] < 0.0
            else self._busy_closed[q] + self._busy_end[q] - self._busy_begin[q]
            for q in range(self.n_qubits)
        )
        idle_time = tuple(max(0.0, self.duration_us - busy) for busy in busy_time)
        log_1q = self.one_qubit_gates * math.log(self.model.one_qubit_fidelity)
        log_2q = self.two_qubit_gates * math.log(self.model.two_qubit_fidelity)
        log_exc = self.idle_excitations * math.log(self.model.idle_excitation_fidelity)
        log_transfer = self.transfers * math.log(self.model.transfer_fidelity)
        common = {
            "one_qubit_gate": log_1q,
            "two_qubit_gate": log_2q,
            "idle_excitation": log_exc,
            "atom_transfer": log_transfer,
        }
        ood_atoms = [q for q, idle in enumerate(idle_time)
                     if idle >= self.model.coherence_time_us]
        warnings: list[str] = []
        high_idle = [q for q, idle in enumerate(idle_time)
                     if 0.1 < idle / self.model.coherence_time_us < 1.0]
        if high_idle:
            warnings.append(
                "linear coherence model used above 10% idle/T2 for atoms: "
                + ",".join(str(q) for q in high_idle)
            )
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
                math.log1p(-idle / self.model.coherence_time_us) for idle in idle_time
            )
            log_fidelity = math.fsum((*common.values(), log_coherence))
            fidelity = _safe_exp(log_fidelity)
        exponential_log_coherence = -math.fsum(idle_time) / self.model.coherence_time_us
        exponential_log_fidelity = math.fsum(
            (*common.values(), exponential_log_coherence)
        )
        component_logs: dict[str, float | None] = dict(common)
        component_logs["coherence_linear"] = log_coherence
        self._finalized = True
        return FidelityResult(
            fidelity=fidelity,
            log_fidelity=log_fidelity,
            exponential_sensitivity_fidelity=_safe_exp(exponential_log_fidelity),
            exponential_sensitivity_log_fidelity=exponential_log_fidelity,
            component_fidelity={
                key: None if value is None else _safe_exp(value)
                for key, value in component_logs.items()
            },
            component_log_fidelity=component_logs,
            one_qubit_gates=self.one_qubit_gates,
            two_qubit_gates=self.two_qubit_gates,
            idle_excitations=self.idle_excitations,
            transfers=self.transfers,
            duration_us=self.duration_us,
            move_batches=self.move_batches,
            move_time_us=self.move_time_us,
            idle_time_us=idle_time,
            ood=bool(ood_atoms),
            warnings=tuple(warnings),
        )

    def rolling_metrics(self) -> dict[str, float]:
        """Small checkpoint-ready summary; full fidelity is computed at finalize."""

        return {
            "events": float(self.event_count),
            "one_qubit_gates": float(self.one_qubit_gates),
            "two_qubit_gates": float(self.two_qubit_gates),
            "idle_excitations": float(self.idle_excitations),
            "transfers": float(self.transfers),
            "duration_us": self.duration_us,
            "move_batches": float(self.move_batches),
            "move_time_us": self.move_time_us,
        }

    def state_dict(self) -> dict[str, Any]:
        """Return the exact O(qubits) scoring state for deterministic resume."""

        return {
            "format": "incremental-trace-scorer-v1",
            "n_qubits": self.n_qubits,
            "model": self.model.to_dict(),
            "event_order": self.event_order,
            "event_count": self.event_count,
            "one_qubit_gates": self.one_qubit_gates,
            "two_qubit_gates": self.two_qubit_gates,
            "idle_excitations": self.idle_excitations,
            "transfers": self.transfers,
            "duration_us": self.duration_us,
            "move_batches": self.move_batches,
            "move_time_us": self.move_time_us,
            "previous_start": self._previous_start,
            "resource_end": list(self._resource_end),
            "resource_kind": list(self._resource_kind),
            "last_one_qubit_end": self._last_one_qubit_end,
            "last_aod_end": self._last_aod_end,
            "active_aod_batch": self._active_aod_batch,
            "busy_closed": list(self._busy_closed),
            "busy_begin": list(self._busy_begin),
            "busy_end": list(self._busy_end),
            "batches": {
                batch_id: {
                    "held": sorted(state.held),
                    "loaded": sorted(state.loaded),
                    "moved": state.moved,
                    "storing": state.storing,
                    "duration_us": state.duration_us,
                }
                for batch_id, state in sorted(self._batches.items())
            },
        }

    @classmethod
    def from_state(cls, value: Mapping[str, Any]) -> "IncrementalTraceScorer":
        state = dict(value)
        if state.pop("format", None) != "incremental-trace-scorer-v1":
            raise ValueError("unsupported incremental scorer checkpoint")
        scorer = cls(
            int(state["n_qubits"]), FidelityModel.from_mapping(state["model"]),
            event_order=str(state.get("event_order", "chronological")),
        )
        for name in (
            "event_count", "one_qubit_gates", "two_qubit_gates",
            "idle_excitations", "transfers", "move_batches",
        ):
            setattr(scorer, name, int(state[name]))
        scorer.duration_us = float(state["duration_us"])
        scorer.move_time_us = float(state["move_time_us"])
        scorer._previous_start = float(state["previous_start"])
        scorer._resource_end = [
            float(item) for item in
            state.get("resource_end", [-1.0] * scorer.n_qubits)
        ]
        scorer._resource_kind = [
            str(item) for item in
            state.get("resource_kind", [""] * scorer.n_qubits)
        ]
        scorer._last_one_qubit_end = float(
            state.get("last_one_qubit_end", -1.0))
        scorer._last_aod_end = float(state.get("last_aod_end", -1.0))
        active_aod_batch = state.get("active_aod_batch")
        scorer._active_aod_batch = (
            None if active_aod_batch is None else str(active_aod_batch))
        scorer._busy_closed = [float(item) for item in state["busy_closed"]]
        scorer._busy_begin = [float(item) for item in state["busy_begin"]]
        scorer._busy_end = [float(item) for item in state["busy_end"]]
        for name, sequence in (
            ("busy_closed", scorer._busy_closed),
            ("busy_begin", scorer._busy_begin),
            ("busy_end", scorer._busy_end),
            ("resource_end", scorer._resource_end),
            ("resource_kind", scorer._resource_kind),
        ):
            if len(sequence) != scorer.n_qubits:
                raise ValueError(f"scorer checkpoint {name} length mismatch")
        scorer._batches = {
            str(batch_id): _ScoreBatch(
                held={int(item) for item in batch["held"]},
                loaded={int(item) for item in batch["loaded"]},
                moved=bool(batch["moved"]),
                storing=bool(batch["storing"]),
                duration_us=float(batch["duration_us"]),
            )
            for batch_id, batch in state["batches"].items()
        }
        return scorer


class IncrementalTracePipeline:
    """Fan each generated event into strict verification, scoring and JSONL."""

    def __init__(
        self,
        validator: IncrementalTraceValidator,
        scorer: IncrementalTraceScorer,
        writer: EventStreamWriter | None = None,
    ):
        if validator.n_qubits != scorer.n_qubits:
            raise ValueError("validator and scorer disagree on n_qubits")
        if validator.event_order != scorer.event_order:
            raise ValueError("validator and scorer disagree on event_order")
        self.validator = validator
        self.scorer = scorer
        self.writer = writer
        self._finalized = False

    def consume(self, value: CanonicalTraceEvent | Mapping[str, Any]) -> None:
        if self._finalized:
            raise RuntimeError("trace pipeline is already finalized")
        event = _event(value)
        self.validator.consume(event)
        self.scorer.consume(event)
        if self.writer is not None:
            self.writer.write(event.to_dict())

    def flush(self) -> None:
        if self.writer is not None:
            self.writer.flush()

    def finalize(self) -> dict[str, Any]:
        if self._finalized:
            raise RuntimeError("trace pipeline is already finalized")
        validation = self.validator.finalize()
        fidelity = self.scorer.finalize()
        self.flush()
        self._finalized = True
        return {
            "validation": validation.to_dict(),
            "fidelity": fidelity.to_dict(),
        }

    def state_dict(self) -> dict[str, Any]:
        """Checkpoint state; store this under ``Checkpoint.dependencies``."""

        if self._finalized:
            raise RuntimeError("cannot checkpoint a finalized trace pipeline")
        return {
            "format": "incremental-trace-pipeline-v1",
            "validator": self.validator.state_dict(),
            "scorer": self.scorer.state_dict(),
        }

    @classmethod
    def from_state(
        cls, value: Mapping[str, Any],
        writer: EventStreamWriter | None = None,
    ) -> "IncrementalTracePipeline":
        state = dict(value)
        if state.pop("format", None) != "incremental-trace-pipeline-v1":
            raise ValueError("unsupported incremental pipeline checkpoint")
        unknown = set(state) - {"validator", "scorer"}
        if unknown:
            raise ValueError(f"unknown incremental pipeline checkpoint fields: {sorted(unknown)}")
        return cls(
            IncrementalTraceValidator.from_state(state["validator"]),
            IncrementalTraceScorer.from_state(state["scorer"]),
            writer,
        )


def consume_trace_incrementally(
    events: Iterable[CanonicalTraceEvent | Mapping[str, Any]],
    *,
    n_qubits: int,
    model: FidelityModel | None = None,
    writer: EventStreamWriter | None = None,
    expected_one_qubit_gates: int | None = None,
    expected_two_qubit_gates: int | None = None,
    event_order: str = "chronological",
) -> dict[str, Any]:
    """Convenience entry point for an existing event iterator."""

    pipeline = IncrementalTracePipeline(
        IncrementalTraceValidator(
            n_qubits,
            expected_one_qubit_gates=expected_one_qubit_gates,
            expected_two_qubit_gates=expected_two_qubit_gates,
            event_order=event_order,
        ),
        IncrementalTraceScorer(n_qubits, model, event_order=event_order),
        writer,
    )
    for event in events:
        pipeline.consume(event)
    return pipeline.finalize()


__all__ = [
    "IncrementalTracePipeline",
    "IncrementalTraceScorer",
    "IncrementalTraceValidator",
    "ValidationSummary",
    "consume_trace_incrementally",
]
